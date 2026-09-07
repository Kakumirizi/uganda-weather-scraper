# Uganda AWS network scraper

Scrapes the public **Adcon LiveData** portal for Uganda's automatic weather station
network (`http://196.0.33.173:8080/livedata/`, addVANTAGE Pro 6 backend) and
accumulates a local time series.

## Why scraping

LiveData has **no API**. The `/values` endpoint returns a single aggregate only, and
raw history / true coordinates require an addVANTAGE Pro login. So we poll the two
public pages Adcon documents as embeddable and build our own record over time.

| Page | Used for |
|---|---|
| `list.jsf?template=weather` | full sensor table — T air, rain, solar, RH, wind, wind max, timestamp (hidden spans hold full-precision values) |
| `map.jsf?template=weather` | `liveDataMapItems` JSON — latitude, longitude, marker state |

Merged on the Adcon node id.

## What runs

`weather_scraper.py` — one scrape per invocation. No login, no server-state change,
no crawling beyond those two pages.

`run.ps1` — Task Scheduler wrapper (resolves the Python path, logs exit code).

### Schedule

Registered as Windows Task **`UgandaWeatherScraper`**, every 30 minutes, runs as
`user` when logged on, 10-minute timeout, skips if the previous run is still going.

```powershell
schtasks /Query  /TN UgandaWeatherScraper /V /FO LIST   # status
schtasks /Run    /TN UgandaWeatherScraper               # run now
schtasks /Change /TN UgandaWeatherScraper /MO 15        # change interval to 15 min
schtasks /End    /TN UgandaWeatherScraper               # kill a stuck run
schtasks /Delete /TN UgandaWeatherScraper /F            # remove
```

## Outputs (`data/`)

| File | Tracked in git | Contents |
|---|---|---|
| `observations.csv` | yes | **append-only time series.** One row per (station, reading timestamp), deduped on `node_id`+`epoch_ms`. This is the record that grows. |
| `stations.csv` | yes | station registry — one row per node, with `first_seen` / `last_reading` / `last_scraped` |
| `latest.json` | no | full current snapshot, all fields + QC |
| `latest.csv` | no | current state, one row per station |
| `latest.geojson` / `latest.gpkg` | no | current state as points (EPSG:4326) for QGIS |
| `snapshots/uganda_weather_<UTC>.json.gz` | no | full raw snapshot per run (last 2000 kept), for reprocessing |

`logs/scrape.log` — one line per run. `logs/run.log` — wrapper line per run with exit code.

## Fields

`t_air_c` °C · `rain_mm` mm · `solar_wm2` W/m² · `rh_pct` % · `wind_kmh` / `wind_max_kmh` km/h.
Times: `epoch_ms` (source), `ts_utc`, `ts_eat` (UTC+3). `age_hours` = reading age at scrape time.

### `quality_flags`

| flag | meaning |
|---|---|
| `stale_gt_24h` / `delayed_gt_3h` | reading older than the network's normal cadence |
| `rh_sensor_suspect` | RH at the `999` sentinel or outside 0–105 % |
| `temp_sensor_suspect` | air temp outside −20…55 °C |
| `all_zero_dead` | every core sensor reads 0 — station offline |
| `no_timestamp` | list view showed `---`; row kept in snapshot, **not** appended to the time series |

## Caveats

- **Coordinates are deliberately fuzzed** by Adcon for anonymous viewers. Fine for a
  regional map, not for survey work.
- The feed itself is sometimes hours behind (some stations report hourly, some have
  been dead since 2024–2025). The QC flags mark this; nothing is silently dropped.
- If Adcon changes the LiveData HTML, `parse_list` returning 0 stations makes the run
  exit 1 and leaves the store untouched — check `logs/scrape.log`.

## Run manually

```powershell
python weather_scraper.py                    # uses ./data and ./logs
python weather_scraper.py --template waterlevel   # the 13 river gauges (separate dataset)
```

## Map

`map/build_map.py` turns `data/latest.json` into a standalone interactive Leaflet
map (`map/uganda_weather_map.html`) — stations plotted on the Uganda outline,
coloured by temperature / humidity / wind / reporting status, click for full
readings. Pulls only Leaflet JS + fonts from a CDN; everything else is inlined.

```powershell
python map/build_map.py        # regenerate after a scrape
```

Publish the HTML as a Claude artifact, or serve `map/` over any static file server.
Inputs (`leaflet-1.9.4.css`, `uganda_outline.geojson`) are vendored; the generated
HTML is gitignored.
