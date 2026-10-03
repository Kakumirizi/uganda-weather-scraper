#!/usr/bin/env python3
"""
One-off: rebuild data/weather.db from the legacy CSV store + raw snapshots.

  1. import data/observations.csv  (legacy store; first-write-wins values)
  2. replay every snapshot in time order through the same upsert the scraper uses, which applies the
     portal's later revisions (last write wins) and counts them in `revisions`
  3. import the station registry (one fixed, fuzzed position per station; per-scrape coordinates in
     the legacy files are random jitter and are intentionally NOT carried over or averaged)

Legacy rows have delayed_metrics = NULL ("unknown"): the old scraper did not capture per-sensor
delay markers, so a few historical values may be older than their timestamp.

Refuses to run if data/weather.db already holds observations (use --rebuild to start over).
"""
import argparse
import csv
import gzip
import json
import sys
from pathlib import Path

import weather_scraper as ws

ROOT = Path(__file__).parent


def num(x):
    return None if x in ("", None) else float(x)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT / "backup" / "observations.pre-sqlite.csv"))
    ap.add_argument("--stations", default=str(ROOT / "backup" / "stations.pre-sqlite.csv"))
    ap.add_argument("--snapshots", default=str(ROOT / "data" / "snapshots"))
    ap.add_argument("--db", default=str(ROOT / "data" / "weather.db"))
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if db.exists() and args.rebuild:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
    con = ws.open_db(db)
    if con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]:
        print("weather.db already has observations; pass --rebuild to start over", file=sys.stderr)
        return 1

    # 1. legacy CSV
    n_csv = 0
    with open(args.csv, newline="", encoding="utf-8") as fh, con:
        for r in csv.DictReader(fh):
            if not r["epoch_ms"]:
                continue
            flags = ";".join(f for f in r["quality_flags"].split(";") if f in ws.INTRINSIC_FLAGS)
            con.execute(
                "INSERT OR IGNORE INTO observations (node_id, epoch_ms, station_code, station_name, ts_utc, ts_eat,"
                " t_air_c, rain_mm, solar_wm2, rh_pct, wind_kmh, wind_max_kmh, delayed_metrics, quality_flags,"
                " first_seen_utc, last_seen_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)",
                (int(r["node_id"]), int(r["epoch_ms"]), r["station_code"], r["station_name"], r["ts_utc"],
                 r["ts_eat"], *[num(r[m]) for m in ws.METRICS], flags,
                 r["scraped_at_utc"], r["scraped_at_utc"]))
            n_csv += 1
    print(f"imported {n_csv} legacy CSV rows")

    # 2. replay snapshots chronologically
    counts = {"new": 0, "updated": 0, "same": 0}
    snaps = sorted(Path(args.snapshots).glob("uganda_weather_*.json.gz"))
    with con:
        for p in snaps:
            d = json.load(gzip.open(p, "rt", encoding="utf-8"))
            seen = d["scraped_at_utc"][:19] + "Z"
            for rec in d["stations"]:
                if not rec.get("epoch_ms"):
                    continue
                row = ws.obs_row(rec)
                row["delayed_metrics"] = rec.get("delayed_metrics")   # None for legacy snapshots = unknown
                counts[ws.upsert_observation(con, rec["node_id"], rec["epoch_ms"], row, seen)] += 1
    print(f"replayed {len(snaps)} snapshots: {counts}")

    # 3. registry
    with open(args.stations, newline="", encoding="utf-8") as fh, con:
        n = 0
        for r in csv.DictReader(fh):
            con.execute("INSERT OR REPLACE INTO stations VALUES (?,?,?,?,?,?,?,?)",
                        (int(r["node_id"]), r["station_code"], r["station_name"], num(r["latitude"]),
                         num(r["longitude"]), r["first_seen_utc"], r["last_reading_utc"] or None,
                         r["last_scraped_utc"]))
            n += 1
    print(f"imported {n} stations")

    tot = con.execute("SELECT COUNT(*), SUM(revisions>0), MAX(revisions) FROM observations").fetchone()
    print(f"weather.db: {tot[0]} observations, {tot[1]} revised at least once (max {tot[2]} revisions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
