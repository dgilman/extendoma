#!/opt/local/bin/python3.14
"""Derive the base street grid of east Omaha from OpenStreetMap.

Reads the OSM layercake parquet files with the duckdb CLI and writes to stdout:
    [{"name": "90th Street", "points": [[lat, lon], [lat, lon]]}, ...]
The two points of each street set its great circle.

N-S numbered streets come from all of Douglas County. E-W named streets come
only from east of 72nd Street, where the base grid is not changed by the
West Dodge / West Maple / West Center transitions. Diagnostics go to stderr.
"""

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
LONG_RUN_M = 1500  # runs used to measure the standard N-S spacing
ANCHOR_NS = 72

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
    """A long contiguous part of one street, with a fitted center line."""

    def __init__(self, axis, name, num, segs):
        self.axis, self.name, self.num = axis, name, num
        self.a1 = min(s.a1 for s in segs)
        self.a2 = max(s.a2 for s in segs)
        self.length = self.a2 - self.a1
        w = sum(s.length for s in segs)
        self.cross = sum(s.cross * s.length for s in segs) / w
        # Center line, in degrees: lon = f(lat) for N-S, lat = f(lon) for E-W.
        # The slope is the mean direction of the segments, not a fit of their
        # positions, so that a jog in the street does not tilt the line.
        self.i = i = 1 if axis == "NS" else 0  # index of the along coordinate
        self.ma = sum((s.p[i] + s.q[i]) / 2 * s.length for s in segs) / w
        self.mc = sum((s.p[1 - i] + s.q[1 - i]) / 2 * s.length for s in segs) / w
        self.slope = (sum(s.q[1 - i] - s.p[1 - i] for s in segs)
                      / sum(s.q[i] - s.p[i] for s in segs))
        lo = min(min(s.p[i], s.q[i]) for s in segs)
        hi = max(max(s.p[i], s.q[i]) for s in segs)
        self.set_ends(lo, hi)

    def set_ends(self, lo, hi):
        """Sets the output points ([lat, lon]) at along coordinates lo, hi."""
        ends = [(a, self.mc + self.slope * (a - self.ma)) for a in (lo, hi)]
        self.points = [[a, c] if self.i == 1 else [c, a] for a, c in ends]


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


def ns_spacing(by_num):
    """Theil-Sen median of metres per street number, from long runs."""
    best = {n: max(rs, key=lambda r: r.length) for n, rs in by_num.items()}
    best = {n: r for n, r in best.items() if r.length >= LONG_RUN_M}
    nums = sorted(best)
    slopes = [(best[m].cross - best[n].cross) / (n - m)
              for i, m in enumerate(nums) for n in nums[i + 1:] if n - m >= 12]
    return statistics.median(slopes)


def step_ns(by_num, spacing):
    """Walks from the anchor to each next numbered street, east and west."""
    anchor = max(by_num[ANCHOR_NS], key=lambda r: r.length)
    found = {ANCHOR_NS: anchor}
    for direction, stop in ((-1, min(by_num) - 1), (1, max(by_num) + 1)):
        last = anchor
        for n in range(ANCHOR_NS + direction, stop, direction):
            steps = abs(n - last.num)
            predicted = last.cross - (n - last.num) * spacing  # east is +x
            tol = 0.6 * spacing * steps
            cands = [r for r in by_num.get(n, [])
                     if abs(r.cross - predicted) <= tol]
            if not cands:
                if n in by_num:
                    log(f"  {n}: runs exist, but none near the grid position")
                continue
            last = found[n] = max(cands, key=lambda r: r.length)
    missing = [n for n in range(min(found), max(found)) if n not in found]
    log(f"N-S: {len(found)} streets, {min(found)}..{max(found)}; "
        f"no long run for: {missing}")
    return found


def check_block_width(found, spacing):
    log("N-S block-width check:")
    for m in sorted(n for n in found if n % 12 == 0):
        if m + 12 in found:
            w = found[m].cross - found[m + 12].cross
            log(f"  {m:>3}-{m + 12:<3} mile section: {w:7.1f} m, "
                f"{w / 12:6.1f} m/number")
    for label, side in (("east of", lambda n: n < ANCHOR_NS),
                        ("west of", lambda n: n >= ANCHOR_NS)):
        nums = sorted(n for n in found if side(n))
        steps = [(found[a].cross - found[b].cross) / (b - a)
                 for a, b in zip(nums, nums[1:])]
        if steps:
            log(f"  median spacing {label} {found[ANCHOR_NS].name}: "
                f"{statistics.median(steps):.1f} m/number ({len(steps)} pairs)")
    miles = sorted(n for n in found if n % 12 == 0)
    mean_n = statistics.fmean(miles)
    mean_x = statistics.fmean(found[n].cross for n in miles)
    b = (sum((n - mean_n) * (found[n].cross - mean_x) for n in miles)
         / sum((n - mean_n) ** 2 for n in miles))
    log(f"  mile-line fit: {-b:.1f} m/number")
    off = [(n, found[n].cross - (mean_x + b * (n - mean_n))) for n in sorted(found)]
    off = [(n, d) for n, d in off if abs(d) > 0.5 * spacing]
    log(f"  {len(off)} streets more than {0.5 * spacing:.0f} m from the "
        "regular grid: " + ", ".join(f"{n} ({d:+.0f} m)" for n, d in off))


def longest_per_name(runs):
    best = {}
    for r in runs:
        if r.name not in best or r.length > best[r.name].length:
            best[r.name] = r
    return list(best.values())


def ew_spacing(runs):
    """Median gap between adjacent long E-W streets."""
    ys = sorted(r.cross for r in runs if r.length >= 2 * MIN_RUN_M)
    return statistics.median(b - a for a, b in zip(ys, ys[1:]) if b - a > 30)


def select_ew(runs, spacing):
    """Keeps one run per street name, longest streets first.

    East Omaha is a patchwork of plats with their own offsets, so there is no
    single E-W origin to step from. A run is dropped only if a longer street
    covers the same longitudes less than 0.4 * spacing north or south of it.
    """
    found = []
    for r in sorted(runs, key=lambda r: -r.length):
        clash = next((f for f in found
                      if abs(f.cross - r.cross) < 0.4 * spacing
                      and min(f.a2, r.a2) > max(f.a1, r.a1)), None)
        if clash:
            log(f"  E-W: dropped {r.name} ({r.length:.0f} m), "
                f"{r.cross - clash.cross:+.0f} m from {clash.name}")
        else:
            found.append(r)
    found.sort(key=lambda r: r.cross)
    for a, b in zip(found, found[1:]):
        if b.cross - a.cross > 1.5 * spacing:
            log(f"  E-W gap: {b.cross - a.cross:.0f} m between "
                f"{a.name} and {b.name}")
    log(f"E-W: {len(found)} streets")
    return found


def main():
    ways = fetch_ways()
    log(f"{len(ways)} named ways in Douglas County")

    ns_groups = group_segments([w for w in ways if NUMBERED.match(w["name"])],
                               keep=lambda axis, seg: axis == "NS")
    by_num = {}
    for r in all_runs(ns_groups):
        by_num.setdefault(r.num, []).append(r)
    spacing = ns_spacing(by_num)
    log(f"N-S standard spacing: {spacing:.1f} m/number")
    found_ns = step_ns(by_num, spacing)
    check_block_width(found_ns, spacing)

    # E-W streets only east of the 72nd Street line.
    line_w = found_ns[ANCHOR_NS].points  # [[lat, lon], [lat, lon]]
    (la1, lo1), (la2, lo2) = line_w

    def east_of_limit(axis, seg):  # any part of it; run ends are cut below
        lon, lat = seg.q  # the east end of an E-W segment
        return axis == "EW" and lon >= lo1 + (lo2 - lo1) * (lat - la1) / (la2 - la1)

    ew_runs = longest_per_name(all_runs(group_segments(ways, keep=east_of_limit)))
    ew_sp = ew_spacing(ew_runs)
    log(f"E-W standard spacing: {ew_sp:.1f} m")
    found_ew = select_ew(ew_runs, ew_sp)
    for r in found_ew:  # a segment can cross the limit line; cut it there
        (lat_w, lon_w), (lat_e, lon_e) = r.points
        limit = lo1 + (lo2 - lo1) * (lat_w - la1) / (la2 - la1)
        if lon_w < limit:
            r.set_ends(limit, lon_e)

    def fmt(r):
        return {"name": r.name,
                "points": [[round(lat, 6), round(lon, 6)] for lat, lon in r.points]}

    out = [fmt(found_ns[n]) for n in sorted(found_ns)]
    out += [fmt(r) for r in sorted(found_ew, key=lambda r: -r.cross)]
    json.dump(out, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
