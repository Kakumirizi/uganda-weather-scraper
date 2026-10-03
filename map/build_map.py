# Assembles a standalone Leaflet map of the latest scrape.
#   python map/build_map.py   ->   map/uganda_weather_map.html
# Reads data/latest.json (written by weather_scraper.py); publish the HTML as an artifact
# or open it over any static file server (it pulls only Leaflet JS + fonts from CDN).
import json, pathlib, re

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
latest = json.loads((ROOT / "data" / "latest.json").read_text(encoding="utf-8"))
scraped = latest["scraped_at_utc"]
no_coords = [s for s in latest["stations"] if s.get("latitude") is None or s.get("longitude") is None]
if no_coords:
    print(f"note: {len(no_coords)} station(s) have no coordinates and are omitted from the map:",
          ", ".join(s["station_name"] for s in no_coords))
stations = [
    {"id": s["node_id"], "code": s["station_code"], "name": s["station_name"],
     "lat": round(s["latitude"], 5), "lon": round(s["longitude"], 5),
     "dl": s.get("delayed_metrics") or "",
     "t": s["t_air_c"], "rain": s["rain_mm"], "solar": s["solar_wm2"], "rh": s["rh_pct"],
     "wind": s["wind_kmh"], "wmax": s["wind_max_kmh"],
     "eat": s["ts_eat"], "age": s["age_hours"], "flags": s["quality_flags"]}
    for s in latest["stations"]
    if s.get("latitude") is not None and s.get("longitude") is not None
]
def script_json(obj):
    """JSON safe inside <script>: escape < (and U+2028/9) so a station name containing </script> cannot break out."""
    return (json.dumps(obj, separators=(",", ":"))
            .replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


import datetime as dt
import sqlite3

LAYER_FILES = {"lakes": "lakes.geojson", "rivers": "rivers.geojson",
               "roads_major": "roads_major.geojson", "roads_minor": "roads_secondary.geojson"}
layers = {k: json.loads((HERE / "layers" / f).read_text(encoding="utf-8")) for k, f in LAYER_FILES.items()}
labels = json.loads((HERE / "layers" / "labels.json").read_text(encoding="utf-8"))


def load_week(scraped_iso):
    """Last 7 days of readings before the scrape time, straight from the store (read-only)."""
    db = ROOT / "data" / "weather.db"
    if not db.exists():
        print("note: data/weather.db not found - CSV download will be hidden")
        return [], {}
    end_ms = int(dt.datetime.fromisoformat(scraped_iso).timestamp() * 1000)
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        rows = con.execute(
            "SELECT node_id, epoch_ms, t_air_c, rain_mm, solar_wm2, rh_pct, wind_kmh, wind_max_kmh,"
            " COALESCE(delayed_metrics,''), quality_flags FROM observations"
            " WHERE epoch_ms BETWEEN ? AND ? ORDER BY epoch_ms, node_id",
            (end_ms - 7 * 86400 * 1000, end_ms)).fetchall()
        ids = sorted({r[0] for r in rows})
        meta = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for nid, code, name, lat, lon in con.execute(
                    "SELECT node_id, station_code, station_name, latitude, longitude FROM stations"
                    f" WHERE node_id IN ({','.join('?' * len(chunk))})", chunk):
                meta[str(nid)] = [code, name, None if lat is None else round(lat, 5), None if lon is None else round(lon, 5)]
    finally:
        con.close()
    return [list(r) for r in rows], meta


csv_rows, csv_stations = load_week(scraped)
print(f"csv window: {len(csv_rows)} readings from {len(csv_stations)} stations")

uganda = (HERE / "uganda_outline.geojson").read_text(encoding="utf-8").strip()
leaflet_css = (HERE / "leaflet-1.9.4.css").read_text(encoding="utf-8")

# strip url(images/...) rules from leaflet css - those assets are CSP-blocked and unused
import re
leaflet_css = re.sub(r"[^{}]*\burl\(images/[^)]*\)[^{}]*\{[^}]*\}", "", leaflet_css)

CSS = r"""
:root{
  --ground:#e8ecee; --panel:#ffffff; --panel-2:#f3f6f7;
  --ink:#19262e; --ink-soft:#47575f; --muted:#7b8a92;
  --line:#d3dbdf; --line-soft:#e3e9eb;
  --accent:#1f6f8b; --accent-ink:#0f5b76;
  --land:#f1eee6; --land-line:#cabf9f;
  --lake-fill:#c5dbe6; --lake-line:#9bbccd; --river:#7aaac3; --road-major:#b08455; --road-minor:#d3c6ac;
  --lbl-ink:#4f7b93;
  --s-fresh:#4f9d69; --s-delayed:#cf9433; --s-stale:#8a97a1; --s-dead:#bd5648;
  --shadow:0 1px 2px rgba(20,40,50,.08),0 10px 30px rgba(20,40,50,.07);
  --marker-stroke:#ffffff; --on-accent:#ffffff;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ground:#0d1319; --panel:#151d23; --panel-2:#1a232a;
    --ink:#e7eef2; --ink-soft:#adb9c0; --muted:#7c8a92;
    --line:#2a343c; --line-soft:#222b32;
    --accent:#5bb0cd; --accent-ink:#8ad0e5;
    --land:#1a222a; --land-line:#36434d;
    --lake-fill:#1d3441; --lake-line:#2d4c5d; --river:#3d7794; --road-major:#8f6b43; --road-minor:#4a4439;
    --lbl-ink:#86b0c4;
    --s-fresh:#57a971; --s-delayed:#d5a049; --s-stale:#8894a0; --s-dead:#cd6153;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
    --marker-stroke:#151d23; --on-accent:#0d1319;
  }
}
:root[data-theme="dark"]{
  --ground:#0d1319; --panel:#151d23; --panel-2:#1a232a;
  --ink:#e7eef2; --ink-soft:#adb9c0; --muted:#7c8a92;
  --line:#2a343c; --line-soft:#222b32;
  --accent:#5bb0cd; --accent-ink:#8ad0e5;
  --land:#1a222a; --land-line:#36434d;
    --lake-fill:#1d3441; --lake-line:#2d4c5d; --river:#3d7794; --road-major:#8f6b43; --road-minor:#4a4439;
    --lbl-ink:#86b0c4;
  --s-fresh:#57a971; --s-delayed:#d5a049; --s-stale:#8894a0; --s-dead:#cd6153;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
  --marker-stroke:#151d23; --on-accent:#0d1319;
}

*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0;background:var(--ground);color:var(--ink);
  font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;
}
.mono{font-family:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}

#app{display:grid;grid-template-columns:344px 1fr;grid-template-rows:minmax(0,1fr);
  height:100dvh;overflow:hidden}
#rail{
  background:var(--panel);border-right:1px solid var(--line);
  display:flex;flex-direction:column;min-height:0;box-shadow:var(--shadow);z-index:5;
}
#map{position:relative;background:var(--ground);min-height:0;height:100%}

.rail-head{padding:20px 20px 16px;border-bottom:1px solid var(--line-soft)}
.eyebrow{
  font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
  font-weight:500;margin:0 0 6px;
}
h1{font-size:19px;font-weight:600;margin:0;letter-spacing:-.01em;line-height:1.25;text-wrap:balance}
.sub{margin:9px 0 0;font-size:12.5px;color:var(--ink-soft)}
.sub .mono{color:var(--ink)}
.feednote{
  margin:12px 0 0;padding:8px 10px;border-radius:7px;
  background:var(--panel-2);border:1px solid var(--line-soft);
  font-size:11.5px;color:var(--ink-soft);
}
.feednote b{color:var(--ink);font-weight:600}

.chips{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line-soft);
  border-bottom:1px solid var(--line-soft)}
.chip{background:var(--panel);padding:11px 8px 10px;text-align:center;cursor:pointer;border:0;
  font-family:inherit;color:inherit;transition:background .12s}
.chip:hover{background:var(--panel-2)}
.chip.on{background:var(--panel-2);box-shadow:inset 0 -2px 0 var(--accent)}
.chip .n{font-size:17px;font-weight:600;line-height:1}
.chip .k{display:block;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-top:5px}
.chip .dot{width:6px;height:6px;border-radius:50%;display:inline-block;margin-right:5px;vertical-align:1px}

.controls{padding:15px 20px 6px}
.control-label{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:500;margin-bottom:8px}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.seg button{
  font-family:inherit;font-size:12.5px;color:var(--ink-soft);
  background:var(--panel-2);border:1px solid var(--line);border-radius:7px;
  padding:7px 6px;cursor:pointer;transition:all .12s;
}
.seg button:hover{border-color:var(--muted);color:var(--ink)}
.seg button[aria-pressed="true"]{
  background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:500;
}

.legend{padding:14px 20px 16px;border-bottom:1px solid var(--line-soft)}
.legend-bar{height:10px;border-radius:3px;margin:0 0 6px}
.legend-scale{display:flex;justify-content:space-between;font-size:11px;color:var(--ink-soft)}
.legend-swatches{display:flex;flex-direction:column;gap:6px}
.legend-swatches .row{display:flex;align-items:center;gap:9px;font-size:12px;color:var(--ink-soft)}
.legend-swatches .sw{width:11px;height:11px;border-radius:50%;flex:none}
.legend-swatches .sw.hollow{background:transparent;border:1.6px solid var(--muted)}
.legend-foot{margin-top:9px;font-size:11px;color:var(--muted);display:flex;align-items:center;gap:8px}
.legend-foot .sw{width:10px;height:10px;border-radius:50%;border:1.6px solid var(--muted);flex:none}

.list-wrap{flex:1;min-height:0;display:flex;flex-direction:column}
.search{padding:12px 20px 10px}
.search input{
  width:100%;font-family:inherit;font-size:13px;color:var(--ink);
  background:var(--panel-2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;
}
.search input::placeholder{color:var(--muted)}
.search input:focus{outline:2px solid var(--accent);outline-offset:1px;border-color:transparent}
#list{flex:1;overflow-y:auto;padding:0 12px 14px}
.st{
  display:flex;align-items:baseline;gap:9px;width:100%;text-align:left;
  font-family:inherit;background:none;border:0;border-radius:6px;
  padding:7px 8px;cursor:pointer;color:var(--ink);
}
.st:hover{background:var(--panel-2)}
.st[aria-current="true"]{background:color-mix(in srgb,var(--accent) 14%,transparent)}
.st .marker{width:8px;height:8px;border-radius:50%;flex:none;align-self:center}
.st .marker.hollow{border:1.5px solid var(--muted);background:transparent!important}
.st .nm{flex:1;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.st .vv{font-size:12px;color:var(--ink-soft);flex:none}
.st .vv .u{color:var(--muted);font-size:10.5px;margin-left:1px}

.rail-foot{padding:11px 20px;border-top:1px solid var(--line-soft);font-size:10.5px;color:var(--muted);line-height:1.45}
.rail-foot a{color:var(--accent-ink);text-decoration:none}

/* leaflet overrides */
.leaflet-container{background:var(--ground);font-family:"IBM Plex Sans",sans-serif}
.leaflet-bar{border:1px solid var(--line);box-shadow:var(--shadow)}
.leaflet-bar a{background:var(--panel);color:var(--ink);border-bottom-color:var(--line)}
.leaflet-bar a:hover{background:var(--panel-2)}
.leaflet-popup-content-wrapper,.leaflet-popup-tip{
  background:var(--panel);color:var(--ink);box-shadow:var(--shadow);border:1px solid var(--line);
}
.leaflet-popup-content-wrapper{border-radius:10px}
.leaflet-popup-content{margin:14px 16px;font-size:13px;line-height:1.45}
.leaflet-container a.leaflet-popup-close-button{color:var(--muted);width:26px;height:26px;font-size:17px;line-height:26px}
.leaflet-container a.leaflet-popup-close-button:hover{color:var(--ink)}

.pop h4{margin:0 0 2px;font-size:14px;font-weight:600}
.pop .pmeta{font-size:11px;color:var(--muted);letter-spacing:.01em}
.pop .pgrid{
  display:grid;grid-template-columns:auto 1fr;gap:3px 14px;margin:11px 0 9px;
  padding:10px 0;border-top:1px solid var(--line-soft);border-bottom:1px solid var(--line-soft);
}
.pop .pgrid dt{color:var(--ink-soft);font-size:12px}
.pop .pgrid dd{margin:0;text-align:right;font-size:12.5px}
.pop .pgrid dd .u{color:var(--muted);font-size:10.5px;margin-left:2px}
.pop .pgrid dd.bad{color:var(--s-dead)}
.pop .ptime{font-size:11.5px;color:var(--ink-soft)}
.pop .pflags{margin-top:8px;display:flex;flex-wrap:wrap;gap:4px}
.pop .pflags span{
  font-size:10px;letter-spacing:.03em;text-transform:uppercase;
  background:var(--panel-2);border:1px solid var(--line);color:var(--ink-soft);
  border-radius:4px;padding:2px 5px;
}
.pop .pflags span.warn{border-color:var(--s-dead);color:var(--s-dead)}

/* map layers card (Leaflet control) */
.layers-card{background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);
  padding:10px 10px 8px;min-width:150px}
.layers-card .control-label{margin:0 0 6px 2px}
.lay{display:flex;align-items:center;gap:9px;width:100%;text-align:left;font-family:inherit;font-size:12.5px;
  color:var(--ink);background:none;border:0;border-radius:6px;padding:5px 6px;cursor:pointer}
.lay:hover{background:var(--panel-2)}
.lay:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.lay .lsw{width:16px;height:10px;flex:none;border-radius:2px;opacity:.35;transition:opacity .12s}
.lay[aria-pressed="true"] .lsw{opacity:1}
.lay[aria-pressed="false"]{color:var(--muted)}
.lsw-lake{background:var(--lake-fill);border:1px solid var(--lake-line)}
.lsw-river{height:0!important;border-top:2px solid var(--river);border-radius:0!important}
.lsw-road{height:0!important;border-top:3px solid var(--road-major);border-radius:0!important}
.lsw-road2{height:0!important;border-top:1.5px solid var(--road-minor);border-radius:0!important}
/* names under the markers */
.lbl{pointer-events:none}
.lbl span{position:absolute;transform:translate(-50%,-50%);white-space:nowrap;font-size:11px;font-style:italic;
  color:var(--lbl-ink);letter-spacing:.04em}
.lbl-lake span{text-transform:uppercase;font-size:10px;letter-spacing:.18em;font-style:normal;
  text-shadow:0 0 3px var(--lake-fill),0 0 3px var(--lake-fill),0 0 6px var(--lake-fill)}
.lbl-river span{text-shadow:0 0 3px var(--land),0 0 3px var(--land),0 0 6px var(--land)}

/* CSV download */
.dl{padding:10px 20px 11px;border-top:1px solid var(--line-soft)}
.dl-btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;font-family:inherit;font-size:12.5px;
  font-weight:500;color:var(--ink);background:var(--panel-2);border:1px solid var(--line);border-radius:7px;
  padding:8px 10px;cursor:pointer;transition:border-color .12s}
.dl-btn:hover{border-color:var(--accent)}
.dl-btn:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.dl-btn svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.dl-note{margin-top:6px;font-size:10.5px;color:var(--muted);line-height:1.4}

@media (max-width:820px){
  #app{grid-template-columns:1fr;grid-template-rows:minmax(0,46dvh) minmax(0,1fr)}
  #rail{border-right:0;border-bottom:1px solid var(--line)}
  .list-wrap{min-height:96px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = r"""
const STATIONS = __STATIONS__;
const UGANDA = __UGANDA__;
const LAYERS = __LAYERS__;
const LABELS = __LABELS__;
const CSV_ROWS = __CSVROWS__;
const CSV_ST = __CSVST__;
const SCRAPED = "__SCRAPED__";

const esc = v => String(v==null?"":v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const cssv = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const NOW = new Date(SCRAPED).getTime();   // every age is relative to scrape time
const reduceMotion = matchMedia("(prefers-reduced-motion:reduce)").matches;

const RAMPS = {
  t:    {label:"Air temperature", unit:"\u00b0C", dec:1,
         stops:[[14,"#3b6ea5"],[19,"#5b9aa0"],[24,"#b8a862"],[29,"#d1823f"],[34,"#b34a33"]]},
  rh:   {label:"Relative humidity", unit:"%", dec:0,
         stops:[[20,"#ece4d4"],[60,"#7fb0c0"],[100,"#25607e"]]},
  wind: {label:"Wind speed", unit:"km/h", dec:1,
         stops:[[0,"#e9decb"],[8,"#cf9a5c"],[16,"#8a5a2c"],[24,"#5c3413"]]},
};
const FLAG_LABEL = {
  stale_gt_24h:"stale >24h", delayed_gt_3h:"delayed >3h",
  rh_sensor_suspect:"RH sensor suspect", temp_sensor_suspect:"temp sensor suspect",
  all_zero_dead:"offline", temp_rh_dead:"T/RH sensors offline", wind_sensor_suspect:"wind sensor suspect", no_timestamp:"no timestamp", future_timestamp:"timestamp in the future",
};

const h2r = h => [1,3,5].map(i=>parseInt(h.slice(i,i+2),16));
const mix = (a,b,t)=>{const A=h2r(a),B=h2r(b);return `rgb(${A.map((x,i)=>Math.round(x+(B[i]-x)*t)).join(",")})`;};
function ramp(stops,v){
  if(v<=stops[0][0])return stops[0][1];
  const last=stops[stops.length-1];
  if(v>=last[0])return last[1];
  for(let i=0;i<stops.length-1;i++){
    const [x0,c0]=stops[i],[x1,c1]=stops[i+1];
    if(v>=x0&&v<=x1)return mix(c0,c1,(v-x0)/(x1-x0));
  }
  return last[1];
}
const flags = s => s.flags ? s.flags.split(";") : [];
function metricVal(s,mode){
  const f=flags(s);
  if(mode==="t")   return (s.t==null||f.includes("temp_sensor_suspect")||f.includes("temp_rh_dead")) ? null : s.t;
  if(mode==="rh")  return (s.rh==null||s.rh>=999||f.includes("rh_sensor_suspect")||f.includes("temp_rh_dead")) ? null : s.rh;
  if(mode==="wind")return (s.wind==null||f.includes("wind_sensor_suspect")) ? null : s.wind;
  return null;
}
function statusOf(s){
  const f=flags(s);
  if(f.includes("all_zero_dead")||f.includes("temp_rh_dead")||f.includes("no_timestamp")||s.age==null||s.age>24*30) return "dead";
  if(s.age<=3) return "fresh";
  if(s.age<=24) return "delayed";
  return "stale";
}
const STATUS_COLOR = {fresh:"--s-fresh",delayed:"--s-delayed",stale:"--s-stale",dead:"--s-dead"};

// ---- feed freshness banner
const times = STATIONS.map(s=>s.eat).filter(Boolean).map(t=>new Date(t).getTime())
  .filter(t=>NOW-t < 1000*3600*24*40);   // ignore stations dead for weeks
const latestFeed = times.length ? new Date(Math.max(...times)) : null;
const fmtEAT = d => d.toLocaleString("en-GB",{timeZone:"Africa/Kampala",day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"});
document.getElementById("scraped").textContent = fmtEAT(new Date(SCRAPED)) + " EAT";
if(latestFeed){
  const hrs = (NOW-latestFeed.getTime())/3.6e6;
  document.getElementById("feednote").innerHTML =
    `Newest reading in the feed: <b>${fmtEAT(latestFeed)} EAT</b>` +
    (hrs>6 ? ` \u2014 ${Math.round(hrs)} h before this snapshot. The network was not reporting live when it was taken; every marker is a last-known value.` : " (as of this snapshot).");
}

// ---- map
const map = L.map("map",{zoomControl:false,attributionControl:false,
  minZoom:5,maxZoom:11,zoomSnap:0,zoomDelta:.5,wheelPxPerZoomLevel:120})
  .setView([1.3,32.3],6);
L.control.zoom({position:"topright"}).addTo(map);

// stacking: land < lakes < rivers < roads < labels < station markers (overlay pane, z 400)
[["land",200],["lakes",210],["rivers",220],["roads",230],["labels",350]].forEach(([n,z])=>{ map.createPane(n).style.zIndex=z; });
map.getPane("labels").style.pointerEvents="none";
const land = L.geoJSON(UGANDA,{pane:"land",interactive:false,style:()=>({
  color:cssv("--land-line"),weight:1,fillColor:cssv("--land"),fillOpacity:1
})}).addTo(map);

// ---- base layers (toggleable). Roads use canvas so ~10k segments stay responsive.
const roadCanvas = L.canvas({pane:"roads",padding:.3});
const LAYER_DEFS = {
  lakes:       {on:true,  pane:"lakes",  style:()=>({color:cssv("--lake-line"),weight:.8,fillColor:cssv("--lake-fill"),fillOpacity:1})},
  rivers:      {on:true,  pane:"rivers", style:()=>({color:cssv("--river"),weight:1.4,opacity:.95,fill:false,lineCap:"round",lineJoin:"round"})},
  roads_major: {on:true,  pane:"roads",  renderer:roadCanvas, style:()=>({color:cssv("--road-major"),weight:1.5,opacity:.95,fill:false})},
  roads_minor: {on:false, pane:"roads",  renderer:roadCanvas, style:()=>({color:cssv("--road-minor"),weight:.8,opacity:.9,fill:false})},
};
function layerGroup(key){
  const d=LAYER_DEFS[key];
  if(!d.group){
    d.group=L.geoJSON(LAYERS[key],{pane:d.pane,renderer:d.renderer,interactive:false,style:d.style});
  }
  return d.group;
}
function setLayer(key,on){
  const d=LAYER_DEFS[key]; d.on=on;
  const g=layerGroup(key);
  on ? g.addTo(map) : map.removeLayer(g);
  syncLabels();
}
function restyleLayers(){
  Object.values(LAYER_DEFS).forEach(d=>{ if(d.group) d.group.setStyle(d.style()); });
}

// ---- names (lakes and main rivers) - shown by zoom, tied to their layer toggle
const labelMarkers = LABELS.map(l=>({l, m:L.marker([l.lat,l.lon],{pane:"labels",interactive:false,keyboard:false,
  icon:L.divIcon({className:"lbl lbl-"+l.kind,html:`<span>${esc(l.name)}</span>`,iconSize:[0,0]})})}));
function syncLabels(){
  const z=map.getZoom();
  labelMarkers.forEach(({l,m})=>{
    const want = LAYER_DEFS[l.kind==="lake"?"lakes":"rivers"].on && z>=l.z;
    if(want && !map.hasLayer(m)) m.addTo(map);
    else if(!want && map.hasLayer(m)) map.removeLayer(m);
  });
}
map.on("zoomend",syncLabels);

// layers card (top-left of the map)
const LayersCtl = L.Control.extend({
  options:{position:"topleft"},
  onAdd(){
    const d=L.DomUtil.create("div","layers-card");
    L.DomEvent.disableClickPropagation(d); L.DomEvent.disableScrollPropagation(d);
    const items=[["lakes","Lakes","lsw-lake"],["rivers","Rivers","lsw-river"],["roads_major","Major roads","lsw-road"],["roads_minor","Secondary roads","lsw-road2"]];
    d.innerHTML=`<div class="control-label">Map layers</div>`+items.map(([k,t,c])=>
      `<button class="lay" type="button" data-layer="${k}" aria-pressed="${LAYER_DEFS[k].on}"><i class="lsw ${c}"></i>${t}</button>`).join("");
    d.querySelectorAll(".lay").forEach(b=>b.addEventListener("click",()=>{
      const on=b.getAttribute("aria-pressed")!=="true";
      b.setAttribute("aria-pressed",on); setLayer(b.dataset.layer,on);
    }));
    return d;
  }
});
new LayersCtl().addTo(map);
Object.keys(LAYER_DEFS).forEach(k=>{ if(LAYER_DEFS[k].on) layerGroup(k).addTo(map); });
let userMoved = false;
["wheel","pointerdown","touchstart"].forEach(ev =>
  map.getContainer().addEventListener(ev, () => { userMoved = true; }, {passive:true}));
const fitUganda = () => { if(!userMoved) map.fitBounds(land.getBounds(),{padding:[26,26],animate:false}); };
fitUganda();
// keep the frame filled while the grid/flex container settles and on resize
const ro = new ResizeObserver(()=>{ map.invalidateSize(); fitUganda(); });
ro.observe(document.getElementById("map"));

let mode = "t";
const markers = new Map();
let selected = null;

function styleFor(s){
  const stroke = cssv("--marker-stroke");
  if(mode==="status"){
    const st=statusOf(s);
    if(st==="dead") return {radius:5,weight:1.7,color:cssv("--s-dead"),opacity:.9,fill:false};
    return {radius:6.5,weight:1.2,color:stroke,fillColor:cssv(STATUS_COLOR[st]),fillOpacity:.92};
  }
  const v=metricVal(s,mode);
  if(v==null) return {radius:5,weight:1.6,color:cssv("--muted"),opacity:.85,fill:false,dashArray:"2 3"};
  const st={radius:6.5,weight:1.2,color:stroke,fillColor:ramp(RAMPS[mode].stops,v),fillOpacity:.92};
  if(s.age!=null && s.age>24*7) st.fillOpacity=.4;
  return st;
}

function popupHTML(s){
  const f=flags(s);
  const row=(dt,v,unit,bad)=> `<dt>${dt}</dt><dd class="mono${bad?" bad":""}">${v==null?"\u2014":v}${v!=null&&unit?`<span class="u">${unit}</span>`:""}</dd>`;
  const NS = s.lat>=0?"N":"S", EW = s.lon>=0?"E":"W";
  let when="\u2014";
  if(s.eat){
    const d=new Date(s.eat);
    const age = s.age<48 ? `${Math.round(s.age)} h ago`
      : s.age<24*90 ? `${Math.round(s.age/24)} d ago`
      : d.toLocaleDateString("en-GB",{month:"short",year:"numeric"});
    when = `${fmtEAT(d)} EAT \u00b7 ${age}`;
  }
  const DLAB={t_air_c:"air temp",rain_mm:"rain",solar_wm2:"solar",rh_pct:"humidity",wind_kmh:"wind",wind_max_kmh:"gust"};
  const dl = (s.dl||"").split(";").filter(Boolean).map(m=>`<span class="warn">${esc(DLAB[m]||m)} delayed</span>`).join("");
  const chips = dl + f.map(x=>{
    const warn = x.includes("suspect")||x==="all_zero_dead"||x==="no_timestamp";
    return `<span class="${warn?"warn":""}">${esc(FLAG_LABEL[x]||x)}</span>`;
  }).join("");
  return `<div class="pop">
    <h4>${esc(s.name)}</h4>
    <div class="pmeta mono">node ${s.id} \u00b7 code ${esc(s.code)}</div>
    <div class="pmeta mono">${Math.abs(s.lat).toFixed(3)}\u00b0 ${NS}, ${Math.abs(s.lon).toFixed(3)}\u00b0 ${EW}</div>
    <dl class="pgrid">
      ${row("Air temp", s.t==null?null:s.t.toFixed(1), "\u00b0C", f.includes("temp_sensor_suspect"))}
      ${row("Humidity", s.rh==null?null:(s.rh>=999?"\u2014":s.rh.toFixed(0)), "%", f.includes("rh_sensor_suspect")||s.rh>=999)}
      ${row("Rain", s.rain==null?null:s.rain.toFixed(1), "mm")}
      ${row("Solar", s.solar==null?null:s.solar.toFixed(0), "W/m\u00b2")}
      ${row("Wind", s.wind==null?null:s.wind.toFixed(1), "km/h")}
      ${row("Gust", s.wmax==null?null:s.wmax.toFixed(1), "km/h")}
    </dl>
    <div class="ptime mono">${when}</div>
    ${chips?`<div class="pflags">${chips}</div>`:""}
  </div>`;
}

STATIONS.forEach(s=>{
  const m=L.circleMarker([s.lat,s.lon],styleFor(s)).bindPopup(()=>popupHTML(s),{maxWidth:280});
  m.on("click",()=>select(s.id,false));
  m.addTo(map);
  markers.set(s.id,m);
});

function select(id,fly){
  selected=id;
  const s=STATIONS.find(x=>x.id===id), m=markers.get(id);
  document.querySelectorAll(".st").forEach(el=>el.setAttribute("aria-current", el.dataset.id==id ? "true":"false"));
  const cur=document.querySelector(`.st[data-id="${id}"]`);
  if(cur) cur.scrollIntoView({block:"nearest"});
  if(fly){
    const to=[s.lat,s.lon];
    reduceMotion ? map.setView(to,9) : map.flyTo(to,9,{duration:.7});
    map.once("moveend",()=>m.openPopup());
  }
  restyle();
}
function restyle(){
  markers.forEach((m,id)=>{
    const s=STATIONS.find(x=>x.id===id), st=styleFor(s);
    if(id===selected){st.weight=2.4;st.color=cssv("--accent");st.radius=(st.radius||6)+1.5;st.opacity=1;}
    m.setStyle(st);
  });
}

// ---- legend
function renderLegend(){
  const el=document.getElementById("legend");
  if(mode==="status"){
    const counts={fresh:0,delayed:0,stale:0,dead:0};
    STATIONS.forEach(s=>counts[statusOf(s)]++);
    const lab={fresh:"Live (\u22643 h)",delayed:"Delayed (3\u201324 h)",stale:"Stale (>24 h)",dead:"Offline"};
    el.innerHTML=`<div class="control-label">Reporting status</div><div class="legend-swatches">`+
      ["fresh","delayed","stale","dead"].map(k=>
        `<div class="row"><span class="sw${k==="dead"?" hollow":""}" style="${k==="dead"?"":"background:"+cssv(STATUS_COLOR[k])}"></span>${lab[k]}<span style="margin-left:auto;color:var(--muted)" class="mono">${counts[k]}</span></div>`
      ).join("")+`</div>`;
    return;
  }
  const R=RAMPS[mode], stops=R.stops;
  const min=stops[0][0], max=stops[stops.length-1][0];
  const grad=stops.map(([x,c])=>`${c} ${Math.round((x-min)/(max-min)*100)}%`).join(",");
  const mid=stops.length>2?stops[Math.floor(stops.length/2)][0]:(min+max)/2;
  el.innerHTML=`<div class="control-label">${R.label} <span style="color:var(--muted);text-transform:none;letter-spacing:0">(${R.unit})</span></div>
    <div class="legend-bar" style="background:linear-gradient(90deg,${grad})"></div>
    <div class="legend-scale mono"><span>${min}</span><span>${mid}</span><span>${max}</span></div>
    <div class="legend-foot"><span class="sw"></span> no reading / sensor suspect &nbsp;\u00b7&nbsp; faded = >24 h old</div>`;
}

// ---- station list
const listEl=document.getElementById("list");
function valueTag(s){
  if(mode==="status") return `<span class="vv">${statusOf(s)}</span>`;
  const v=metricVal(s,mode), R=RAMPS[mode];
  return `<span class="vv mono">${v==null?"\u2014":v.toFixed(R.dec)}<span class="u">${v==null?"":R.unit}</span></span>`;
}
function markerDot(s){
  if(mode==="status"){
    const st=statusOf(s);
    return st==="dead" ? `<span class="marker hollow"></span>`
      : `<span class="marker" style="background:${cssv(STATUS_COLOR[st])}"></span>`;
  }
  const v=metricVal(s,mode);
  return v==null ? `<span class="marker hollow"></span>`
    : `<span class="marker" style="background:${ramp(RAMPS[mode].stops,v)}"></span>`;
}
function renderList(filter=""){
  const q=filter.trim().toLowerCase();
  const rows=STATIONS
    .filter(s=>!q || s.name.toLowerCase().includes(q))
    .sort((a,b)=>a.name.localeCompare(b.name));
  listEl.innerHTML = rows.map(s=>
    `<button class="st" data-id="${s.id}" aria-current="${s.id===selected}">
      ${markerDot(s)}<span class="nm">${esc(s.name)}</span>${valueTag(s)}
    </button>`).join("") || `<div style="padding:16px;color:var(--muted);font-size:12px">No stations match \u201c${esc(filter)}\u201d</div>`;
  listEl.querySelectorAll(".st").forEach(el=>
    el.addEventListener("click",()=>select(+el.dataset.id,true)));
}
document.getElementById("q").addEventListener("input",e=>renderList(e.target.value));

// ---- stat chips
function renderChips(){
  const c={fresh:0,delayed:0,stale:0,dead:0};
  STATIONS.forEach(s=>c[statusOf(s)]++);
  const map_={live:["fresh","--s-fresh"],delayed:["delayed","--s-delayed"],stale:["stale","--s-stale"],offline:["dead","--s-dead"]};
  document.querySelectorAll(".chip").forEach(ch=>{
    const [k,cvar]=map_[ch.dataset.k];
    ch.querySelector(".n").textContent=c[k];
    ch.querySelector(".dot").style.background=cssv(cvar);
  });
}

// ---- mode switching
function setMode(m){
  mode=m;
  document.querySelectorAll(".seg button").forEach(b=>b.setAttribute("aria-pressed", b.dataset.mode===m));
  document.querySelectorAll(".chip").forEach(ch=>ch.classList.toggle("on", m==="status"));
  restyle(); renderLegend(); renderList(document.getElementById("q").value);
}
document.querySelectorAll(".seg button").forEach(b=>
  b.addEventListener("click",()=>setMode(b.dataset.mode)));
document.querySelectorAll(".chip").forEach(ch=>
  ch.addEventListener("click",()=>setMode("status")));

// theme change -> recompute colours
matchMedia("(prefers-color-scheme:dark)").addEventListener("change",()=>{
  land.setStyle({color:cssv("--land-line"),fillColor:cssv("--land")});
  restyleLayers(); restyle(); renderLegend(); renderChips(); renderList(document.getElementById("q").value);
});
addEventListener("resize",()=>map.invalidateSize());

// ---- CSV download: last 7 days before the snapshot, one row per reading
const CSV_COLS=["node_id","station_code","station_name","latitude","longitude","time_eat","time_utc",
  "t_air_c","rain_mm","solar_wm2","rh_pct","wind_kmh","wind_max_kmh","delayed_metrics","quality_flags"];
// text from the portal is untrusted: neutralise spreadsheet formulas (=,+,-,@) before quoting
const csvCell = v => {
  if(v==null||v==="") return "";
  let t=String(v);
  if(typeof v==="string" && /^[=+\-@\t\r]/.test(t)) t="'"+t;
  return /[",\r\n]/.test(t) ? '"'+t.replace(/"/g,'""')+'"' : t;
};
function buildCSV(){
  const out=[CSV_COLS.join(",")];
  for(const r of CSV_ROWS){
    const [nid,ep,t,rain,solar,rh,w,wm,dl,fl]=r, st=CSV_ST[nid]||["","",null,null];
    const utc=new Date(ep).toISOString().slice(0,19)+"Z";
    const eat=new Date(ep+3*3600*1000).toISOString().slice(0,19)+"+03:00";
    out.push([nid,st[0],st[1],st[2],st[3],eat,utc,t,rain,solar,rh,w,wm,dl,fl].map(csvCell).join(","));
  }
  return "\ufeff"+out.join("\r\n")+"\r\n";     // BOM so Excel reads UTF-8
}
const dlBtn=document.getElementById("dl"), dlNote=document.getElementById("dl-note"), dlWrap=document.getElementById("dl-wrap");
const setNote = t => { dlNote.textContent=t; };
function describeWindow(){
  if(!CSV_ROWS.length) return "";
  const ids=new Set(CSV_ROWS.map(r=>r[0]));
  const f=d=>d.toLocaleString("en-GB",{timeZone:"Africa/Kampala",day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"});
  return `${CSV_ROWS.length.toLocaleString("en-GB")} readings \u00b7 ${ids.size} stations \u00b7 ${f(new Date(NOW-7*864e5))} \u2013 ${f(new Date(NOW))} EAT`;
}
async function downloadCSV(){
  const name=`uganda_weather_7d_${SCRAPED.replace(/[-:]/g,"").slice(0,13)}Z.csv`;
  const csv=buildCSV();
  try{
    const dls = window.claude ? await window.claude.use("downloads") : null;
    if(dls){
      setNote("Waiting for you to confirm the save\u2026");
      await dls.save({filename:name,data:csv});
      setNote("Saved \u00b7 "+name);
    }else if(!window.claude){                      // plain file / static host: normal browser download
      const a=document.createElement("a");
      a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv;charset=utf-8"})); a.download=name;
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(a.href),5000);
      setNote("Downloaded \u00b7 "+name);
    }
  }catch(e){
    setNote(e && e.code==="declined" ? "Cancelled \u2014 nothing was saved." : "Couldn\u2019t save the file ("+((e&&e.code)||"error")+").");
  }
}
if(!CSV_ROWS.length){ dlWrap.hidden=true; }
else{
  setNote(describeWindow());
  dlBtn.addEventListener("click",downloadCSV);
  // inside an artifact the viewer must grant the download; hide the button if this view cannot save
  if(window.claude){
    window.claude.use("downloads").then(d=>{ if(!d) dlWrap.hidden=true; }).catch(()=>{ dlWrap.hidden=true; });
  }
}

renderChips(); renderLegend(); renderList(); syncLabels();
setTimeout(()=>{ map.invalidateSize(); fitUganda(); syncLabels(); },60);
"""

JS_FINAL = (JS.replace("__STATIONS__", script_json(stations))
            .replace("__UGANDA__", script_json(json.loads(uganda)))
            .replace("__LAYERS__", script_json(layers))
            .replace("__LABELS__", script_json(labels))
            .replace("__CSVROWS__", script_json(csv_rows))
            .replace("__CSVST__", script_json(csv_stations))
            .replace("__SCRAPED__", scraped))

html = f"""<title>Uganda Weather Stations</title>
<meta name="description" content="Live-scraped map of Uganda's 103-station Adcon weather telemetry network.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
{leaflet_css}
{CSS}
</style>

<div id="app">
  <aside id="rail">
    <div class="rail-head">
      <p class="eyebrow">Adcon LiveData &middot; addVANTAGE Pro 6</p>
      <h1>Uganda Weather Stations</h1>
      <p class="sub">103 automatic stations &middot; scraped <span class="mono" id="scraped">&mdash;</span></p>
      <p class="feednote" id="feednote">Feed status unavailable.</p>
    </div>

    <div class="chips" role="group" aria-label="Reporting status summary">
      <button class="chip" data-k="live"><span class="n mono">0</span><span class="k"><span class="dot"></span>Live</span></button>
      <button class="chip" data-k="delayed"><span class="n mono">0</span><span class="k"><span class="dot"></span>Delayed</span></button>
      <button class="chip" data-k="stale"><span class="n mono">0</span><span class="k"><span class="dot"></span>Stale</span></button>
      <button class="chip" data-k="offline"><span class="n mono">0</span><span class="k"><span class="dot"></span>Offline</span></button>
    </div>

    <div class="controls">
      <div class="control-label">Colour markers by</div>
      <div class="seg">
        <button data-mode="t" aria-pressed="true">Temperature</button>
        <button data-mode="rh" aria-pressed="false">Humidity</button>
        <button data-mode="wind" aria-pressed="false">Wind</button>
        <button data-mode="status" aria-pressed="false">Status</button>
      </div>
    </div>
    <div class="legend" id="legend"></div>

    <div class="list-wrap">
      <div class="search"><input id="q" type="search" placeholder="Filter stations&hellip;" autocomplete="off"></div>
      <div id="list"></div>
    </div>

    <div class="dl" id="dl-wrap">
      <button class="dl-btn" id="dl" type="button" title="One row per reading: all stations, last 7 days before this snapshot. Coordinates are the portal's fuzzed positions.">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2v8M4.5 7 8 10.5 11.5 7M2.5 13.5h11"/></svg>
        Download last 7 days (CSV)
      </button>
      <div class="dl-note mono" id="dl-note" aria-live="polite"></div>
    </div>

    <div class="rail-foot">
      Source: public Adcon LiveData portal at 196.0.33.173:8080 (Uganda national AWS network).
      Coordinates are deliberately fuzzed by the portal for anonymous viewers &mdash; regional context only, not survey grade.
    </div>
  </aside>
  <div id="map"></div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<script>
{JS_FINAL}
</script>
"""

out = HERE / "uganda_weather_map.html"
out.write_text(html, encoding="utf-8")
print("wrote", out, len(html), "bytes")
