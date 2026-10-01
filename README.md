Inspired by [extendny.com](https://extendny.com) | [Source code](https://github.com/dgilman/extendoma/) | [Contact me](mailto:davidgilman1@gmail.com)

## Why are all the roads straight lines?
Omaha's grid is aligned to true north making its roads straight lines on a Mercator projection. On the globe Omaha's north-south roads all meet at two points, the north and south poles, but on a Mercator map the poles are smeared along along the entire top and bottom edges of the map. Manhattan's grid is at an angle so its north-south roads meet at an alternate north pole in Uzbekistan. Both maps have east-west roads that never intersect and slice the earth into circles, but once again because Manhattan's grid is at an angle the circle has to be displayed a different way on the Mercator map.

## Why doesn't the grid always align in Omaha?
Buddy, the Omaha "grid" isn't much of a grid at all. There are two downtown grids and a grid for the rest of the city starting at 24th. They had to integrate Benson's grid into the system. I don't know why there is a gap between B and C streets and D and E streets. I did several passes of visual checks and wound up with something I thought was good enough. If you want to study the results and make suggestions the [grid_view.html](grid_view.html) file created by `make_grid_view.py` is how you view and troubleshoot my work.

## What is Biden Street?
I generalized the "presidential district" in South Omaha by adding more named streets in Sarpy County for the rest of the presidents.

## How do the streets repeat outside of Omaha?
Repeats are named 'North'/'South' if they repeat north or south of Dodge and the roman numeral at the end is the number of repeats. We follow the same order of named streets north to south each time, so after the last (southernmost) named street (Biden Street) you get 'South Girard Street II'. Numbered streets east of Omaha are named 'East 132nd Street.'
