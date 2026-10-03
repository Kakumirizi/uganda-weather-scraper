#!/usr/bin/env python3
"""
Uganda AWS network scraper  --  Adcon LiveData (addVANTAGE Pro 6).

Polls the public portal and accumulates a local time series in SQLite.

Each run (python weather_scraper.py):
  1. take an exclusive run lock (a concurrent run exits cleanly instead of racing)
  2. GET list.jsf?template=weather   -> full sensor table
  3. parse + sanity-check (fail BEFORE touching the store if the page looks truncated)
  4. write a raw snapshot            -> data/snapshots/uganda_weather_<UTC>.json.gz
  5. upsert readings (node_id, epoch_ms) into data/weather.db  (last write wins; revisions counted)
  6. refresh current-state exports   -> data/latest.{json,csv,geojson,gpkg}
  7. append one line to              -> logs/scrape.log

  python weather_scraper.py --export            # monthly CSVs + stations.csv -> data/exports/
  python weather_scraper.py --export 2026-09,2026-10   # chosen months only

Design notes
  * The portal only exposes each station's LATEST value, so a missed poll is lost for good.
    Poll at or below the stations' 15-minute cadence (see the scheduled task).
  * The portal revises values for a timestamp while its interval is still aggregating, so
    readings are upserted, not first-write-wins.
  * Individual sensor cells can be flagged "data delayed" (older than the row time). Those
    values are NOT stored against the row timestamp.
  * Public map coordinates are deliberately re-randomised by Adcon on every request. We keep ONE
    fixed position per station (first sight) and never average repeated samples: that would
    defeat the operator's location protection. Treat positions as regional context only.

Exit codes: 0 ok (or skipped: another run active), 1 fetch/parse/sanity failure (store untouched).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import requests

DEFAULT_BASE = os.environ.get("WEATHER_BASE", "http://196.0.33.173:8080")
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
DELAYED_ICON = "tag.data.delayed"          # per-cell marker: value is older than the row time

# Flags that describe the reading itself (safe to store forever). Staleness / lateness depend on
# when we looked, so they are computed for the "latest" views only and never persisted.
INTRINSIC_FLAGS = {"rh_sensor_suspect", "temp_sensor_suspect", "all_zero_dead",
                   "temp_rh_dead", "wind_sensor_suspect"}

OBS_VALUE_COLS = ["station_code", "station_name", "ts_utc", "ts_eat", *METRICS,
                  "delayed_metrics", "quality_flags"]
EXPORT_OBS_COLS = ["node_id", "station_code", "station_name", "latitude", "longitude",
                   "epoch_ms", "ts_utc", "ts_eat", *METRICS, "delayed_metrics",
                   "quality_flags", "first_seen_utc", "last_seen_utc", "revisions"]
STATION_COLS = ["node_id", "station_code", "station_name", "latitude", "longitude",
                "first_seen_utc", "last_reading_utc", "last_scraped_utc"]
LATEST_COLS = ["node_id", "station_code", "station_name", "latitude", "longitude",
               "epoch_ms", "ts_utc", "ts_eat", *METRICS, "delayed_metrics",
               "quality_flags", "age_hours", "ok", "scraped_at_utc"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    node_id         INTEGER NOT NULL,
    epoch_ms        INTEGER NOT NULL,
    station_code    TEXT,
    station_name    TEXT,
    ts_utc          TEXT,
    ts_eat          TEXT,
    t_air_c         REAL, rain_mm REAL, solar_wm2 REAL, rh_pct REAL,
    wind_kmh        REAL, wind_max_kmh REAL,
    delayed_metrics TEXT,            -- ';'-joined metrics the portal marked delayed (stored NULL); NULL = unknown (pre-migration)
    quality_flags   TEXT NOT NULL DEFAULT '',   -- intrinsic flags only
    first_seen_utc  TEXT NOT NULL,
    last_seen_utc   TEXT NOT NULL,
    revisions       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (node_id, epoch_ms)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_obs_epoch ON observations(epoch_ms);
CREATE TABLE IF NOT EXISTS stations (
    node_id          INTEGER PRIMARY KEY,
    station_code     TEXT,
    station_name     TEXT,
    latitude         REAL,           -- fixed at first sight (fuzzed by the portal); never averaged
    longitude        REAL,
    first_seen_utc   TEXT NOT NULL,
    last_reading_utc TEXT,
    last_scraped_utc TEXT NOT NULL
);
"""


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


def parse_list(html: str) -> tuple[dict, int]:
    """Return ({node_id: record}, skipped_rows)."""
    out, skipped = {}, 0
    for row_html in ROW_RE.findall(html):
        cells = CELL_RE.findall(row_html)
        if len(cells) < 9:
            skipped += 1
            continue
        nm = NODE_RE.search(cells[1])
        if not nm:
            skipped += 1
            continue
        node_id = int(nm.group(1))
        label = _text(cells[1])
        cm = re.match(r"(\d+)\s+(.*)", label)
        code, name = (cm.group(1), cm.group(2)) if cm else ("", label)

        sk = SORTKEY_RE.search(cells[2])
        epoch_ms = int(sk.group(1)) if sk and sk.group(1).strip().isdigit() else None

        rec = {"node_id": node_id, "station_code": code, "station_name": name, "epoch_ms": epoch_ms}
        delayed = []
        for key, cell in zip(METRICS, cells[3:9]):
            sk = SORTKEY_RE.search(cell)
            # sortkey span = full-precision value; else visible text with tags stripped first so
            # jsessionid hex inside a status-icon <img src> can never be read as a number.
            rec[key] = _clean_num(sk.group(1)) if sk else _clean_num(_text(cell))
            if DELAYED_ICON in cell:
                delayed.append(key)
        rec["delayed_metrics"] = ";".join(delayed)
        out[node_id] = rec
    return out, skipped


def parse_map(html: str) -> dict:
    m = MAPITEMS_RE.search(html)
    if not m:
        return {}
    out = {}
    for it in json.loads(m.group(1)):
        try:
            out[int(it["id"])] = {"latitude": it.get("latitude"), "longitude": it.get("longitude")}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def intrinsic_flags(rec: dict) -> list[str]:
    """Properties of the reading itself. Safe to persist."""
    flags = []
    rh, t = rec.get("rh_pct"), rec.get("t_air_c")
    if rh is not None and (rh >= 999 or rh > 105 or rh < 0):
        flags.append("rh_sensor_suspect")
    if t is not None and (t < -20 or t > 55):
        flags.append("temp_sensor_suspect")
    core = [t, rh, rec.get("wind_kmh")]
    if all(v == 0 for v in core) and rec.get("rain_mm") in (0, None):
        flags.append("all_zero_dead")
    elif t == 0 and rh == 0:
        flags.append("temp_rh_dead")          # T and RH both pinned at 0 while wind may still read
    for k in ("wind_kmh", "wind_max_kmh"):
        w = rec.get(k)
        if w is not None and w > 100:
            flags.append("wind_sensor_suspect")   # >100 km/h is not credible for this network
            break
    return flags


def enrich(rec: dict, now_utc: dt.datetime) -> dict:
    """Add timestamps, reading age and flags for the 'latest' views."""
    flags = intrinsic_flags(rec)
    ts_utc = ts_eat = age_h = None
    if rec.get("epoch_ms"):
        ts_utc = dt.datetime.fromtimestamp(rec["epoch_ms"] / 1000, tz=dt.timezone.utc)
        ts_eat = ts_utc.astimezone(EAT)
        age_h = round((now_utc - ts_utc).total_seconds() / 3600, 2)
        if age_h < -0.25:
            flags.append("future_timestamp")
        elif age_h > 24:
            flags.append("stale_gt_24h")
        elif age_h > 3:
            flags.append("delayed_gt_3h")
    else:
        flags.append("no_timestamp")
    rec["ts_utc"] = ts_utc.isoformat() if ts_utc else None
    rec["ts_eat"] = ts_eat.isoformat() if ts_eat else None
    rec["age_hours"] = age_h
    rec["quality_flags"] = ";".join(flags)
    rec["ok"] = not flags
    return rec


# --------------------------------------------------------------------------- io helpers
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


def _csv_bytes(columns: list[str], rows) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def log_line(log_path: Path, msg: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp}  {msg}\n")


class RunLock:
    """Exclusive, non-blocking, process-crash-safe lock (OS releases it if we die)."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.fh.close()
            self.fh = None
            return False

    def release(self):
        if self.fh:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.fh.close()
            self.fh = None


# --------------------------------------------------------------------------- store
def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def obs_row(rec: dict) -> dict:
    """Values to persist for a reading. Sensors the portal marked delayed are stored NULL."""
    delayed = [m for m in rec.get("delayed_metrics", "").split(";") if m]
    row = {
        "station_code": rec["station_code"], "station_name": rec["station_name"],
        "ts_utc": rec["ts_utc"], "ts_eat": rec["ts_eat"],
        "delayed_metrics": rec.get("delayed_metrics", ""),
    }
    for m in METRICS:
        row[m] = None if m in delayed else rec.get(m)
    kept = set(intrinsic_flags({**rec, **{m: row[m] for m in METRICS}})) & INTRINSIC_FLAGS
    row["quality_flags"] = ";".join(sorted(kept))
    return row


def upsert_observation(con, node_id: int, epoch_ms: int, row: dict, seen_iso: str) -> str:
    cur = con.execute("SELECT * FROM observations WHERE node_id=? AND epoch_ms=?", (node_id, epoch_ms))
    old = cur.fetchone()
    if old is None:
        cols = ["node_id", "epoch_ms", *OBS_VALUE_COLS, "first_seen_utc", "last_seen_utc"]
        vals = [node_id, epoch_ms, *[row[c] for c in OBS_VALUE_COLS], seen_iso, seen_iso]
        con.execute(f"INSERT INTO observations ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        return "new"
    changed = any(old[c] != row[c] for c in OBS_VALUE_COLS if c not in ("delayed_metrics",))
    # delayed_metrics NULL (pre-migration) -> now known: record it without counting a value revision
    sets = ", ".join(f"{c}=?" for c in OBS_VALUE_COLS)
    con.execute(
        f"UPDATE observations SET {sets}, last_seen_utc=?, revisions=revisions+? WHERE node_id=? AND epoch_ms=?",
        [*[row[c] for c in OBS_VALUE_COLS], seen_iso, 1 if changed else 0, node_id, epoch_ms])
    return "updated" if changed else "same"


def upsert_station(con, rec: dict, geo: dict, seen_iso: str):
    nid = rec["node_id"]
    prev = con.execute("SELECT * FROM stations WHERE node_id=?", (nid,)).fetchone()
    g = geo.get(nid) or {}
    if prev is None:
        con.execute("INSERT INTO stations VALUES (?,?,?,?,?,?,?,?)",
                    (nid, rec["station_code"], rec["station_name"], g.get("latitude"), g.get("longitude"),
                     seen_iso, rec.get("ts_utc"), seen_iso))
        return
    lat = prev["latitude"] if prev["latitude"] is not None else g.get("latitude")   # position never updated
    lon = prev["longitude"] if prev["longitude"] is not None else g.get("longitude")
    con.execute("UPDATE stations SET station_code=?, station_name=?, latitude=?, longitude=?, "
                "last_reading_utc=COALESCE(?, last_reading_utc), last_scraped_utc=? WHERE node_id=?",
                (rec["station_code"], rec["station_name"], lat, lon, rec.get("ts_utc"), seen_iso, nid))


# --------------------------------------------------------------------------- exports
def write_geojson(path: Path, records: list[dict]):
    feats = []
    for r in records:
        if r.get("latitude") is None or r.get("longitude") is None:
            continue
        props = {k: v for k, v in r.items() if k not in ("latitude", "longitude")}
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [r["longitude"], r["latitude"]]},
                      "properties": props})
    _atomic_write(path, json.dumps({"type": "FeatureCollection", "features": feats},
                                   ensure_ascii=False).encode("utf-8"))


def write_gpkg(path: Path, records: list[dict], log_path: Path):
    try:
        import geopandas as gpd
        from shapely.geometry import Point
        pts = [r for r in records if r.get("latitude") is not None]
        gdf = gpd.GeoDataFrame(
            [{k: v for k, v in r.items() if k not in ("latitude", "longitude")} for r in pts],
            geometry=[Point(r["longitude"], r["latitude"]) for r in pts], crs="EPSG:4326")
        tmp = path.with_suffix(".tmp.gpkg")
        if tmp.exists():
            tmp.unlink()
        gdf.to_file(tmp, layer="stations", driver="GPKG")
        os.replace(tmp, path)                  # replace only after a complete write
    except Exception as e:  # noqa: BLE001 - optional output; never fail the run for it
        log_line(log_path, f"gpkg skipped: {e}")


def export_csvs(con, datadir: Path, months: list[str] | None):
    outdir = datadir / "exports"
    months = months or [r[0] for r in con.execute(
        "SELECT DISTINCT substr(ts_eat,1,7) FROM observations WHERE ts_eat IS NOT NULL ORDER BY 1")]
    for mth in months:
        rows = con.execute(
            "SELECT o.*, s.latitude, s.longitude FROM observations o "
            "LEFT JOIN stations s USING(node_id) WHERE substr(o.ts_eat,1,7)=? "
            "ORDER BY o.epoch_ms, o.node_id", (mth,)).fetchall()
        if not rows:
            print(f"skipped {mth}: no rows")
            continue
        _atomic_write(outdir / f"observations_{mth}.csv", _csv_bytes(EXPORT_OBS_COLS, [dict(r) for r in rows]))
        print(f"exported {mth}: {len(rows)} rows")
    st = con.execute("SELECT * FROM stations ORDER BY node_id").fetchall()
    _atomic_write(datadir / "stations.csv", _csv_bytes(STATION_COLS, [dict(r) for r in st]))
    print(f"exported stations.csv: {len(st)} stations")


# --------------------------------------------------------------------------- network
class Fetcher:
    def __init__(self, base: str, template: str, timeout, attempts: int):
        self.base, self.template, self.timeout, self.attempts = base, template, timeout, attempts
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "uganda-weather-scraper/2.0"

    def get(self, view: str, attempts: int | None = None) -> str:
        url = f"{self.base}/livedata/{view}.jsf"
        params = {"template": self.template, "units": "metric", "locale": "en"}
        last = None
        n = attempts or self.attempts
        for i in range(1, n + 1):
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
                r.raise_for_status()
                return r.text
            except requests.RequestException as e:
                last = e
                if i < n:
                    time.sleep(3 * (3 ** (i - 1)) + random.uniform(0, 1.5))   # 3s, 9s, ... + jitter
        raise last


def load_geo_cache(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {int(k): v for k, v in raw.items()}
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--template", default="weather")
    ap.add_argument("--datadir", default=str(Path(__file__).parent / "data"))
    ap.add_argument("--logdir", default=str(Path(__file__).parent / "logs"))
    ap.add_argument("--connect-timeout", type=float, default=10)
    ap.add_argument("--read-timeout", type=float, default=40)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--keep-snapshots", type=int, default=3000, help="0 = keep all")
    ap.add_argument("--min-station-ratio", type=float, default=0.9,
                    help="fail if fewer than this fraction of recently seen stations parse")
    ap.add_argument("--force", action="store_true", help="skip the station-count sanity check")
    ap.add_argument("--export", nargs="?", const="ALL", metavar="YYYY-MM[,YYYY-MM...]",
                    help="write monthly CSV exports (+ stations.csv) and exit")
    args = ap.parse_args()

    datadir, logdir = Path(args.datadir), Path(args.logdir)
    log_path = logdir / "scrape.log"
    db_path = datadir / "weather.db"

    lock = RunLock(datadir / ".lock")
    if not lock.acquire():
        log_line(log_path, "SKIPPED: another run holds the lock")
        print("skipped: another run in progress")
        return 0
    try:
        if args.export:
            con = open_db(db_path)
            export_csvs(con, datadir, None if args.export == "ALL" else [m for m in args.export.split(",") if m])
            return 0
        return run_once(args, datadir, log_path, db_path)
    finally:
        lock.release()


def run_once(args, datadir: Path, log_path: Path, db_path: Path) -> int:
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = now_utc.strftime("%Y%m%d_%H%M%SZ")
    fetcher = Fetcher(args.base, args.template, (args.connect_timeout, args.read_timeout), args.retries)

    # ---- fetch + parse + sanity (nothing is written until all of this passes)
    try:
        list_html = fetcher.get("list")
    except Exception as e:  # noqa: BLE001
        log_line(log_path, f"FETCH FAILED: {e!r}")
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1
    try:
        stations, skipped = parse_list(list_html)
    except Exception as e:  # noqa: BLE001
        log_line(log_path, f"PARSE FAILED: {e!r}")
        print(f"parse failed: {e}", file=sys.stderr)
        return 1
    if not stations:
        log_line(log_path, "PARSE FAILED: 0 stations from list.jsf")
        print("parse failed: 0 stations", file=sys.stderr)
        return 1

    con = open_db(db_path)
    known = con.execute("SELECT COUNT(*) FROM stations WHERE last_scraped_utc >= ?",
                        ((now_utc - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),)).fetchone()[0]
    if not args.force and known and len(stations) < args.min_station_ratio * known:
        msg = (f"SANITY FAILED: parsed {len(stations)} stations but {known} seen in the last 3 days "
               f"(skipped_rows={skipped}); layout change or truncated page - store untouched")
        log_line(log_path, msg)
        print(msg, file=sys.stderr)
        return 1

    # ---- positions: fetch map.jsf ONLY when a listed station has no stored position yet
    have = {r[0] for r in con.execute("SELECT node_id FROM stations WHERE latitude IS NOT NULL")}
    geo = {}
    if set(stations) - have:
        try:
            geo = parse_map(fetcher.get("map", attempts=1))
        except Exception as e:  # noqa: BLE001 - positions are non-essential; try again next run
            log_line(log_path, f"map fetch skipped: {e!r}")

    records = []
    for node_id, rec in sorted(stations.items()):
        records.append(enrich(rec, now_utc))
        rec["scraped_at_utc"] = now_iso

    # ---- raw snapshot (audit / reprocessing)
    snap = {"scraped_at_utc": now_utc.isoformat(), "source": args.base, "template": args.template,
            "station_count": len(records), "skipped_rows": skipped, "stations": records}
    _atomic_write(datadir / "snapshots" / f"uganda_weather_{stamp}.json.gz",
                  gzip.compress(json.dumps(snap, ensure_ascii=False).encode("utf-8")))

    # ---- store: one transaction for readings + registry
    counts = {"new": 0, "updated": 0, "same": 0}
    with con:
        for rec in records:
            upsert_station(con, rec, geo, now_iso)
            if rec.get("epoch_ms"):
                counts[upsert_observation(con, rec["node_id"], rec["epoch_ms"], obs_row(rec), now_iso)] += 1
    pos = {r["node_id"]: (r["latitude"], r["longitude"]) for r in con.execute("SELECT * FROM stations")}
    for rec in records:
        rec["latitude"], rec["longitude"] = pos.get(rec["node_id"], (None, None))
    con.close()

    # ---- current-state exports
    out = {"scraped_at_utc": now_utc.isoformat(), "source": args.base, "template": args.template,
           "station_count": len(records), "skipped_rows": skipped, "stations": records}
    _atomic_write(datadir / "latest.json", json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8"))
    _atomic_write(datadir / "latest.csv", _csv_bytes(LATEST_COLS, records))
    write_geojson(datadir / "latest.geojson", records)
    write_gpkg(datadir / "latest.gpkg", records, log_path)

    if args.keep_snapshots:
        snaps = sorted((datadir / "snapshots").glob("uganda_weather_*.json.gz"))
        for old in snaps[:-args.keep_snapshots]:
            old.unlink()

    fresh = sum(1 for r in records if r["age_hours"] is not None and r["age_hours"] <= 3)
    msg = (f"OK  stations={len(records)}  new={counts['new']}  revised={counts['updated']}  "
           f"fresh<=3h={fresh}  flagged={sum(1 for r in records if not r['ok'])}"
           + (f"  skipped_rows={skipped}" if skipped else ""))
    log_line(log_path, msg)
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
