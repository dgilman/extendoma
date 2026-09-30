# Omaha street grid: session notes

Date: 2026-09-30

## Goal

Make a program that finds the base street grid of east Omaha, Nebraska, from OpenStreetMap (OSM) data. The output is a JSON object:

```json
{
  "ns": [{"name": "1st Street", "kind": "meridian", "lon": lon, "points": [[lat, lon], [lat, lon]]}, ...],
  "ew": [{"name": "Girard Street", "kind": "parallel", "lat": lat, "points": [[lat, lon], [lat, lon]]}, ...],
  "ns_nominal": [...],
  "ew_nominal": [...],
  "continuation": {"east": {...}, "west": {...}, "north": {...}, "south": {...}}
}
```

- `ns`: the N-S (numbered) streets, from east to west. 1st Street is item 0, 2nd Street is item 1, and so on. An avenue comes after the street with the same number.
- `ew`: the E-W (named) streets, from north to south. Girard Street is item 0.
- `ns_nominal`, `ew_nominal`: the same streets in the same order, on the nominal grid (the spacings in US survey feet).
- `continuation`: how the grid continues past its last line in each direction. Each item has `from` (the last street), `direction`, `lines` (`N-S` or `E-W`), `spacing_ft` (the nominal value), `spacing_m`, `spacing_deg` (degrees of longitude for N-S, of latitude for E-W), `measured_ft` (the regular grid at that end), and a `description`. For east and west, `next` is the name of the next street (for example, `0th Street` east of 1st Street).

- Each N-S street is a meridian (`"kind": "meridian"`, with its longitude in `lon`). A meridian is a great circle, and the two points set it.
- Each E-W street is a parallel of latitude (`"kind": "parallel"`, with its latitude in `lat`). A parallel is a small circle, not a great circle. Do not project it as the great circle through its two points: that line is 7 m from the parallel at 10 km, 170 m at 50 km and 1.5 km at 150 km.

`extendny.js` extends the Manhattan grid over the globe. This data is the equivalent input for Omaha.

## Files

| File | Contents |
|---|---|
| `extract_grid.py` | The program. Python 3.14, standard library only. It runs the duckdb CLI in a subprocess (`duckdb -init /dev/null -json`), because the duckdb Python module is not installed. |
| `omaha_grid.json` | The output. |
| `make_grid_view.py` | Makes `grid_view.html` from `omaha_grid.json` (started by David). It reads the new format and the older list format. |
| `grid_view.html` | The map viewer. It draws the N-S lines as great circles (meridians) and the E-W lines as parallels, 150 km each way. Radio buttons change between the regular and the nominal grid. Check boxes show the N-S lines, the E-W lines, the source segments, and 5 dashed continuation lines past each end. |

Run it with:

```sh
/opt/local/bin/python3.14 extract_grid.py > omaha_grid.json
```

It takes about 15 s. The diagnostics go to stderr, in US survey feet. To make the map:

```sh
/opt/local/bin/python3.14 make_grid_view.py omaha_grid.json grid_view.html
python3.14 -m http.server  # OSM does not send map tiles to a file:// page
```

## Data sources

- `https://data.openstreetmap.us/layercake/highways.parquet`: OSM ways with `name`, `highway`, a `bbox` struct and `geometry` (CRS84). A query with a bbox filter on the remote file takes about 2 s.
- `https://data.openstreetmap.us/layercake/boundaries.parquet`: Douglas County is `id = 1425409` (admin_level 6, wikidata Q490695). The `name` column is `VARCHAR[]`.

The program clips all ways to the Douglas County polygon. This removes the numbered streets of Council Bluffs, Iowa. The polygon stops at the Missouri River in the east and at Harrison Street in the south.

## How the program works

1. **Fetch.** One SQL query gets the named ways of the classes trunk, primary, secondary, tertiary, unclassified, residential and living_street that are in the county.
2. **Segments.** The program breaks each way into 2-point segments. It keeps a segment only if its bearing is within 3° of N-S or E-W.
3. **Names.**
   - N-S: `North 90th Street` and `South 90th Street` become `90th Street`.
   - E-W: the program removes names with a direction prefix (`West Dodge Road` and similar), names with digits, and names that end in Highway, Expressway, Parkway or Freeway.
4. **Contiguous runs.** For each street name, the program joins the segments that are less than 30 m apart across the street (the two carriageways of a divided road). Along the street, a gap of more than 60 m starts a new run. It keeps only runs of 300 m or more.
5. **N-S grid (numbered streets).**
   - Anchors: the mile and half-mile roads (every 6 numbers) that have a run of 1.5 km or more.
   - Between two anchors, the streets are a regular grid. The width of each section comes from its two anchors.
   - If a long street (1.5 km or more) is off the grid of its section, the section is irregular. Then its streets use their measured positions. Short streets off the grid are usually subdivision streets, so the program moves them onto the grid.
   - The program fills in numbers that have no street on the grid.
   - It measures the streets only south of 41.30°N if they exist there, because north of that line the roads west of 132nd move west by 15–50 m.
6. **E-W grid (named streets, east of 72nd Street only).**
   - The E-W streets have no numbers. The program finds the spacing with a periodogram of the run positions, weighted by length.
   - It then refits the grid by least squares on the runs within 25% of a step from a grid line.
   - Naming, pass 1: each grid slot gets the name that has the most on-grid length there. Each name is used one time only. Thus a vanity rename of part of a street (Mike Fahey Street on Webster Street) loses to the name of the full street.
   - Naming, pass 2: slots that are still empty get long streets (600 m or more) from an older plat that is off the grid.
   - A group of fewer than 3 streets that is more than 3 slots from the other streets is removed if a street in it is shorter than 1 km.
7. **Manual E-W fixes** (constants near the top of the file):
   - `EW_NORTH_LIMIT`: remove Young Street and all streets north of it.
   - `EW_RENAMES`: rename Mary Street to Titus Avenue.
   - `EW_FILL`: add Ohern Street between O and P Streets, and Gold Street between Arbor and Bancroft Streets. The program stops with an error if there is not exactly one empty slot between the two streets.
8. **Manual N-S fixes** (constants near the top of the file):
   - `EAST_FIRST`: the downtown grid continues east of 4th Street to 1st Street, with the step of 6th–12th Street.
   - `NS_REGRID`: 55th and 57th Streets go on the straight line from 52nd to 60th Street.
   - `NS_AVENUES`: 48th, 49th, 50th, 51st, 60th and 65th Avenues, each halfway between the nth and the (n+1)th Street.
9. **Presidents** (added by David): south of Harrison Street, the E-W grid continues with artificial streets that have the names of the presidents after William Henry Harrison.
10. **Nominal grid.** The same streets on a grid with the nominal spacings in US survey feet:
    - N-S: the nominal width of a section between two anchors is its number of streets times 360 ft (east of 12th Street), 344 ft (12th–24th Street) or 440 ft (west of 24th Street). The lines in the section, avenues included, are evenly spaced over that width. In Benson this gives 330 ft, because 48th–60th Street has 16 lines in one mile.
    - The PLSS range lines (`RANGE_LINES`: 60th, 132nd, 204th and 276th Street) are at their regular positions. From each range line, the nominal widths go west. The west column of each range (the last mile before the next range line) takes the error: its lines are evenly spaced between the nominal position of its east line and the next range line. East of 60th Street and west of 276th Street, the nominal widths go out from that range line.
    - E-W: 330 ft, with Dodge Street at its regular position.
11. **Output.** All N-S lines are meridians and all E-W lines are parallels, so all lines are parallel and on the grid.

The program converts degrees to metres with the WGS84 ellipsoid at 41.26°N: 83,804 m per degree of longitude and 111,059 m per degree of latitude.

## Changes during the session

| Step | Change | Reason |
|---|---|---|
| 1 | The program used the measured line through the longest run of each street. | First version. |
| 2 | One global regular grid for each axis. | David: the lines must be parallel and evenly spaced (R Street was at an angle and collided with S Street). |
| 3 | Name each slot from the on-grid length. | David: identify vanity renames and streets that are not on the grid. |
| 4 | Fill in the missing numbered streets. | David: after the grid is regular, the program can interpolate (for example 148th and 149th). |
| 5 | One grid for each mile or half-mile section. | David: the west grid is a little wider than one global grid. The sections are 127–139 m per number. |
| 6 | A section is irregular only if a long street is off its grid. | David: 108th and 120th are exactly right, so interpolate between them evenly. |
| 7 | Measure the N-S streets south of 41.30°N. | David: 156th, 168th and 180th were too far west. |
| 8 | Manual E-W fixes. | David's review of the map. |
| 9 | Continue the downtown grid east to 1st Street. | David. |
| 10 | Add the avenues, and put 55th and 57th Streets on the Benson grid. | David: the avenues are part of the grid. 55th and 57th Streets are the odd ones in Benson. The problem is how the city developed, not the OSM data. |
| 11 | Use the WGS84 ellipsoid to convert to metres. | The first formula (a sphere) gave east-west distances 0.14% too small. The output lines did not change, only the metre values in the diagnostics. |
| 12 | New output format (`ns`, `ew`, `continuation`), diagnostics in feet, and a nominal grid. The viewer can change between the regular and the nominal grid. | David. The survey units are the best description of the pattern. |
| 13 | Pin the nominal N-S grid at each PLSS range line, not only at 72nd Street. The west column of each range takes the error. | The nominal grid moved up to 443 ft from the regular grid in the west. The main roads showed that the alignment starts again at each range line. |
| 14 | Add `kind` (`meridian` or `parallel`) and `lon` or `lat` to each street. The viewer draws the E-W lines as parallels. | The review of `extendny.js`: its streets are small circles (parallels of a rotated globe). The viewer drew the E-W lines as great circles, which were 1.5 km from their parallel at 150 km. |

## Findings

### The N-S grid

- The mile roads (24th, 36th … 324th) are about 1 mile apart. The median is 5272 ft. The sections change from 127 m to 139 m per number.
- 12th to 24th Street (downtown) is a separate regular grid at 344.5 ft per number. 6th to 12th Street is 360.2 ft per number. This comes from the original 1854 town plat.
- 48th to 60th Street (Benson) does not fit the 1/12-mile grid. In Benson, the numbered streets and the avenues between them make a grid at about 330 ft. 52nd to 60th Street is a regular grid at 331.0 ft per number, except 55th and 57th Streets, which are now moved onto it. 61st to 65th Street also use measured positions, because the 2.2 km run of 65th Street is 63 m off the 1/12-mile grid. David found these results acceptable. He thinks that the cause is how the city developed.
- 264th to 276th Street: 270th Street is off by half a step, so 264th–270th (138.8 m per number) and 270th–276th (115.6 m per number) are separate sections.
- North of about 41.30°N, the N-S roads west of 132nd Street move 15–50 m west. The latitude where this starts is not the same on each road (Dodge for 168th, Maple for 144th, Fort for 156th).
- The streets 110th–112th, 195th–202nd and others are short subdivision streets with the wrong position or number. Thus a regular grid is better than the measured positions.

### The E-W grid

- The spacing is 100.58 m (329.96 ft), which is 16.001 streets per mile (1/16 mile).
- The E-W mile roads are exactly 16 slots apart: Harrison, Q, F, West Center, Pacific, West Dodge, Blondo, Fort, State, Rainwood, Bennington.
- West of about 48th Street, the E-W streets are on the grid. East of it, the older plats (downtown and South Omaha) have their own offsets. For example, Harney Street is at slot −3.36 from Dodge, and the South Omaha letter streets are about half a step off.
- The E-W names are the same in different longitudes only in part. Many streets at the same latitude have different names in different plats (Shirley Street and Center Street, Underwood Avenue and California Street).
- "West X Road" streets are at different latitudes than the streets with the same names in east Omaha. For example, Dodge Street is at 41.2597°N and West Dodge Road is at 41.2631°N. West Dodge Road and West Center Road are the mile roads of the regular grid. The east streets with the same names are not.

### Survey units

The city was laid out in US survey units (1 chain = 66 ft, 1 mile = 80 chains = 5280 ft). In these units, most of the spacings are round numbers:

| Grid | Measured | Round value |
|---|---|---|
| E-W streets | 329.96 ft (16 streets = 5279 ft) | 330 ft = 5 chains = 1/16 mile |
| N-S numbered streets, 24th–324th | 438.6 ft per number | 440 ft = 1/12 mile |
| N-S mile sections | median 5272 ft, mean 5263 ft | 5280 ft = 1 mile |
| Downtown, 12th–24th Street | 344.5 ft | 344 ft = 264 ft block (4 chains) + 80 ft street |
| Downtown, 6th–12th Street | 360.2 ft | 360 ft |
| Benson, 52nd–60th Street | 331.0 ft | 330 ft = 5 chains |

- The E-W grid is exact to less than 1 ft per mile.
- The mile sections are from 5192 ft to 5338 ft, which is within about 1% of a mile. This is usual for chain surveys of the 1850s. 264th–276th Street (5014 ft) contains the jog at 270th Street.
- The 264 ft block with an 80 ft street is a usual plat layout of the time. I did not verify it against the Omaha town plat records.

### Main roads and PLSS range lines

These values come directly from the OSM road geometry, not from the grid. For each pair of adjacent main roads, the program sampled the latitude every 0.001° (about 365 ft) and took the distance between the two roads at each latitude. Samples more than 200 ft from the median of the section are removed. Those are places where a short subdivision street with the same number is at the sampled latitude.

| Section | Width (trimmed mean) | Change from south to north |
|---|---|---|
| 120th–132nd | 5231 ft | +96 ft per 0.1° |
| 132nd–144th | 5274 ft | +9 |
| 144th–156th | 5306 ft | −15 |
| 156th–168th | 5281 ft | +5 |
| 168th–180th | 5282 ft | +7 |
| 180th–192nd | 5291 ft | +15 |
| 192nd–204th | 5173 ft | −56 |

- **132nd to 192nd Street:** average 5286.7 ft, 6.7 ft (0.13%) more than a mile. The standard deviation between sections is 12.5 ft, and the range is 5274–5306 ft.
- **The sections are measured along the full roads.** On the grid, which is measured south of 41.30°N, 156th–168th and 168th–180th looked 36 ft narrower and 51 ft wider than the mean. Along the full roads both are about 5281 ft.
- **204th Street is Main Street in Elkhorn and it is not on the Omaha alignment.** A 5280 ft grid pinned at 204th Street fits the mile roads from 120th to 192nd Street with an RMS error of 163 ft. Pinned at 120th Street, the RMS error is 43 ft. With the best offset it is 24 ft.
- **60th, 132nd, 204th and 276th Street are PLSS range lines.** A range is a 6-mile column of townships. Scaled to the 40°N baseline, these roads are 72.4, 66.3, 60.2 and 54.1 miles east of the 6th Principal Meridian, which is almost exactly 72, 66, 60 and 54 miles. This is my conclusion from the 6-mile pattern. I did not check it against the survey plats.
- **Each range was surveyed on its own.** The alignment starts again at each range line. For example, 216th to 264th Street agree with 204th Street within −42 to +73 ft, but they are 88–204 ft from the best-fit alignment of 120th to 192nd Street.
- **The west column of a range takes the error.** This agrees with the PLSS rule that sections are laid out from the east boundary of a township. Two of the three west columns are short: 192nd–204th is 5173 ft (107 ft short) and 120th–132nd is 5231 ft (49 ft short). 264th–276th is also short, but it contains the jog at 270th Street.
- **Convergence of the meridians.** A range 6 miles wide becomes about 49 ft narrower for each 0.1° to the north. 192nd–204th becomes 56 ft narrower for each 0.1°, which agrees. 120th–132nd becomes wider to the north, so the test does not confirm that one.

### How `extendny.js` extends the Manhattan grid

- It rotates the globe so that 1st Avenue (heading 28.8°) is a meridian. The grid poles are the points a quarter of the circumference from Manhattan in the direction of 1st Avenue and in the opposite direction.
- Avenues are great circles through the two grid poles, so they are the meridians of the rotated globe. On the grid's equator, which goes through Manhattan, they are `circumference / 160000` ≈ 250.5 m apart. Away from it, they converge toward the grid poles.
- Streets are small circles around the grid's south pole, so they are the parallels of the rotated globe. They are `circumference / 508000` ≈ 78.9 m apart everywhere, and they never cross.
- There is no correction for surveys or for the irregular parts of Manhattan. The grid is set by one origin, one heading and two spacings.

For Omaha:

- **The rotation is zero.** The Omaha grid runs true north, so the grid poles are the geographic poles. The extended Omaha grid is the latitude and longitude graticule at a fixed spacing: meridians every 440 ft (measured at Omaha) and parallels every 330 ft.
- **The N-S lines converge automatically.** They are meridians. The PLSS range lines and short west columns were the surveyors' way to keep square miles on the ground against that convergence. Thus the extension does not need them, and the nominal continuation uses even spacing.
- **The E-W lines must stay parallels.** All great circles that go due east at the Omaha meridian meet at two points on the equator, 90° of longitude east and west of Omaha. Thus a great circle cannot extend an E-W street.

### Curvature of the earth

The Omaha mile roads are section lines of the Public Land Survey System (PLSS), measured from the 6th Principal Meridian (97°22′W, baseline 40°N). Meridians converge to the north, so surveyors reset the N-S lines at standard parallels (correction lines). North of a correction line, and east of the principal meridian, the N-S lines are shifted east.

Evidence in the data:

1. **All N-S roads jog east at the Douglas–Washington County line (41.393°N).** The jog is 180–230 m from 36th to 192nd Street, and 45–65 m from 276th to 324th Street. The direction agrees with a correction line, and the jog is larger farther from the principal meridian. The line is about 96 miles north of the baseline, which agrees with standard parallels every 24 miles. I did not verify this against survey records. Simple geometry gives a jog of about 600 m at 72nd Street, not 195 m, and the jog does not decrease evenly with distance. Different survey contracts probably cause this.
2. **There is no correction line inside Douglas County.** 204th Street is one straight 20.8 km run from Harrison Street to the north county line. 72nd Street and 132nd Street are also straight.
3. **Convergence inside the county is too small to see.** Over 22 km from south to north, a 1-mile section becomes only about 5 m narrower. The road positions change by 15–50 m.
4. **The west shift north of 41.30°N is probably not a curvature correction.** It is in the wrong direction and it does not start on one line.
5. **The E-W grid needs no correction.** Parallels of latitude do not converge.

Result: inside Douglas County, meridians with a width for each section are a correct model. North of 41.393°N, the grid must shift about 200 m east.

## Output (last run)

- 330 N-S lines and 166 E-W lines (496 in total), for the regular grid and for the nominal grid.
- Nominal minus regular, N-S: RMS 33 ft over the 330 lines, and at most 55 ft (240th Street), except near 270th Street (239 ft), where the regular grid has the jog. Before the nominal grid was pinned at the range lines, the difference was as much as 443 ft (300th Street).
- The west columns on the nominal grid are 5191 ft (120th–132nd), 5175 ft (192nd–204th) and 5034 ft (264th–276th).
- Nominal minus regular, E-W: within 3 ft.
- Continuation: east of 1st Street 360 ft (measured 360.2 ft), west of 324th Street 440 ft (measured 442.3 ft), north of Girard Street and south of Biden Street 330 ft (measured 330.0 ft).
- N-S: 1st to 324th Street, with no numbers missing, and the 6 avenues. 1st to 3rd Street continue the downtown step.
  - The endpoints of the N-S lines are the edges of the grid, not the ends of the streets.
- E-W, north to south: Girard Street to Harrison Street, then the president streets to Biden Street.
  - The E-W lines go from 72nd Street to 1st Street.

## Open items

From `todo`:

- Mark the irregular streets in the output, so that the map can switch them.
- Final product: a map with the lines. The user can change between the irregular and regular streets, and can show only the core Omaha grid (with no generalizations).

Other items:

- 2nd and 3rd Streets in OSM are not on the downtown grid. The output continues the downtown step (360 ft per number) east from 4th Street to 1st Street. 1st Street can be in the Missouri River at some latitudes.
- The new output format, the nominal grid, the diagnostics in feet and the new `make_grid_view.py` are not committed.
