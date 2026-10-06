#!/opt/local/bin/python3.14
"""Write a static HTML map that extends the Omaha street grid over the globe.

Usage: make_extendoma.py [omaha_grid.json [extendoma.html [README.md]]]

This is the Omaha version of extendny.js. The page draws the grid over
OpenStreetMap tiles with Leaflet. Serve it over http (for example
python3.14 -m http.server), because OSM does not send tiles to a file:// page.

The data comes from "ns", "ew" and "continuation" in omaha_grid.json. The
order of "ns" (east to west) and "ew" (north to south) sets the names:

- N-S streets west of the last street continue the numbers (325th Street,
  326th Street, ...), every continuation.west.spacing_deg of longitude.
- N-S streets east of 1st Street are East 1st Street, East 2nd Street, ...
  (there is no 0th Street), every continuation.east.spacing_deg.
- E-W streets repeat the "ew" names in order: south of the last street comes
  the first street again, and north of the first street comes the last
  street. A repeat gets "North" or "South" and a Roman numeral: south of
  Biden Street is South Girard Street II, north of Girard Street is North
  Biden Street II. The spacing is continuation.north/south.spacing_deg.

The east and west N-S streets meet at the meridian opposite the grid center.
All N-S streets are meridians and all E-W streets are parallels, so on the
Web Mercator map they are straight vertical and horizontal lines.
"""

import html
import json
import re
import sys

# The E-W mile roads are 16 slots apart. At low zoom, the map shows every
# 16th (32nd, ...) E-W street, counted from this one.
EW_MILE_ANCHOR = "Harrison Street"

NS_NUMBERED = re.compile(r"^(\d+)(st|nd|rd|th) Street$")
HEADING = re.compile(r"^#+\s*(.+?)$")
CODE = re.compile(r"`([^`]+)`")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ExtendOMA</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body, #map { height: 100%; margin: 0; }
  .panel { background: #fff; padding: 6px 8px; font: 13px/1.5 sans-serif; }
  .panel form { display: flex; gap: 4px; margin: 0; }
  .panel input[type=search] { width: 14em; }
  .panel .msg { color: #a00; font-size: 12px; }
  .panel .info { max-width: 32em; max-height: 60vh; overflow-y: auto; margin-top: 4px; }
  .panel .info h3 { font-size: 13px; margin: 8px 0 2px; }
  .panel .info p { margin: 0 0 6px; }
  /* Undo the Leaflet .leaflet-bar a style, which is for the zoom buttons. */
  .leaflet-bar .info a, .leaflet-bar .info a:hover {
    display: inline; width: auto; height: auto; line-height: inherit; text-align: left;
    border: 0; border-radius: 0; background: none; color: #0078a8; text-decoration: underline; }
  .signs { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; }
  .sign { background: #2c7669; color: #fff; border: 2px solid #fff; border-radius: 4px;
          padding: 1px 6px; font: bold 13px/1.4 sans-serif; white-space: nowrap;
          box-shadow: 0 1px 3px rgba(0, 0, 0, 0.5); }
  #mouse { position: absolute; z-index: 1000; pointer-events: none; }
  .pin .dot { width: 10px; height: 10px; margin: -7px 0 6px -7px; border-radius: 50%;
              background: #2c7669; border: 2px solid #fff; }
</style>
</head>
<body>
<div id="map"></div>
<script>
const D = __DATA__;

const MAX_LAT = 85.0511;  // Web Mercator limit
const MIN_PX = 12;  // minimum distance between drawn lines, px
const NS_COLOR = '#d7191c', EW_COLOR = '#2c7bb6', SIGN_GREEN = '#2c7669';

const mod = (a, b) => ((a % b) + b) % b;
const ord = n => n.toLocaleString('en-US') +
  (mod(n, 100) >= 11 && mod(n, 100) <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] || 'th');
const ROMAN = [[1000, 'M'], [900, 'CM'], [500, 'D'], [400, 'CD'], [100, 'C'], [90, 'XC'],
               [50, 'L'], [40, 'XL'], [10, 'X'], [9, 'IX'], [5, 'V'], [4, 'IV'], [1, 'I']];
function toRoman(n) {
  let out = '';
  for (const [v, r] of ROMAN) while (n >= v) { out += r; n -= v; }
  return out;
}
function fromRoman(s) {
  let n = 0;
  for (const [v, r] of ROMAN) while (s.startsWith(r)) { n += v; s = s.slice(r.length); }
  return s ? NaN : n;
}

// One axis of the grid. rows are [name, coordinate, key], in the order of
// decreasing coordinate (east to west, or north to south). Index i < 0 is
// before the rows (east or north), i >= rows.length is after them. The key
// is the street number (N-S) or the grid slot (E-W). It is null for a row
// off the key sequence (an avenue). Negative indices have key i.
function makeAxis(rows, dBefore, dAfter, cMin, cMax, name) {
  const n = rows.length, c0 = rows[0][1], cL = rows[n - 1][1], kL = rows[n - 1][2];
  const keyToI = new Map();
  rows.forEach(([, , k], i) => { if (k !== null) keyToI.set(k, i); });
  const iMin = -Math.floor((cMax - c0) / dBefore);
  const iMax = n - 1 + Math.floor((cL - cMin) / dAfter);
  return {
    n, iMin, iMax,
    coord: i => i < 0 ? c0 - i * dBefore : i < n ? rows[i][1] : cL - (i - n + 1) * dAfter,
    key: i => i < 0 ? i : i < n ? rows[i][2] : kL + i - n + 1,
    indexOfKey: k => k < 0 ? k : k > kL ? n - 1 + k - kL : keyToI.get(k),
    name: i => name(i, rows, kL),
    // Index of the line nearest to coordinate c.
    nearest(c) {
      let i;
      if (c >= c0) i = -Math.round((c - c0) / dBefore);
      else if (c <= cL) i = n - 1 + Math.round((cL - c) / dAfter);
      else {
        let a = 0, b = n - 1;
        while (b - a > 1) { const m = (a + b) >> 1; if (rows[m][1] >= c) a = m; else b = m; }
        i = rows[a][1] - c <= c - rows[b][1] ? a : b;
      }
      return Math.min(iMax, Math.max(iMin, i));
    },
  };
}

// N-S lines cover the longitudes [LON_W, LON_E), centered on the grid.
const LON_C = (D.ns[0][1] + D.ns[D.ns.length - 1][1]) / 2;
const LON_W = LON_C - 180, LON_E = LON_C + 180;
const canonLon = lon => mod(lon - LON_W, 360) + LON_W;

const NS = makeAxis(D.ns, D.east, D.west, LON_W, LON_E, (i, rows, kL) =>
  i < 0 ? `East ${ord(-i)} Street` : i < rows.length ? rows[i][0] : `${ord(kL + i - rows.length + 1)} Street`);
// Repeat c of the E-W names (c = floor(i / n); 0 is Omaha) is
// "South <name> <c + 1>" for c > 0 and "North <name> <1 - c>" for c < 0.
const EW = makeAxis(D.ew, D.north, D.south, -MAX_LAT, MAX_LAT, (i, rows) => {
  const c = Math.floor(i / rows.length), name = rows[mod(i, rows.length)][0];
  return c === 0 ? name : `${c > 0 ? 'South' : 'North'} ${name} ${toRoman(Math.abs(c) + 1)}`;
});

// Show every s-th line: at s = 1 all lines, else the lines whose key minus
// anchor is a multiple of s. Powers of 2 keep half the lines when zooming out.
// There are no steps between 1 and a mile (12 N-S, 16 E-W): they mix thick
// and thin lines. Thus the map shows all streets or only the mile roads.
const NS_STEPS = [1], EW_STEPS = [1];
for (let s = 12; s < 1 << 22; s *= 2) NS_STEPS.push(s);
for (let s = 16; s < 1 << 22; s *= 2) EW_STEPS.push(s);
const NS_ANCHOR = 0, EW_ANCHOR = D.ewAnchor;
const isMile = {ns: k => k !== null && k % 12 === 0,
                ew: k => mod(k - EW_ANCHOR, 16) === 0};

// Indices of the lines to draw between coordinates cLo and cHi.
function visible(ax, cLo, cHi, s, anchor) {
  const a = Math.max(ax.iMin, ax.nearest(cHi) - 1), b = Math.min(ax.iMax, ax.nearest(cLo) + 1);
  const out = [];
  if (s === 1) { for (let i = a; i <= b; i++) out.push(i); return out; }
  const kA = ax.key(a) ?? ax.key(a + 1), kB = ax.key(b) ?? ax.key(b - 1);
  for (let k = kA + mod(anchor - kA, s); k <= kB; k += s) {
    const i = ax.indexOfKey(k);
    if (i !== undefined) out.push(i);
  }
  return out;
}

const map = L.map('map', {worldCopyJump: true, minZoom: 2, renderer: L.canvas(), zoomControl: false});
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

const gridLayer = L.layerGroup().addTo(map);
const hoverLayer = L.layerGroup().addTo(map);

function lineStyle(color, mile) {
  return {color, weight: mile ? 2 : 1, opacity: 0.8, interactive: false};
}

function viewBox() {
  const b = map.getBounds().pad(0.25);  // margin for resizes and small pans
  return {w: b.getWest(), e: b.getEast(),
          s: Math.max(b.getSouth(), -MAX_LAT), n: Math.min(b.getNorth(), MAX_LAT)};
}

// Call f(i, offset) for each N-S line in view. offset is the multiple of
// 360 to add to its longitude for the world copy in view.
function forNsInView(v, s, f) {
  for (let k = Math.floor((v.w - LON_W) / 360); k <= Math.floor((v.e - LON_W) / 360); k++) {
    const a = Math.max(v.w - 360 * k, LON_W), b = Math.min(v.e - 360 * k, LON_E);
    if (a < b) for (const i of visible(NS, a, b, s, NS_ANCHOR)) f(i, 360 * k);
  }
}

function drawGrid() {
  gridLayer.clearLayers();
  const v = viewBox();
  const pxPerDeg = 256 * 2 ** map.getZoom() / 360;
  const latC = (v.s + v.n) / 2;
  const sNs = NS_STEPS.find(s => s * D.west * pxPerDeg >= MIN_PX) ?? NS_STEPS.at(-1);
  const sEw = EW_STEPS.find(s => s * D.north * pxPerDeg / Math.cos(latC * Math.PI / 180) >= MIN_PX)
    ?? EW_STEPS.at(-1);
  forNsInView(v, sNs, (i, off) => {
    const lon = NS.coord(i) + off;
    L.polyline([[v.s, lon], [v.n, lon]],
               lineStyle(NS_COLOR, isMile.ns(NS.key(i)))).addTo(gridLayer);
  });
  for (const j of visible(EW, v.s, v.n, sEw, EW_ANCHOR)) {
    const lat = EW.coord(j);
    L.polyline([[lat, v.w], [lat, v.e]],
               lineStyle(EW_COLOR, isMile.ew(EW.key(j)))).addTo(gridLayer);
  }
  drawHover();
}

const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
const signs = (i, j) =>
  `<div class="signs"><div class="sign">${esc(NS.name(i))}</div><div class="sign">${esc(EW.name(j))}</div></div>`;

// The intersection nearest to latlng: {i, j, off}.
function nearestIntersection(latlng) {
  const lon = canonLon(latlng.lng);
  return {i: NS.nearest(lon), j: EW.nearest(Math.max(-MAX_LAT, Math.min(MAX_LAT, latlng.lat))),
          off: latlng.lng - lon};
}
const intersectionLatLng = ({i, j, off}) => L.latLng(EW.coord(j), NS.coord(i) + off);

// Mouse: a sign at the pointer and the two nearest lines in green.
const mouseDiv = document.createElement('div');
mouseDiv.id = 'mouse';
mouseDiv.hidden = true;
map.getContainer().appendChild(mouseDiv);
let hover = null, hoverFrame = null;

function drawHover() {
  hoverLayer.clearLayers();
  if (!hover) return;
  const v = viewBox(), lon = NS.coord(hover.i) + hover.off, lat = EW.coord(hover.j);
  const style = {color: SIGN_GREEN, weight: 3, opacity: 0.9, interactive: false};
  L.polyline([[v.s, lon], [v.n, lon]], style).addTo(hoverLayer);
  L.polyline([[lat, v.w], [lat, v.e]], style).addTo(hoverLayer);
}

// A tap on a touch screen also sends a mousemove, but no mouseout after it.
// Thus show the hover sign only for a mouse. Pointer events come before the
// mouse events that they cause.
let pointerType = 'mouse';
const onPointer = e => {
  pointerType = e.pointerType;
  if (pointerType !== 'mouse') hideHover();
};
map.getContainer().addEventListener('pointerdown', onPointer, true);
map.getContainer().addEventListener('pointermove', onPointer, true);

map.on('mousemove', e => {
  // Leaflet also sends mousemove for the controls (zoom, panel, info box).
  if (e.originalEvent.target.closest('.leaflet-control')) return hideHover();
  if (hoverFrame || pointerType !== 'mouse') return;
  hoverFrame = requestAnimationFrame(() => {
    hoverFrame = null;
    if (pointerType !== 'mouse') return;
    const h = nearestIntersection(e.latlng);
    if (!hover || h.i !== hover.i || h.j !== hover.j || h.off !== hover.off) {
      hover = h;
      mouseDiv.innerHTML = signs(h.i, h.j);
      drawHover();
    }
    mouseDiv.style.left = e.containerPoint.x + 12 + 'px';
    mouseDiv.style.top = e.containerPoint.y + 12 + 'px';
    mouseDiv.hidden = false;
  });
});
function hideHover() {
  cancelAnimationFrame(hoverFrame);
  hoverFrame = null;
  mouseDiv.hidden = true;
  hover = null;
  hoverLayer.clearLayers();
}
map.on('mouseout dragstart', hideHover);

// Pinned intersection, also kept in the URL hash:
// #<N-S name>/<E-W name>/<zoom>, with "_" for spaces.
let pin = null, oldHash = null;
const nsIndexByName = new Map(D.ns.map(([name], i) => [name, i]));
const ewIndexByName = new Map(D.ew.map(([name], i) => [name, i]));

function setPin(x, updateHash = true) {
  if (pin) pin.remove();
  pin = L.marker(intersectionLatLng(x), {
    interactive: false,
    icon: L.divIcon({className: 'pin', iconSize: null, html: '<div class="dot"></div>' + signs(x.i, x.j)}),
  }).addTo(map);
  if (updateHash) {
    location.hash = oldHash = '#' + [NS.name(x.i), EW.name(x.j), map.getZoom()]
      .map(s => String(s).replace(/ /g, '_')).join('/');
  }
}

function parseNsName(s) {
  let m;
  if ((m = /^East (\\d+)(st|nd|rd|th) Street$/.exec(s))) return -Number(m[1]);
  if (nsIndexByName.has(s)) return nsIndexByName.get(s);
  if ((m = /^(\\d+)(st|nd|rd|th) Street$/.exec(s))) return NS.indexOfKey(Number(m[1]));
}

function parseEwName(s) {
  if (ewIndexByName.has(s)) return ewIndexByName.get(s);
  const m = /^(North|South) (.+) ([IVXLCDM]+)$/.exec(s);
  const j0 = m && ewIndexByName.get(m[2]), r = m && fromRoman(m[3]);
  if (j0 === undefined || j0 === null || !(r >= 2)) return undefined;
  return j0 + EW.n * (m[1] === 'South' ? r - 1 : 1 - r);
}

function parseHash() {
  if (location.hash === oldHash) return null;
  oldHash = location.hash;
  const parts = decodeURIComponent(location.hash.slice(1)).replace(/_/g, ' ').replace(/,/g, '').split('/');
  if (parts.length !== 3) return null;
  const i = parseNsName(parts[0]), j = parseEwName(parts[1]), zoom = parseInt(parts[2], 10);
  if (i === undefined || i < NS.iMin || i > NS.iMax || isNaN(zoom)) return null;
  if (j === undefined || j < EW.iMin || j > EW.iMax) return null;
  return {x: {i, j, off: 0}, zoom};
}

function hashChange() {
  const h = parseHash();
  if (!h) return false;
  map.setView(intersectionLatLng(h.x), h.zoom);
  setPin(h.x, false);
  return true;
}

map.on('click', e => setPin(nearestIntersection(e.latlng)));
map.on('moveend', drawGrid);
window.addEventListener('hashchange', hashChange);

// Controls: address search (Nominatim), current location.
const Panel = L.Control.extend({
  options: {position: 'topleft'},
  onAdd() {
    const div = L.DomUtil.create('div', 'leaflet-bar panel');
    div.innerHTML =
      '<form><input type="search" placeholder="Search for a place" aria-label="Search for a place">' +
      '<button type="submit">Go</button>' +
      ('geolocation' in navigator ? '<button type="button" class="locate">My location</button>' : '') +
      '<button type="button" class="about" title="About this map" aria-expanded="false">&#9432; Info</button>' +
      '</form><div class="msg" hidden></div><div class="info" hidden>__README__</div>';
    const msg = div.querySelector('.msg');
    const say = text => { msg.textContent = text; msg.hidden = !text; };
    const goTo = (latlng, zoom) => { map.setView(latlng, zoom); setPin(nearestIntersection(latlng)); };
    div.querySelector('form').addEventListener('submit', async e => {
      e.preventDefault();
      const q = div.querySelector('input[type=search]').value.trim();
      if (!q) return;
      say('');
      try {
        const r = await fetch('https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=' +
                              encodeURIComponent(q));
        const [hit] = await r.json();
        if (!hit) return say('No result.');
        const [s, n, w, e2] = hit.boundingbox.map(Number);
        map.fitBounds([[s, w], [n, e2]], {maxZoom: 16});
        setPin(nearestIntersection(L.latLng(Number(hit.lat), Number(hit.lon))));
      } catch (err) {
        say('Search failed.');
      }
    });
    div.querySelector('.locate')?.addEventListener('click', () => {
      say('');
      navigator.geolocation.getCurrentPosition(
        p => goTo(L.latLng(p.coords.latitude, p.coords.longitude), 16),
        () => say('Location is not available.'));
    });
    const info = div.querySelector('.info'), about = div.querySelector('.about');
    about.addEventListener('click', () => {
      info.hidden = !info.hidden;
      about.setAttribute('aria-expanded', String(!info.hidden));
    });
    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.disableScrollPropagation(div);
    return div;
  },
});
new Panel().addTo(map);
// Add the zoom buttons after the panel, so that they show below it.
L.control.zoom().addTo(map);

// Default view: centered on 72nd and Dodge, at the zoom that shows all of Omaha.
if (!hashChange()) {
  const omaha = L.latLngBounds([[D.ew.at(-1)[1], D.ns.at(-1)[1]], [D.ew[0][1], D.ns[0][1]]]);
  map.setView(intersectionLatLng({i: NS.indexOfKey(72), j: ewIndexByName.get('Dodge Street'), off: 0}),
              map.getBoundsZoom(omaha));
}
drawGrid();
</script>
</body>
</html>
"""


def extract(grid):
    """The data for the page, from the omaha_grid.json object."""
    ns, ew, cont = grid["ns"], grid["ew"], grid["continuation"]
    for key, first, last in (("east", ns, None), ("west", None, ns),
                             ("north", ew, None), ("south", None, ew)):
        end = first[0] if first else last[-1]
        if cont[key]["from"] != end["name"]:
            sys.exit(f"continuation.{key}.from is {cont[key]['from']!r}, not {end['name']!r}")

    ns_rows = []
    for s in ns:
        m = NS_NUMBERED.match(s["name"])
        ns_rows.append([s["name"], s["lon"], int(m[1]) if m else None])
    if ns_rows[0][2] != 1 or ns_rows[-1][2] is None:
        sys.exit("ns must start with 1st Street and end with a numbered street")
    nums = [r[2] for r in ns_rows if r[2] is not None]
    if any(a >= b for a, b in zip(nums, nums[1:])):
        sys.exit("ns street numbers must increase from east to west")
    if any(a[1] <= b[1] for a, b in zip(ns_rows, ns_rows[1:])):
        sys.exit("ns longitudes must decrease from east to west")

    step = cont["north"]["spacing_deg"]
    lat0 = ew[0]["lat"]
    ew_rows = [[s["name"], s["lat"], round((lat0 - s["lat"]) / step)] for s in ew]
    if any(a[1] <= b[1] or a[2] >= b[2] for a, b in zip(ew_rows, ew_rows[1:])):
        sys.exit("ew latitudes must decrease from north to south, one grid slot or more apart")
    if len({r[0] for r in ew_rows}) != len(ew_rows):
        sys.exit("ew names must be unique")
    anchor = next((r[2] for r in ew_rows if r[0] == EW_MILE_ANCHOR), 0)

    return {"ns": ns_rows, "ew": ew_rows, "ewAnchor": anchor,
            "east": cont["east"]["spacing_deg"], "west": cont["west"]["spacing_deg"],
            "north": cont["north"]["spacing_deg"], "south": cont["south"]["spacing_deg"]}


def link(m):
    """HTML for a Markdown link. m is a LINK match on escaped text."""
    new_tab = ' target="_blank" rel="noopener"' if re.match(r"https?:", m[2]) else ""
    return f'<a href="{m[2]}"{new_tab}>{m[1]}</a>'


def readme_html(text):
    """HTML for the info box, from README.md.

    A "# Title" line is a heading. Other lines make paragraphs, which blank
    lines separate. `text` is code and [text](url) is a link. A link to an
    http(s) URL opens in a new tab, so the map stays open. Other Markdown
    stays as plain text.
    """
    out, para = [], []

    def flush():
        if para:
            text = CODE.sub(r"<code>\1</code>", html.escape(" ".join(para)))
            text = LINK.sub(link, text)
            out.append(f"<p>{text}</p>")
            para.clear()

    for line in text.splitlines():
        line = line.strip()
        if m := HEADING.match(line):
            flush()
            out.append(f"<h3>{html.escape(m[1])}</h3>")
        elif line:
            para.append(line)
        else:
            flush()
    flush()
    return "".join(out)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "omaha_grid.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "extendoma.html"
    readme = sys.argv[3] if len(sys.argv) > 3 else "README.md"
    with open(src) as f:
        data = extract(json.load(f))
    with open(readme) as f:
        info = readme_html(f.read())
    text = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    # info goes in a JS string literal in single quotes.
    info = json.dumps(info)[1:-1].replace("'", "\\'").replace("</", "<\\/")
    with open(dst, "w") as f:
        f.write(TEMPLATE.replace("__DATA__", text).replace("__README__", info))
    print(f"{dst}: {len(data['ns'])} N-S and {len(data['ew'])} E-W streets", file=sys.stderr)


if __name__ == "__main__":
    main()
