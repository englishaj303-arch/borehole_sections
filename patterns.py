"""Lithologic patterns of the FGDC Digital Cartographic Standard for Geologic Map Symbolization
(FGDC-STD-013-2006, Appendix A, chapter 37 - a US Government publication).

The tiles are in the folder ``patterns`` (``601.png`` ... ``733.png``, black lines on a
transparent background, 1200 dpi, from the "geologic-patterns" repackaging by Daven Quinn,
CC0; 718 comes from the FGDC PDF) - see LICENSE.  This module gives the names, a default
pattern for a lithology and the Qt brush made from a tile.
"""
import os

PATTERN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "patterns")

NAMES = {
    "601": "Gravel or conglomerate (1st option)", "602": "Gravel or conglomerate (2nd option)",
    "603": "Crossbedded gravel or conglomerate", "605": "Breccia (1st option)",
    "606": "Breccia (2nd option)", "607": "Massive sand or sandstone",
    "608": "Bedded sand or sandstone", "609": "Crossbedded sand or sandstone (1st option)",
    "610": "Crossbedded sand or sandstone (2nd option)", "611": "Ripple-bedded sand or sandstone",
    "612": "Argillaceous or shaly sandstone", "613": "Calcareous sandstone",
    "614": "Dolomitic sandstone", "616": "Silt, siltstone, or shaly silt",
    "617": "Calcareous siltstone", "618": "Dolomitic siltstone", "619": "Sandy or silty shale",
    "620": "Clay or clay shale", "621": "Cherty shale", "622": "Dolomitic shale",
    "623": "Calcareous shale or marl", "624": "Carbonaceous shale", "625": "Oil shale",
    "626": "Chalk", "627": "Limestone", "628": "Clastic limestone",
    "629": "Fossiliferous clastic limestone", "630": "Nodular or irregularly bedded limestone",
    "631": "Limestone, irregular (burrow?) fillings of saccharoidal dolomite",
    "632": "Crossbedded limestone", "633": "Cherty crossbedded limestone",
    "634": "Cherty and sandy crossbedded clastic limestone", "635": "Oolitic limestone",
    "636": "Sandy limestone", "637": "Silty limestone", "638": "Argillaceous or shaly limestone",
    "639": "Cherty limestone (1st option)", "640": "Cherty limestone (2nd option)",
    "641": "Dolomitic limestone, limy dolostone, or limy dolomite",
    "642": "Dolostone or dolomite", "643": "Crossbedded dolostone or dolomite",
    "644": "Oolitic dolostone or dolomite", "645": "Sandy dolostone or dolomite",
    "646": "Silty dolostone or dolomite", "647": "Argillaceous or shaly dolostone or dolomite",
    "648": "Cherty dolostone or dolomite", "649": "Bedded chert (1st option)",
    "650": "Bedded chert (2nd option)", "651": "Fossiliferous bedded chert",
    "652": "Fossiliferous rock", "653": "Diatomaceous rock", "654": "Subgraywacke",
    "655": "Crossbedded subgraywacke", "656": "Ripple-bedded subgraywacke", "657": "Peat",
    "658": "Coal", "659": "Bony coal or impure coal", "660": "Underclay", "661": "Flint clay",
    "662": "Bentonite", "663": "Glauconite", "664": "Limonite", "665": "Siderite",
    "666": "Phosphatic-nodular rock", "667": "Gypsum", "668": "Salt",
    "669": "Interbedded sandstone and siltstone", "670": "Interbedded sandstone and shale",
    "671": "Interbedded ripple-bedded sandstone and shale",
    "672": "Interbedded shale and silty limestone (shale dominant)",
    "673": "Interbedded shale and limestone (shale dominant, 1st option)",
    "674": "Interbedded shale and limestone (shale dominant, 2nd option)",
    "675": "Interbedded calcareous shale and limestone (shale dominant)",
    "676": "Interbedded silty limestone and shale",
    "677": "Interbedded limestone and shale (1st option)",
    "678": "Interbedded limestone and shale (2nd option)",
    "679": "Interbedded limestone and shale (limestone dominant)",
    "680": "Interbedded limestone and calcareous shale", "681": "Till or diamicton (1st option)",
    "682": "Till or diamicton (2nd option)", "683": "Till or diamicton (3rd option)",
    "684": "Loess (1st option)", "685": "Loess (2nd option)", "686": "Loess (3rd option)",
    "701": "Metamorphism", "702": "Quartzite", "703": "Slate",
    "704": "Schistose or gneissoid granite", "705": "Schist", "706": "Contorted schist",
    "707": "Schist and gneiss", "708": "Gneiss", "709": "Contorted gneiss",
    "710": "Soapstone, talc, or serpentinite", "711": "Tuffaceous rock", "712": "Crystal tuff",
    "713": "Devitrified tuff", "714": "Volcanic breccia and tuff",
    "715": "Volcanic breccia or agglomerate", "716": "Zeolitic rock", "717": "Basaltic flows",
    "718": "Granite (1st option)", "719": "Granite (2nd option)", "720": "Banded igneous rock",
    "721": "Igneous rock (1st option)", "722": "Igneous rock (2nd option)",
    "723": "Igneous rock (3rd option)", "724": "Igneous rock (4th option)",
    "725": "Igneous rock (5th option)", "726": "Igneous rock (6th option)",
    "727": "Igneous rock (7th option)", "728": "Igneous rock (8th option)",
    "729": "Porphyritic rock (1st option)", "730": "Porphyritic rock (2nd option)",
    "731": "Vitrophyre", "732": "Quartz", "733": "Ore",
}

# keyword (lower case, without diacritics) -> pattern.  The keyword standing FIRST in the
# lithology text decides ("jil piscity" = clay, "pisek jilovity" = sand, "sterkopisek" = gravel).
_KEYWORDS = [
    ("601", ("sterk", "gravel", "valoun", "slepenec", "conglomerate", "pebble", "cobble")),
    ("605", ("brekcie", "breccia")),
    ("607", ("pisek", "pisk", "sand", "arkoz")),
    ("616", ("prach", "silt", "hlin", "loam")),
    ("684", ("spras", "loess")),
    ("620", ("jil", "clay", "bridlic", "shale", "lupek", "mud", "kaolin")),
    ("623", ("slin", "marl")),
    ("626", ("kreda", "chalk")),
    ("627", ("vapenec", "limestone")),
    ("642", ("dolomit", "dolostone")),
    ("657", ("raselin", "peat")),
    ("658", ("uhli", "coal")),
    ("667", ("sadrovec", "gypsum")),
    ("702", ("kremenec", "quartzite")),
    ("708", ("rula", "gneis")),
    ("705", ("svor", "schist")),
    ("718", ("zula", "granit")),
    ("717", ("bazalt", "basalt")),
    ("711", ("tuf",)),
]


def default_pattern(litho):
    """Pattern code guessed from the name of a lithology; '' when nothing fits (plain colour)."""
    from . import core
    n = core._norm(litho)
    best = None
    for code, keys in _KEYWORDS:
        for k in keys:
            pos = n.find(k)
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, code)
    return best[1] if best else ""


def label(code):
    return f"{code} - {NAMES[code]}" if code in NAMES else "(none)"


def tile_path(code):
    return os.path.join(PATTERN_DIR, f"{code}.png")


_cache = {}
OVERSAMPLE = 2.0       # tiles are kept 2x larger than shown, so the pattern stays sharp when zoomed


def tile_pixmap(code, scale=0.45, oversample=OVERSAMPLE):
    """QPixmap of a tile.  scale 1 = the tile shown in 300 dpi (the PNG files have 1200 dpi),
    `oversample` makes the pixmap larger by that factor.  None for an unknown code."""
    if code not in NAMES:
        return None
    key = (code, round(scale, 3), oversample)
    if key not in _cache:
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtGui import QPixmap
        pm = QPixmap(tile_path(code))
        if pm.isNull():
            _cache[key] = None
        else:
            k = 0.25 * scale * oversample
            w, h = max(8, round(pm.width() * k)), max(8, round(pm.height() * k))
            _cache[key] = pm.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
    return _cache[key]


def pattern_brush(code, scale=0.45):
    """Texture brush (black pattern on a transparent background) or None.

    The texture is OVERSAMPLE times larger than shown; the brush transform shrinks it
    (the views and exports use SmoothPixmapTransform)."""
    pm = tile_pixmap(code, scale)
    if pm is None:
        return None
    from qgis.PyQt.QtGui import QBrush, QTransform
    b = QBrush(pm)
    b.setTransform(QTransform.fromScale(1.0 / OVERSAMPLE, 1.0 / OVERSAMPLE))
    return b


_icons = {}


def icon(code, size=(64, 48)):
    """Small icon of a pattern on white for menus (None for an unknown code); cached."""
    if (code, size) not in _icons:
        _icons[(code, size)] = _make_icon(code, size)
    return _icons[(code, size)]


def _make_icon(code, size):
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QIcon, QPainter, QPixmap
    pm = tile_pixmap(code, 0.3, 1.0)
    if pm is None:
        return None
    out = QPixmap(size[0], size[1])
    out.fill(Qt.GlobalColor.white)
    p = QPainter(out)
    p.drawTiledPixmap(0, 0, size[0], size[1], pm)
    p.end()
    return QIcon(out)
