# Borehole Geology Sections (QGIS plugin)

Geological cross sections (2D) and 3D lithology models from borehole data stored in two CSV files.

## What it does

1. **Data import** - two CSV files:
    - *Boreholes*: name, X, Y, ground altitude, total depth.
    - *Layers*: borehole name, depth from, depth to, lithology (optional: stratigraphy). Columns are detected automatically and can be reassigned; delimiter and encoding are auto-detected. Coordinates may be S-JTSK (EPSG:5514, also without the minus sign), WGS84 or ETRS89. Boreholes can be added to the map as a point layer; lithology colours are editable.
2. **2D cross section** - draw a line on the map (or use a selected line feature); boreholes within a chosen distance are projected onto it. Layers of the same lithology are correlated between neighbouring boreholes (dip following, pinch-outs, jagged lateral lithology change, bedrock surface, stratigraphic boundaries, faults). Vertical exaggeration is automatic or manual. Export to PNG, SVG or PDF.
3. **3D model** for the QGIS 3D Map View (IN DEVELOPMENT):
    - *Grid model* - blocks interpolated from the nearest boreholes.
    - *Fence model* - 3D slabs built from 2D sections, added manually or generated automatically (N-S and E-W).

## Usage

Open **Plugins \> Borehole Geology \> Borehole Geology Sections** and follow the tabs 1-4. For 3D, open **View \> New 3D Map View** and raise the vertical scale.

Requires QGIS 4.0 or newer (Qt6).
