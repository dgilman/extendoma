#!/opt/local/bin/python3.14
"""Derive the base street grid of east Omaha from OpenStreetMap.

Reads the OSM layercake parquet files with the duckdb CLI and writes to stdout:
    [{"name": "90th Street", "points": [[lat, lon], [lat, lon]]}, ...]
The two points of each street set its great circle.

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
KX = 111320 * math.cos(math.radians(LAT0))  # metres per degree of longitude
KY = 111047  # metres per degree of latitude

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
    def __init__(self, num, lon, how):
        self.num, self.lon, self.how = num, lon, how


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
        log(f"  {label}: {-step * KX:5.1f} m/number, "
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
        log(f"  {added[0]:>3}-{EAST_FIRST:<3}: {-step * KX:5.1f} m/number, "
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
                log(f"  E-W: {name} is {label} ({frac:+.2f} step, {length:.0f} m), "
                    "put in the empty slot")
    for name, k, length in lost:
        if name not in used:
            log(f"  E-W: {name} ({length:.0f} m) not used, "
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
                    log(f"  E-W: {slots[k]} ({length_of[k]:.0f} m) removed, "
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
    for how in ("anchor", "grid", "imputed", "measured"):
        nums = sorted(n for n, st in ns.items() if st.how == how)
        log(f"  {how}: {len(nums)}" + (f" {nums}" if how != "grid" else ""))
    missing = [n for n in range(n_min, n_max) if n not in ns]
    log(f"N-S: {n_min}..{n_max}, not placed: {missing}")

    # E-W streets only east of the 72nd Street grid line.
    lon_limit = ns[WEST_LIMIT].lon
    ew_runs = all_runs(group_segments(
        ways, keep=lambda axis, seg: axis == "EW" and seg.q[0] >= lon_limit))
    ew = fit_ew(ew_runs)
    log(f"E-W grid: {ew.step * KY:.2f} m per street, "
        f"{1609.344 / (ew.step * KY):.3f} streets per mile")
    slots = name_ew(ew, ew_runs)
    fix_ew(slots)
    log(f"E-W: artificial streets south of {SOUTH_OF}:")
    add_presidents(slots, fetch_street_names())

    lat_s, lat_n = ew.at(min(slots)), ew.at(max(slots))
    lon_w, lon_e = lon_limit, ns[n_min].lon

    def line(name, a, b):
        return {"name": name,
                "points": [[round(c, 6) for c in a], [round(c, 6) for c in b]]}

    out = [line(f"{ordinal(n)} Street", (lat_s, ns[n].lon), (lat_n, ns[n].lon))
           for n in sorted(ns)]
    out += [line(slots[k], (ew.at(k), lon_w), (ew.at(k), lon_e))
            for k in sorted(slots, reverse=True)]
    json.dump(out, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
