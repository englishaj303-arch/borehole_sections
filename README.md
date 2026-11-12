# Borehole Geology Sections (QGIS plugin)

Borehole logs, sedimentary records, geological cross sections (2D) and 3D lithology models from borehole data stored in two CSV files.

## What it does

1. **Data import** - two CSV files:
    - *Boreholes*: name, X, Y, ground altitude, total depth (optional: azimuth 0–360° clockwise from North, dip/deviation 0–90° from vertical).
    - *Layers*: borehole name, depth from, depth to, lithology (optional: stratigraphy). Columns are detected automatically and can be reassigned; delimiter and encoding are auto-detected. Coordinates may be S-JTSK (EPSG:5514, also without the minus sign), WGS84 or ETRS89. Boreholes can be added to the map as a point layer; lithology colours and grain sizes (clay, silt, sand, gravel) are editable (tab 2).
2. **Borehole logs** (tab 3) - select one to four boreholes and create
    - *profiles*: the columns side by side with a depth (or altitude) ruler and the description of each layer next to the column (thin layers get a slanted label),
    - *sedimentary records*: the width of the column follows the grain size of each layer (Clay, Silt, Sand, Gravel) and an *Index* legend lists the rock types with their grain size. Optionally one window per borehole.
    Both can be exported to PNG, SVG or PDF. The profiles have a stratigraphy column next to the lithology column.
    Rocks are drawn with the FGDC lithologic patterns (e.g. 620 Clay or clay shale); the pattern of each lithology is set in tab 2 and the 2D section window can show them too.
3. **2D cross section** (tab 4, **IN DEVELOPMENT**) - draw a line on the map (or use a selected line feature); boreholes within a chosen distance are projected onto it. Layers of the same lithology are correlated between neighbouring boreholes (dip following, pinch-outs, jagged lateral lithology change, bedrock surface, stratigraphic boundaries, faults). Vertical exaggeration is automatic or manual. Export to PNG, SVG or PDF.
4. **3D model** (tab 5) for the QGIS 3D Map View (**IN DEVELOPMENT - NOT WORKING YET**)::
    - *Grid model* - blocks interpolated from the nearest boreholes.
    - *Fence model* - 3D slabs built from 2D sections, added manually or generated automatically (N-S and E-W).

## Usage

Open **Plugins \> Borehole Geology \> Borehole Geology Sections** and follow the tabs 1-5. For 3D, open **View \> New 3D Map View** and raise the vertical scale.

Requires QGIS 4.0 or newer (Qt6).

## License

Free software, released under the GNU General Public License v2 or later (see `LICENSE`).

## Credits

Lithologic patterns (folder `patterns`): FGDC Digital Cartographic Standard for Geologic Map Symbolization (FGDC-STD-013-2006), Appendix A, chapter 37, a publication of the US Federal Geographic Data Committee.
