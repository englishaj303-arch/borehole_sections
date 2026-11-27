# Borehole Geology Sections (QGIS plugin)

Borehole logs, sedimentary records, geological cross sections (2D) and 3D lithology models from borehole data stored in two CSV files.
Requires QGIS 4.0 or newer (Qt6). Open **Plugins \> Borehole Geology \> Borehole Geology Sections** and follow the tabs 1-5.

## Features

### 1. Data
- Two CSV files:
    - *Boreholes*: name, X, Y, ground altitude, total depth; optional azimuth (0-360 deg clockwise from North) and dip/deviation (0-90 deg from vertical) for inclined boreholes.
    - *Layers*: borehole name, depth from, depth to, lithology; optional stratigraphy. Any further columns can be used later as descriptions in the borehole logs.
- Columns, delimiter and encoding are detected automatically and can be changed.
- Coordinates in the coordinate system of the CSV file, used as it is (no conversion to S-JTSK): S-JTSK (EPSG:5514; coordinates written without the minus sign are fine, the sign is added automatically), WGS84, ETRS89, NAD83 and its variants (HARN, NSRS2007, 2011, CSRS), UTM (WGS84 zones 1-60 N/S, NAD83 zones 1-23N, NAD83(CSRS) 7-22N, NAD83(2011) 10-19N, NAD83(HARN) 4-19N, ETRS89 zones 28-38N) or Web Mercator. The QGIS project takes the CRS of the file. Degrees and Web Mercator do not give distances in metres, so for them the plugin works in the UTM zone of the boreholes.
- The boreholes can be added to the map as a point layer; boreholes selected in it can be used for the logs and the 3D models.

### 2. Lithology (colour, grain size)
- Colour, grain size class (clay, silt, sand, gravel) and an FGDC lithologic pattern for every lithology.
- The pattern is guessed from the name of the lithology (e.g. clay = 620 Clay or clay shale) and can be changed.
- The colour is taken from a colour word in the name (green clay, červený jíl, dark grey silt...), otherwise from the type of the lithology (sand, sandstone, coal, conglomerate, arkose, clay, soil, dirt...); it can be changed. Patterns 901 (soil / humus) and 902 (dirt) are not in the FGDC standard - they were drawn for this plugin.
- Pattern lines are black (as in the FGDC standard); on a dark colour of the lithology (e.g. coal, soil, dirt, peat, dark clay) the plugin automatically draws them pale cream instead, so they stay readable. The choice is made from the luminance of the lithology colour and is the same in the section, the borehole log, the legend and the 3D viewer; there is nothing to set.

### 3. Borehole logs
- Select one to four boreholes (also taken from the map) and create
    - *profiles*: the columns side by side with a depth (or altitude) ruler, the lithological description next to the column (labels of thin layers hang on slanted lines) and a stratigraphy column,
    - *sedimentary records*: the width of the column follows the grain size and a *Legend* lists the rock types.
- Both windows have a side panel with the column of the layers table used for the lithological description, the column used for the profile (colours, patterns), merging of identical lithologies and a **Regenerate** button.
- *A separate window for each borehole* works for profiles and for sedimentary records.
- Export to PNG, SVG and PDF.

### 4. 2D section
- Draw a section line on the map or use a selected line feature; boreholes within the chosen distance are projected onto it (inclined boreholes included).
- Layers of the same lithology are correlated between neighbouring boreholes: dip following, pinch-outs, lateral lithology changes, bedrock surface, stratigraphic boundaries and user-defined faults; identical neighbouring layers can be merged.
- Vertical exaggeration automatic or manual; optional lithologic patterns.
- Boreholes that do not lie on the section line are drawn with a dashed outline on a white halo. A borehole lies on the line when the line passes through its map marker (marker size of the *Boreholes* layer at the current scale, or a distance set by hand).
- *Min. spacing of boreholes*: of boreholes that are almost at the same point only the one nearer to the line is used.
- *Edit section* panel in the section window - the automatic correlation is the starting point, the geologist has the last word:
  - *Move boundary*: press on a layer boundary and drag it up or down; it bulges smoothly and stays fixed at the boreholes, the neighbouring boundaries do not move.
  - *Links*: the correlation links between neighbouring boreholes are drawn as lines (grey = automatic, orange = locked, blue = manual, red dashed = removed). Click a line to select it, then *Lock / unlock* or *Remove / restore* it; drag from a layer of one borehole to a layer of the neighbouring one to make a link by hand. A locked or manual link is kept by the automatic correlation whatever else changes; a removed link is never made. *Lock all links* freezes the current correlation.
  - *Lens*: click a lens (or its layer in the borehole) to suppress it - the layer then ends next to its borehole - or to bring it back; click a hand-drawn lens to delete it. *Add lens*: drag from the left to the right tip (lithology and thickness are set in the panel).
  - *Undo / Redo* (Ctrl+Z, Ctrl+Y), *Reset edits*, *Save edits / Load edits* (JSON). The edits are stored by borehole names, layer depths, distance and elevation, so they survive *Regenerate section*; edits whose layers no longer exist (e.g. after merging identical lithologies) are reported and skipped. With an editing tool active the left button belongs to the tool, the middle button pans, the right button cancels a drag.
- Export to PNG, SVG and PDF.

### 5. 3D model (own 3D viewer and QGIS 3D Map View)
- *Fence model*: closed 3D solids built from the 2D sections between neighbouring boreholes (grid of nearest neighbours or Delaunay triangulation), so inclined boundaries, pinch-outs and the terrain of an optional DEM are smooth. Borehole columns follow inclined boreholes. *Interpolate layers between the sections* also calculates the layers between the panels: every triangle of boreholes is interpolated from its three sections into smooth closed solids with sloped tops and bottoms (dipping layers stay inclined, layers pinch out smoothly, no blocks). *Interpolation > Blocks* keeps the old column-of-boxes method; *Mesh size* sets the fineness.
- *Full model from boreholes* (group B): a complete 3D volume made directly from the boreholes, no sections needed. *Smooth layers* (default): the layer stack is interpolated at the nodes of a fine triangular mesh from the nearest boreholes (weights fall to zero smoothly, so layers do not jump) and joined layer by layer into closed bodies with sloped tops and bottoms; layer boundaries are exact surfaces (not multiples of the vertical step), dipping layers stay inclined and lenses pinch out. *Extent*: the hull of the boreholes (only triangles between neighbouring boreholes) or a rectangle around them (limited by *Max. distance from a borehole*). *Layer geometry* chooses horizontal layers or layers parallel to the ground; the Quaternary cover always follows the ground and its base is exact. *Blocks* keeps the older brick model (square grid).
- A list with tick boxes shows or hides single lithologies of the created models; *Show block edges* draws thin outlines (off by default).
- *Open the created model in*: choose the viewer BEFORE creating the model - the plugin's own 3D viewer (opens in the web browser) or the QGIS 3D layers (opens in the native QGIS 3D Map View; raise the vertical scale there). The model is created for the chosen viewer only and opens automatically; *Reopen 3D viewer* opens the models of this session again.

#### Own 3D viewer
A viewer built into the plugin (WebGL 2, own code, no libraries to install). Fence and full models are shown together; **Reopen 3D viewer** reopens the models created in the current session.
- Rotate (left button), pan (right button or Shift), zoom (wheel), double-click centres the view on the clicked point; buttons for top / side views; perspective or orthographic projection.
- Docked side panel (like in the 2D section windows; the splitter changes its width, *Panel* hides it): vertical scale (set automatically for flat models), edges, light / white / dark background, grid with an elevation scale and a N-E-Z gizmo.
- *Section plane*: cut the model along X, Y or Z with a slider to look inside.
- *Layers (lithology)*: tick box = show / hide a lithology, colour box = change its colour; select layers by clicking the list (Ctrl / Shift for several) or the layer in the model; *Hide selected*, *Show selected*, *Show only selected* (double-click a row = only this layer), *Show all*.
- *Opacity of layers*: a slider sets the opacity of the selected layers (all layers when none is selected); every layer shows its own opacity. Tick boxes also show or hide the fence panels, the interpolated volume, the full model and the grid blocks.
- *Lithology textures (hatching)*: draws the FGDC hatching patterns on the surfaces (the patterns chosen for the lithologies in tab 2; lithologies without a pattern stay plain). The patterns follow the model in world coordinates - in plan on horizontal faces, along the wall on vertical faces - and *Texture size* scales them. The layer list shows a small thumbnail of the pattern.
- *Faults*: insert fault planes into the model. Each fault has a **position** (between two chosen boreholes as a % of the distance A→B, or X / Y coordinates), a **dip direction** (azimuth, 0 = N, 90 = E; buttons *Dip towards A / B* make the fault strike across the line A–B), a **dip angle** (1–90°, 90 = vertical), a **length** along the strike (centred on the position), a **top elevation** (default: ground level between the boreholes) and a **depth** (default: down to the bottom of the model). Faults are semi-transparent planes with an outline and a dip arrow, a name label, colour and opacity, and can be added, duplicated, hidden or deleted; the fields can be edited at any time. The planes are only drawn - the layers are not displaced. They are not stored in the QGIS project and a page opened in a web browser cannot save itself, so use **Export faults** (a JSON file) and **Import faults** (adds the faults of such a file, also to another model) to keep them.
- Borehole traces (also inclined) with names; hovering shows the lithology, coordinates and elevation under the cursor.
- *Save PNG* (screenshot of the view) and *Export faults* / *Import faults* (see Faults).

Requirements: nothing has to be installed. The viewer opens in the default web browser (Chrome, Edge, Firefox or Safari with WebGL 2). Models with up to about 100 000 blocks stay smooth on an ordinary graphics card.

## License

The source code is free software, released under the GNU General Public License v2 or later (see `LICENSE`).
The lithologic pattern tiles in the folder `patterns` are not covered by the GPL; they are public domain / CC0 (see the separate section in `LICENSE`).

## Credits

Lithologic patterns (folder `patterns`): FGDC Digital Cartographic Standard for Geologic Map Symbolization (FGDC-STD-013-2006), Appendix A, chapter 37, a publication of the US Federal Geographic Data Committee. The patterns comply with this standard (pattern numbers 601-686 and 701-733). High-resolution tiles come from the CC0 repackaging "geologic-patterns" by Daven Quinn (https://github.com/davenquinn/geologic-patterns).
