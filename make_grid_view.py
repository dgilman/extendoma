#!/opt/local/bin/python3.14
"""Write an HTML map to visually check the street grid from extract_grid.py.

Usage: make_grid_view.py [omaha_grid.json [grid_view.html]]

The HTML embeds the grid data and draws each street's great circle, extended
far past its two points, over OpenStreetMap tiles. Serve it over http (e.g.
python3.14 -m http.server); OSM does not send tiles to a file:// page.
"""

import json
import sys

TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Omaha grid check</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body, #map { height: 100%; margin: 0; }
</style>
</head>
<body>
<div id="map"></div>
<script>
const GRID = __GRID__;

const R = 6371008.8;  // mean earth radius, m
const HALF_LEN = 150000;  // extend each line this far each way, m
const STEP = 250;  // sample spacing, m
const NS_NAME = /^\\d+(st|nd|rd|th) Street$/;

const rad = d => d * Math.PI / 180, deg = r => r * 180 / Math.PI;
const toVec = ([lat, lon]) => [Math.cos(rad(lat)) * Math.cos(rad(lon)),
                               Math.cos(rad(lat)) * Math.sin(rad(lon)),
                               Math.sin(rad(lat))];
const toLatLng = ([x, y, z]) => [deg(Math.atan2(z, Math.hypot(x, y))), deg(Math.atan2(y, x))];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
                         a[0] * b[1] - a[1] * b[0]];
const norm = v => { const l = Math.hypot(...v); return v.map(c => c / l); };

// Samples of the great circle through p and q, centered on their midpoint.
function greatCircle(p, q) {
  const a = toVec(p), b = toVec(q);
  const n = norm(cross(a, b));
  const m = norm(a.map((c, i) => c + b[i]));
  const t = cross(n, m);
  const pts = [];
  for (let s = -HALF_LEN; s <= HALF_LEN; s += STEP) {
    const th = s / R, c = Math.cos(th), sn = Math.sin(th);
    pts.push(toLatLng(m.map((v, i) => v * c + t[i] * sn)));
  }
  return pts;
}

const map = L.map('map', {renderer: L.canvas({tolerance: 6})});
// tile.openstreetmap.org refuses requests with no Referer, as from file://.
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

const nsLines = L.layerGroup(), ewLines = L.layerGroup(), segments = L.layerGroup();
const bounds = L.latLngBounds([]);
for (const {name, points: [p, q]} of GRID) {
  const ns = NS_NAME.test(name);
  const color = ns ? '#d7191c' : '#2c7bb6';
  L.polyline(greatCircle(p, q), {color, weight: 1, opacity: 0.7})
    .bindTooltip(name, {sticky: true}).addTo(ns ? nsLines : ewLines);
  L.polyline([p, q], {color, weight: 5, opacity: 0.6})
    .bindTooltip(name, {sticky: true}).addTo(segments);
  bounds.extend(p).extend(q);
}
nsLines.addTo(map);
ewLines.addTo(map);
segments.addTo(map);
L.control.layers(null, {
  'N-S lines': nsLines, 'E-W lines': ewLines, 'Source segments': segments,
}, {collapsed: false}).addTo(map);
map.fitBounds(bounds);
</script>
</body>
</html>
"""


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "omaha_grid.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "grid_view.html"
    with open(src) as f:
        grid = json.load(f)
    data = json.dumps(grid, separators=(",", ":")).replace("</", "<\\/")
    with open(dst, "w") as f:
        f.write(TEMPLATE.replace("__GRID__", data))
    print(f"{dst}: {len(grid)} streets", file=sys.stderr)


if __name__ == "__main__":
    main()
