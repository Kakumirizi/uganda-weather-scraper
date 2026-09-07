#!/usr/bin/env python3
"""
Uganda AWS network scraper  --  Adcon LiveData (addVANTAGE Pro 6).

Option A: recurring snapshots that accumulate into a local time series.

Each run:
  1. GET  list.jsf?template=weather   -> full sensor table (precise values)
  2. GET  map.jsf?template=weather    -> liveDataMapItems JSON (lat/lon, marker)
  3. merge on Adcon node id, enrich (ISO timestamps, reading age, QC flags)
  4. write a full raw snapshot            -> data/snapshots/uganda_weather_<UTC>.json.gz
  5. append NEW readings (node_id+epoch)  -> data/observations.csv   (append-only, deduped)
  6. refresh the current-state exports    -> data/latest.{json,csv,geojson}
  7. update the station registry          -> data/stations.csv
  8. append a line to                     -> logs/scrape.log

Nothing here logs in, changes server state, or crawls beyond the two public pages
that Adcon explicitly documents as embeddable. Coordinates from the public map view
are deliberately fuzzed by Adcon and are NOT survey grade.

Exit codes: 0 ok, 1 fetch/parse failure (store left untouched).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import requests

DEFAULT_BASE = "http://196.0.33.173:8080"
EAT = dt.timezone(dt.timedelta(hours=3))  # East Africa Time, no DST

METRICS = ["t_air_c", "rain_mm", "solar_wm2", "rh_pct", "wind_kmh", "wind_max_kmh"]
NULL_TOKENS = {"", "-", "--", "---", "n/a", "na"}

ROW_RE = re.compile(r"<tr>\s*(<td title=\"\" class=\"node-cell\".*?)</tr>", re.S)
CELL_RE = re.compile(r"<td[^>]*class=\"node-cell\"[^>]*>(.*?)</td>", re.S)
NODE_RE = re.compile(r"[?&]node=(\d+)")
SORTKEY_RE = re.compile(r'<span[^>]*:sortkey"[^>]*>(.*?)</span>', re.S)
TAG_RE = re.compile(r"<[^>]+>")
NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
MAPITEMS_RE = re.compile(r"liveDataMapItems\s*=\s*(\[.*?\]);", re.S)

OBS_COLUMNS = [
    "node_id", "station_code", "station_name",
    "epoch_ms", "ts_utc", "ts_eat",
    "latitude", "longitude",
    "t_air_c", "rain_mm", "solar_wm2", "rh_pct", "wind_kmh", "wind_max_kmh",
    "quality_flags", "scraped_at_utc",
]
LATEST_COLUMNS = OBS_COLUMNS + ["age_hours", "marker_state", "ok"]
STATION_COLUMNS = [
    "node_id", "station_code", "station_name", "latitude", "longitude",
    "first_seen_utc", "last_reading_utc", "last_scraped_utc",
]


# --------------------------------------------------------------------------- parse
def _text(html: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", html)).strip()


def _clean_num(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if s.lower() in NULL_TOKENS:
        return None
    m = NUM_RE.search(s)
    if not m:
        return None
    try:
        v = float(m.group(0))
    except ValueError:
        return None
    return 0.0 if abs(v) < 1e-9 else v


def parse_list(html: str) -> dict:
    out = {}
    for row_html in ROW_RE.findall(html):
        cells = CELL_RE.findall(row_html)
        if len(cells) < 9:
            continue
        nm = NODE_RE.search(cells[1])
        if not nm:
            continue
        node_id = int(nm.group(1))
        label = _text(cells[1])
        cm = re.match(r"(\d+)\s+(.*)", label)
        code, name = (cm.group(1), cm.group(2)) if cm else ("", label)

        sk = SORTKEY_RE.search(cells[2])
        epoch_ms = int(sk.group(1)) if sk and sk.group(1).strip().isdigit() else None

        rec = {
            "node_id": node_id, "station_code": code, "station_name": name,
            "epoch_ms": epoch_ms,
        }
        for key, cell in zip(METRICS, cells[3:9]):
            sk = SORTKEY_RE.search(cell)
            rec[key] = _clean_num(sk.group(1)) if sk else _clean_num(SORTKEY_RE.sub("", cell))
        out[node_id] = rec
    return out


def parse_map(html: str) -> dict:
    m = MAPITEMS_RE.search(html)
    if not m:
        return {}
    out = {}
    for it in json.loads(m.group(1)):
        try:
            node_id = int(it["id"])
        except (KeyError, TypeError, ValueError):
            continue
        marker = str(it.get("markerColor", "")).lower()
        out[node_id] = {
            "latitude": it.get("latitude"),
            "longitude": it.get("longitude"),
            "marker_state": "ok" if marker in ("#00d900", "#0d0") else "stale",
        }
    return out


def enrich(rec: dict, now_utc: dt.datetime) -> dict:
    flags = []
    ts_utc = ts_eat = age_h = None
    if rec.get("epoch_ms"):
        ts_utc = dt.datetime.fromtimestamp(rec["epoch_ms"] / 1000, tz=dt.timezone.utc)
        ts_eat = ts_utc.astimezone(EAT)
        age_h = round((now_utc - ts_utc).total_seconds() / 3600, 2)
        if age_h > 24:
            flags.append("stale_gt_24h")
        elif age_h > 3:
            flags.append("delayed_gt_3h")
    else:
        flags.append("no_timestamp")

    rh = rec.get("rh_pct")
    if rh is not None and (rh >= 999 or rh > 105 or rh < 0):
        flags.append("rh_sensor_suspect")
    t = rec.get("t_air_c")
    if t is not None and (t < -20 or t > 55):
        flags.append("temp_sensor_suspect")
    core = [rec.get("t_air_c"), rec.get("rh_pct"), rec.get("wind_kmh")]
    if all(v == 0 for v in core) and rec.get("rain_mm") in (0, None):
        flags.append("all_zero_dead")

    rec["ts_utc"] = ts_utc.isoformat() if ts_utc else None
    rec["ts_eat"] = ts_eat.isoformat() if ts_eat else None
    rec["age_hours"] = age_h
    rec["quality_flags"] = ";".join(flags)
    rec["ok"] = not flags
    return rec


# --------------------------------------------------------------------------- io
def _atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def load_seen_keys(obs_path: Path) -> set:
    seen = set()
    if not obs_path.exists():
        return seen
    with obs_path.open("r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("epoch_ms"):
                seen.add((row["node_id"], row["epoch_ms"]))
    return seen


def append_observations(obs_path: Path, rows: list[dict]):
    new_file = not obs_path.exists()
    obs_path.parent.mkdir(parents=True, exist_ok=True)
    with obs_path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OBS_COLUMNS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)


def write_csv(path: Path, columns: list[str], rows: list[dict]):
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    _atomic_write(path, buf.getvalue().encode("utf-8"))


def update_station_registry(path: Path, records: list[dict], now_iso: str):
    reg: dict[str, dict] = {}
    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                reg[row["node_id"]] = row
    for r in records:
        nid = str(r["node_id"])
        prev = reg.get(nid, {})
        reg[nid] = {
            "node_id": nid,
            "station_code": r["station_code"],
            "station_name": r["station_name"],
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "first_seen_utc": prev.get("first_seen_utc") or now_iso,
            "last_reading_utc": r.get("ts_utc") or prev.get("last_reading_utc") or "",
            "last_scraped_utc": now_iso,
        }
    rows = sorted(reg.values(), key=lambda x: int(x["node_id"]))
    write_csv(path, STATION_COLUMNS, rows)


def write_geojson(path: Path, records: list[dict]):
    feats = []
    for r in records:
        if r.get("latitude") is None or r.get("longitude") is None:
            continue
        props = {k: v for k, v in r.items() if k not in ("latitude", "longitude")}
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["longitude"], r["latitude"]]},
            "properties": props,
        })
    _atomic_write(path, json.dumps(
        {"type": "FeatureCollection", "features": feats}, ensure_ascii=False).encode("utf-8"))


def log_line(log_path: Path, msg: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp}  {msg}\n")


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--template", default="weather")
    ap.add_argument("--datadir", default=str(Path(__file__).parent / "data"))
    ap.add_argument("--logdir", default=str(Path(__file__).parent / "logs"))
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--keep-snapshots", type=int, default=2000,
                    help="prune snapshot files beyond this count (0 = keep all)")
    args = ap.parse_args()

    datadir = Path(args.datadir)
    logdir = Path(args.logdir)
    log_path = logdir / "scrape.log"
    obs_path = datadir / "observations.csv"

    now_utc = dt.datetime.now(dt.timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = now_utc.strftime("%Y%m%d_%H%M%SZ")

    s = requests.Session()
    s.headers["User-Agent"] = "uganda-weather-scraper/1.0"

    def fetch(url: str) -> str:
        last = None
        for attempt in range(1, args.retries + 1):
            try:
                r = s.get(url, timeout=args.timeout)
                r.raise_for_status()
                return r.text
            except Exception as e:  # noqa: BLE001
                last = e
        raise last

    try:
        list_html = fetch(f"{args.base}/livedata/list.jsf?template={args.template}")
        map_html = fetch(f"{args.base}/livedata/map.jsf?template={args.template}")
    except Exception as e:  # noqa: BLE001
        log_line(log_path, f"FETCH FAILED: {e!r}")
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1

    stations = parse_list(list_html)
    geo = parse_map(map_html)
    if not stations:
        log_line(log_path, "PARSE FAILED: 0 stations from list.jsf")
        print("parse failed: 0 stations", file=sys.stderr)
        return 1

    records = []
    for node_id, rec in sorted(stations.items()):
        rec.update(geo.get(node_id, {"latitude": None, "longitude": None, "marker_state": None}))
        rec["scraped_at_utc"] = now_iso
        records.append(enrich(rec, now_utc))

    # 4. raw snapshot (gzipped)
    snap = {
        "scraped_at_utc": now_utc.isoformat(), "source": args.base,
        "template": args.template, "station_count": len(records),
        "stations": records,
    }
    snap_path = datadir / "snapshots" / f"uganda_weather_{stamp}.json.gz"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(snap_path, gzip.compress(
        json.dumps(snap, ensure_ascii=False, indent=1).encode("utf-8")))

    # 5. append new readings
    seen = load_seen_keys(obs_path)
    new_rows = [r for r in records
                if r.get("epoch_ms") and (str(r["node_id"]), str(r["epoch_ms"])) not in seen]
    if new_rows:
        append_observations(obs_path, new_rows)

    # 6. current-state exports
    _atomic_write(datadir / "latest.json",
                  json.dumps(snap, ensure_ascii=False, indent=2).encode("utf-8"))
    write_csv(datadir / "latest.csv", LATEST_COLUMNS, records)
    write_geojson(datadir / "latest.geojson", records)

    # optional GeoPackage of current state (only if geopandas is installed)
    try:
        import geopandas as gpd
        from shapely.geometry import Point
        pts = [r for r in records if r.get("latitude") is not None]
        gdf = gpd.GeoDataFrame(
            [{k: v for k, v in r.items() if k not in ("latitude", "longitude")} for r in pts],
            geometry=[Point(r["longitude"], r["latitude"]) for r in pts],
            crs="EPSG:4326",
        )
        gpkg = datadir / "latest.gpkg"
        if gpkg.exists():
            gpkg.unlink()
        gdf.to_file(gpkg, layer="stations", driver="GPKG")
    except Exception as e:  # noqa: BLE001
        log_line(log_path, f"gpkg skipped: {e}")

    # 7. station registry
    update_station_registry(datadir / "stations.csv", records, now_iso)

    # prune old snapshots
    if args.keep_snapshots:
        snaps = sorted((datadir / "snapshots").glob("uganda_weather_*.json.gz"))
        for old in snaps[:-args.keep_snapshots]:
            old.unlink()

    fresh = sum(1 for r in records if r["age_hours"] is not None and r["age_hours"] <= 3)
    msg = (f"OK  stations={len(records)}  new_readings={len(new_rows)}  "
           f"fresh<=3h={fresh}  flagged={sum(1 for r in records if not r['ok'])}")
    log_line(log_path, msg)
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
