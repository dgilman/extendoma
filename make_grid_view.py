#!/opt/local/bin/python3.14
"""Write an HTML map to visually check the street grid from extract_grid.py.

Usage: make_grid_view.py [omaha_grid.json [grid_view.html]]

The HTML embeds the grid data and draws each street's great circle, extended
far past its two points, over OpenStreetMap tiles. Serve it over http (e.g.
python3.14 -m http.server); OSM does not send tiles to a file:// page.

The input is the object from extract_grid.py ({"ns", "ew", "ns_nominal",
"ew_nominal", "continuation"}) or the older format, a list of streets. With
the object, the map can change between the regular and the nominal grid, and
draws some lines past each end of the grid from "continuation".
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
  .panel { background: #fff; padding: 6px 10px; font: 13px/1.5 sans-serif; }
  .panel label { display: block; white-space: nowrap; }
  .panel .off { color: #999; }
  .panel hr { border: 0; border-top: 1px solid #ddd; margin: 4px 0; }
  .panel .cont { color: #555; font-size: 12px; }
</style>
</head>
<body>
<div id="map"></div>
<script>
const GRID = __GRID__;

const R = 6371008.8;  // mean earth radius, m
const HALF_LEN = 150000;  // extend each line this far each way, m
const STEP = 250;  // sample spacing, m
const CONT_COUNT = 5;  // continuation lines to draw past each end
const NS_NAME = /^\\d+(st|nd|rd|th) (Street|Avenue)$/;  // old format only

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

// {modes: {regular: {ns, ew}, nominal?: {ns, ew}}, continuation}, from the
// object format or from the older list of streets.
function normalize(g) {
  if (Array.isArray(g)) {
    return {modes: {regular: {ns: g.filter(s => NS_NAME.test(s.name)),
                              ew: g.filter(s => !NS_NAME.test(s.name))}},
            continuation: null};
  }
  const modes = {regular: {ns: g.ns, ew: g.ew}};
  if (g.ns_nominal && g.ew_nominal) modes.nominal = {ns: g.ns_nominal, ew: g.ew_nominal};
  return {modes, continuation: g.continuation || null};
}
const DATA = normalize(GRID);

const map = L.map('map', {renderer: L.canvas({tolerance: 6})});
// tile.openstreetmap.org refuses requests with no Referer, as from file://.
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

const NS_COLOR = '#d7191c', EW_COLOR = '#2c7bb6';

function addStreet(group, segments, {name, points: [p, q]}, color) {
  L.polyline(greatCircle(p, q), {color, weight: 1, opacity: 0.7})
    .bindTooltip(name, {sticky: true}).addTo(group);
  L.polyline([p, q], {color, weight: 5, opacity: 0.6})
    .bindTooltip(name, {sticky: true}).addTo(segments);
}

// Dashed lines past the end street "from", every spacing_deg.
function addContinuation(group, streets, c) {
  const ref = streets.find(s => s.name === c.from);
  if (!ref) return;
  const sign = {east: 1, west: -1, north: 1, south: -1}[c.direction];
  const color = c.lines === 'N-S' ? NS_COLOR : EW_COLOR;
  for (let i = 1; i <= CONT_COUNT; i++) {
    const d = sign * i * c.spacing_deg;
    const [p, q] = ref.points.map(([lat, lon]) =>
      c.lines === 'N-S' ? [lat, lon + d] : [lat + d, lon]);
    L.polyline(greatCircle(p, q), {color, weight: 1.5, opacity: 0.8, dashArray: '6 6'})
      .bindTooltip(`${c.direction} of ${c.from}, +${i} (${c.spacing_ft} ft)`, {sticky: true})
      .addTo(group);
  }
}

// LAYERS[mode][key] is a layer group; key is ns, ew, seg or cont.
const LAYERS = {};
const bounds = L.latLngBounds([]);
for (const [mode, {ns, ew}] of Object.entries(DATA.modes)) {
  const layers = LAYERS[mode] = {ns: L.layerGroup(), ew: L.layerGroup(),
                                 seg: L.layerGroup(), cont: L.layerGroup()};
  for (const s of ns) addStreet(layers.ns, layers.seg, s, NS_COLOR);
  for (const s of ew) addStreet(layers.ew, layers.seg, s, EW_COLOR);
  for (const s of [...ns, ...ew]) bounds.extend(s.points[0]).extend(s.points[1]);
  for (const c of Object.values(DATA.continuation || {}))
    addContinuation(layers.cont, c.lines === 'N-S' ? ns : ew, c);
}

const state = {mode: 'regular', ns: true, ew: true, seg: true, cont: true};
function refresh() {
  for (const [mode, layers] of Object.entries(LAYERS))
    for (const [key, layer] of Object.entries(layers))
      if (mode === state.mode && state[key]) layer.addTo(map); else layer.remove();
}

const Panel = L.Control.extend({
  options: {position: 'topright'},
  onAdd() {
    const div = L.DomUtil.create('div', 'leaflet-bar panel');
    const radio = (mode, label) => {
      const ok = mode in DATA.modes;
      return `<label class="${ok ? '' : 'off'}"><input type="radio" name="mode" value="${mode}"` +
             `${mode === state.mode ? ' checked' : ''}${ok ? '' : ' disabled'}> ${label}</label>`;
    };
    const box = (key, label) => `<label><input type="checkbox" data-key="${key}"` +
      `${state[key] ? ' checked' : ''}${key === 'cont' && !DATA.continuation ? ' disabled' : ''}> ${label}</label>`;
    const c = DATA.continuation;
    const cont = c ? '<div class="cont">' + ['east', 'west', 'north', 'south']
      .filter(d => c[d]).map(d => `${d}: ${c[d].spacing_ft} ft`).join(', ') + '</div>' : '';
    div.innerHTML = radio('regular', 'Regular grid (fitted)') +
      radio('nominal', 'Nominal grid (survey feet)') + '<hr>' +
      box('ns', 'N-S lines') + box('ew', 'E-W lines') + box('seg', 'Source segments') +
      box('cont', 'Continuation') + cont;
    div.addEventListener('change', e => {
      const t = e.target;
      if (t.name === 'mode') state.mode = t.value; else state[t.dataset.key] = t.checked;
      refresh();
    });
    L.DomEvent.disableClickPropagation(div);
    return div;
  },
});
new Panel().addTo(map);
refresh();
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
    if isinstance(grid, list):
        summary = f"{len(grid)} streets (old format)"
    else:
        summary = (f"{len(grid['ns'])} N-S and {len(grid['ew'])} E-W streets"
                   + (", nominal grid" if "ns_nominal" in grid else ""))
    print(f"{dst}: {summary}", file=sys.stderr)


if __name__ == "__main__":
    main()
