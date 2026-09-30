#!/opt/local/bin/python3.14
"""Derive the base street grid of east Omaha from OpenStreetMap.

Reads the OSM layercake parquet files with the duckdb CLI and writes a JSON
object to stdout:
    {"ns": [...], "ew": [...], "ns_nominal": [...], "ew_nominal": [...],
     "continuation": {"east": {...}, "west": {...}, "north": {...},
                      "south": {...}}}
Each list has items {"name": "90th Street", "points": [[lat, lon], [lat, lon]]}.
The two points of each street set its great circle. "ns" is in order from
east to west (1st Street first) and "ew" from north to south. The "_nominal"
lists have the same streets, with the nominal spacings in US survey feet.
"continuation" tells how the grid continues past its last line in each
direction.

The output is a regular grid: N-S lines (meridians) at a fixed spacing per
street number, and E-W lines (parallels) at a fixed spacing. The grid is fitted
to the long contiguous straight runs of the streets. Numbered streets with no
run on the grid are imputed. N-S streets are measured in all of Douglas County.
E-W names come only from east of 72nd Street, where the base grid is not
changed by the West Dodge / West Maple / West Center transitions.
South of Harrison Street (the county line), the E-W grid continues with
artificial streets named after the presidents after William Henry Harrison.
Diagnostics go to stderr.
"""

import cmath
import json
import math
import re
import statistics
import subprocess
import sys

HIGHWAYS = "https://data.openstreetmap.us/layercake/highways.parquet"
BOUNDARIES = "https://data.openstreetmap.us/layercake/boundaries.parquet"
DOUGLAS_COUNTY_ID = 1425409
COUNTY_BBOX = (-96.48, 41.18, -95.86, 41.40)  # xmin, ymin, xmax, ymax
ROAD_CLASSES = ("trunk", "primary", "secondary", "tertiary", "unclassified",
                "residential", "living_street")

# Local equirectangular projection, only to classify and measure segments.
LAT0, LON0 = 41.26, -96.0
# Metres per degree on the WGS84 ellipsoid at LAT0.
_A, _E2 = 6378137.0, 0.00669437999014
_W = math.sqrt(1 - _E2 * math.sin(math.radians(LAT0)) ** 2)
KX = _A / _W * math.cos(math.radians(LAT0)) * math.pi / 180  # longitude
KY = _A * (1 - _E2) / _W ** 3 * math.pi / 180  # latitude
FT = 1200 / 3937  # metres per US survey foot. Diagnostics are in feet.

MAX_BEARING_DEG = 3  # a segment must be this near to N-S or E-W
CLUSTER_TOL_M = 30  # joins the two carriageways of a divided road
MAX_GAP_M = 60  # a larger gap along the street ends a contiguous run
MIN_RUN_M = 300
ANCHOR_EVERY = 6  # the mile and half-mile roads anchor the N-S grid
ANCHOR_MIN_M = 1500  # an anchor road needs a contiguous run this long
# N-S streets are measured only south of this latitude, if they exist there.
# North of about 41.30 (Fort Street), the roads west of 132nd Street move west
# by 15-50 m.
NS_LAT_MIN, NS_LAT_MAX = COUNTY_BBOX[1], 41.30
ON_GRID_TOL = 0.25  # a run is on the grid within this fraction of a step
OFF_GRID_MIN_M = 600  # an off-grid street must be this long to fill a slot
EW_MIN_M, EW_MAX_M = 80, 130  # range searched for the E-W spacing
ISOLATED_GAP = 3  # E-W streets more slots apart than this are in separate groups
ISOLATED_GROUP = 3  # a group with fewer E-W streets than this is small
ISOLATED_MIN_M = 1000  # a street in a small group must be this long
WEST_LIMIT = 72  # E-W streets only east of this numbered street
EAST_FIRST = 1  # the N-S grid continues east to this numbered street
# Manual N-S fix: "<n>th Avenue" halfway between the nth and (n+1)th Streets.
NS_AVENUES = (48, 49, 50, 51, 60, 65)
# Manual N-S fix: put a street on the line between two other streets. In Benson,
# 52nd to 60th Street is a regular grid except 55th and 57th.
NS_REGRID = {55: (52, 60), 57: (52, 60)}
# Nominal grids, in US survey feet (see SESSION_NOTES.md). Between two N-S
# anchors, the lines are evenly spaced over the nominal width of the section.
NOMINAL_FT_PER_NUMBER = ((12, 360), (24, 344), (None, 440))  # (below, ft)
NOMINAL_EW_FT = 330  # 5 chains, 1/16 mile
# PLSS range lines (6-mile columns of townships). Scaled to the 40N baseline,
# they are 72, 66, 60 and 54 miles east of the 6th Principal Meridian. Each
# range was surveyed on its own: the nominal N-S grid has each range line at
# its regular place, and the west column of each range takes the error.
RANGE_LINES = (60, 132, 204, 276)
NOMINAL_PIN_EW = "Dodge Street"  # the nominal E-W grid has this street at
                                 # its regular place
# Manual E-W fixes, from a review of the output on the map.
EW_NORTH_LIMIT = "Young Street"  # remove this street and all streets north
EW_RENAMES = {"Mary Street": "Titus Avenue"}
EW_FILL = (  # (street, street, name for the one empty slot between them)
    ("O Street", "P Street", "Ohern Street"),
    ("Arbor Street", "Bancroft Street", "Gold Street"),
)
# Named ways of these classes make a name "used" in Omaha.
STREET_CLASSES = ROAD_CLASSES + ("motorway", "motorway_link", "trunk_link",
                                 "primary_link", "secondary_link",
                                 "tertiary_link", "service", "pedestrian")
SOUTH_OF = "Harrison Street"  # the artificial E-W streets start south of this
# The presidents after William Henry Harrison, in order. A president with
# non-contiguous terms (Cleveland, Trump) is in the list one time only.
PRESIDENTS = ("Tyler", "Polk", "Taylor", "Fillmore", "Pierce", "Buchanan",
              "Lincoln", "Johnson", "Grant", "Hayes", "Garfield", "Arthur",
              "Cleveland", "Harrison", "McKinley", "Roosevelt", "Taft",
              "Wilson", "Harding", "Coolidge", "Hoover", "Roosevelt",
              "Truman", "Eisenhower", "Kennedy", "Johnson", "Nixon", "Ford",
              "Carter", "Reagan", "Bush", "Clinton", "Bush", "Obama",
              "Orange Cheeto", "Biden")

NUMBERED = re.compile(r"^(?:North|South) ((\d+)(?:st|nd|rd|th) Street)$")
DIRECTIONAL = re.compile(r"^(?:North|South|East|West) ")
# Straight E-W parts of limited-access roads are not grid streets.
NOT_GRID = re.compile(r" (?:Highway|Expressway|Parkway|Freeway)$")


def log(*args):
    print(*args, file=sys.stderr)


def run_sql(sql):
    out = subprocess.run(["duckdb", "-init", "/dev/null", "-json", "-c", sql],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else []


def fetch_ways():
    xmin, ymin, xmax, ymax = COUNTY_BBOX
    classes = ", ".join(f"'{c}'" for c in ROAD_CLASSES)
    sql = f"""
    LOAD spatial;
    SET geometry_always_xy = true;
    WITH county AS (
      SELECT geometry FROM '{BOUNDARIES}'
      WHERE id = {DOUGLAS_COUNTY_ID}
        AND bbox.xmin > {xmin} AND bbox.xmax < {xmax}
        AND bbox.ymin > {ymin} AND bbox.ymax < {ymax}
    )
    SELECT h.name, ST_AsGeoJSON(ST_Intersection(h.geometry, c.geometry)) AS geom
    FROM '{HIGHWAYS}' h, county c
    WHERE h.bbox.xmax >= {xmin} AND h.bbox.xmin <= {xmax}
      AND h.bbox.ymax >= {ymin} AND h.bbox.ymin <= {ymax}
      AND h.name IS NOT NULL
      AND h.highway IN ({classes})
      AND ST_Intersects(h.geometry, c.geometry);
    """
    return run_sql(sql)


def fetch_street_names():
    """All street names in Douglas County, with no geometry."""
    xmin, ymin, xmax, ymax = COUNTY_BBOX
    classes = ", ".join(f"'{c}'" for c in STREET_CLASSES)
    sql = f"""
    LOAD spatial;
    SET geometry_always_xy = true;
    WITH county AS (
      SELECT geometry FROM '{BOUNDARIES}'
      WHERE id = {DOUGLAS_COUNTY_ID}
        AND bbox.xmin > {xmin} AND bbox.xmax < {xmax}
        AND bbox.ymin > {ymin} AND bbox.ymax < {ymax}
    )
    SELECT DISTINCT h.name
    FROM '{HIGHWAYS}' h, county c
    WHERE h.bbox.xmax >= {xmin} AND h.bbox.xmin <= {xmax}
      AND h.bbox.ymax >= {ymin} AND h.bbox.ymin <= {ymax}
      AND h.name IS NOT NULL
      AND h.highway IN ({classes})
      AND ST_Intersects(h.geometry, c.geometry);
    """
    return [r["name"] for r in run_sql(sql)]


def linestrings(geom):
    if isinstance(geom, str):
        geom = json.loads(geom)
    match geom["type"]:
        case "LineString":
            yield geom["coordinates"]
        case "MultiLineString":
            yield from geom["coordinates"]
        case "GeometryCollection":
            for g in geom["geometries"]:
                yield from linestrings(g)


def to_xy(lon, lat):
    return (lon - LON0) * KX, (lat - LAT0) * KY


class Seg:
    """A 2-point segment, stored as along/cross coordinates for its axis."""

    def __init__(self, axis, p, q):
        (x1, y1), (x2, y2) = to_xy(*p), to_xy(*q)
        if axis == "NS":  # along = y (north), cross = x (east)
            a1, a2, c1, c2 = y1, y2, x1, x2
        else:  # along = x (east), cross = y (north)
            a1, a2, c1, c2 = x1, x2, y1, y2
        if a1 > a2:
            a1, a2, c1, c2, p, q = a2, a1, c2, c1, q, p
        self.a1, self.a2 = a1, a2
        self.cross = (c1 + c2) / 2
        self.length = math.hypot(x2 - x1, y2 - y1)
        self.p, self.q = p, q  # (lon, lat), sorted along the axis


def axis_of(p, q):
    (x1, y1), (x2, y2) = to_xy(*p), to_xy(*q)
    if x1 == x2 and y1 == y2:
        return None
    b = math.degrees(math.atan2(x2 - x1, y2 - y1))  # bearing from north
    b = (b + 90) % 180 - 90  # fold to [-90, 90)
    if abs(b) <= MAX_BEARING_DEG:
        return "NS"
    if 90 - abs(b) <= MAX_BEARING_DEG:
        return "EW"
    return None


def street_key(name, axis):
    """Returns (display name, number or None), or None to drop the street."""
    if axis == "NS":
        m = NUMBERED.match(name)
        return (m.group(1), int(m.group(2))) if m else None
    if DIRECTIONAL.match(name) or NOT_GRID.search(name) or re.search(r"\d", name):
        return None
    return name, None


class Run:
    """A long contiguous part of one street."""

    def __init__(self, axis, name, num, segs):
        self.axis, self.name, self.num = axis, name, num
        self.a1 = min(s.a1 for s in segs)
        self.a2 = max(s.a2 for s in segs)
        self.length = self.a2 - self.a1
        w = sum(s.length for s in segs)
        self.cross = sum(s.cross * s.length for s in segs) / w  # metres
        # The cross coordinate in degrees: longitude for N-S, latitude for E-W.
        i = 0 if axis == "NS" else 1
        self.mc = sum((s.p[i] + s.q[i]) / 2 * s.length for s in segs) / w


def build_runs(axis, name, num, segs):
    segs = sorted(segs, key=lambda s: s.cross)
    clusters, cur = [], [segs[0]]
    for s in segs[1:]:
        if s.cross - cur[-1].cross > CLUSTER_TOL_M:
            clusters.append(cur)
            cur = []
        cur.append(s)
    clusters.append(cur)

    runs = []
    for cl in clusters:
        cl.sort(key=lambda s: s.a1)
        cur, end = [cl[0]], cl[0].a2
        for s in cl[1:]:
            if s.a1 > end + MAX_GAP_M:
                runs.append(cur)
                cur = []
            cur.append(s)
            end = max(end, s.a2)
        runs.append(cur)
    return [r for r in (Run(axis, name, num, rs) for rs in runs)
            if r.length >= MIN_RUN_M]


def group_segments(ways, keep=lambda axis, seg: True):
    groups = {}
    for w in ways:
        if not w["geom"]:
            continue
        for line in linestrings(w["geom"]):
            for p, q in zip(line, line[1:]):
                axis = axis_of(p, q)
                if not axis:
                    continue
                key = street_key(w["name"], axis)
                if not key:
                    continue
                seg = Seg(axis, p, q)
                if keep(axis, seg):
                    groups.setdefault((axis, *key), []).append(seg)
    return groups


def all_runs(groups):
    return [r for (axis, name, num), segs in groups.items()
            for r in build_runs(axis, name, num, segs)]


def ordinal(n):
    if n % 100 in (11, 12, 13):
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def fit_line(obs):
    """Weighted least squares of c = a + b * k over (k, c, weight)."""
    w = sum(l for _, _, l in obs)
    mk = sum(k * l for k, _, l in obs) / w
    mc = sum(c * l for _, c, l in obs) / w
    b = (sum(l * (k - mk) * (c - mc) for k, c, l in obs)
         / sum(l * (k - mk) ** 2 for k, _, l in obs))
    return mc - b * mk, b


class Grid:
    """Parallel lines at c = offset + k * step, for integer k.

    c is longitude for N-S lines (k is the street number) and latitude for
    E-W lines, so the lines are meridians and parallels.
    """

    def __init__(self, offset, step):
        self.offset, self.step = offset, step

    def at(self, k):
        return self.offset + k * self.step

    def slot(self, c):
        """The fractional grid index of coordinate c."""
        return (c - self.offset) / self.step

    def on_grid(self, c, k):
        return abs(self.slot(c) - k) <= ON_GRID_TOL

    def refine(self, runs, index):
        """Refits the grid to the runs within ON_GRID_TOL of their slot."""
        for _ in range(20):
            obs = [(index(r), r.mc, r.length) for r in runs
                   if self.on_grid(r.mc, index(r))]
            offset, step = fit_line(obs)
            if (offset, step) == (self.offset, self.step):
                break
            self.offset, self.step = offset, step
        return self


class NSStreet:
    def __init__(self, num, lon, how, name=None):
        self.num, self.lon, self.how = num, lon, how
        self.name = name or f"{ordinal(num)} Street"


def build_ns(runs):
    """Places each numbered street. Returns {number: NSStreet}.

    The mile and half-mile roads (every ANCHOR_EVERY numbers) with a long run
    are anchors. Between two anchors the streets are a regular grid, because
    the block width changes a little from one section to the next, and there
    are jogs at survey correction lines. If a long street (ANCHOR_MIN_M) is
    off that grid, the section is not regular and uses measured positions.
    Short streets off the grid are usually subdivision streets and are moved
    onto the grid.
    """
    by_num = {}
    for r in runs:
        by_num.setdefault(r.num, []).append(r)
    longest = {n: max(rs, key=lambda r: r.length) for n, rs in by_num.items()}
    anchors = sorted(n for n, r in longest.items()
                     if n % ANCHOR_EVERY == 0 and r.length >= ANCHOR_MIN_M)
    lon = {n: longest[n].mc for n in anchors}
    out = {n: NSStreet(n, lon[n], "anchor") for n in anchors}

    def on_grid(n, x, step):
        return any(abs(r.mc - x) <= ON_GRID_TOL * abs(step)
                   for r in by_num.get(n, []))

    def place(nums, grid_lon, step, label):
        nums = list(nums)
        moved = [n for n in nums
                 if n in by_num and not on_grid(n, grid_lon(n), step)]
        regular = all(longest[n].length < ANCHOR_MIN_M for n in moved)
        log(f"  {label}: {-step * KX / FT:5.1f} ft/number, "
            + ("regular" if regular else "IRREGULAR, measured positions")
            + (f"; off the grid: {moved}" if moved else ""))
        for n in nums:
            if regular:
                how = "grid" if on_grid(n, grid_lon(n), step) else "imputed"
                out[n] = NSStreet(n, grid_lon(n), how)
            elif n in by_num:
                out[n] = NSStreet(n, longest[n].mc, "measured")

    for a, b in zip(anchors, anchors[1:]):
        step = (lon[b] - lon[a]) / (b - a)
        place(range(a + 1, b), lambda n: lon[a] + (n - a) * step, step,
              f"{a:>3}-{b:<3}")

    # Past the first and last anchors, continue the step of the next section
    # for as far as there are streets on it.
    for end, other, direction in ((anchors[0], anchors[1], -1),
                                  (anchors[-1], anchors[-2], 1)):
        step = (lon[end] - lon[other]) / (end - other)
        grid_lon = lambda n: lon[end] + (n - end) * step
        found = [n for n in by_num if (n - end) * direction > 0
                 and on_grid(n, grid_lon(n), step)]
        if found:
            last = max(found, key=lambda n: n * direction)
            nums = range(end + direction, last + direction, direction)
            place(nums, grid_lon, step, f"{end:>3}-{last:<3}")

    # East of the last street on the grid, continue the downtown step to
    # EAST_FIRST. 2nd and 3rd Streets are off the grid, and 1st Street is not
    # in the data.
    end, other = anchors[0], anchors[1]
    step = (lon[end] - lon[other]) / (end - other)
    added = range(min(out) - 1, EAST_FIRST - 1, -1)
    for n in added:
        out[n] = NSStreet(n, lon[end] + (n - end) * step, "imputed")
    if added:
        log(f"  {added[0]:>3}-{EAST_FIRST:<3}: {-step * KX / FT:5.1f} ft/number, "
            "added east of the grid")
    return out


def fit_ew(runs):
    """The E-W grid. The streets have no numbers, so the spacing comes from a
    periodogram of the run positions, weighted by length."""
    best = (0, None, None)
    for i in range(int((EW_MAX_M - EW_MIN_M) / 0.01) + 1):
        sy = EW_MIN_M + i * 0.01
        z = sum(r.length * cmath.exp(2j * math.pi * r.cross / sy) for r in runs)
        if abs(z) > best[0]:
            best = (abs(z), sy, z)
    _, sy, z = best
    y0 = cmath.phase(z) / (2 * math.pi) * sy  # metres north of LAT0
    grid = Grid(LAT0 + y0 / KY, sy / KY)
    return grid.refine(runs, lambda r: round(grid.slot(r.mc)))


def name_ew(grid, runs):
    """Gives each E-W grid slot one street name, and each name one slot.

    Pass 1 uses the runs on the grid, and the name with the most length on a
    slot gets it: a vanity rename of part of a street (Mike Fahey Street on
    Webster Street) loses to the name of the full street. Pass 2 fills the
    empty slots from long streets of an older plat that is off the grid.
    """
    on, off = {}, {}  # (name, slot) -> total run length
    for r in runs:
        k = round(grid.slot(r.mc))
        d = on if grid.on_grid(r.mc, k) else off
        d[r.name, k] = d.get((r.name, k), 0) + r.length
    slots, used, lost, length_of = {}, set(), [], {}
    for cands, min_len, label in ((on, MIN_RUN_M, None), (off, OFF_GRID_MIN_M, "off-grid")):
        for (name, k), length in sorted(cands.items(), key=lambda kv: -kv[1]):
            if name in used or length < min_len:
                continue
            if k in slots:
                lost.append((name, k, length))
                continue
            slots[k] = name
            length_of[k] = length
            used.add(name)
            if label:
                frac = grid.slot(max((r for r in runs if r.name == name),
                                     key=lambda r: r.length).mc) - k
                log(f"  E-W: {name} is {label} ({frac:+.2f} step, "
                    f"{length / FT:.0f} ft), "
                    "put in the empty slot")
    for name, k, length in lost:
        if name not in used:
            log(f"  E-W: {name} ({length / FT:.0f} ft) not used, "
                f"its slot has {slots[k]}")
    # Outside the main block of streets, a small group of nearby streets
    # (for example, a subdivision in the north) must be long to be a grid road.
    ks = sorted(slots)
    groups, cur = [], [ks[0]]
    for k in ks[1:]:
        if k - cur[-1] > ISOLATED_GAP:
            groups.append(cur)
            cur = []
        cur.append(k)
    groups.append(cur)
    for grp in groups:
        if len(grp) < ISOLATED_GROUP:
            for k in grp:
                if length_of[k] < ISOLATED_MIN_M:
                    log(f"  E-W: {slots[k]} ({length_of[k] / FT:.0f} ft) removed, "
                        "a short street away from the grid")
                    del slots[k]
    ks = sorted(slots)
    empty = [k for k in range(ks[0], ks[-1]) if k not in slots]
    log(f"E-W: {len(slots)} named slots, {len(empty)} empty slots")
    return slots


def fix_ew(slots):
    """Applies the manual E-W fixes."""
    slot_of = {name: k for k, name in slots.items()}
    north = slot_of[EW_NORTH_LIMIT]
    for k in [k for k in slots if k >= north]:
        log(f"  E-W: {slots.pop(k)} removed, north of the grid")
    for old, new in EW_RENAMES.items():
        slots[slot_of[old]] = new
        log(f"  E-W: {old} renamed {new}")
    for a, b, name in EW_FILL:
        lo, hi = sorted((slot_of[a], slot_of[b]))
        gap = [k for k in range(lo + 1, hi) if k not in slots]
        assert len(gap) == 1, f"no single empty slot between {a} and {b}"
        for k in [k for k, n in slots.items() if n == name]:
            del slots[k]
        slots[gap[0]] = name
        log(f"  E-W: {name} added between {a} and {b}")


def add_presidents(slots, street_names):
    """Adds artificial E-W streets in the slots south of SOUTH_OF, named after
    PRESIDENTS. A surname is not used if Omaha has a street named
    "<surname> Street" (Lincoln Boulevard does not stop Lincoln), and each
    surname is used one time only."""
    k = min(k for k, name in slots.items() if name == SOUTH_OF)
    used = set()
    for surname in PRESIDENTS:
        if surname in used:
            log(f"  E-W: {surname} not used, the name is already used south "
                f"of {SOUTH_OF}")
            continue
        same = re.compile(rf"(?:{DIRECTIONAL.pattern})?{surname} Street",
                          re.IGNORECASE)
        existing = sorted(n for n in street_names if same.fullmatch(n))
        if existing:
            log(f"  E-W: {surname} not used, Omaha has {', '.join(existing)}")
            continue
        k -= 1
        while k in slots:
            k -= 1
        slots[k] = f"{surname} Street"
        used.add(surname)
        log(f"  E-W: {surname} Street added at slot {k}")


def nominal_ft_per_number(n):
    """The nominal N-S spacing east or west of street number n."""
    for below, ft in NOMINAL_FT_PER_NUMBER:
        if below is None or n < below:
            return ft


def nominal_grid(ns, slots, ew_lat):
    """The grid with the nominal spacings in US survey feet.

    N-S: the nominal width of a section between two anchors is its number of
    streets times nominal_ft_per_number(). The lines in the section, avenues
    included, are evenly spaced over that width. In Benson this gives 330 ft,
    because 48th-60th Street has 16 lines in one mile. The ends of the grid
    (1st Street and the last street) are also section ends.
    Each range line in RANGE_LINES is at its regular place, and the nominal
    widths go west from it. The lines of the west column of a range (the last
    mile before the next range line) are evenly spaced between the nominal
    position of its east line and the next range line. East of the first
    range line and west of the last one, the nominal widths go out from it.
    Returns ({number: lon}, {slot: lat}).
    """
    keys = sorted(ns)
    ends = sorted({n for n, st in ns.items() if st.how == "anchor"}
                  | {keys[0], keys[-1]})
    pos = {ends[0]: 0.0}  # feet west of the east end
    for a, b in zip(ends, ends[1:]):
        width = (b - a) * nominal_ft_per_number(a)
        lines = [n for n in keys if a <= n <= b]
        for i, n in enumerate(lines):
            pos[n] = pos[a] + width * i / (len(lines) - 1)
    ranges = [r for r in RANGE_LINES if r in ns]

    def from_pin(r, n):  # nominal widths out from range line r
        return ns[r].lon - (pos[n] - pos[r]) * FT / KX

    ns_nom = {r: ns[r].lon for r in ranges}
    for n in keys:
        if n <= ranges[0]:
            ns_nom[n] = from_pin(ranges[0], n)
        elif n >= ranges[-1]:
            ns_nom[n] = from_pin(ranges[-1], n)
    for east, west in zip(ranges, ranges[1:]):
        col = west - 12  # the east line of the west column
        for n in keys:
            if east < n <= col:
                ns_nom[n] = from_pin(east, n)
        lines = [n for n in keys if col <= n <= west]
        for i, n in enumerate(lines[1:-1], 1):
            ns_nom[n] = (ns_nom[col] + (ns[west].lon - ns_nom[col])
                         * i / (len(lines) - 1))
        log(f"  nominal range {east}-{west}: west column {col}-{west} is "
            f"{(ns_nom[col] - ns[west].lon) * KX / FT:.0f} ft (nominal 5280)")

    k0 = next(k for k, name in slots.items() if name == NOMINAL_PIN_EW)
    step = NOMINAL_EW_FT * FT / KY
    ew_nom = {k: ew_lat[k0] + (k - k0) * step for k in slots}
    return ns_nom, ew_nom


def ns_lines(ns, lon, lat):
    """N-S lines, east to west, over the latitudes of the E-W streets."""
    lat_s, lat_n = min(lat.values()), max(lat.values())
    return [line(ns[n].name, (lat_s, lon[n]), (lat_n, lon[n]))
            for n in sorted(ns)]


def ew_lines(slots, lat, lon):
    """E-W lines, north to south, from WEST_LIMIT to the east end."""
    lon_w, lon_e = lon[WEST_LIMIT], lon[min(lon)]
    return [line(slots[k], (lat[k], lon_w), (lat[k], lon_e))
            for k in sorted(slots, reverse=True)]


def line(name, a, b):
    return {"name": name,
            "points": [[round(c, 6) for c in a], [round(c, 6) for c in b]]}


def continuation(ns, lon, slots, ew_step):
    """How the grid continues past its last line in each direction.

    The spacing is the nominal value in US survey feet. "measured_ft" is the
    spacing of the regular grid at that end (N-S) or of the whole E-W grid,
    for comparison. N-S lines are meridians and E-W lines are parallels.
    """
    keys = sorted(ns)
    east, west = keys[0], keys[-1]
    k_n, k_s = max(slots), min(slots)

    def ns_end(end, other, direction, next_name):
        step_ft = abs(lon[end] - lon[other]) / abs(end - other) * KX / FT
        ft = nominal_ft_per_number(end if direction == "east" else end - 1)
        return {"from": ns[end].name, "direction": direction, "lines": "N-S",
                "spacing_ft": ft, "spacing_m": round(ft * FT, 3),
                "spacing_deg": round(ft * FT / KX, 8),
                "measured_ft": round(step_ft, 1), "next": next_name,
                "description": f"N-S lines (meridians) every {ft} ft to the "
                               f"{direction} of {ns[end].name}: {next_name}, "
                               "and so on."}

    def ew_end(k, direction):
        ft = NOMINAL_EW_FT
        return {"from": slots[k], "direction": direction, "lines": "E-W",
                "spacing_ft": ft, "spacing_m": round(ft * FT, 3),
                "spacing_deg": round(ft * FT / KY, 8),
                "measured_ft": round(ew_step * KY / FT, 1),
                "description": f"E-W lines (parallels) every {ft} ft to the "
                               f"{direction} of {slots[k]}."}

    # The 6 numbers next to each end, to measure the regular spacing there.
    east_ref = min(n for n in keys if n >= east + 6 and n == int(n))
    west_ref = max(n for n in keys if n <= west - 6 and n == int(n))
    return {
        "east": ns_end(east, east_ref, "east",
                       f"{ordinal(int(east) - 1)} Street"),
        "west": ns_end(west, west_ref, "west",
                       f"{ordinal(int(west) + 1)} Street"),
        "north": ew_end(k_n, "north"),
        "south": ew_end(k_s, "south"),
    }


def main():
    ways = fetch_ways()
    log(f"{len(ways)} named ways in Douglas County")

    numbered = [w for w in ways if NUMBERED.match(w["name"])]
    ns_runs = all_runs(group_segments(
        numbered, keep=lambda axis, seg: axis == "NS"
        and NS_LAT_MIN <= (seg.p[1] + seg.q[1]) / 2 <= NS_LAT_MAX))
    # Far west, some streets exist only north of NS_LAT_MAX.
    in_band = {r.num for r in ns_runs}
    ns_runs += [r for r in all_runs(group_segments(
                    numbered, keep=lambda axis, seg: axis == "NS"))
                if r.num not in in_band]
    log("N-S sections:")
    ns = build_ns(ns_runs)
    n_min, n_max = min(ns), max(ns)
    for n, (a, b) in NS_REGRID.items():
        ns[n] = NSStreet(n, ns[a].lon + (n - a) * (ns[b].lon - ns[a].lon) / (b - a),
                         "manual")
    for n in NS_AVENUES:
        ns[n + 0.5] = NSStreet(n + 0.5, (ns[n].lon + ns[n + 1].lon) / 2,
                               "manual", f"{ordinal(n)} Avenue")
    for how in ("anchor", "grid", "imputed", "measured", "manual"):
        nums = sorted(n for n, st in ns.items() if st.how == how)
        log(f"  {how}: {len(nums)}" + (f" {nums}" if how != "grid" else ""))
    missing = [n for n in range(n_min, n_max) if n not in ns]
    log(f"N-S: {n_min}..{n_max}, not placed: {missing}")

    # E-W streets only east of the 72nd Street grid line.
    lon_limit = ns[WEST_LIMIT].lon
    ew_runs = all_runs(group_segments(
        ways, keep=lambda axis, seg: axis == "EW" and seg.q[0] >= lon_limit))
    ew = fit_ew(ew_runs)
    log(f"E-W grid: {ew.step * KY / FT:.2f} ft per street, "
        f"{5280 * FT / (ew.step * KY):.3f} streets per mile")
    slots = name_ew(ew, ew_runs)
    fix_ew(slots)
    log(f"E-W: artificial streets south of {SOUTH_OF}:")
    add_presidents(slots, fetch_street_names())

    ns_lon = {n: st.lon for n, st in ns.items()}
    ew_lat = {k: ew.at(k) for k in slots}
    ns_nom, ew_nom = nominal_grid(ns, slots, ew_lat)
    result = {
        "ns": ns_lines(ns, ns_lon, ew_lat),
        "ew": ew_lines(slots, ew_lat, ns_lon),
        "ns_nominal": ns_lines(ns, ns_nom, ew_nom),
        "ew_nominal": ew_lines(slots, ew_nom, ns_nom),
        "continuation": continuation(ns, ns_lon, slots, ew.step),
    }
    json.dump(result, sys.stdout, indent=1)
    print()

if __name__ == "__main__":
    main()
