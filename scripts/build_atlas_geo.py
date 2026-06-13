#!/usr/bin/env python3
"""One-time authoring step for the Atlas feature's geometry.

Reads public-domain source geometry (Natural Earth admin-0 countries + admin-1
states/provinces, both WGS84 lon/lat, public domain; plus the ONS UK European
Electoral Regions for a clean Scotland / England split, OGL) and bakes a single,
projected, heavily-simplified `corpus-app/atlas/geo.json` that `build.py` reads
at build time. Doing the heavy geo work here keeps the runtime build offline and
stdlib-only — geo.json is just projected SVG path strings.

Run from anywhere:  python3 corpus-app/scripts/build_atlas_geo.py
Sources are fetched into /tmp by the companion fetch step (see the session log);
if missing, the script re-downloads them with urllib.
"""
import json, math, os, ssl, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "atlas" / "geo.json"
TMP = Path("/tmp")

SOURCES = {
    "ne50":     ("ne50.json",     "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"),
    "ne110":    ("ne110.json",    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson"),
    "ne50adm1": ("ne50adm1.json", "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_1_states_provinces.geojson"),
    "uk":       ("uk_eer.json",   "https://raw.githubusercontent.com/martinjc/UK-GeoJSON/master/json/electoral/gb/topo_eer.json"),
}

def load(key):
    fn, url = SOURCES[key]
    p = TMP / fn
    if not p.exists():
        print(f"  fetching {url}")
        try:
            urllib.request.urlretrieve(url, p)
        except Exception:
            # some macOS Python builds lack CA certs; this is one-time fetching of
            # public-domain geometry, so fall back to an unverified context.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(url, context=ctx) as r:
                p.write_bytes(r.read())
    return json.loads(p.read_text())

# ---- projection: Web Mercator into a fixed lon/lat window over the corpora ----
WIN = dict(W=-135.0, E=45.0, N=62.0, S=28.0)
VIEW_W = 3600.0
def _mx(lon): return math.radians(lon)
def _my(lat):
    lat = max(min(lat, 84.0), -84.0)
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
_X0, _X1 = _mx(WIN["W"]), _mx(WIN["E"])
_Y0, _Y1 = _my(WIN["N"]), _my(WIN["S"])      # Y0 (north) maps to top
_SX = VIEW_W / (_X1 - _X0)
VIEW_H = (_Y0 - _Y1) * _SX
def proj(lon, lat):
    return ((_mx(lon) - _X0) * _SX, (_Y0 - _my(lat)) * _SX)

# ---- Douglas-Peucker simplification (in projected px) ----
def _dp(pts, tol):
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    t2 = tol * tol
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]; bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        d2 = dx * dx + dy * dy
        idx, far = -1, t2
        for i in range(a + 1, b):
            px, py = pts[i]
            if d2 == 0:
                dd = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / d2
                t = max(0.0, min(1.0, t))
                cx, cy = ax + t * dx, ay + t * dy
                dd = (px - cx) ** 2 + (py - cy) ** 2
            if dd > far:
                idx, far = i, dd
        if idx != -1:
            keep[idx] = True
            stack.append((a, idx)); stack.append((idx, b))
    return [p for i, p in enumerate(pts) if keep[i]]

def _rings_of(geom):
    """Yield lists of [lon,lat] rings from a GeoJSON Polygon/MultiPolygon."""
    t, c = geom["type"], geom["coordinates"]
    if t == "Polygon":
        for r in c:
            yield r
    elif t == "MultiPolygon":
        for poly in c:
            for r in poly:
                yield r

def path_from_geoms(geoms, tol=0.8, min_ring_px=3.0):
    """Project + simplify a list of GeoJSON geometries into one SVG path `d`.
    Returns (d, bbox, visual_center). Drops slivers smaller than min_ring_px and
    rings that fall entirely outside the view window (e.g. overseas territories)."""
    parts, xs, ys = [], [], []
    big_area, big_c = -1.0, None
    MARGIN = 600.0
    for g in geoms:
        for ring in _rings_of(g):
            pr = [proj(lon, lat) for lon, lat in ring]
            pr = _dp(pr, tol)
            if len(pr) < 3:
                continue
            rxs = [p[0] for p in pr]; rys = [p[1] for p in pr]
            # cull rings wholly off-canvas (drops French Guiana, Réunion, etc.)
            if (max(rxs) < -MARGIN or min(rxs) > VIEW_W + MARGIN
                    or max(rys) < -MARGIN or min(rys) > VIEW_H + MARGIN):
                continue
            if (max(rxs) - min(rxs)) < min_ring_px and (max(rys) - min(rys)) < min_ring_px:
                continue
            # shoelace area + centroid (for picking a label anchor in the biggest ring)
            a = cx = cy = 0.0
            for i in range(len(pr)):
                x0, y0 = pr[i]; x1, y1 = pr[(i + 1) % len(pr)]
                cr = x0 * y1 - x1 * y0
                a += cr; cx += (x0 + x1) * cr; cy += (y0 + y1) * cr
            a *= 0.5
            if abs(a) > big_area and a != 0:
                big_area = abs(a); big_c = (cx / (6 * a), cy / (6 * a))
            d = "M" + " ".join(f"{x:.1f},{y:.1f}" for x, y in pr) + "Z"
            parts.append(d)
            xs += rxs; ys += rys
    if not parts:
        return None
    bbox = [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]
    ctr = big_c if big_c else ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
    return "".join(parts), bbox, [round(ctr[0], 1), round(ctr[1], 1)]

# ---- topojson decode (UK electoral regions) ----
def topo_decode(topo):
    tr = topo.get("transform"); arcs = topo["arcs"]
    def dearc(a):
        out = []; x = y = 0
        for p in a:
            if tr:
                x += p[0]; y += p[1]
                out.append([x * tr["scale"][0] + tr["translate"][0], y * tr["scale"][1] + tr["translate"][1]])
            else:
                out.append([p[0], p[1]])
        return out
    darcs = [dearc(a) for a in arcs]
    def arc(i): return darcs[i] if i >= 0 else darcs[~i][::-1]
    def ring(idxs):
        r = []
        for i in idxs:
            seg = arc(i); r += seg[1:] if r else seg
        return r
    feats = []
    for o in topo["objects"].values():
        for g in o["geometries"]:
            gt = g["type"]; cs = g.get("arcs")
            if gt == "Polygon":
                coords = [ring(r) for r in cs]
            elif gt == "MultiPolygon":
                coords = [[ring(r) for r in poly] for poly in cs]
            else:
                continue
            feats.append({"properties": g.get("properties", {}),
                          "geometry": {"type": gt, "coordinates": coords}})
    return feats

def main():
    ne50 = load("ne50"); ne110 = load("ne110"); adm1 = load("ne50adm1"); uk = load("uk")

    def adm0(*names):
        names = set(names)
        return [f["geometry"] for f in ne50["features"]
                if f["properties"].get("NAME") in names or f["properties"].get("ADMIN") in names]
    def states(*names):
        names = set(names)
        return [f["geometry"] for f in adm1["features"]
                if f["properties"].get("admin") == "United States of America" and f["properties"].get("name") in names]
    def province(country, name):
        return [f["geometry"] for f in adm1["features"]
                if f["properties"].get("admin") == country and f["properties"].get("name") == name]

    # continental US = all admin-1 USA units except the far-flung ones
    drop = {"Alaska", "Hawaii"}
    us_cont = [f["geometry"] for f in adm1["features"]
               if f["properties"].get("admin") == "United States of America" and f["properties"].get("name") not in drop]

    uk_feats = topo_decode(uk)
    def uk_region(pred):
        return [f["geometry"] for f in uk_feats if pred(f["properties"].get("EER13NM", ""), f["properties"].get("EER13CD", ""))]
    scotland = uk_region(lambda nm, cd: nm == "Scotland")
    england  = uk_region(lambda nm, cd: cd.startswith("E"))   # 9 English electoral regions → England

    # region catalog: id -> (geometries, display name, simplify tol)
    NEW_ENGLAND = ["Maine", "New Hampshire", "Vermont", "Massachusetts", "Rhode Island", "Connecticut"]
    catalog = {
        "US":            (us_cont,                                  "United States", 1.1),
        "US-NY":         (states("New York"),                       "New York", 0.6),
        "US-MA":         (states("Massachusetts"),                  "Massachusetts", 0.5),
        "US-NM":         (states("New Mexico"),                     "New Mexico", 0.7),
        "US-CA":         (states("California"),                     "California", 0.7),
        "US-NEWENGLAND": (states(*NEW_ENGLAND),                     "New England", 0.7),
        "CA-ON":         (province("Canada", "Ontario"),            "Ontario", 1.0),
        "IE":            (adm0("Ireland"),                          "Ireland", 0.7),
        "GB-SCT":        (scotland,                                 "Scotland", 0.7),
        "GB-ENG":        (england,                                  "England", 0.7),
        "CH":            (adm0("Switzerland"),                      "Switzerland", 0.45),
        "DE":            (adm0("Germany"),                          "Germany", 0.7),
        "IL":            (adm0("Israel", "Palestine"),              "The Holy Land", 0.4),
        "LEVANT":        (adm0("Israel", "Palestine", "Jordan", "Lebanon"), "The Levant", 0.6),
    }

    regions = {}
    for rid, (geoms, name, tol) in catalog.items():
        if not geoms:
            print(f"  ! region {rid}: no geometry", file=sys.stderr); continue
        res = path_from_geoms(geoms, tol=tol)
        if not res:
            print(f"  ! region {rid}: empty after simplify", file=sys.stderr); continue
        d, bbox, ctr = res
        regions[rid] = {"d": d, "bbox": bbox, "cx": ctr[0], "cy": ctr[1], "name": name}
        print(f"  ✓ {rid:14s} {name:16s} bbox={bbox} chars={len(d)}")

    # faint backdrop: every NE-110m country that intersects the window (+ a margin)
    def in_window(geom):
        for ring in _rings_of(geom):
            for lon, lat in ring:
                if -160 <= lon <= 70 and 12 <= lat <= 78:
                    return True
        return False
    backdrop = []
    for f in ne110["features"]:
        g = f["geometry"]
        if not in_window(g):
            continue
        res = path_from_geoms([g], tol=1.6, min_ring_px=4.0)
        if res:
            backdrop.append(res[0])

    # graticule (faint orientation lines), clipped to the viewBox by the renderer
    grat = []
    for lon in range(-180, 61, 20):
        pts = [proj(lon, lat) for lat in range(20, 76, 2)]
        grat.append("M" + " ".join(f"{x:.0f},{y:.0f}" for x, y in pts))
    for lat in range(20, 76, 10):
        pts = [proj(lon, lat) for lon in range(-160, 61, 2)]
        grat.append("M" + " ".join(f"{x:.0f},{y:.0f}" for x, y in pts))

    out = {
        "viewBox": f"0 0 {VIEW_W:.0f} {VIEW_H:.0f}",
        "w": round(VIEW_W, 1), "h": round(VIEW_H, 1),
        "window": WIN,
        "graticule": grat,
        "backdrop": backdrop,
        "regions": regions,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    kb = OUT.stat().st_size / 1024
    print(f"\nWrote {OUT}  ({len(regions)} regions, {len(backdrop)} backdrop, {kb:.0f} KB)  viewBox {out['viewBox']}")

if __name__ == "__main__":
    main()
