# Borehole Geology Sections (QGIS plugin)

Borehole logs, sedimentary records, geological cross sections (2D) and 3D lithology models from borehole data stored in two CSV files.
Requires QGIS 4.0 or newer (Qt6). Open **Plugins \> Borehole Geology \> Borehole Geology Sections** and follow the tabs 1-5.

## Features

### 1. Data
- Two CSV files:
    - *Boreholes*: name, X, Y, ground altitude, total depth; optional azimuth (0-360 deg clockwise from North) and dip/deviation (0-90 deg from vertical) for inclined boreholes.
    - *Layers*: borehole name, depth from, depth to, lithology; optional stratigraphy. Any further columns can be used later as descriptions in the borehole logs.
- Columns, delimiter and encoding are detected automatically and can be changed.
- Coordinates in S-JTSK (EPSG:5514, also written without the minus sign), WGS84 or ETRS89.
- The boreholes can be added to the map as a point layer; boreholes selected in it can be used for the logs and the 3D models.

### 2. Lithology (colour, grain size)
- Colour, grain size class (clay, silt, sand, gravel) and an FGDC lithologic pattern for every lithology.
- The pattern is guessed from the name of the lithology (e.g. clay = 620 Clay or clay shale) and can be changed.

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
- Export to PNG, SVG and PDF.

### 5. 3D model (own 3D viewer and QGIS 3D Map View)
- *Fence model*: closed 3D solids built from the 2D sections between neighbouring boreholes (grid of nearest neighbours or Delaunay triangulation), so inclined boundaries, pinch-outs and the terrain of an optional DEM are smooth. Borehole columns follow inclined boreholes. *Interpolate layers between the sections* also calculates the layers between the panels: every triangle of boreholes is interpolated from its three sections into smooth closed solids with sloped tops and bottoms (dipping layers stay inclined, layers pinch out smoothly, no blocks). *Interpolation > Blocks* keeps the old column-of-boxes method; *Mesh size* sets the fineness.
- *Full model from boreholes* (group B): a complete 3D volume made directly from the boreholes, no sections needed. *Smooth layers* (default): the layer stack is interpolated at the nodes of a fine triangular mesh from the nearest boreholes (weights fall to zero smoothly, so layers do not jump) and joined layer by layer into closed bodies with sloped tops and bottoms; layer boundaries are exact surfaces (not multiples of the vertical step), dipping layers stay inclined and lenses pinch out. *Extent*: the hull of the boreholes (only triangles between neighbouring boreholes) or a rectangle around them (limited by *Max. distance from a borehole*). *Layer geometry* chooses horizontal layers or layers parallel to the ground; the Quaternary cover always follows the ground and its base is exact. *Blocks* keeps the older brick model (square grid).
- A list with tick boxes shows or hides single lithologies of the created models; *Show block edges* draws thin outlines (off by default).
- *Show the created model in*: the plugin's own 3D viewer, the QGIS 3D layers, or both. For the QGIS layers open **View \> New 3D Map View** and raise the vertical scale.

#### Own 3D viewer
A viewer built into the plugin (WebGL 2, own code, no libraries to install). Fence and full models are shown together; **Open 3D viewer** reopens the models created in the current session.
- Rotate (left button), pan (right button or Shift), zoom (wheel), double-click centres the view on the clicked point; buttons for top / side views; perspective or orthographic projection.
- Docked side panel (like in the 2D section windows; the splitter changes its width, *Panel* hides it): vertical scale (set automatically for flat models), edges, light / white / dark background, grid with an elevation scale and a N-E-Z gizmo.
- *Section plane*: cut the model along X, Y or Z with a slider to look inside.
- *Layers (lithology)*: tick box = show / hide a lithology, colour box = change its colour; select layers by clicking the list (Ctrl / Shift for several) or the layer in the model; *Hide selected*, *Show selected*, *Show only selected* (double-click a row = only this layer), *Show all*.
- *Opacity of layers*: a slider sets the opacity of the selected layers (all layers when none is selected); every layer shows its own opacity. Tick boxes also show or hide the fence panels, the interpolated volume, the full model and the grid blocks.
- *Lithology textures (hatching)*: draws the FGDC hatching patterns on the surfaces (the patterns chosen for the lithologies in tab 2; lithologies without a pattern stay plain). The patterns follow the model in world coordinates - in plan on horizontal faces, along the wall on vertical faces - and *Texture size* scales them. The layer list shows a small thumbnail of the pattern.
- Borehole traces (also inclined) with names; hovering shows the lithology, S-JTSK coordinates and elevation under the cursor.
- *Save PNG* and, in the window of QGIS, *Save as HTML...* (one file with the whole model that opens in any browser and can be sent to colleagues).

Requirements: nothing has to be installed. The viewer opens in an embedded window when QGIS has QtWebEngine (Python module `qgis.PyQt.QtWebEngineWidgets`); otherwise, or when *Open the viewer in the web browser* is ticked, it opens in the default browser (Chrome, Edge, Firefox or Safari with WebGL 2). Models with up to about 100 000 blocks stay smooth on an ordinary graphics card.

## License

The source code is free software, released under the GNU General Public License v2 or later (see `LICENSE`).
The lithologic pattern tiles in the folder `patterns` are not covered by the GPL; they are public domain / CC0 (see the separate section in `LICENSE`).

## Credits

Lithologic patterns (folder `patterns`): FGDC Digital Cartographic Standard for Geologic Map Symbolization (FGDC-STD-013-2006), Appendix A, chapter 37, a publication of the US Federal Geographic Data Committee. The patterns comply with this standard (pattern numbers 601-686 and 701-733). High-resolution tiles come from the CC0 repackaging "geologic-patterns" by Daven Quinn (https://github.com/davenquinn/geologic-patterns).
