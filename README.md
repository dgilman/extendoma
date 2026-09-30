== Why are all the roads straight lines? ==
Omaha's grid runs true north, so on a Mercator projection, these are straight lines. Omaha's north-south roads all meet at a single point, the north and south poles, which on a Mercator map are expanded to be the entire width of the top and bottom of the map. Manhattan's grid is at an angle so its north-south roads meet at an alternate north pole in Uzbekistan. Both maps have east-west roads that never intersect and slice the earth into circles, but once again because Manhattan's grid is at an angle the circle has to be displayed a different way on the Mercator map.

== Why doesn't the grid always align in Omaha? ==
Buddy, the Omaha "grid" isn't much of a grid at all. There are offset streets, survey errors, and they had to integrate Benson and Elkhorn into the system at one point too. I don't know why there is a gap between B and C streets and D and E streets. Ultimately I did the best I could to try and derive the "correct" Omaha grid. If you want to study the results and make suggestions the grid_view.html file created by make_grid_view.py is how you view and troubleshoot my work.

== What is Biden Street? ==
I generalized the "presidental district" by adding more named streets in Sarpy County for the rest of the presidents.

== How do the named streets work? ==
Repeats are named 'North'/'South' if they repeat north or south of Dodge and the roman numeral at the end is the number of repeats. We follow the same order of named streets north to south each time, so after the last (southernmost) named street (Biden Street) you get 'South Girard Street II'.
