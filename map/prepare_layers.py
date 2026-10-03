#!/usr/bin/env python3
"""
One-off: prepare the base-map layers used by build_map.py -> map/layers/*.geojson

  lakes.geojson            Natural Earth 10m lakes (public domain), clipped to the view box
  rivers.geojson           Natural Earth 10m river centrelines, with the parts inside lakes removed
  roads_major.geojson      AllRoads.shp, DESCRIP == "Major road"      (one MultiLineString)
  roads_secondary.geojson  AllRoads.shp, DESCRIP == "Secondary road"  (one MultiLineString)
  labels.json              name + anchor point + minimum zoom for lake / river labels

Sources
  * Natural Earth is downloaded into data/_src/ (gitignored) when missing.
  * Roads come from a local AllRoads.shp inside Roads.zip (EPSG:21096, reprojected to WGS84).
    Provenance of that file is not recorded; keep the repo/artifact private unless confirmed.

  python map/prepare_layers.py [--roads-zip D:/DATAs/Roads.zip]
"""
import argparse
import json
import tempfile
import warnings
import zipfile
from pathlib import Path

import geopandas as gpd
import requests
from shapely.geometry import MultiLineString, box, mapping
from shapely.ops import unary_union

warnings.filterwarnings("ignore")
ROOT = Path(__file__).parent.parent
OUT = Path(__file__).parent / "layers"
SRC = ROOT / "data" / "_src"
NE = "https://naciscdn.org/naturalearth/10m/physical/{}.zip"
VIEW = box(28.6, -3.2, 36.0, 5.0)     # generous box around Uganda; nothing outside is drawn


def fetch_ne(name: str) -> Path:
    SRC.mkdir(parents=True, exist_ok=True)
    p = SRC / f"{name}.zip"
    if not p.exists():
        r = requests.get(NE.format(name), timeout=120)
        r.raise_for_status()
        p.write_bytes(r.content)
    return p


def rounded(geom, dp):
    def rc(c):
        return [round(x, dp) for x in c]
    g = mapping(geom)
    t = g["type"]
    if t == "Polygon":
        g["coordinates"] = [[rc(p) for p in ring] for ring in g["coordinates"]]
    elif t == "MultiPolygon":
        g["coordinates"] = [[[rc(p) for p in ring] for ring in poly] for poly in g["coordinates"]]
    elif t == "LineString":
        g["coordinates"] = [rc(p) for p in g["coordinates"]]
    elif t == "MultiLineString":
        g["coordinates"] = [[rc(p) for p in line] for line in g["coordinates"]]
    return g


def write_fc(name, rows, dp):
    """rows: [(geometry, properties)] -> compact GeoJSON FeatureCollection."""
    feats = [{"type": "Feature", "properties": props, "geometry": rounded(g, dp)} for g, props in rows if not g.is_empty]
    s = json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":"))
    (OUT / name).write_text(s, encoding="utf-8")
    print(f"{name:26s} {len(feats):5d} features  {len(s)/1e3:7.0f} KB")


def lines_only(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom] if len(geom.coords) >= 2 else []
    if geom.geom_type in ("MultiLineString", "GeometryCollection"):
        return [g for part in geom.geoms for g in lines_only(part)]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roads-zip", default=r"D:/DATAs/Roads.zip")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    ug = gpd.read_file(r"D:/DATAs/LGWK/Uganda.shp").to_crs(4326).geometry.iloc[0]
    near = ug.buffer(0.6)

    # ---- lakes
    lakes = gpd.read_file(f"zip://{fetch_ne('ne_10m_lakes')}")
    lakes = lakes[lakes.intersects(near)].copy()
    lakes["geometry"] = lakes.geometry.intersection(VIEW).simplify(0.004, preserve_topology=True)
    write_fc("lakes.geojson", [(r.geometry, {"name": r["name"]}) for _, r in lakes.iterrows()], 3)
    lake_union = unary_union(list(lakes.geometry))

    # ---- rivers (centrelines run straight through lakes in Natural Earth: cut those parts out)
    riv = gpd.read_file(f"zip://{fetch_ne('ne_10m_rivers_lake_centerlines')}")
    riv = riv[riv.intersects(near)].copy()
    rows, riv_lab = [], []
    for _, r in riv.iterrows():
        g = r.geometry.intersection(VIEW).difference(lake_union.buffer(0.004))
        parts = lines_only(g)
        if not parts:
            continue
        g = MultiLineString(parts).simplify(0.003, preserve_topology=False)
        parts = lines_only(g)
        if not parts:
            continue
        g = MultiLineString(parts)
        rows.append((g, {"name": r["name"]}))
        riv_lab.append((r["name"], g))
    write_fc("rivers.geojson", rows, 3)

    # ---- roads
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(args.roads_zip) as z:
            for n in z.namelist():
                if n.startswith("Roads/AllRoads."):
                    z.extract(n, td)
        roads = gpd.read_file(Path(td) / "Roads" / "AllRoads.shp").to_crs(4326)
    for cls, fname in (("Major road", "roads_major.geojson"), ("Secondary road", "roads_secondary.geojson")):
        sub = roads[roads.DESCRIP == cls]
        parts = [p for g in sub.geometry.simplify(0.0015, preserve_topology=False) for p in lines_only(g)]
        write_fc(fname, [(MultiLineString(parts), {"class": cls})], 3)
    print(f"road segments: major={int((roads.DESCRIP=='Major road').sum())} secondary={int((roads.DESCRIP=='Secondary road').sum())}")

    # ---- labels: only features that touch Uganda; anchor = a point guaranteed on the feature
    labels = []
    lk_zoom = {"Lake Victoria": 5.5, "Lake Kyoga": 6.5, "Lake Albert": 6.5, "Lake Edward": 7, "Lake Kwania": 8.5, "Lake Bunyonyi": 9}
    for _, r in lakes.iterrows():
        if r["name"] in lk_zoom and r.geometry.intersects(ug):
            p = max(getattr(r.geometry, "geoms", [r.geometry]), key=lambda g: g.area).representative_point()
            labels.append({"kind": "lake", "name": r["name"], "lat": round(p.y, 4), "lon": round(p.x, 4), "z": lk_zoom[r["name"]]})
    rv_zoom = {"Victoria Nile": 7.5, "Albert Nile": 7.5, "Kagera": 7.5, "Semliki": 8}
    for name, g in riv_lab:
        if name in rv_zoom and g.intersects(ug):
            line = max(g.geoms, key=lambda x: x.length)
            p = line.interpolate(0.5, normalized=True)
            labels.append({"kind": "river", "name": name, "lat": round(p.y, 4), "lon": round(p.x, 4), "z": rv_zoom[name]})
    (OUT / "labels.json").write_text(json.dumps(labels, separators=(",", ":")), encoding="utf-8")
    print("labels:", [(l["name"], l["z"]) for l in labels])


if __name__ == "__main__":
    main()
