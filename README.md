# Uganda AWS network scraper

Polls the public **Adcon LiveData** portal for Uganda's automatic weather station network
(`http://196.0.33.173:8080/livedata/`, addVANTAGE Pro 6) and accumulates a local time series in SQLite.

## Why scraping

LiveData has **no API**. The portal exposes only each station's **latest** value (the `/values`
endpoint returns single aggregates; raw history needs an addVANTAGE login). So a poll that is missed
is lost for good — coverage depends on the collector running continuously.

## What runs

| File | Role |
|---|---|
| `weather_scraper.py` | one scrape per run: fetch → sanity-check → snapshot → upsert → exports |
| `run.ps1` | Task Scheduler wrapper; logs the scraper's **real** exit code and stderr |
| `scheduler/install_task.ps1` | (re)creates the Task Scheduler job from a known-good definition |
| `migrate_to_sqlite.py` | one-off: rebuilt the DB from the legacy CSV store + snapshots |
| `map/build_map.py` | renders `data/latest.json` to an interactive Leaflet map |

### Schedule

Windows Task **`UgandaWeatherScraper`**, **every 15 minutes** (stations log on a 15-minute grid),
installed by `scheduler/install_task.ps1`:

- runs **on battery**, and **catches up** one run after sleep (`StartWhenAvailable`)
- 5-minute execution limit, never overlaps itself (`IgnoreNew`), only when a network is available
- runs only while the user is logged on; **does not wake the machine**. A sleeping or powered-off
  laptop still misses polls. For continuous coverage, host it on an always-on machine.

```powershell
powershell -ExecutionPolicy Bypass -File scheduler\install_task.ps1 -IntervalMinutes 15   # install / reset
schtasks /Run /TN UgandaWeatherScraper                                                    # run now
schtasks /Change /TN UgandaWeatherScraper /DISABLE                                        # pause
```

A run also takes an exclusive file lock (`data/.lock`); a concurrent manual run exits cleanly with
`SKIPPED` instead of racing the scheduled one.

## Data (`data/`)

| Path | Git | Contents |
|---|---|---|
| `weather.db` | no | **the store** (SQLite, WAL). `observations` keyed `(node_id, epoch_ms)`, `stations` registry |
| `exports/observations_YYYY-MM.csv` | yes | monthly CSV exports of the store (`--export`) |
| `stations.csv` | yes | registry export |
| `latest.{json,csv,geojson,gpkg}` | no | current state, rewritten every run |
| `snapshots/*.json.gz` | no | full raw snapshot per run (last 3000 kept) |

```powershell
python weather_scraper.py --export            # all months + stations.csv -> data/exports/
python weather_scraper.py --export 2026-10    # one month
```

Query the store directly: `sqlite3 data/weather.db` (or `pandas.read_sql`).

### How readings are stored

- **Upsert, last write wins.** The portal revises a reading while its interval is still aggregating
  (e.g. wind avg 13.03 → 12.75 for the same timestamp). `revisions` counts changes; `first_seen_utc` /
  `last_seen_utc` bracket when the portal showed it.
- **Delayed sensors.** The portal flags individual sensor cells "data delayed" (older than the row
  time). Those values are stored as `NULL` and listed in `delayed_metrics` — never against the row
  timestamp. `delayed_metrics = NULL` means *unknown* (rows from before 2026-10-03).
- **Intrinsic flags only** are persisted in `quality_flags`. Staleness depends on when you look, so it
  is derived (`scraped_at − ts_utc`) and only appears in the `latest.*` views.

| stored flag | meaning |
|---|---|
| `rh_sensor_suspect` | RH at the `999` sentinel or outside 0–105 % |
| `temp_sensor_suspect` | air temp outside −20…55 °C |
| `wind_sensor_suspect` | wind or gust above 100 km/h |
| `all_zero_dead` | every core sensor reads 0 |
| `temp_rh_dead` | T and RH both pinned at 0 (wind may still read) |

`latest.*` adds `stale_gt_24h`, `delayed_gt_3h`, `no_timestamp`, `future_timestamp`.

Fields: `t_air_c` °C · `rain_mm` mm · `solar_wm2` W/m² · `rh_pct` % · `wind_kmh` / `wind_max_kmh` km/h.
Times: `epoch_ms` (source), `ts_utc`, `ts_eat` (UTC+3).

## Safety checks

- **Fails before writing** if the page is empty, or parses fewer than 90 % of the stations seen in the
  last 3 days (layout change / truncated page). The store is left untouched; `--force` overrides.
- Requests pin `units=metric&locale=en` (the portal otherwise switches to imperial for US browsers).
- Fetches retry with backoff (3 s, 9 s + jitter); `map.jsf` is fetched **only** when a listed station
  has no stored position yet.
- Exit code 1 on failure (visible in `logs/run.log`); `0` for success or a lock-skip.

## Caveats

- **Positions are fuzzed and are not survey grade.** Adcon deliberately re-randomises public-map
  coordinates on every request (~0.3 km σ here). We store **one fixed position per station** (first
  sight) and do **not** average repeated samples — that would defeat the operator's location
  protection. Regional context only.
- Rain: whether `rain_mm` is per-interval or cumulative is **unverified** — check before summing.
- Plain HTTP to a hardcoded IP; there is no TLS on the portal, so content is unauthenticated. Station
  names are HTML-escaped in the map and JSON is escaped before embedding in `<script>`.
- Some stations are dead or reporting sentinels; the flags mark them, nothing is dropped.

## Map

`python map/build_map.py` → `map/uganda_weather_map.html` (standalone Leaflet map: stations on the
Uganda outline, coloured by temperature / humidity / wind / reporting status; click for readings and
delayed-sensor notes). Ages are relative to the scrape time shown in the page, so a published copy
is a snapshot — rebuild and republish to refresh. Stations without coordinates are omitted and
reported by the build.

Publish the HTML as a Claude artifact, or serve `map/` over any static file server.
