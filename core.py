"""Data handling, section drawing and 3D layer creation (no GUI widgets)."""
import contextlib
import csv
import difflib
import io
import itertools
import logging
import math
import os
import re
import stat
from dataclasses import dataclass, field

from qgis.PyQt.QtCore import Qt, QPointF
from qgis.PyQt.QtGui import QColor, QBrush, QPen, QPolygonF, QFont, QPainterPath
from qgis.PyQt.QtWidgets import QGraphicsScene
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsFeature, QgsGeometry, QgsPoint, QgsPointXY, QgsVectorLayer,
    QgsMessageLog, Qgis,
)

from . import patterns as _patterns

LOG_TAG = "Borehole Geology"


def log_warning(msg):
    QgsMessageLog.logMessage(msg, LOG_TAG, Qgis.MessageLevel.Warning)


def log_debug(msg):
    QgsMessageLog.logMessage(msg, LOG_TAG, Qgis.MessageLevel.Info)


_GEOM_ISSUES = {}          # message -> number of occurrences since the last take_geometry_issues()


def geometry_issue(msg):
    """A geometric correction failed and the uncorrected geometry was kept: write it to the QGIS
    log at once and remember it, so that the dialog can tell the user (take_geometry_issues)."""
    if msg not in _GEOM_ISSUES and len(_GEOM_ISSUES) >= 20:
        return
    if msg not in _GEOM_ISSUES:
        log_warning(msg)
    _GEOM_ISSUES[msg] = _GEOM_ISSUES.get(msg, 0) + 1


def take_geometry_issues():
    """[(message, count)] of the geometry problems reported since the last call (cleared)."""
    out = list(_GEOM_ISSUES.items())
    _GEOM_ISSUES.clear()
    return out


CRS_SJTSK = "EPSG:5514"
CRS_WEB_MERCATOR = "EPSG:3857"
WEB_MERCATOR_LIMIT = 20037508.35
WEB_MERCATOR_RADIUS = 6378137.0

_LONLAT = ("Longitude [deg]", "Latitude [deg]",
           ["lon", "lng", "longitude", "x"], ["lat", "latitude", "y"])
_EASTNORTH = ("Easting (E) [m]", "Northing (N) [m]",
              ["easting", "east", "e", "x"], ["northing", "north", "n", "y"])

# key -> (authid, label, x label, y label, x keywords, y keywords)
# authid None = UTM family: the EPSG code follows from the zone (see UTM_FAMILIES, crs_authid)
INPUT_CRS = {
    "sjtsk": ("EPSG:5514", "S-JTSK (EPSG:5514)",
              "X (southing / Krovak X [m])", "Y (westing / Krovak Y [m])", ["x"], ["y"]),
    "wgs84": ("EPSG:4326", "WGS84 geographic - lon/lat in degrees (EPSG:4326)", *_LONLAT),
    "etrs89": ("EPSG:4258", "ETRS89 geographic - lon/lat in degrees (EPSG:4258)", *_LONLAT),
    "nad83": ("EPSG:4269", "NAD83 geographic - lon/lat in degrees (EPSG:4269)", *_LONLAT),
    "nad83_harn": ("EPSG:4152", "NAD83(HARN) geographic - lon/lat in degrees (EPSG:4152)",
                   *_LONLAT),
    "nad83_nsrs2007": ("EPSG:4759", "NAD83(NSRS2007) geographic - lon/lat in degrees (EPSG:4759)",
                       *_LONLAT),
    "nad83_2011": ("EPSG:6318", "NAD83(2011) geographic - lon/lat in degrees (EPSG:6318)",
                   *_LONLAT),
    "nad83_csrs": ("EPSG:4617", "NAD83(CSRS) geographic - lon/lat in degrees (EPSG:4617)",
                   *_LONLAT),
    "utm_wgs84": (None, "WGS84 / UTM zone (Universal Transverse Mercator, EPSG:326xx N, 327xx S)", *_EASTNORTH),
    "utm_nad83": (None, "NAD83 / UTM zone 1N-23N (Universal Transverse Mercator, EPSG:269xx)", *_EASTNORTH),
    "utm_etrs89": (None, "ETRS89 / UTM zone 28N-38N (Universal Transverse Mercator, EPSG:258xx)", *_EASTNORTH),
    "utm_nad83_csrs": (None, "NAD83(CSRS) / UTM zone 7N-22N (Universal Transverse Mercator)",
                       *_EASTNORTH),
    "utm_nad83_2011": (None, "NAD83(2011) / UTM zone 10N-19N (Universal Transverse Mercator)",
                       *_EASTNORTH),
    "utm_nad83_harn": (None, "NAD83(HARN) / UTM zone 4N-5N, 10N-19N (Universal Transverse Mercator)",
                       *_EASTNORTH),
    "webmerc": (CRS_WEB_MERCATOR, "Web Mercator / Pseudo-Mercator (EPSG:3857)", *_EASTNORTH),
}

# UTM families: key -> (EPSG base of the northern zones, base of the southern zones or None,
#                       first zone, last zone, default zone)
UTM_FAMILIES = {
    "utm_wgs84": (32600, 32700, 1, 60, 33),     # WGS84 / UTM zone 1N..60N (EPSG:326xx), 1S..60S (327xx)
    "utm_nad83": (26900, None, 1, 23, 14),      # NAD83 / UTM zone 1N..23N (EPSG:269xx)
    "utm_etrs89": (25800, None, 28, 38, 33),    # ETRS89 / UTM zone 28N..38N (EPSG:258xx)
}

# geographic systems of the NAD83 family: their metric working system is NAD83 / UTM
_NAD83_GEOGRAPHIC = ("EPSG:4269", "EPSG:4152", "EPSG:4759", "EPSG:6318", "EPSG:4617")


# UTM of the NAD83 realizations: key -> (name in the EPSG registry, first zone, last zone,
# default zone, {zone: EPSG code}).  The codes are only the first guess: every one is checked
# against the name QGIS has for it, and when it does not match (or is missing) the EPSG codes are
# searched for the name "<name> / UTM zone <n>N", so a wrong code can never be used silently.
_HARN_UTM = {4: 3750, 5: 3751, **{z: 3740 + z - 10 for z in range(10, 20)}}    # no 6N-9N in EPSG

_UTM_NAMED = {
    "utm_nad83_csrs": ("NAD83(CSRS)", 7, 22, 17,
                       {7: 3154, 8: 3155, 9: 3156, 10: 3157, 11: 2955, 12: 2956, 13: 2957,
                        14: 3158, 15: 3159, 16: 3160, 17: 2958, 18: 2959, 19: 2960, 20: 2961,
                        21: 2962, 22: 3761}),
    "utm_nad83_2011": ("NAD83(2011)", 10, 19, 17,
                       {z: 6339 + z - 10 for z in range(10, 20)}),
    "utm_nad83_harn": ("NAD83(HARN)", 4, 19, 17, _HARN_UTM),
}
_UTM_FOUND = {}                      # (key, zone) -> authid, found once per session


def is_utm(crs_key):
    return crs_key in UTM_FAMILIES or crs_key in _UTM_NAMED


def utm_zone_info(crs_key):
    """(first zone, last zone, default zone, southern hemisphere possible) of a UTM entry."""
    if crs_key in _UTM_NAMED:
        _, z0, z1, default, _ = _UTM_NAMED[crs_key]
        return z0, z1, default, False
    _, south_base, z0, z1, default = UTM_FAMILIES[crs_key]
    return z0, z1, default, south_base is not None


def utm_valid_zones(crs_key):
    """Sorted list of the UTM zones that exist for a UTM entry (the datums NAD83(HARN) has gaps)."""
    if crs_key in _UTM_NAMED and _UTM_NAMED[crs_key][4]:
        return sorted(_UTM_NAMED[crs_key][4])
    z0, z1, _, _ = utm_zone_info(crs_key)
    return list(range(z0, z1 + 1))


def _utm_name_matches(description, name, zone):  # noqa: D401
    """True for '<name> / UTM zone <zone>N', also when a newer PROJ/GDAL adds a realization or
    an epoch to the datum name (e.g. 'NAD83(CSRS)v7 / UTM zone 17N')."""
    pattern = (re.escape(name) + r"(?:\s*v\d+(?:\.\d+)?|\s*\([^)]*\))*\s*/\s*UTM\s+zone\s+"
               + str(int(zone)) + r"N\b")
    # re.match: an authority or a locale added after the name ("... 17N [EPSG:2958]") is allowed
    return re.match(pattern, description.strip(), re.IGNORECASE) is not None


def _find_named_utm(crs_key, zone):
    """EPSG code of '<datum> / UTM zone <zone>N' (names are checked, see _UTM_NAMED)."""
    if (crs_key, zone) in _UTM_FOUND:
        return _UTM_FOUND[(crs_key, zone)]
    name, _, _, _, known = _UTM_NAMED[crs_key]
    want = f"{name} / UTM zone {zone}N"
    first = known.get(zone)
    # the search is limited to the blocks of the EPSG registry where these systems are
    blocks = itertools.chain(range(2955, 2963), range(3154, 3161), range(3740, 3762),
                             range(6330, 6350))
    codes = ([first] if first else []) + [c for c in blocks if c != first]
    for code in codes:
        crs = QgsCoordinateReferenceSystem(f"EPSG:{code}")
        if crs.isValid() and _utm_name_matches(crs.description(), name, zone):
            _UTM_FOUND[(crs_key, zone)] = f"EPSG:{code}"
            return _UTM_FOUND[(crs_key, zone)]
    raise ValueError(f"'{want}' was not found in the coordinate systems of this QGIS "
                     "installation - choose another zone or coordinate system.")


def crs_authid(crs_key, zone=None, south=False):
    """EPSG code of an input system; the UTM entries need the zone (and the hemisphere)."""
    if not is_utm(crs_key):
        return INPUT_CRS[crs_key][0]
    z0, z1, default, south_ok = utm_zone_info(crs_key)
    zone = default if zone is None else int(zone)
    if not z0 <= zone <= z1:
        raise ValueError(f"UTM zone {zone} is outside {z0}..{z1} for this datum.")
    if south and not south_ok:
        raise ValueError("The southern hemisphere is not available for this datum.")
    if crs_key in _UTM_NAMED:
        if _UTM_NAMED[crs_key][4] and zone not in _UTM_NAMED[crs_key][4]:
            raise ValueError(f"{_UTM_NAMED[crs_key][0]} / UTM zone {zone}N does not exist "
                             f"(zones: {', '.join(str(z) for z in sorted(_UTM_NAMED[crs_key][4]))}).")
        return _find_named_utm(crs_key, zone)
    north, south_base = UTM_FAMILIES[crs_key][:2]
    return f"EPSG:{(south_base if south else north) + zone}"


_GEOGRAPHIC_TO_UTM_KEY = {"EPSG:4617": "utm_nad83_csrs", "EPSG:6318": "utm_nad83_2011",
                           "EPSG:4152": "utm_nad83_harn"}


def utm_authid_and_note(lon, lat, src_authid):
    """(metric UTM system, note) for a point in degrees.  The UTM of the same datum realization
    is used where there is one; otherwise the note says which other datum is the fallback
    (it is empty when the realization is kept)."""
    zone = min(max(int(math.floor((lon + 180.0) / 6.0)) + 1, 1), 60)
    key = _GEOGRAPHIC_TO_UTM_KEY.get(src_authid)
    if key and lat >= 0:                   # NAD83(CSRS), NAD83(2011), NAD83(HARN): no datum shift
        with contextlib.suppress(ValueError):      # zone outside the range of that realization
            return crs_authid(key, zone), ""
    if src_authid in _NAD83_GEOGRAPHIC and lat >= 0 and 1 <= zone <= 23:
        auth, other = f"EPSG:{26900 + zone}", "NAD83 / UTM"
    elif src_authid == "EPSG:4258" and lat >= 0 and 28 <= zone <= 38:
        return f"EPSG:{25800 + zone}", ""
    else:
        auth, other = f"EPSG:{(32600 if lat >= 0 else 32700) + zone}", "WGS84 / UTM"
    note = ""
    if key or src_authid == "EPSG:4759":      # a realization of its own (plain NAD83 = NAD83 / UTM)
        note = (f"no UTM zone {zone}{'N' if lat >= 0 else 'S'} of the same NAD83 realization as the "
                f"file ({src_authid}) - the working system is {other} ({auth}); the datums differ "
                "by up to about 1-2 m")
    return auth, note


def utm_authid_for(lon, lat, src_authid):
    """Metric UTM system (the same datum realization where there is one) for a point in degrees."""
    return utm_authid_and_note(lon, lat, src_authid)[0]


# The coordinate system of the loaded boreholes: the one chosen for the CSV file ("source"), and
# the metric one the plugin works in ("work": map layers, sections, 3D).  They are the same for
# every projected system; a geographic system (degrees) and Web Mercator (distances are
# stretched by 1 / cos(latitude)) cannot be used for distances in metres, so the plugin works in
# the UTM zone of the data and the QGIS project keeps the system of the CSV file.
_DATA_CRS = {"source": CRS_SJTSK, "work": CRS_SJTSK}


def source_crs_authid():
    return _DATA_CRS["source"]


def work_crs_authid():
    return _DATA_CRS["work"]


def work_crs():
    return QgsCoordinateReferenceSystem(_DATA_CRS["work"])


DELIMITERS = [("Auto-detect", None), ("Semicolon ;", ";"), ("Comma ,", ","),
              ("Tab", "\t"), ("Space", " "), ("Pipe |", "|")]


@dataclass
class Layer:
    top: float
    bottom: float
    litho: str
    age: tuple = None   # (youngest_index, oldest_index) in STRAT_NAMES, or None
    extra: dict = None  # all columns of the layers CSV row: header -> text
    desc: str = None    # description text for the borehole logs (None = same as litho)


@dataclass
class Borehole:
    name: str
    x: float          # easting in the working CRS (work_crs_authid())
    y: float          # northing in the working CRS (work_crs_authid())
    z: float          # collar altitude
    depth: float
    layers: list = field(default_factory=list)
    azimuth: float = 0.0   # 0..360 deg clockwise from North (0 = N, 90 = E)
    dip: float = 0.0       # 0..90 deg inclination from vertical (0 = vertical, 90 = horizontal)

    def point_at(self, md):
        """3D coordinates (x, y, z) at measured along-hole depth `md`."""
        inc = math.radians(self.dip)
        az = math.radians(self.azimuth)
        horiz = md * math.sin(inc)
        return (
            self.x + horiz * math.sin(az),
            self.y + horiz * math.cos(az),
            self.z - md * math.cos(inc),
        )

    def z_at(self, md):
        """Elevation a.s.l. at measured along-hole depth `md`."""
        return self.z - md * math.cos(math.radians(self.dip))


@dataclass
class Projected:
    bh: Borehole
    dist: float
    offset: float
    d_top: float = None
    d_bot: float = None
    s_top: float = 0.0     # signed distance of the collar from the section line (+ = left of it)
    s_bot: float = 0.0     # the same for the bottom of the borehole
    kx: float = 0.0        # gradient of the strata perpendicular to the section (m per m, + = rising to the left)
    path: object = None    # inclined hole + broken line: md -> (distance along the line, signed offset)

    def __post_init__(self):
        if self.d_top is None:
            self.d_top = self.dist
        if self.d_bot is None:
            self.d_bot = self.dist

    def d_at(self, md):
        """Distance along section polyline at measured along-hole depth `md`."""
        if self.bh.depth <= 1e-9:
            return self.d_top
        if self.path is not None:          # the trace of the hole bends with the line: no interpolation
            return self.path(md)[0]
        t = md / self.bh.depth
        return self.d_top + (self.d_bot - self.d_top) * t

    def s_at(self, md):
        """Signed distance of the borehole from the section plane at along-hole depth `md`
        (an inclined borehole crosses the plane in one point only, there s = 0)."""
        if self.bh.depth <= 1e-9:
            return self.s_top
        if self.path is not None:
            return self.path(md)[1]
        return self.s_top + (self.s_bot - self.s_top) * md / self.bh.depth

    def z_at(self, md):
        """Elevation of the stratum point at `md` moved into the section plane: the strata
        rise by kx per metre to the left of the line, so the point at distance s is
        lowered by kx * s; = real elevation if kx = 0."""
        return self.bh.z_at(md) - self.kx * self.s_at(md)


# ----------------------------------------------------------------- CSV input
def _norm(s):
    """Text without diacritics, lower case (shared with patterns.py, which core imports)."""
    return _patterns.norm_text(s)


MAX_TABLE_BYTES = 100 * 1024 * 1024
# Plausible ranges of the input values; anything outside is a data error (units, a shifted
# column ...) and would only produce absurdly large models.
MAX_COORD = 1e8                 # projected coordinates [m]
# physically sensible ranges of some projected systems [m]: (min, max) of easting and northing
UTM_EASTING = (0.0, 1000000.0)      # the zones overlap: eastings beyond 100-900 km are legal
UTM_NORTHING = (0.0, 10000000.0)
UTM_LAT_MIN, UTM_LAT_MAX = -80.0, 84.0     # latitudes covered by UTM (poles: UPS, not supported)
SJTSK_LIMIT = 3e6               # |X| and |Y| of S-JTSK (the Czech Republic: ~0.4-1.3 million)
MAX_ELEV = 12000.0              # ground elevation [m a.s.l.]: -MAX_ELEV .. +MAX_ELEV
MAX_DEPTH = 20000.0             # total depth and layer depths [m] (deepest borehole: 12.3 km)
MAX_TABLE_ROWS = 1_000_000
MAX_TABLE_COLUMNS = 1000
MAX_TABLE_CELL = 32768
MAX_NAME_LEN = 256                # borehole names and lithology names are cut to this (labels, textures)


_BLANK_HEADER_RE = re.compile(r"^\(column \d+\)$")


def is_blank_header(name):
    """True for the placeholder name '(column N)' that read_table gives to a blank header."""
    return bool(_BLANK_HEADER_RE.match(name or ""))


def read_table(path, delimiter=None, max_rows=None, max_bytes=MAX_TABLE_BYTES):
    """Return (header, rows). Auto-detects encoding (UTF-8, UTF-16 with BOM, cp1250, latin-1);
    delimiter=None -> auto.  max_rows=N reads only the beginning of the file (header + at most
    N rows) - enough for a preview of the column names of a big file.  A file larger than
    max_bytes (when read whole) or a binary file (NUL bytes) is rejected."""
    try:
        fh = open(path, "rb")                 # open first, then check the descriptor (no TOCTOU)
    except OSError as e:
        raise ValueError(f"Cannot read the file ({e.strerror or e}): {path}")
    with fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):      # FIFO, device ...: size 0, read() could block forever
            raise ValueError("Not a regular file: " + path)
        if max_rows is None and st.st_size > max_bytes:
            raise ValueError(f"File is larger than {max_bytes // (1024 * 1024)} MB - "
                             f"not a CSV table: {path}")
        head = fh.read(2)
        fh.seek(0)
        utf16 = head in (b"\xff\xfe", b"\xfe\xff")
        if max_rows is None:
            raw = fh.read(max_bytes + 1)        # the file may have grown since os.stat
            if len(raw) > max_bytes:
                raise ValueError(f"File is larger than {max_bytes // (1024 * 1024)} MB - "
                                 f"not a CSV table: {path}")
        else:                              # preview: at most 256 KB, whatever the file looks like
            raw = fh.read(262144)          # (a file without line breaks would be read whole)
            if len(raw) == 262144 and not utf16:      # do not cut a multi-byte character
                cut = max(raw.rfind(b"\n"), raw.rfind(b"\r"))
                if cut > 0:
                    raw = raw[:cut]
    if utf16:
        raw = raw[:len(raw) // 2 * 2]
        text = raw.decode("utf-16", errors="replace" if max_rows is not None else "strict")
    else:
        if b"\x00" in raw:
            raise ValueError("Binary file (contains NUL bytes), not a CSV table: " + path)
        text = None
        for enc in ("utf-8-sig", "cp1250", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                logging.getLogger(__name__).debug("CSV is not %s, trying the next encoding", enc)
                continue
    first = next((ln for ln in text[:65536].splitlines() if ln.strip()), "")   # first non-blank line
    if delimiter is None:
        def _splits(cand):                # the delimiter must split the line outside the quotes
            try:
                return len(next(csv.reader([first], delimiter=cand))) > 1
            except (csv.Error, StopIteration):
                return False
        for cand in (";", "\t", "|", ","):
            if cand in first and _splits(cand):
                delimiter = cand
                break
        else:
            delimiter = " " if len(first.split()) > 1 else ","
    if max_rows is not None:
        text = text[:100000]               # a preview needs only the first lines
    raw = None                             # raw bytes and text would otherwise coexist with rows
    rows = []
    try:
        for r in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter,
                            skipinitialspace=(delimiter == " ")):
            if not any(c.strip() for c in r):
                continue
            if len(r) > MAX_TABLE_COLUMNS:
                raise ValueError(f"More than {MAX_TABLE_COLUMNS} columns - not a CSV table: {path}")
            if max(map(len, r)) > MAX_TABLE_CELL:
                raise ValueError(f"A cell longer than {MAX_TABLE_CELL} characters (row "
                                 f"{len(rows) + 1}) - not a CSV table: {path}")
            rows.append(r)
            if max_rows is not None and len(rows) > max_rows:
                break                      # a preview needs only the header and a few rows
            if len(rows) > MAX_TABLE_ROWS:
                raise ValueError(f"More than {MAX_TABLE_ROWS:,} rows - too large for the plugin: "
                                 f"{path}")
    except csv.Error as e:
        raise ValueError(f"Not a valid CSV table ({e}): {path}")
    finally:
        text = None
    if not rows:
        raise ValueError("File is empty: " + path)
    if max_rows is not None:
        rows = rows[:max_rows + 1]
    # a blank header would collide with other blank headers (and with "no column") later on
    header, seen = [], {}
    for i, h in enumerate(rows[0]):
        h = h.strip() or f"(column {i + 1})"
        if h in seen:                          # two equal names would overwrite each other
            seen[h] += 1
            h = f"{h} ({seen[h]})"
        else:
            seen[h] = 1
        header.append(h)
    return header, rows[1:]


def to_float(s):
    """Number from text ('1,5', '1 000.5'); None for text, NaN and +/-Inf (the latter would
    break the sorting of layers and the JSON of the 3D viewer)."""
    try:
        v = float(str(s).strip().replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    return v if math.isfinite(v) else None


# Stratigraphy, youngest -> oldest (index grows with age): ONE table, so that the names, the
# labels and the colours (ICS) of a period can never get out of step
STRATIGRAPHY = [
    # (key, Czech label, English name, colour)
    ("kvarter", "kvartér", "Quaternary", "#F9F97F"),
    ("neogen", "neogén", "Neogene", "#FFE619"),
    ("paleogen", "paleogén", "Palaeogene", "#FD9A52"),
    ("krida", "křída", "Cretaceous", "#7FC64E"),
    ("jura", "jura", "Jurassic", "#34B2C9"),
    ("trias", "trias", "Triassic", "#812B92"),
    ("perm", "perm", "Permian", "#F04028"),
    ("karbon", "karbon", "Carboniferous", "#67A599"),
    ("devon", "devon", "Devonian", "#CB8C37"),
    ("silur", "silur", "Silurian", "#B3E1B6"),
    ("ordovik", "ordovik", "Ordovician", "#009270"),
    ("kambrium", "kambrium", "Cambrian", "#7FA056"),
    ("proterozoikum", "proterozoikum", "Proterozoic", "#F74370"),
]
STRAT_NAMES = [r[0] for r in STRATIGRAPHY]
STRAT_LABELS = [r[1] for r in STRATIGRAPHY]
STRAT_ENGLISH = [r[2] for r in STRATIGRAPHY]
STRAT_COLORS = [r[3] for r in STRATIGRAPHY]
_STRAT_TERMS = {n: (i, i) for i, n in enumerate(STRAT_NAMES)}
_STRAT_TERMS.update({
    "tercier": (1, 2), "mesozoikum": (3, 5), "paleozoikum": (6, 11), "permokarbon": (6, 7),
    # English / Latin synonyms
    "quaternary": (0, 0), "neogene": (1, 1), "paleogene": (2, 2), "cretaceous": (3, 3),
    "jurassic": (4, 4), "triassic": (5, 5), "permian": (6, 6), "carboniferous": (7, 7),
    "devonian": (8, 8), "silurian": (9, 9), "ordovician": (10, 10), "cambrian": (11, 11),
    "proterozoic": (12, 12), "tertiary": (1, 2), "mesozoic": (3, 5), "paleozoic": (6, 11),
    # British spelling
    "palaeogene": (2, 2), "palaeozoic": (6, 11),
})


# Czech adjective endings after the stem of a period name (kridovy, triasovy, permsky, kvarterni)
_STRAT_ENDING = re.compile(r"(?:(?:ov|sk|ck|n)?(?:y|a|e|i|ym|ych|ymi|eho|ou|em|ni)|a|u|y|e|ou)?")


def _strat_word(word):
    """Whole-word match of a period name: the word is the name itself or its Czech adjective
    ('triasovy', 'permsky'); 'permeable' or 'industrial' do not match."""
    if word in _STRAT_TERMS:
        return _STRAT_TERMS[word]
    for name in sorted(_STRAT_TERMS, key=len, reverse=True):
        stem = name[:-1] if name.endswith("a") and len(name) > 4 else name
        if len(stem) >= 4 and word.startswith(stem) and \
                _STRAT_ENDING.fullmatch(word[len(stem):]):
            return _STRAT_TERMS[name]
    return None


def parse_strat(text):
    """'neogén', 'Terciér', 'Cretaceous', 'křída-jura' ... -> (youngest_idx, oldest_idx) or None.

    Upper / lower case and diacritics are ignored (é = e, ř = r, ...).  A letter that was
    lost or garbled by a machine error ('k?ida', 'kida') is tolerated as well."""
    t = _norm(str(text).replace("\ufffd", "?"))
    if not t:
        return None
    found = []
    left = []
    for word in re.findall(r"[a-z?*]+", t):
        word = word.rstrip("?*") if word.strip("?*") else word   # 'kreda?', 'trias??': uncertain, not damaged
        if "?" not in word and "*" not in word:
            hit = _strat_word(word)
            if hit is not None:
                found.append(hit)
                continue
        left.append(word)
    # what is left: words with a lost or garbled character
    for word in (w for w in left if len(w) >= 4):
        rx = re.compile("^" + "".join("." if c in "?*" else re.escape(c) for c in word) + "$")
        hits = [n for n in _STRAT_TERMS if rx.match(n)]
        if not hits:
            hits = difflib.get_close_matches(word.replace("?", "").replace("*", ""),
                                             list(_STRAT_TERMS), n=1, cutoff=0.8)
        if hits:
            found.append(_STRAT_TERMS[hits[0]])
    if not found:
        return None
    return (min(a for a, _ in found), max(b for _, b in found))


def ages_compatible(a, b):
    """Unknown age never blocks a correlation; known ages must overlap."""
    if a is None or b is None:
        return True
    return a[0] <= b[1] and b[0] <= a[1]


def age_label(a):
    if a is None:
        return "?"
    return STRAT_LABELS[a[0]] if a[0] == a[1] else f"{STRAT_LABELS[a[0]]}-{STRAT_LABELS[a[1]]}"


def guess_column(header, keywords, default=0):
    """Index of best matching column (exact match first, then 'contains')."""
    h = [_norm(x) for x in header]
    for k in keywords:
        if k in h:
            return h.index(k)
    for k in keywords:
        if len(k) >= 3:
            for i, c in enumerate(h):
                if k in c:
                    return i
    return default


def _convert_to_work_crs(bhs, raw_line, src, src_id, warnings):
    """Boreholes given in degrees (or Web Mercator) -> metres of the UTM zone of their centre.

    bhs: name -> Borehole with the coordinates of the file; the boreholes that cannot be
    converted are removed.  Returns the EPSG code of the working system."""
    ctx = QgsProject.instance().transformContext()
    geographic = src.isGeographic()

    def lonlat(b):
        """Longitude / latitude [deg] of a borehole without a transformation: the file is in
        degrees (of its own datum - the difference to WGS84 does not matter for the zone) or in
        Web Mercator (spherical formulas of EPSG:3857)."""
        if geographic:
            return b.x, b.y
        return (math.degrees(b.x / WEB_MERCATOR_RADIUS),
                math.degrees(2.0 * math.atan(math.exp(b.y / WEB_MERCATOR_RADIUS)) - math.pi / 2.0))
    # centre of the boreholes in degrees: the longitude is a circular mean (boreholes at 179.9
    # and -179.9 deg lie at the antimeridian, not at 0 deg); at most ~2000 boreholes are used
    items = list(bhs.values())
    step = max(1, len(items) // 2000)
    try:
        s_sin = s_cos = s_lat = 0.0
        n_ok = 0
        for b in items[::step]:
            try:
                w_lon, w_lat = lonlat(b)
            except (OverflowError, ValueError) as e:     # the borehole is skipped for the centre
                log_debug(f"Centre of the boreholes: {b.name} skipped ({type(e).__name__}: {e})")
                continue
            if not (math.isfinite(w_lon) and math.isfinite(w_lat)):
                continue
            if not (UTM_LAT_MIN <= w_lat <= UTM_LAT_MAX):    # polar outliers are skipped below:
                continue                                      # they must not move the centre
            rl = math.radians(w_lon)
            s_sin += math.sin(rl)
            s_cos += math.cos(rl)
            s_lat += w_lat
            n_ok += 1
        if n_ok == 0:
            raise ValueError("centre of the boreholes is not finite")
        lon = math.degrees(math.atan2(s_sin, s_cos))
        lat = s_lat / n_ok
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise ValueError("centre of the boreholes is not finite")
    except Exception as e:                 # QgsCsException, ValueError
        raise ValueError(f"Cannot find the UTM zone of the boreholes ({type(e).__name__}: {e}); "
                         "check the coordinate system and the swap option.") from e
    work_id, note = utm_authid_and_note(lon, lat, src_id)
    if note:
        warnings.append("Coordinate system: " + note)
    xform = QgsCoordinateTransform(src, QgsCoordinateReferenceSystem(work_id), ctx)
    zone_c = min(max(int(math.floor((lon + 180.0) / 6.0)) + 1, 1), 60)
    cm = (zone_c - 1) * 6.0 - 180.0 + 3.0                  # central meridian of the chosen zone
    n_far, far_max, n_other_hemi = 0, 0.0, 0
    for name in list(bhs):
        bh = bhs[name]
        try:
            b_lat = lonlat(bh)[1]
            if not (UTM_LAT_MIN <= b_lat <= UTM_LAT_MAX):      # beyond the UTM grid (polar caps)
                raise ValueError(f"latitude {b_lat:.2f} deg is outside the UTM range "
                                 f"{UTM_LAT_MIN:g}..{UTM_LAT_MAX:g} deg")
            p = xform.transform(QgsPointXY(bh.x, bh.y))
            if not (math.isfinite(p.x()) and math.isfinite(p.y())
                    and abs(p.x()) <= MAX_COORD and abs(p.y()) <= MAX_COORD):
                raise ValueError("outside the valid range")
            if not (UTM_EASTING[0] <= p.x() <= UTM_EASTING[1] and abs(p.y()) <= UTM_NORTHING[1]):
                raise ValueError(f"UTM coordinates {p.x():.0f}, {p.y():.0f} are outside the range of the "
                                 "zone (the boreholes are too far from its central meridian)")
            b_lon = lonlat(bh)[0]
            d_lon = abs(((b_lon - cm + 180.0) % 360.0) - 180.0)
            if d_lon > 9.0:
                n_far += 1
                far_max = max(far_max, d_lon)
            if (b_lat >= 0) != (lat >= 0):
                n_other_hemi += 1
        except Exception as e:             # QgsCsException, ValueError (range) and the like
            warnings.append(f"Boreholes file, line {raw_line.get(name, '?')} ({name}): coordinate "
                            f"transformation failed ({type(e).__name__}: {e}) - skipped")
            del bhs[name]
            continue
        bh.x, bh.y = p.x(), p.y()
    if n_far:
        warnings.append(f"Coordinate system: {n_far} borehole(s) lie more than 9 deg of longitude "
                        f"(up to {far_max:.1f} deg) from the central meridian of the working UTM zone "
                        f"{work_id} - distances there are distorted; the data should lie in one or two "
                        "neighbouring zones")
    if n_other_hemi:
        warnings.append(f"Coordinate system: {n_other_hemi} borehole(s) lie in the other hemisphere than "
                        f"the centre of the data - their northings are negative / shifted by 10 000 000 m "
                        f"in {work_id}")
    return work_id


def build_boreholes(h1, r1, m1, h2, r2, m2, crs_key="sjtsk", negate=True, swap=False,
                    utm_zone=None, utm_south=False):
    """m1: dict name,x,y,z,depth[,azimuth,dip] -> column index (-1 = none).
    m2: dict name,top,bottom,litho[,strat] -> column index (-1 = none).

    The coordinate system of the CSV file is given by crs_key (see INPUT_CRS; for the UTM
    families also utm_zone and utm_south).  A projected system is used as it is - the boreholes
    keep the coordinates of the file.  A geographic system (degrees) and Web Mercator cannot be
    used for distances in metres, so those boreholes are converted to the UTM zone of the data
    (see work_crs_authid()); source_crs_authid() is the system of the file.
    S-JTSK: column 'x' = X (northing), 'y' = Y (easting), both negative in
    EPSG:5514; QGIS point = (-Y, -X). `negate` adds the minus sign.
    Other CRS: column 'x' = lon/easting, 'y' = lat/northing.
    `swap` exchanges the meaning of the two coordinate columns.
    """
    src_id = crs_authid(crs_key, utm_zone, utm_south)
    src = QgsCoordinateReferenceSystem(src_id)
    if not src.isValid():
        raise ValueError(f"The coordinate system {src_id} is not known to this QGIS installation.")
    # degrees and Web Mercator (scale 1/cos(lat)) are not metric: work in a local UTM zone
    needs_work_crs = src.isGeographic() or src_id == CRS_WEB_MERCATOR
    raw_line = {}                      # borehole name -> line of the file (for the warnings)
    for key in ("name", "x", "y", "z", "depth"):
        if m1.get(key, -1) < 0:
            raise ValueError(f"Boreholes file: no column selected for '{key}'.")
    for key in ("name", "top", "bottom", "litho"):
        if m2.get(key, -1) < 0:
            raise ValueError(f"Layers file: no column selected for '{key}'.")
    warnings, bhs, bad_strat = [], {}, set()
    n_signed = n_positive = 0          # S-JTSK rows that already have a minus / have none and are not negated
    n_cut = 0                          # names cut to MAX_NAME_LEN
    az_idx = m1.get("azimuth", -1)
    dip_idx = m1.get("dip", -1)
    for n, r in enumerate(r1, start=2):
        try:
            name = r[m1["name"]].strip()
            if len(name) > MAX_NAME_LEN:
                name = name[:MAX_NAME_LEN]
                n_cut += 1
            sx, sy = to_float(r[m1["x"]]), to_float(r[m1["y"]])
            z, depth = to_float(r[m1["z"]]), to_float(r[m1["depth"]])
        except IndexError:
            warnings.append(f"Boreholes file, line {n}: too few columns - skipped")
            continue
        if not name or None in (sx, sy, z):
            warnings.append(f"Boreholes file, line {n}: invalid values - skipped")
            continue
        if depth is not None and depth > MAX_DEPTH:
            warnings.append(f"Boreholes file, line {n} ({name}): total depth {depth:g} m is "
                            f"over {MAX_DEPTH:g} m - skipped")
            continue
        if abs(z) > MAX_ELEV:
            warnings.append(f"Boreholes file, line {n} ({name}): elevation {z:g} m is outside "
                            f"+-{MAX_ELEV:g} m - skipped")
            continue
        if depth is None and r[m1["depth"]].strip():
            warnings.append(f"Boreholes file, line {n} ({name}): invalid total depth "
                            f"'{r[m1['depth']].strip()}' - the depth of the layers is used")
        if depth == 0:
            warnings.append(f"Boreholes file, line {n} ({name}): total depth 0 - the depth of the "
                            "layers is used")
        if depth is not None and depth < 0:
            warnings.append(f"Boreholes file, line {n} ({name}): negative total depth {depth:g} "
                            "- replaced by 0 (the depth of the layers is used)")
            depth = 0.0

        raw_az = r[az_idx].strip() if 0 <= az_idx < len(r) else ""
        if not raw_az:
            az = 0.0
        else:
            az = to_float(raw_az)
            if az is None:
                warnings.append(f"Boreholes file, line {n} ({name}): invalid azimuth '{raw_az}' - assumed 0")
                az = 0.0
            elif az < 0.0 or az > 360.0:
                warnings.append(f"Boreholes file, line {n} ({name}): azimuth {az:g} outside 0..360 deg - normalized")
                az = az % 360.0

        raw_dip = r[dip_idx].strip() if 0 <= dip_idx < len(r) else ""
        if not raw_dip:
            dip = 0.0
        else:
            dip = to_float(raw_dip)
            if dip is None:
                warnings.append(f"Boreholes file, line {n} ({name}): invalid dip/deviation '{raw_dip}' - assumed 0")
                dip = 0.0
            elif dip < 0.0 or dip > 90.0:
                warnings.append(f"Boreholes file, line {n} ({name}): dip/deviation {dip:g} outside 0..90 deg - clamped")
                dip = min(max(dip, 0.0), 90.0)
            if dip > 89.0:                    # cos(90 deg) = 0: the hole would collapse to zero height
                warnings.append(f"Boreholes file, line {n} ({name}): dip/deviation {dip:g} deg - limited to 89 deg")
                dip = 89.0
            elif dip > 85.0:
                warnings.append(f"Boreholes file, line {n} ({name}): nearly horizontal borehole "
                                f"(dip {dip:g} deg) - the 2D section uses at most 85 deg")

        if swap:
            sx, sy = sy, sx
        # geographic systems are in degrees, projected ones (S-JTSK, ETRS89 / UTM 33N) in metres
        if src.isGeographic():
            lim_x, lim_y = 180.0, 90.0
        elif src_id == CRS_WEB_MERCATOR:
            lim_x = lim_y = WEB_MERCATOR_LIMIT          # the whole world: +-20037508.34 m
        elif crs_key == "sjtsk":
            lim_x = lim_y = SJTSK_LIMIT
        else:
            lim_x, lim_y = MAX_COORD, MAX_COORD
        if is_utm(crs_key):                # easting / northing in metres (a unit or decimal error)
            ex, ny = sx, sy                # the columns are already swapped when asked
            ok = (UTM_EASTING[0] <= ex <= UTM_EASTING[1] and UTM_NORTHING[0] <= ny <= UTM_NORTHING[1])
            if not ok:
                warnings.append(f"Boreholes file, line {n} ({name}): UTM coordinates {sx:g}, {sy:g} "
                                f"are outside the range of easting {UTM_EASTING[0]:g}-{UTM_EASTING[1]:g} m "
                                f"and northing {UTM_NORTHING[0]:g}-{UTM_NORTHING[1]:g} m "
                                "(units? shifted decimal point? swapped columns?) - skipped")
                continue
        if abs(sx) > lim_x or abs(sy) > lim_y:
            warnings.append(f"Boreholes file, line {n} ({name}): coordinates {sx:g}, {sy:g} are "
                            "outside the valid range - skipped")
            continue
        if crs_key == "sjtsk":
            qx, qy = sy, sx
            if negate:
                if qx < 0 or qy < 0:
                    n_signed += 1
                qx, qy = -abs(qx), -abs(qy)
            elif qx > 0 or qy > 0:
                n_positive += 1
        else:
            qx, qy = sx, sy                   # kept as in the file (converted below if not metric)
        if name in bhs:
            warnings.append(f"Duplicate borehole '{name}' - last one used")
        bhs[name] = Borehole(name, qx, qy, z, depth or 0.0, azimuth=az, dip=dip)
        raw_line[name] = n

    for n, r in enumerate(r2, start=2):
        try:
            name = r[m2["name"]].strip()[:MAX_NAME_LEN]
            t, b = to_float(r[m2["top"]]), to_float(r[m2["bottom"]])
            lit = r[m2["litho"]].strip() or "(unknown)"
            if len(lit) > MAX_NAME_LEN:
                lit = lit[:MAX_NAME_LEN]
                n_cut += 1
            raw_age = r[m2["strat"]].strip() if 0 <= m2.get("strat", -1) < len(r) else ""
        except IndexError:
            warnings.append(f"Layers file, line {n}: too few columns - skipped")
            continue
        if name not in bhs:
            warnings.append(f"Layers file, line {n}: borehole '{name}' not in boreholes file")
            continue
        if t is None or b is None or b <= t:
            warnings.append(f"Layers file, line {n} ({name}): invalid interval - skipped")
            continue
        if t < -1e-6 or b > MAX_DEPTH:          # depths run downwards from the collar: no negative top
            warnings.append(f"Layers file, line {n} ({name}): depth {t:g}-{b:g} m is outside "
                            f"+-{MAX_DEPTH:g} m - skipped")
            continue
        age = parse_strat(raw_age) if raw_age else None
        if raw_age and age is None:
            bad_strat.add(raw_age)
        extra = {h: r[i].strip() for i, h in enumerate(h2) if i < len(r)}
        bhs[name].layers.append(Layer(t, b, lit, age, extra))

    for bh in bhs.values():
        bh.layers.sort(key=lambda L: L.top)
        if bh.layers:
            last = max(L.bottom for L in bh.layers)      # layers may overlap: the deepest bottom
            if bh.depth < last:
                if bh.depth:
                    warnings.append(f"{bh.name}: layers go deeper ({last}) than total depth ({bh.depth})")
                bh.depth = last
            running_bottom = bh.layers[0].bottom       # deepest bottom of the layers above
            for a, b in zip(bh.layers, bh.layers[1:]):
                if a.age and b.age and b.age[1] < a.age[0]:
                    warnings.append(f"{bh.name}: younger stratigraphy ({age_label(b.age)}) below "
                                    f"older ({age_label(a.age)}) at {b.top} m - fault or data error?")
                if b.top < running_bottom - 1e-6:
                    warnings.append(f"{bh.name}: overlapping layers at {b.top} m")
                elif b.top > running_bottom + 1e-6:
                    warnings.append(f"{bh.name}: gap between {running_bottom} and {b.top} m")
                running_bottom = max(running_bottom, b.bottom)
        else:
            warnings.append(f"{bh.name}: no layers")
    for raw in sorted(bad_strat):
        warnings.append(f"Unknown stratigraphy '{raw}' - layer treated as unknown age")
    if n_cut:
        warnings.append(f"{n_cut} borehole or lithology name(s) longer than {MAX_NAME_LEN} "
                        "characters were cut")
    if n_signed:
        warnings.append(f"S-JTSK: {n_signed} borehole(s) already have a minus sign although the option "
                        "'without minus sign' is on - the sign was made negative anyway (mixed signs "
                        "in the file?)")
    if n_positive:
        warnings.append(f"S-JTSK: {n_positive} borehole(s) have positive coordinates, but EPSG:5514 "
                        "needs negative ones - tick 'Coordinates are given without minus sign' "
                        "(they lie far outside the Czech Republic and Slovakia now)")
    work_id = src_id
    if needs_work_crs and bhs:
        work_id = _convert_to_work_crs(bhs, raw_line, src, src_id, warnings)
    if bhs:                    # no valid borehole: the system of the previous data stays
        wcrs = QgsCoordinateReferenceSystem(work_id)
        if not wcrs.isValid() or wcrs.isGeographic():
            raise ValueError(f"The working coordinate system {work_id or '(none)'} is not a metric "
                             "projected system - the distances in metres cannot be computed.")
        _DATA_CRS["source"] = src_id
        _DATA_CRS["work"] = work_id
    if crs_key == "sjtsk":
        # S-JTSK is used in the Czech Republic and Slovakia: a point elsewhere means a wrong
        # system, a missing minus sign or swapped columns (lon 11.5-22.7, lat 47.7-51.2)
        try:
            to_wgs = QgsCoordinateTransform(src, QgsCoordinateReferenceSystem("EPSG:4326"),
                                            QgsProject.instance().transformContext())
            out = 0
            for bh in bhs.values():
                p = to_wgs.transform(QgsPointXY(bh.x, bh.y))
                if not (11.5 <= p.x() <= 22.7 and 47.7 <= p.y() <= 51.2):
                    out += 1
            if out:
                warnings.insert(0, f"{out} borehole(s) lie outside the Czech Republic and Slovakia - "
                                   "check the coordinate system, the minus-sign option and the swap "
                                   "option")
        except Exception as e:
            # the location check is only advisory; keep the data but record why it was skipped
            log_warning(f"Coordinate plausibility check skipped: {type(e).__name__}: {e}")
    return list(bhs.values()), warnings


# ------------------------------------------------------------------- colours
_KEYWORDS = [
    (("jilovec",), "#6b4423"), (("pisk", "sandstone"), "#e6c27a"),     # specific words first,
    (("jil", "clay"), "#8b5a2b"),                                       # else "jil" / "sand" win
    (("pisek", "sand"), "#f2d16b"), (("sterk", "stk", "gravel"), "#e0a458"),
    (("hlina", "silt", "loam"), "#c9a66b"), (("navaz", "fill"), "#9a9a9a"),
    (("skal", "rock", "granit", "rula", "bridlic"), "#6e7b8b"),
    (("slin", "sinter"), "#a0a98f"),
    (("humus", "orn", "topsoil", "soil"), "#4b3621"), (("rasel", "peat"), "#3b3b2b"),
    (("vapen", "limestone"), "#cfd8dc"),
]
_PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b",
            "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]


def default_colors(lithos):
    out, i = {}, 0
    for lit in lithos:
        n = _norm(lit)
        col = None
        for keys, c in _KEYWORDS:
            if any(k in n for k in keys):
                col = c
                break
        if col is None:
            col = _PALETTE[i % len(_PALETTE)]
            i += 1
        out[lit] = QColor(col)
    return out


def lithologies(boreholes):
    seen = []
    for b in boreholes:
        for L in b.layers:
            if L.litho not in seen:
                seen.append(L.litho)
    return seen


# --------------------------------------------------------------- projection
def _extend_polyline(points, ext):
    """Extend the first and last segments of a polyline by `ext` so inclined boreholes
    near the ends of the section line project without endpoint clamping."""
    pts = [QgsPointXY(p) for p in points]
    if len(pts) < 2 or ext <= 0:
        return pts
    dx0, dy0 = pts[1].x() - pts[0].x(), pts[1].y() - pts[0].y()
    ln0 = math.hypot(dx0, dy0)
    if ln0 > 1e-9:
        pts[0] = QgsPointXY(pts[0].x() - ext * dx0 / ln0, pts[0].y() - ext * dy0 / ln0)
    dx1, dy1 = pts[-1].x() - pts[-2].x(), pts[-1].y() - pts[-2].y()
    ln1 = math.hypot(dx1, dy1)
    if ln1 > 1e-9:
        pts[-1] = QgsPointXY(pts[-1].x() + ext * dx1 / ln1, pts[-1].y() + ext * dy1 / ln1)
    return pts


OFF_LINE_TOL = 1.0     # m; default: boreholes farther from the section line are drawn dashed


def drop_close_boreholes(proj, min_gap):
    """Of boreholes closer together than `min_gap` (collar distance, m) only the one nearest
    to the section line is kept."""
    if min_gap <= 0:
        return proj
    kept = []
    for p in sorted(proj, key=lambda q: (q.offset, q.bh.name)):
        if all(math.hypot(p.bh.x - k.bh.x, p.bh.y - k.bh.y) >= min_gap for k in kept):
            kept.append(p)
    kept.sort(key=lambda q: q.dist)
    return kept


MAX_WALL_SLOPE = math.tan(math.radians(85.0))


def signed_offset(points, x, y):
    """Signed distance (+ = left of the direction of the line) of (x, y) from a polyline."""
    cands = []                               # (distance, sign, direction of the segment, end vertex)
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        dx, dy = bx - ax, by - ay
        ln2 = dx * dx + dy * dy
        if ln2 < 1e-18:
            continue
        t = min(max(((x - ax) * dx + (y - ay) * dy) / ln2, 0.0), 1.0)
        dist = math.hypot(x - (ax + t * dx), y - (ay + t * dy))
        cross = dx * (y - ay) - dy * (x - ax)
        ln = math.sqrt(ln2)
        cands.append((dist, 1.0 if cross >= 0 else -1.0, (dx / ln, dy / ln), (ax, ay), (bx, by), t))
    if not cands:
        return 0.0
    best = min(c[0] for c in cands)
    ties = [c for c in cands if c[0] <= best + 1e-9 * max(1.0, best)]
    sign = ties[0][1]
    if len({c[1] for c in ties}) > 1:
        # equally near to the end of one segment and the start of the next (an inner vertex of
        # a broken line) and the two cross products disagree: take the side of the bisector
        for c1, c2 in zip(ties, ties[1:]):
            if c1[4] == c2[3]:
                bx_, by_ = c1[2][0] + c2[2][0], c1[2][1] + c2[2][1]
                vx, vy = c1[4]
                if math.hypot(bx_, by_) > 1e-12:
                    sign = 1.0 if bx_ * (y - vy) - by_ * (x - vx) >= 0 else -1.0
                else:                       # the line turns back by 180 deg: normal of the first segment
                    nx_, ny_ = -c1[2][1], c1[2][0]
                    sign = 1.0 if nx_ * (x - vx) + ny_ * (y - vy) >= 0 else -1.0
                break
    return best * sign


def apply_cross_dip(proj, mode="auto", manual_deg=0.0):
    """Set the cross gradient kx of the strata for the projection into the section plane.
    mode 'off': kx = 0 (orthogonal projection of the real elevations); 'manual': the strata
    rise perpendicular to the section by `manual_deg` degrees (+ = to the left of the line);
    'auto': slope of a plane fitted to the collar elevations (ground surface) across the line.
    Returns kx."""
    kx = 0.0
    if mode == "manual":
        kx = math.tan(math.radians(max(-80.0, min(80.0, manual_deg))))
    elif mode == "auto" and len(proj) >= 3:
        n = len(proj)
        md_, ms_, mz_ = (sum(v) / n for v in zip(*[(p.d_top, p.s_top, p.bh.z) for p in proj]))
        sdd = sds = sss = sdz = ssz = 0.0
        for p in proj:
            a, b, z = p.d_top - md_, p.s_top - ms_, p.bh.z - mz_
            sdd += a * a
            sds += a * b
            sss += b * b
            sdz += a * z
            ssz += b * z
        det = sdd * sss - sds * sds
        if sss / n > 1.0 and det > 1e-6 * sdd * sss:      # boreholes spread across the line
            kx = (sdd * ssz - sds * sdz) / det
            lim = math.tan(math.radians(25.0))
            kx = max(-lim, min(lim, kx))
    for p in proj:
        p.kx = kx
    return kx


def project_boreholes(points, boreholes, max_offset, min_gap=0.0):
    line = QgsGeometry.fromPolylineXY(points)
    if len(points) < 2 or line.length() < 1e-6:
        return []          # degenerate (zero-length) section line
    max_dep = max([b.depth for b in boreholes] or [0.0])
    # the virtual prolongation of the end segments: enough for boreholes that lie beside the
    # ends of the line, but not so long that it could cut another part of a broken line
    ext = max_dep + min(max_offset, line.length() + 1000.0) + 10.0
    ext_pts = _extend_polyline(points, ext)
    ext_line = QgsGeometry.fromPolylineXY(ext_pts)
    has_ext = len(ext_pts) >= 2 and ext_line.length() > line.length()
    out = []
    sp = [(q.x(), q.y()) for q in (ext_pts if has_ext else _extend_polyline(points, 0))]
    for b in boreholes:
        bx, by, _ = b.point_at(b.depth)
        p_top = QgsPointXY(b.x, b.y)
        p_bot = QgsPointXY(bx, by)
        if math.hypot(bx - b.x, by - b.y) > 1e-6:
            trace = QgsGeometry.fromPolylineXY([p_top, p_bot])
        else:
            trace = QgsGeometry.fromPointXY(p_top)
        off = line.distance(trace)
        if off <= max_offset:
            g_top = QgsGeometry.fromPointXY(p_top)
            g_bot = QgsGeometry.fromPointXY(p_bot)
            if has_ext:
                d_top = ext_line.lineLocatePoint(g_top) - ext
                d_bot = ext_line.lineLocatePoint(g_bot) - ext
            else:
                d_top = line.lineLocatePoint(g_top)
                d_bot = line.lineLocatePoint(g_bot)
            d_mid = 0.5 * (d_top + d_bot)
            path = None
            if len(points) >= 3 and math.hypot(bx - b.x, by - b.y) > 1e-6:
                def path(md, b=b):          # point of the hole at md projected on the broken line
                    x, y, _ = b.point_at(md)
                    g = QgsGeometry.fromPointXY(QgsPointXY(x, y))
                    d = (ext_line.lineLocatePoint(g) - ext) if has_ext else line.lineLocatePoint(g)
                    return d, signed_offset(sp, x, y)
            out.append(Projected(b, d_mid, off, d_top, d_bot,
                                 signed_offset(sp, b.x, b.y), signed_offset(sp, bx, by), path=path))
    out.sort(key=lambda p: p.dist)
    return drop_close_boreholes(out, min_gap)


def _nice_step(span, target=8):
    raw = max(span, 1e-9) / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def clean_layers(layers):
    """Remove overlaps inside one borehole: only one rock at a time.
    The upper layer wins; the lower one is trimmed (or dropped)."""
    out, run = [], None
    for L in sorted(layers, key=lambda lay: (lay.top, lay.bottom)):
        top = L.top if run is None else max(L.top, run)
        if L.bottom - top > 1e-9:
            out.append(Layer(top, L.bottom, L.litho, L.age, L.extra, L.desc))
            run = L.bottom if run is None else max(run, L.bottom)
    return out


def merge_same_lithology(layers, tol=1e-6):
    """Merge directly neighbouring layers with the same lithology (and age).

    Different descriptions of the merged layers are joined with '; '."""
    out = []
    for L in layers:
        if (out and out[-1].litho == L.litho and out[-1].age == L.age
                and abs(L.top - out[-1].bottom) <= tol):
            p = out[-1]
            desc = p.desc
            if L.desc and L.desc != p.desc:
                desc = L.desc if not p.desc else f"{p.desc}; {L.desc}"
            out[-1] = Layer(p.top, L.bottom, L.litho, L.age, p.extra, desc)
        else:
            out.append(Layer(L.top, L.bottom, L.litho, L.age, L.extra, L.desc))
    return out


def align_layers(LA, LB, zA, zB):
    """Order-preserving, non-crossing correlation of two layer sequences.

    Returns a set of (i, j) pairs (indices into LA / LB).  One layer may be
    paired with several layers of the other borehole (splitting / merging)."""
    n, m = len(LA), len(LB)

    def mid(L):
        return (L.top + L.bottom) / 2.0

    def same(i, j):
        return LA[i].litho == LB[j].litho and ages_compatible(LA[i].age, LB[j].age)

    def dz(i, j):
        return abs((zA - mid(LA[i])) - (zB - mid(LB[j])))

    def w(i, j):
        return 1.0 + 0.4 / (1.0 + dz(i, j))

    f = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = max(f[i - 1][j], f[i][j - 1])
            if same(i - 1, j - 1):
                best = max(best, f[i - 1][j - 1] + w(i - 1, j - 1))
            f[i][j] = best
    pairs, i, j = set(), n, m
    while i > 0 and j > 0:
        if (same(i - 1, j - 1)
                and abs(f[i][j] - (f[i - 1][j - 1] + w(i - 1, j - 1))) < 1e-12):
            pairs.add((i - 1, j - 1))
            i -= 1
            j -= 1
        elif abs(f[i][j] - f[i - 1][j]) < 1e-12:
            i -= 1
        else:
            j -= 1

    def consistent(i, j):
        return all((a - i) * (b - j) >= 0 for a, b in pairs)

    # extension: split (one A layer -> several B layers) and merge (the opposite)
    while True:
        mA, mB = {a for a, _ in pairs}, {b for _, b in pairs}
        cands = []
        for j in range(m):
            for i in range(n):
                if ((j not in mB) or (i not in mA)) and (i, j) not in pairs \
                        and same(i, j) and consistent(i, j):
                    cands.append((dz(i, j), i, j))
        if not cands:
            break
        _, i, j = min(cands)
        pairs.add((i, j))
    return pairs


def _run_age(run):
    """Thickness-weighted mean age index of a list of layers (None = unknown)."""
    tot = sum(lay.bottom - lay.top for lay in run)
    known = sum(lay.bottom - lay.top for lay in run if lay.age is not None)
    if tot <= 0 or known < 0.5 * tot:
        return None
    return sum((lay.bottom - lay.top) * (lay.age[0] + lay.age[1]) / 2.0
               for lay in run if lay.age is not None) / known


def _older(X, Z, margin=1.5):
    """True when the layers X are clearly older than the layers Z."""
    ax, az = _run_age(X), _run_age(Z)
    return ax is not None and az is not None and ax - az >= margin


def _run_range(run):
    if not run or any(lay.age is None for lay in run):
        return None
    return (min(lay.age[0] for lay in run), max(lay.age[1] for lay in run))


def _thickness(run):
    return sum(lay.bottom - lay.top for lay in run)


def fence_secant(LA, LB, zA, zB, xa, xb, YA, YB):
    """Median dip (scene dy/dx) of the correlated contacts, None if no match."""
    span = xb - xa
    sl = []
    for i, j in align_layers(LA, LB, zA, zB):
        sl.append((YB(LB[j].top) - YA(LA[i].top)) / span)
        sl.append((YB(LB[j].bottom) - YA(LA[i].bottom)) / span)
    if not sl:
        return None
    sl.sort()
    return sl[len(sl) // 2]


def blend_slopes(s1, s2):
    """Slope at a borehole from the dips on both sides (no overshoot)."""
    if s1 is None:
        return 0.0 if s2 is None else s2
    if s2 is None:
        return s1
    if s1 * s2 <= 0:
        return 0.0
    return 2.0 * s1 * s2 / (s1 + s2)


def _band(top, bot, xl):
    """Polygons of the region between curves top(x) and bot(x) where bot > top;
    the ends are located by interpolation so layers taper out cleanly."""
    ds = [bot(x) - top(x) for x in xl]
    out, i, n = [], 0, len(xl)
    while i < n:
        if ds[i] <= 1e-6:
            i += 1
            continue
        j = i
        while j + 1 < n and ds[j + 1] > 1e-6:
            j += 1
        seg = list(xl[i:j + 1])
        if i > 0:
            d0, d1 = ds[i - 1], ds[i]
            seg.insert(0, xl[i - 1] + (xl[i] - xl[i - 1]) * (-d0) / (d1 - d0))
        if j < n - 1:
            d0, d1 = ds[j], ds[j + 1]
            seg.append(xl[j] + (xl[j + 1] - xl[j]) * d0 / (d0 - d1))
        out.append([(x, top(x)) for x in seg] + [(x, bot(x)) for x in reversed(seg)])
        i = j + 1
    return out


def _clip_halfplane(pts, f, keep_negative=True):
    """Sutherland-Hodgman clip of a polygon by the line f(point) = 0."""
    sgn = 1.0 if keep_negative else -1.0
    out = []
    n = len(pts)
    for k in range(n):
        p, q = pts[k], pts[(k + 1) % n]
        fp, fq = sgn * f(p), sgn * f(q)
        if fp <= 0:
            out.append(p)
        if (fp < 0 < fq) or (fq < 0 < fp):
            t = fp / (fp - fq)
            out.append((p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t))
    return out


def fence_polygons(LA, LB, zA, zB, xa, xb, YA, YB, curvature=0.6, roundness=0.6,
                   dips=None, transitions=True, bedrock=None, cover=False):
    """Polygons between two neighbouring borehole columns.

    YA(depth) / YB(depth) -> scene y.  dips = (secant, slope_at_A, slope_at_B)
    in scene dy/dx; boundaries follow the dip of the neighbouring boreholes.
    curvature 0..1 (0 = straight lines), roundness 0..1 (pinch-out ends).
    bedrock = minimal slope (scene dy/dx) of an older surface that is not
    reached by the younger borehole, or None.

    Situations between the columns:
      * matched layers (same lithology and compatible age): smooth contacts,
        splitting / merging of layers possible,
      * older and younger rocks (known stratigraphy): the younger layers lap
        onto the older surface, older layers continue with their dip and are
        cut by that surface,
      * unmatched layers on both sides with similar total thickness: lateral
        change of lithology, drawn as a jagged boundary,
      * otherwise: layers taper out; thin layers end close to their own
        borehole, thick layers reach further.
    Everything is constructed inside the space left by the neighbours, so the
    polygons do not overlap."""
    if not LA or not LB:
        return []
    pairs = sorted(align_layers(LA, LB, zA, zB))
    span = xb - xa
    xm = (xa + xb) / 2.0
    kc = min(max(curvature, 0.0), 1.0)
    P = 1.0 - 0.5 * min(max(roundness, 0.0), 1.0)
    GAMMA = 0.5
    sec, mA, mB = dips if dips else (0.0, 0.0, 0.0)
    mAe, mBe = sec + kc * (mA - sec), sec + kc * (mB - sec)
    D = sec * span

    def tau(x):
        t = (x - xa) / span
        return (1 - kc) * t + kc * t * t * (3 - 2 * t)

    def base(x):
        t = (x - xa) / span
        return ((3 * t * t - 2 * t ** 3) * D
                + ((t ** 3 - 2 * t * t + t) * mAe + (t ** 3 - t * t) * mBe) * span)

    def S(yl, yr):
        return lambda x: base(x) + (1 - tau(x)) * yl + tau(x) * (yr - D)

    def adjust(f, yl, yr):
        return lambda x: (f(x) + (yl - f(xa)) * (1 - tau(x)) + (yr - f(xb)) * tau(x))

    def wf(w):
        w = min(max(w, 0.0), 1.0)
        return (1.0 - (1.0 - w) ** 2) ** P

    N = 14
    taus = [(k / N) ** 2 for k in range(N + 1)]
    xs_left = [xm - (xm - xa) * t for t in reversed(taus)]     # xa .. xm
    xs_right = [xm + (xb - xm) * t for t in taus]               # xm .. xb
    xs = sorted(set(round(x, 6) for x in
                    xs_left + xs_right + [xa + span * k / 40.0 for k in range(41)]))
    xd = sorted(set(xs + [round(xa + span * k / 120.0, 6) for k in range(121)]))

    def split(own, other, idx_own, idx_other):
        res = {}
        for i in {p[idx_own] for p in pairs}:
            parts = [p for p in pairs if p[idx_own] == i]
            tot = sum(other[p[idx_other]].bottom - other[p[idx_other]].top for p in parts)
            cum = 0.0
            for p in parts:
                t = other[p[idx_other]].bottom - other[p[idx_other]].top
                th = own[i].bottom - own[i].top
                res[p] = (own[i].top + th * cum / tot, own[i].top + th * (cum + t) / tot)
                cum += t
        return res

    left = split(LA, LB, 0, 1)
    right = split(LB, LA, 1, 0)
    nP, n, m = len(pairs), len(LA), len(LB)

    def fan(G, L, d0, d1, xsw, cf):
        res, run = [], d1 - d0
        if not G or run <= 1e-9:
            return res
        for i in G:
            f0 = min(max((L[i].top - d0) / run, 0.0), 1.0)
            f1 = min(max((L[i].bottom - d0) / run, 0.0), 1.0)
            pts = ([(x, cf(x, f0)) for x in xsw] + [(x, cf(x, f1)) for x in reversed(xsw)])
            res.append((pts, L[i].litho, L[i].age))
        return res

    def trough_end(Ubase, xO, xY):
        """Older strata end where the base of the younger stratum is deepest
        ('most concave'), but not before 30 % and not after 90 % of the span."""
        best_x, best_y = xO, Ubase(xO)
        for x in xd:
            y = Ubase(x)
            if y > best_y:
                best_x, best_y = x, y
        D = min(max(abs(best_x - xO), 0.3 * span), 0.9 * span)
        return xO + math.copysign(D, xY - xO)

    def make_taper(xO, xend):
        De = abs(xend - xO)
        d0 = 0.4 * De

        def w(x):
            d = abs(x - xO)
            if d <= d0:
                return 1.0
            if d >= De:
                return 0.0
            return wf((De - d) / (De - d0))
        return w

    def end_samples(xO, xend):
        """Sample positions between the borehole and the end, dense near the end."""
        De = xend - xO
        pts = [xO + De * k / 40.0 for k in range(41)]
        pts += [xend - De * (k / 16.0) ** 2 for k in range(17)]
        return sorted(set(round(x, 6) for x in pts))

    def run_info(G, L, dd, Ysrc, side, dmax):
        """Segments of a run (layers and unlogged gaps) with their tip positions:
        the thinner the layer, the closer to its own borehole it ends."""
        segs, cur = [], dd[0]
        for i in G:
            lay = L[i]
            if lay.top > cur + 1e-9:
                segs.append([Ysrc(lay.top) - Ysrc(cur), None, None])
            segs.append([Ysrc(lay.bottom) - Ysrc(lay.top), lay.litho, lay.age])
            cur = lay.bottom
        if dd[1] > cur + 1e-9:
            segs.append([Ysrc(dd[1]) - Ysrc(cur), None, None])
        dist = []
        for h, lit, _age in segs:
            if lit is None:
                dist.append(None)
            else:
                dist.append(span * (0.08 + (dmax - 0.08)
                                    * (1.0 - math.exp(-max(h, 0.0) / (0.25 * span)))))
        denv = max([d for d in dist if d is not None] or [dmax * span])
        tips = []
        for d in dist:
            d = denv if d is None else d
            tips.append(xa + d if side == "A" else xb - d)
        return {"segs": segs, "tips": tips, "denv": denv, "side": side}

    def taper_polys(info, ctop, cbot):
        side, segs, tips = info["side"], info["segs"], info["tips"]
        hs = [sg[0] for sg in segs]
        toth = sum(hs) or 1.0

        def omega(i, x):
            xt = tips[i]
            if side == "A":
                if xt - xa <= 1e-12:                 # zero-length wedge: no division by zero
                    return 0.0
                return hs[i] * wf((xt - x) / (xt - xa)) if x <= xt else 0.0
            if xb - xt <= 1e-12:
                return 0.0
            return hs[i] * wf((x - xt) / (xb - xt)) if x >= xt else 0.0

        def frac(j, x):
            om = [omega(i, x) for i in range(len(segs))]
            tot = sum(om)
            if tot < 1e-9:
                return sum(hs[:j]) / toth
            return sum(om[:j]) / tot

        res = []
        for s, (h, lit, age) in enumerate(segs):
            if lit is None or h <= 1e-9:
                continue
            xt = tips[s]
            if side == "A":
                xsw = [xt - (xt - xa) * (k / 16.0) ** 2 for k in range(16, -1, -1)]
            else:
                xsw = [xt + (xb - xt) * (k / 16.0) ** 2 for k in range(0, 17)]
            up = [(x, ctop(x) + (cbot(x) - ctop(x)) * frac(s, x)) for x in xsw]
            lo = [(x, ctop(x) + (cbot(x) - ctop(x)) * frac(s + 1, x)) for x in reversed(xsw)]
            res.append((up + lo, lit, age))
        return res

    gaps = []
    for c in range(nP + 1):
        a0 = pairs[c - 1][0] + 1 if c > 0 else 0
        a1 = pairs[c][0] if c < nP else n
        b0 = pairs[c - 1][1] + 1 if c > 0 else 0
        b1 = pairs[c][1] if c < nP else m
        Ag, Bg = list(range(a0, a1)), list(range(b0, b1))
        above = pairs[c - 1] if c > 0 else None
        below = pairs[c] if c < nP else None

        def edge(G, L, iv, above=above, below=below):
            top = (iv[above][1] if above else (L[G[0]].top if G else iv[below][0]))
            bot = (iv[below][0] if below else (L[G[-1]].bottom if G else iv[above][1]))
            return top, bot

        dA = edge(Ag, LA, left)
        dB = edge(Bg, LB, right)
        T = S(YA(dA[0]), YB(dB[0]))
        Bt = S(YA(dA[1]), YB(dB[1]))
        runA = [LA[i] for i in Ag]
        runB = [LB[j] for j in Bg]

        mode, young = "lens", None
        if cover:               # Quaternary cover: continuous, fills its space completely
            if Ag and Bg:
                mode = "trans"
            elif Ag or Bg:
                mode = "fill"
        elif Ag and Bg:
            if _older(runA, runB):
                mode, young = "stack", "B"
            elif _older(runB, runA):
                mode, young = "stack", "A"
            elif transitions and 0.4 <= (min(_thickness(runA), _thickness(runB))
                                         / max(_thickness(runA), _thickness(runB), 1e-9)) \
                    and ages_compatible(_run_range(runA), _run_range(runB)):
                mode = "trans"
        elif Ag and c == nP and _older(runA, LB):
            mode = "ext"
        elif Bg and c == nP and _older(runB, LA):
            mode = "ext"

        polys = []
        if mode == "lens":
            dmax = 0.5 if (Ag and Bg) else 0.7
            iA = run_info(Ag, LA, dA, YA, "A", dmax) if Ag else None
            iB = run_info(Bg, LB, dB, YB, "B", dmax) if Bg else None
            xtA = xa + iA["denv"] if iA else None
            xtB = xb - iB["denv"] if iB else None

            def wx(x, xtA=xtA, xtB=xtB):
                if xtA is not None and x <= xtA:
                    return wf((xtA - x) / (xtA - xa))
                if xtB is not None and x >= xtB:
                    return wf((x - xtB) / (xb - xtB))
                return 0.0

            if c == nP and (bool(Ag) != bool(Bg)):
                # layers below the bottom of the neighbouring borehole: they cannot be
                # correlated and end "in the ground" - horizontal, with rounded ends
                yb0 = YA(dA[1]) if Ag else YB(dB[1])
                ctop = T

                def cbot(x, T=T, wx=wx, yb0=yb0):
                    return T(x) + max(yb0 - T(x), 0.0) * wx(x)
            else:
                def ctop(x, T=T, Bt=Bt, wx=wx):
                    return T(x) + (Bt(x) - T(x)) * GAMMA * (1.0 - wx(x))

                def cbot(x, T=T, Bt=Bt, wx=wx):
                    return T(x) + (Bt(x) - T(x)) * (GAMMA + (1.0 - GAMMA) * wx(x))

            for info in (iA, iB):
                if info:
                    polys += taper_polys(info, ctop, cbot)

        elif mode == "fill":    # one-sided layers of the cover fill the whole gap
            ctop, cbot = T, Bt

            def cf(x, f, T=T, Bt=Bt):
                return T(x) + (Bt(x) - T(x)) * f
            polys = fan(Ag, LA, dA[0], dA[1], xs, cf) + fan(Bg, LB, dB[0], dB[1], xs, cf)

        elif mode == "ext":     # older strata not found in the shallower borehole: they end
            xO, xY = (xa, xb) if Ag else (xb, xa)
            xend = trough_end(T, xO, xY)
            wO = make_taper(xO, xend)
            ctop = T

            def cbot(x, T=T, Bt=Bt, wO=wO):
                return T(x) + (Bt(x) - T(x)) * wO(x)

            def cf(x, f, T=T, Bt=Bt, wO=wO):
                return T(x) + (Bt(x) - T(x)) * f * wO(x)
            xe = end_samples(xO, xend)
            polys = (fan(Ag, LA, dA[0], dA[1], xe, cf) + fan(Bg, LB, dB[0], dB[1], xe, cf))

        elif mode == "trans":   # lateral change of lithology, jagged boundary
            ctop, cbot = T, Bt
            amp = min(0.05 * span, 10.0)
            # the sampled grid xs has a step of span / 40; the ends of the jagged band are added so
            # that the boundary follows the top / bottom curves right up to the zigzag
            xl = [x for x in xs if x < xm - amp - 1e-9] + [xm - amp]
            xr = [xm + amp] + [x for x in xs if x > xm + amp + 1e-9]

            def cq(x, f, T=T, Bt=Bt):
                return T(x) + (Bt(x) - T(x)) * f
            nT = max(3, int(abs(Bt(xm) - T(xm)) / 16.0))
            verts = ([(0.0, 0.0)] + [(k / (2.0 * nT), 1.0 if k % 2 else -1.0)
                                     for k in range(1, 2 * nT)] + [(1.0, 0.0)])

            def zz(u0, u1, cq=cq, verts=verts, amp=amp):
                def pt(u):
                    s = 0.0
                    for (ua, sa), (ub, sb) in zip(verts, verts[1:]):
                        if ua <= u <= ub:
                            s = sa + (sb - sa) * ((u - ua) / (ub - ua))
                            break
                    x = xm + amp * s
                    return (x, cq(x, u))
                pts = [pt(u0)]
                pts += [pt(ua) for ua, _ in verts if u0 < ua < u1]
                pts.append(pt(u1))
                return pts

            def frac(L, i, d0, d1):
                run = d1 - d0
                return (min(max((L[i].top - d0) / run, 0.0), 1.0),
                        min(max((L[i].bottom - d0) / run, 0.0), 1.0))

            if dA[1] - dA[0] > 1e-9 and dB[1] - dB[0] > 1e-9:
                for i in Ag:
                    f0, f1 = frac(LA, i, *dA)
                    pts = ([(x, cq(x, f0)) for x in xl] + zz(f0, f1)
                           + [(x, cq(x, f1)) for x in reversed(xl)])
                    polys.append((pts, LA[i].litho, LA[i].age))
                for j in Bg:
                    g0, g1 = frac(LB, j, *dB)
                    zp = zz(g0, g1)
                    pts = ([zp[0]] + [(x, cq(x, g0)) for x in xr]
                           + [(x, cq(x, g1)) for x in reversed(xr)] + list(reversed(zp)))
                    polys.append((pts, LB[j].litho, LB[j].age))

        else:                   # "stack": younger layers lap onto the older surface
            ctop, cbot = T, Bt
            if young == "B":
                xY, xO, YY, YO, GY, LY, GO, LO = xb, xa, YB, YA, Bg, LB, Ag, LA

                def Uw(x):
                    return tau(x)
            else:
                xY, xO, YY, YO, GY, LY, GO, LO = xa, xb, YA, YB, Ag, LA, Bg, LB

                def Uw(x):
                    return 1.0 - tau(x)
            if c == nP and bedrock is not None:
                # nothing below: the older surface may run deeper than the drilled depth
                yO0 = T(xO)
                sU = max(bedrock, (Bt(xY) - yO0) / span, 0.0)

                def U(x, T=T, yO0=yO0, sU=sU, xO=xO):
                    return max(yO0 + sU * abs(x - xO), T(x))
            else:
                def U(x, T=T, Bt=Bt, Uw=Uw):
                    return T(x) + (Bt(x) - T(x)) * Uw(x)
            term = not (c == nP and bedrock is not None)
            if term:
                # older strata end at the deepest point of the younger base
                xend = trough_end(U, xO, xY)
                wO = make_taper(xO, xend)

                def cbot(x, U=U, Bt=Bt, wO=wO):
                    return U(x) + (Bt(x) - U(x)) * wO(x)
                xdo = sorted(set(xd + end_samples(xO, xend)))
            else:
                def wO(x):
                    return 1.0
                xdo = xd
            for k, idx in enumerate(GY):
                L = LY[idx]

                def ct(x, y=YY(L.top)):
                    return y + base(x) - base(xY)

                def cb(x, y=YY(L.bottom)):
                    return y + base(x) - base(xY)
                top = (lambda x, T=T: T(x)) if k == 0 else (lambda x, ct=ct, T=T: max(ct(x), T(x)))
                bot = U if k == len(GY) - 1 else (lambda x, cb=cb, U=U: min(cb(x), U(x)))
                polys += [(p, L.litho, L.age) for p in _band(top, bot, xd)]
            for k, idx in enumerate(GO):
                L = LO[idx]

                def ct(x, y=YO(L.top)):
                    return y + base(x) - base(xO)

                def cb(x, y=YO(L.bottom)):
                    return y + base(x) - base(xO)
                top = U if k == 0 else (lambda x, ct=ct, U=U, wO=wO:
                                        U(x) + (max(ct(x), U(x)) - U(x)) * wO(x))
                bot = ((lambda x, U=U, Bt=Bt, wO=wO: U(x) + (Bt(x) - U(x)) * wO(x))
                       if k == len(GO) - 1 else
                       (lambda x, cb=cb, U=U, Bt=Bt, wO=wO:
                        U(x) + (min(cb(x), Bt(x)) - U(x)) * wO(x)))
                polys += [(p, L.litho, L.age) for p in _band(top, bot, xdo)]

        gaps.append({"ctop": ctop, "cbot": cbot, "polys": polys, "has": bool(Ag or Bg)})

    out_main, out_wedge = [], []
    for c, p in enumerate(pairs):
        (lt, lb), (rt, rb) = left[p], right[p]
        top = (adjust(gaps[c]["cbot"], YA(lt), YB(rt)) if gaps[c]["has"]
               else S(YA(lt), YB(rt)))
        bot = (adjust(gaps[c + 1]["ctop"], YA(lb), YB(rb)) if gaps[c + 1]["has"]
               else S(YA(lb), YB(rb)))
        pts = [(x, top(x)) for x in xs] + [(x, bot(x)) for x in reversed(xs)]
        out_main.append((pts, LA[p[0]].litho, LA[p[0]].age or LB[p[1]].age))
    for g in gaps:
        out_wedge += g["polys"]
    return _no_overlap(out_main + out_wedge)


def _quat_run(layers):
    """Number of leading layers of Quaternary age (not affected by tectonics)."""
    n = 0
    for lay in layers:
        if lay.age == (0, 0):
            n += 1
        else:
            break
    return n


def _contact_curve(xa, xb, yl, yr, dips, curvature):
    """The same smooth contact curve that fence_polygons uses between two columns."""
    span = xb - xa
    kc = min(max(curvature, 0.0), 1.0)
    sec, mA, mB = dips if dips else (0.0, 0.0, 0.0)
    mAe, mBe = sec + kc * (mA - sec), sec + kc * (mB - sec)
    D = sec * span

    def f(x):
        t = (x - xa) / span
        tau = (1 - kc) * t + kc * t * t * (3 - 2 * t)
        base = ((3 * t * t - 2 * t ** 3) * D
                + ((t ** 3 - 2 * t * t + t) * mAe + (t ** 3 - t * t) * mBe) * span)
        return base + (1 - tau) * yl + tau * (yr - D)
    return f


def fault_polygons(LA, LB, xa, xb, YA, YB, pos, dip_deg, direction, mA, mB, aspect,
                   G=None):
    """Two separate blocks divided by a fault.  Layers of each borehole continue
    with the dip of their own block and are cut by the fault plane.
    pos 0..1 = fault position between the columns at the surface, direction
    +1/-1 = fault plane dips to the right/left (0 = vertical).
    Returns (polygons, ((x1, y1), (x2, y2)) fault line)."""
    if not LA and not LB:
        return [], None                 # nothing below the cover: no fault line to draw
    span = xb - xa
    if G is None:
        def G(x):
            return YA(0.0) + (YB(0.0) - YA(0.0)) * (x - xa) / span
    y_bot = max(YA(max([lay.bottom for lay in LA] or [0.0])), YB(max([lay.bottom for lay in LB] or [0.0])))
    x_ft = xa + min(max(pos, 0.05), 0.95) * span
    # the fault plane starts exactly at the ground surface at the requested position: a
    # different reference height (the higher collar) would shift it along the slope
    y_ref = G(x_ft)
    sf = 0.0
    if direction and dip_deg < 89.5:
        sf = direction / (max(aspect, 1e-6) * math.tan(math.radians(max(dip_deg, 20.0))))
    hy = max(y_bot - y_ref, 1e-6)
    allowed = min(x_ft - xa, xb - x_ft) - 2.0
    if allowed > 0 and abs(sf) * hy > allowed:
        sf = math.copysign(allowed / hy, sf)

    def xf(y):
        return x_ft + sf * (y - y_ref)

    def fval(p):
        return p[0] - xf(p[1])
    xd = sorted(set(round(xa + span * k / 120.0, 6) for k in range(121)))
    out = []
    for LL, Y_, x0, mm, keep_left in ((LA, YA, xa, mA, True), (LB, YB, xb, mB, False)):
        for k, lay in enumerate(LL):
            def ct(x, y=Y_(lay.top), x0=x0, mm=mm):
                return y + mm * (x - x0)

            def cb(x, y=Y_(lay.bottom), x0=x0, mm=mm):
                return y + mm * (x - x0)
            top = G if k == 0 else (lambda x, ct=ct: max(ct(x), G(x)))
            for poly in _band(top, cb, xd):
                clipped = _clip_halfplane(poly, fval, keep_negative=keep_left)
                if len(clipped) >= 3:
                    out.append((clipped, lay.litho, lay.age))
    y_top = G(x_ft)
    return _no_overlap(out), ((xf(y_top), y_top), (xf(y_bot), y_bot))


def _ring_area(r):
    return sum(r[k][0] * r[(k + 1) % len(r)][1] - r[(k + 1) % len(r)][0] * r[k][1]
               for k in range(len(r))) / 2.0


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segments_conflict(p, q, a, b, eps):
    """True when the open segment p-q is crossed or touched by the segment a-b anywhere but at
    the common end points (a bridge may start and end on a vertex, nowhere else)."""
    if a == p or a == q or b == p or b == q:
        # the segments share an end point: only a collinear overlap is a conflict
        if abs(_cross(p, q, a)) <= eps and abs(_cross(p, q, b)) <= eps:
            dx, dy = q[0] - p[0], q[1] - p[1]
            n2 = dx * dx + dy * dy
            if n2 <= 0.0:
                return False
            ta = ((a[0] - p[0]) * dx + (a[1] - p[1]) * dy) / n2
            tb = ((b[0] - p[0]) * dx + (b[1] - p[1]) * dy) / n2
            return max(min(ta, tb), 0.0) < min(max(ta, tb), 1.0) - 1e-12
        return False
    d1, d2 = _cross(p, q, a), _cross(p, q, b)
    d3, d4 = _cross(a, b, p), _cross(a, b, q)
    if ((d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)) and \
            ((d3 > eps and d4 < -eps) or (d3 < -eps and d4 > eps)):
        return True                                    # proper crossing

    def on_seg(u, v, w):                               # w lies on the segment u-v
        return (abs(_cross(u, v, w)) <= eps
                and min(u[0], v[0]) - 1e-9 <= w[0] <= max(u[0], v[0]) + 1e-9
                and min(u[1], v[1]) - 1e-9 <= w[1] <= max(u[1], v[1]) + 1e-9)
    return on_seg(p, q, a) or on_seg(p, q, b) or on_seg(a, b, p) or on_seg(a, b, q)


def _ring_edges(ring):
    n = len(ring)
    return [(ring[k], ring[(k + 1) % n]) for k in range(n)]


def _bridge_holes(rings):
    """One closed outline of a polygon with holes: every hole is joined to the outer ring by
    a zero-width bridge between two vertices (the hole stays open when filled).

    The bridge is the shortest connection that is VISIBLE: it must not cross or touch any edge
    of the outline (including the bridges already made), of the hole itself or of the holes still
    to be joined, and it must run inside the original polygon (its midpoint is in the polygon, not
    in a hole and not outside).  When no such connection exists (invalid input) the nearest pair
    is used as the last resort and the problem is reported."""
    outer = list(rings[0])
    if len(outer) > 1 and outer[0] == outer[-1]:
        outer.pop()
    sign = _ring_area(outer) >= 0
    holes = []
    for hole in rings[1:]:
        hole = list(hole)
        if len(hole) > 1 and hole[0] == hole[-1]:
            hole.pop()
        if len(hole) < 3:
            continue
        if (_ring_area(hole) >= 0) == sign:            # the hole runs the other way round
            hole.reverse()
        holes.append(hole)
    orig = [list(rings[0])] + [list(h) for h in holes]    # even-odd test of the bridge midpoint
    xs = [pt[0] for pt in outer]
    ys = [pt[1] for pt in outer]
    scale = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
    eps = 1e-12 * scale * scale                        # tolerance of the orientation tests

    def inside_polygon(pt):
        return sum(1 for r in orig if len(r) >= 3 and point_in_polygon(pt, r)) % 2 == 1

    todo = list(holes)
    while todo:
        hole = todo.pop(0)
        others = [e for h in todo for e in _ring_edges(h)]
        own = _ring_edges(hole)
        edges = _ring_edges(outer)
        pairs = sorted(((outer[a][0] - hole[b][0]) ** 2 + (outer[a][1] - hole[b][1]) ** 2, a, b)
                       for a in range(len(outer)) for b in range(len(hole)))
        best = None
        for _d, i, j in pairs:
            p, q = outer[i], hole[j]
            mid = ((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0)
            if not inside_polygon(mid):
                continue
            if any(_segments_conflict(p, q, u, v, eps) for u, v in edges) or \
                    any(_segments_conflict(p, q, u, v, eps) for u, v in own) or \
                    any(_segments_conflict(p, q, u, v, eps) for u, v in others):
                continue
            best = (i, j)
            break
        if best is None:
            geometry_issue("2D section: a hole of a polygon could not be joined to the outline by "
                           "a connection lying inside the polygon (invalid or degenerate "
                           "geometry); the nearest vertices were used and the fill of that "
                           "polygon may show an artefact")
            _d, i, j = pairs[0]
        else:
            i, j = best
        outer = (outer[:i + 1] + hole[j:] + hole[:j] + [hole[j], outer[i]] + outer[i + 1:])
    return outer


def _no_overlap(items):
    """Safety net: subtract everything already drawn from each polygon.

    When a GEOS operation fails (the result is a null geometry, or an exception is raised), the
    polygon is kept as it was - it is NOT corrected - and the failure is written to the QGIS log
    and registered with geometry_issue(), so the dialog can warn the user that the overlaps of
    that section were not removed."""
    acc, out = None, []
    try:
        for item in items:
            pts, rest = item[0], item[1:]
            raw = QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in pts]])
            g, failed = raw, None
            try:
                if not g.isGeosValid():
                    g = g.makeValid()
                    if g.isNull():
                        failed = "makeValid()"
                if failed is None and acc is not None:
                    g = g.difference(acc)
                    if g.isNull():              # an empty result is fine, a null one is an error
                        failed = "difference()"
            except Exception as e:
                failed = f"{type(e).__name__}: {e}"
            if failed is not None:
                geometry_issue(f"2D section: GEOS operation {failed} failed - the overlaps of "
                               f"some polygons were NOT removed (the uncorrected geometry is "
                               f"shown)")
                out.append((list(pts),) + rest)
                try:                           # at least keep the polygon out of the later ones
                    merged = raw if acc is None else acc.combine(raw)
                    if not merged.isNull():
                        acc = merged
                except Exception as e:
                    log_warning(f"2D section: combine() of an uncorrected polygon failed: "
                                f"{type(e).__name__}: {e}")
                continue
            if g.isEmpty():
                continue
            if acc is None:
                acc = g
            else:
                merged = acc.combine(g)
                if merged.isNull():
                    geometry_issue("2D section: GEOS operation combine() failed - later polygons "
                                   "may overlap the earlier ones")
                else:
                    acc = merged
            for part in g.asGeometryCollection():
                if part.type() != Qgis.GeometryType.Polygon:
                    continue        # degenerate line / point left by difference()
                rings = part.asPolygon()
                if not rings:
                    continue
                pp = _bridge_holes([[(q.x(), q.y()) for q in ring] for ring in rings])
                area = abs(sum(pp[k][0] * pp[(k + 1) % len(pp)][1]
                               - pp[(k + 1) % len(pp)][0] * pp[k][1]
                               for k in range(len(pp)))) / 2.0 if len(pp) >= 3 else 0
                if area > 1e-6:
                    out.append((pp,) + rest)
        return out
    except Exception as e:
        geometry_issue(f"2D section: removing the overlaps failed ({type(e).__name__}: {e}) - "
                       f"the polygons are shown uncorrected and may overlap")
        return items


def _age_disjoint(a, b):
    return a is not None and b is not None and not ages_compatible(a, b)


def point_in_polygon(pt, poly):
    """Even-odd test: is the point pt = (x, y) inside the polygon [(x, y), ...]? (one shared
    implementation, also used by model3d)"""
    x, y = pt
    ins = False
    n = len(poly)
    for k in range(n):
        x1, y1 = poly[k]
        x2, y2 = poly[(k + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            ins = not ins
    return ins


def age_boundary_lines(items, offset=1.5):
    """items = [(points, litho, age)] (section polygons and borehole columns).
    Returns polylines along the edges where polygons of different age touch."""
    boxes = []
    for it in items:
        xs = [p[0] for p in it[0]]
        ys = [p[1] for p in it[0]]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    lines = []
    for i, it in enumerate(items):
        pts, age = it[0], it[2]
        if age is None or len(pts) < 3:
            continue
        bi = boxes[i]
        hi = bi[3] - bi[1]
        cands = [j for j, o in enumerate(items)
                 if j != i and _age_disjoint(age, o[2])
                 and boxes[j][0] <= bi[2] + 2 * offset and boxes[j][2] >= bi[0] - 2 * offset
                 and boxes[j][1] <= bi[3] + 2 * offset and boxes[j][3] >= bi[1] - 2 * offset]
        if not cands:
            continue
        m = len(pts)
        flags = [False] * m
        for k in range(m):
            p, q = pts[k], pts[(k + 1) % m]
            dx, dy = q[0] - p[0], q[1] - p[1]
            ln = math.hypot(dx, dy)
            if ln < 1e-9:
                continue
            mx, my = (p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0
            for j in cands:
                b = boxes[j]
                if not (b[0] - offset <= mx <= b[2] + offset and b[1] - offset <= my <= b[3] + offset):
                    continue
                # the probe must stay inside a thin layer (never jump over it into the next one)
                off = min(offset, max(0.02, 0.25 * min(hi, b[3] - b[1])))
                nx, ny = -dy / ln * off, dx / ln * off
                if (point_in_polygon((mx + nx, my + ny), items[j][0])
                        or point_in_polygon((mx - nx, my - ny), items[j][0])):
                    flags[k] = True
                    break
        if not any(flags):
            continue
        if all(flags):
            lines.append(list(pts) + [pts[0]])
            continue
        start = flags.index(False)
        run = None
        for step in range(1, m + 1):
            k = (start + step) % m
            if flags[k]:
                if run is None:
                    run = [pts[k]]
                run.append(pts[(k + 1) % m])
            elif run is not None:
                lines.append(run)
                run = None
        if run is not None:
            lines.append(run)
    return lines


def _top_envelope(polys, xl):
    """y of the upper edge of the polygon union at each x (None where nothing is drawn)."""
    res = []
    for x in xl:
        ymin = None
        for item in polys:
            pts = item[0]
            m = len(pts)
            for k in range(m):
                x1, y1 = pts[k]
                x2, y2 = pts[(k + 1) % m]
                if (x1 <= x < x2) or (x2 <= x < x1):
                    y = y1 + (y2 - y1) * (x - x1) / (x2 - x1)
                    if ymin is None or y < ymin:
                        ymin = y
        res.append(ymin)
    return res


def _fill_cover_gap(rest, base, xa, xb, left, right):
    """Where the older layers (rest) start below the base of the Quaternary cover, the
    gap is filled with the nearest Quaternary layer (left / right = lowest Quaternary
    layers of the two boreholes)."""
    if not rest:
        return []
    span = xb - xa
    xm = (xa + xb) / 2.0
    xl = sorted(set([xa + span * k / 80.0 for k in range(81)] + [xm]))
    xl[0] += 1e-6
    xl[-1] -= 1e-6
    env = _top_envelope(rest, xl)
    ys = [e if e is not None else base(x) for x, e in zip(xl, env)]

    def bot(x):
        if x <= xl[0]:
            return ys[0]
        if x >= xl[-1]:
            return ys[-1]
        lo, hi = 0, len(xl) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if xl[mid] <= x:
                lo = mid
            else:
                hi = mid
        t = (x - xl[lo]) / (xl[hi] - xl[lo])
        return ys[lo] + (ys[hi] - ys[lo]) * t

    out = []
    for lay, sel in ((left, [x for x in xl if x <= xm]), (right, [x for x in xl if x >= xm])):
        if len(sel) < 2:
            continue
        for poly in _band(base, bot, sel):
            out.append((poly, lay.litho, lay.age))
    return out


def _bh_column_geom(p, X, Y, cw):
    """Scene-space geometry for a (possibly inclined) borehole column.

    Returns dict with:
      x0, y0   : collar center in scene coordinates
      x1, y1   : bottom center in scene coordinates
      tx, ty   : unit tangent vector along borehole (collar -> bottom)
      nx, ny   : right-hand unit normal vector perpendicular to borehole
      left(md) : (x, y) on left edge at measured depth `md`
      right(md): (x, y) on right edge at measured depth `md`
      quad(t, b): [(x, y), ...] 4 corners of interval [t, b]
    """
    bh = p.bh
    x0, y0 = X(p.d_top), Y(p.z_at(0.0))
    x1, y1 = X(p.d_bot), Y(p.z_at(bh.depth))
    dx, dy = x1 - x0, y1 - y0
    ln = math.hypot(dx, dy)
    if ln > 1e-9:
        tx, ty = dx / ln, dy / ln
    else:
        tx, ty = 0.0, 1.0
    nx, ny = ty, -tx
    hw = cw / 2.0

    def left(md):
        xc, yc = X(p.d_at(md)), Y(p.z_at(md))
        return (xc - hw * nx, yc - hw * ny)

    def right(md):
        xc, yc = X(p.d_at(md)), Y(p.z_at(md))
        return (xc + hw * nx, yc + hw * ny)

    def quad(top, bot):
        return [left(top), right(top), right(bot), left(bot)]

    return {
        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
        "tx": tx, "ty": ty, "nx": nx, "ny": ny,
        "left": left, "right": right, "quad": quad,
    }


def _map_to_inclined(pts, span_ref, x_A, x_B, y_cross_hi=None, y_cross_lo=None):
    """Map reference polygon coordinates (xr in [0, span_ref], y) onto the region
    between tilted borehole edges x_A(y) and x_B(y)."""
    mapped = []
    for xr, y in pts:
        u = xr / span_ref
        mapped.append(((1.0 - u) * x_A(y) + u * x_B(y), y))
    if y_cross_lo is not None:
        mapped = _clip_halfplane(mapped, lambda p: p[1] - y_cross_lo, keep_negative=True)
    if y_cross_hi is not None:
        mapped = _clip_halfplane(mapped, lambda p: y_cross_hi - p[1], keep_negative=True)
    return mapped


MAX_GROUND_SLOPE = 10.0          # limit of the ground slope (scene dy/dx) between two columns


def _fence_secant_inclined(LA, LB, YA, YB, x_A, x_B, zA=0.0, zB=0.0):
    """Median apparent dip (scene dy/dx) of correlated contacts between inclined walls.
    zA, zB: elevations of the collars - the same correlation as the one that is drawn."""
    sl = []
    for i, j in align_layers(LA, LB, zA, zB):
        ya_t, yb_t = YA(LA[i].top), YB(LB[j].top)
        dx_t = x_B(yb_t) - x_A(ya_t)
        if abs(dx_t) > 1e-6:
            sl.append((yb_t - ya_t) / dx_t)
        ya_b, yb_b = YA(LA[i].bottom), YB(LB[j].bottom)
        dx_b = x_B(yb_b) - x_A(ya_b)
        if abs(dx_b) > 1e-6:
            sl.append((yb_b - ya_b) / dx_b)
    if not sl:
        return None
    sl.sort()
    return sl[len(sl) // 2]


def correlation_polygons(proj, LAY, X, Y, cw, curvature=0.6, roundness=0.6,
                         use_dip=True, transitions=True, faults=None, aspect=1.0,
                         bedrock=None):
    """All correlation polygons of a section (supports both vertical and inclined boreholes).
    faults = {k: {"pos": 0..1, "dip": degrees, "dir": +1/-1/0}} for the fence
    between proj[k] and proj[k+1].  Layers of Quaternary age are not cut by
    faults.  Returns ([(points, litho, age)], fault_lines: one polyline [(x, y), ...] per fault)."""
    faults = faults or {}
    cols_geom = [_bh_column_geom(p, X, Y, cw) for p in proj]
    fences = []
    for k, (a, b) in enumerate(zip(proj, proj[1:])):
        gA, gB = cols_geom[k], cols_geom[k + 1]
        pRA0 = gA["right"](0.0)
        pLB0 = gB["left"](0.0)
        pRA1 = gA["right"](a.bh.depth)
        pLB1 = gB["left"](b.bh.depth)

        tyA = gA["ty"] if abs(gA["ty"]) > 1e-4 else (1e-4 if gA["ty"] >= 0 else -1e-4)
        tyB = gB["ty"] if abs(gB["ty"]) > 1e-4 else (1e-4 if gB["ty"] >= 0 else -1e-4)
        # a (nearly) horizontal borehole has ty -> 0: the slope would grow to +-10000 while the
        # layer height is zero; it is limited to the slope of an 85 deg inclination
        mxA = max(-MAX_WALL_SLOPE, min(MAX_WALL_SLOPE, gA["tx"] / tyA))
        mxB = max(-MAX_WALL_SLOPE, min(MAX_WALL_SLOPE, gB["tx"] / tyB))

        def x_A(y, x0=pRA0[0], y0=pRA0[1], mx=mxA):
            return x0 + mx * (y - y0)

        def x_B(y, x0=pLB0[0], y0=pLB0[1], mx=mxB):
            return x0 + mx * (y - y0)

        y_top_mid = 0.5 * (pRA0[1] + pLB0[1])
        y_bot_mid = 0.5 * (pRA1[1] + pLB1[1])
        s_top = x_B(y_top_mid) - x_A(y_top_mid)
        s_bot = x_B(y_bot_mid) - x_A(y_bot_mid)
        span_ref = max(s_top, s_bot, 0.5 * (s_top + s_bot))
        if span_ref < 2.0:
            fences.append(None)
            continue

        y_cross_lo = y_cross_hi = None
        if abs(mxA - mxB) > 1e-9:
            yc = (x_B(0.0) - x_A(0.0)) / (mxA - mxB)
            if mxA > mxB:
                y_cross_lo = yc
            else:
                y_cross_hi = yc

        def YA(d, pp=a):
            return Y(pp.z_at(d))

        def YB(d, pp=b):
            return Y(pp.z_at(d))

        LAk, LBk = LAY[id(a.bh)], LAY[id(b.bh)]
        zA0, zB0 = a.z_at(0.0), b.z_at(0.0)
        faulted = k in faults
        sec = qsec = None
        if not faulted:
            sec = _fence_secant_inclined(LAk, LBk, YA, YB, x_A, x_B, zA0, zB0)
        matched = sec is not None
        dx_g = pLB0[0] - pRA0[0]
        # ground slope; a (nearly) vertical projection must not give a huge slope
        ground = (max(-MAX_GROUND_SLOPE, min(MAX_GROUND_SLOPE, (pLB0[1] - pRA0[1]) / dx_g))
                  if abs(dx_g) >= 1.0 else 0.0)
        if faulted:
            qa, qb = _quat_run(LAk), _quat_run(LBk)
            if qa and qb:
                qsec = _fence_secant_inclined(LAk[:qa], LBk[:qb], YA, YB, x_A, x_B, zA0, zB0)
                if qsec is None:
                    qsec = ground
        if sec is None:         # nothing correlated: follow the ground surface
            sec = ground
        fences.append((0.0, span_ref, YA, YB, sec, faulted, qsec, matched,
                       x_A, x_B, y_cross_hi, y_cross_lo))

    def real_sec(k):            # dip usable for neighbours (Quaternary continues over faults)
        fc = fences[k] if 0 <= k < len(fences) else None
        if not fc:
            return None
        return fc[6] if fc[5] else fc[4]

    def block_sec(k):           # dip of a tectonic block: only unfaulted fences
        fc = fences[k] if 0 <= k < len(fences) else None
        return fc[4] if fc and not fc[5] and fc[7] else None

    out, lines = [], []
    for k, fc in enumerate(fences):
        if fc is None:
            continue
        xa, xb, YA, YB, sec, faulted, qsec, matched, x_A, x_B, y_cross_hi, y_cross_lo = fc
        LA, LB = LAY[id(proj[k].bh)], LAY[id(proj[k + 1].bh)]
        zA, zB = proj[k].z_at(0.0), proj[k + 1].z_at(0.0)

        def map_polys(raw_polys):
            res = []
            for pts, lit, age in raw_polys:
                mp = _map_to_inclined(pts, xb, x_A, x_B, y_cross_hi, y_cross_lo)
                if len(mp) >= 3:
                    res.append((mp, lit, age))
            return res

        if faulted:
            f = faults[k]
            sl, sr = block_sec(k - 1), block_sec(k + 1)
            mA = sl if sl is not None else 0.0     # not correlated: horizontal strata
            mB = sr if sr is not None else 0.0
            if not use_dip:
                mA = mB = 0.0
            G = None
            if qsec is not None:        # Quaternary cover continues across the fault
                qa, qb = _quat_run(LA), _quat_run(LB)
                dq = (LA[qa - 1].bottom, LB[qb - 1].bottom)
                dipsq = (qsec, blend_slopes(real_sec(k - 1), qsec),
                         blend_slopes(qsec, real_sec(k + 1))) if use_dip else None
                out += map_polys(fence_polygons(LA[:qa], LB[:qb], zA, zB, xa, xb, YA, YB,
                                                curvature, roundness, dipsq, transitions, None, True))
                G = _contact_curve(xa, xb, YA(dq[0]), YB(dq[1]), dipsq, curvature)
                LA, LB = LA[qa:], LB[qb:]
            polys, line = fault_polygons(LA, LB, xa, xb, YA, YB, f.get("pos", 0.5),
                                         f.get("dip", 80.0), f.get("dir", 1), mA, mB, aspect,
                                         G)
            out += map_polys(polys)
            if line is None:            # only Quaternary cover in both boreholes: no fault line
                continue
            (fx1, fy1), (fx2, fy2) = line
            # the fault line, like the polygons, ends where the two inclined boreholes cross
            lo_y = y_cross_hi if y_cross_hi is not None else -math.inf    # y must stay above
            hi_y = y_cross_lo if y_cross_lo is not None else math.inf     # and below
            if lo_y > -math.inf or hi_y < math.inf:
                t0, t1 = 0.0, 1.0
                dyl = fy2 - fy1
                if abs(dyl) < 1e-12:
                    if not (lo_y < fy1 < hi_y):
                        continue
                else:
                    ta, tb = (lo_y - fy1) / dyl, (hi_y - fy1) / dyl
                    t0, t1 = max(t0, min(ta, tb)), min(t1, max(ta, tb))
                    if t1 - t0 <= 1e-9:
                        continue
                fx1, fy1, fx2, fy2 = (fx1 + t0 * (fx2 - fx1), fy1 + t0 * dyl,
                                      fx1 + t1 * (fx2 - fx1), fy1 + t1 * dyl)
            # boreholes of different inclination: the image of a straight reference line is a
            # curve in y (x_A(y), x_B(y) differ), so it is sampled in steps along y
            nseg = 16 if abs(fy2 - fy1) > 1e-9 else 1
            fpts = []
            for s in range(nseg + 1):
                f = s / nseg
                fx, fy = fx1 + f * (fx2 - fx1), fy1 + f * (fy2 - fy1)
                u = fx / xb
                fpts.append(((1.0 - u) * x_A(fy) + u * x_B(fy), fy))
            lines.append(fpts)               # ONE polyline per fault
            continue
        sl, sr = real_sec(k - 1), real_sec(k + 1)
        dips = (sec, blend_slopes(sl, sec), blend_slopes(sec, sr)) if use_dip else None
        qa, qb = _quat_run(LA), _quat_run(LB)
        if qa and qb and qa < len(LA) and qb < len(LB):
            # Quaternary cover is a separate, continuous sequence (never terminated)
            out += map_polys(fence_polygons(LA[:qa], LB[:qb], zA, zB, xa, xb, YA, YB,
                                            curvature, roundness, dips, transitions, None, True))
            qlast = (LA[qa - 1], LB[qb - 1])
            base = _contact_curve(xa, xb, YA(qlast[0].bottom), YB(qlast[1].bottom), dips,
                                  curvature)
            LA, LB = LA[qa:], LB[qb:]
            rest = fence_polygons(LA, LB, zA, zB, xa, xb, YA, YB,
                                  curvature, roundness, dips, transitions, bedrock)
            out += map_polys(rest)
            out += map_polys(_fill_cover_gap(rest, base, xa, xb, qlast[0], qlast[1]))
        else:
            out += map_polys(fence_polygons(LA, LB, zA, zB, xa, xb, YA, YB,
                                            curvature, roundness, dips, transitions, bedrock))
    return out, lines


def _section_bounds(proj):
    """(d0, d1, zmax, zmin) accounting for collar and inclined bottom positions."""
    ds = [d for p in proj for d in (p.d_top, p.d_bot)]
    d0, d1 = min(ds), max(ds)
    zmax = max(p.z_at(0.0) for p in proj)
    zmin = min(p.z_at(p.bh.depth) for p in proj)
    return d0, d1, zmax, zmin


def section_geo(proj, vexag=0.0, curvature=0.6, roundness=0.6, merge_same=False,
                use_dip=True, transitions=True, bedrock_dip=0.0):
    """Geometry of a section in scene units (the same as build_section_scene(...).geo)
    without creating any graphics."""
    d0, d1, zmax, zmin = _section_bounds(proj)
    span = max(d1 - d0, 1.0)
    kx = 1000.0 / span
    ky = kx * vexag if vexag > 0 else 450.0 / max(zmax - zmin, 1.0)
    cw = 16.0

    def X(d):
        return (d - d0) * kx

    def Y(z):
        return (zmax - z) * ky

    LAY = {id(p.bh): clean_layers(p.bh.layers) for p in proj}
    if merge_same:
        LAY = {k: merge_same_lithology(v) for k, v in LAY.items()}
    bedrock = (math.tan(math.radians(bedrock_dip)) * ky / kx) if bedrock_dip and bedrock_dip > 0 else None
    polys, _ = correlation_polygons(proj, LAY, X, Y, cw, curvature, roundness, use_dip,
                                    transitions, None, ky / kx, bedrock)
    cols = []
    for p in proj:
        cg = _bh_column_geom(p, X, Y, cw)
        for L in LAY[id(p.bh)]:
            cols.append((cg["quad"](L.top, L.bottom), L.litho, L.age))
    return {"polys": polys, "cols": cols, "d0": d0, "kx": kx, "ky": ky, "zmax": zmax,
            "line": None, "strat_items": [], "terrain_items": []}


def hatch_brush(code):
    """Brush with the FGDC lithologic pattern `code` (patterns.py); None when there is none."""
    return _patterns.pattern_brush(code, 0.35) if code else None


def build_section_scene(proj, colors, vexag=0.0, correlate=True, labels=True,
                        curvature=0.6, roundness=0.6, merge_same=False,
                        use_dip=True, transitions=True, faults=None, bedrock_dip=0.0,
                        strat_lines=True, patterns=None, off_tol=OFF_LINE_TOL):
    """Return a QGraphicsScene with the cross section. vexag<=0 -> auto."""
    scene = QGraphicsScene()
    scene.setBackgroundBrush(QBrush(Qt.GlobalColor.white))

    def text(s, x, y, size=9, align="c", bold=False, color=Qt.GlobalColor.black):
        it = scene.addSimpleText(s)
        f = QFont()
        f.setPointSizeF(size)
        f.setBold(bold)
        it.setFont(f)
        it.setBrush(QBrush(color))
        w = it.boundingRect().width()
        it.setPos(x - (w / 2 if align == "c" else w if align == "r" else 0), y)
        it.setZValue(5)
        return it

    d0, d1, zmax, zmin = _section_bounds(proj)
    span = max(d1 - d0, 1.0)
    kx = 1000.0 / span
    zr = max(zmax - zmin, 1.0)
    ky = kx * vexag if vexag > 0 else 450.0 / zr
    cw = 16.0

    def X(d):
        return (d - d0) * kx

    def Y(z):
        return (zmax - z) * ky

    # grid + elevation axis
    step = _nice_step(zr)
    ax = X(d0) - cw / 2 - 70
    xr = X(d1) + cw / 2 + 20
    z = math.floor(zmin / step) * step
    grid = QPen(QColor(220, 220, 220))
    while z <= zmax + step:
        if zmin - 1e-9 <= z <= zmax + step:
            g = scene.addLine(ax, Y(z), xr, Y(z), grid)
            g.setZValue(-2)
            scene.addLine(ax - 4, Y(z), ax, Y(z))
            text(f"{z:g}", ax - 6, Y(z) - 8, 8, "r")
        z += step
    scene.addLine(ax, Y(zmax + step), ax, Y(math.floor(zmin / step) * step))
    text("Elevation [m a.s.l.]", ax - 60, Y(zmax) - 62, 8, "l")

    # cleaned (non-overlapping) layers of each borehole
    LAY = {id(p.bh): clean_layers(p.bh.layers) for p in proj}
    if merge_same:
        LAY = {k: merge_same_lithology(v) for k, v in LAY.items()}

    hatch_items = []        # lithologic patterns, shown or hidden in the section window
    no_pen = QPen(Qt.PenStyle.NoPen)

    def add_hatch(item_poly, lit, z):
        brush = hatch_brush(patterns.get(lit)) if patterns else None
        if brush is not None:
            h = item_poly(brush)
            h.setZValue(z)
            h.setOpacity(0.85)
            hatch_items.append(h)

    # correlation polygons
    polys = []
    if correlate:
        bedrock = (math.tan(math.radians(bedrock_dip)) * ky / kx) if bedrock_dip and bedrock_dip > 0 else None
        polys, flines = correlation_polygons(proj, LAY, X, Y, cw, curvature, roundness,
                                             use_dip, transitions, faults, ky / kx, bedrock)
        for pts, lit, age in polys:
            c = QColor(colors.get(lit, QColor(Qt.GlobalColor.gray)))
            c.setAlpha(170)
            poly = QPolygonF([QPointF(x, y) for x, y in pts])
            it = scene.addPolygon(poly, QPen(c.darker(150), 0.6), QBrush(c))
            it.setZValue(0)
            add_hatch(lambda b, poly=poly: scene.addPolygon(poly, no_pen, b), lit, 0.5)
        for n_f, fpts in enumerate(flines, start=1):
            pen = QPen(QColor(170, 0, 0), 2.6)
            for (xa_, ya_), (xb_, yb_) in zip(fpts, fpts[1:]):
                ln = scene.addLine(xa_, ya_, xb_, yb_, pen)
                ln.setZValue(4)
            text(f"F{n_f}", fpts[0][0], fpts[0][1] - 18, 9, "c", True, QColor(170, 0, 0))

    # boreholes (vertical or inclined)
    col_geoms = {}
    for p in proj:
        bh = p.bh
        cg = _bh_column_geom(p, X, Y, cw)
        col_geoms[id(bh)] = cg
        q_full = QPolygonF([QPointF(px, py) for px, py in cg["quad"](0.0, bh.depth)])
        # a borehole off the section line has dashed edges
        dashed = p.offset > off_tol
        full = scene.addPolygon(q_full, QPen(Qt.GlobalColor.black, 1), QBrush(Qt.GlobalColor.white))
        full.setZValue(1)
        for L in LAY[id(bh)]:
            c = colors.get(L.litho, QColor(Qt.GlobalColor.gray))
            q_lay = QPolygonF([QPointF(px, py) for px, py in cg["quad"](L.top, L.bottom)])
            r = scene.addPolygon(q_lay, QPen(Qt.GlobalColor.black, 0.8), QBrush(c))
            r.setZValue(2)
            add_hatch(lambda b, q_lay=q_lay: scene.addPolygon(q_lay, no_pen, b), L.litho, 2.5)
        if dashed:
            # dashed outline on a white halo: stays readable above lithologic patterns
            halo = scene.addPolygon(q_full, QPen(QColor(255, 255, 255), 4.0))
            halo.setZValue(3.4)
            dash = QPen(QColor(0, 0, 0), 1.8)
            dash.setDashPattern([3.0, 2.0])
            dash_it = scene.addPolygon(q_full, dash)
            dash_it.setZValue(3.5)
        if labels:
            x_top, y_top = cg["x0"], cg["y0"]
            x_bot, y_bot = cg["x1"], cg["y1"]
            z_bot = bh.z_at(bh.depth)
            text(bh.name, x_top, y_top - 36, 9, "c", True)
            text(f"{bh.z:.2f}", x_top, y_top - 20, 8)
            text(f"{z_bot:.2f}", x_bot, y_bot + 3, 8)
            info_lbl = f"d={p.d_top - d0:.1f} m\noff={p.offset:.1f} m"
            if bh.dip > 0:
                info_lbl += f"\naz={bh.azimuth:g}°, dip={bh.dip:g}°"
            text(info_lbl, x_top, Y(zmin) + 22, 7)

    # interfaces between stratigraphic sequences of different age
    strat_pen = QPen(QColor(30, 30, 30), 2.6)
    strat_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    strat_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    cols = []
    for p in proj:
        cg = col_geoms[id(p.bh)]
        for L in LAY[id(p.bh)]:
            cols.append((cg["quad"](L.top, L.bottom), L.litho, L.age))
    strat_found = False
    strat_items, terrain_items = [], []
    if strat_lines:
        for p in proj:
            cg = col_geoms[id(p.bh)]
            lays = LAY[id(p.bh)]
            for L1, L2 in zip(lays, lays[1:]):
                if _age_disjoint(L1.age, L2.age):
                    pL, pR = cg["left"](L2.top), cg["right"](L2.top)
                    it = scene.addLine(pL[0], pL[1], pR[0], pR[1], strat_pen)
                    it.setZValue(3)
                    strat_items.append(it)
                    strat_found = True
        try:
            for line in age_boundary_lines(list(polys) + cols):
                path = QPainterPath()
                path.moveTo(line[0][0], line[0][1])
                for (px, py) in line[1:]:
                    path.lineTo(px, py)
                it = scene.addPath(path, strat_pen)
                it.setZValue(3)
                strat_items.append(it)
                strat_found = True
        except Exception as e:
            # stratigraphic boundary lines are optional decoration; the section is still drawn
            log_warning(f"Stratigraphic boundary lines skipped: {type(e).__name__}: {e}")

    # terrain (connects borehole collars)
    pen = QPen(QColor(40, 120, 40), 1.6)
    for a, b in zip(proj, proj[1:]):
        it = scene.addLine(X(a.d_top), Y(a.z_at(0.0)), X(b.d_top), Y(b.z_at(0.0)), pen)
        it.setZValue(3)
        terrain_items.append(it)

    # legend
    lx, ly = xr + 25, Y(zmax)
    text("Legend", lx, ly - 20, 9, "l", True)
    used = lithologies([p.bh for p in proj])
    for i, lit in enumerate(used):
        scene.addRect(lx, ly + i * 20, 18, 14, QPen(Qt.GlobalColor.black, 0.8),
                      QBrush(colors.get(lit, QColor(Qt.GlobalColor.gray))))
        add_hatch(lambda b, i=i: scene.addRect(lx, ly + i * 20, 18, 14, no_pen, b), lit, 0.5)
        text(lit, lx + 24, ly + i * 20 - 1, 9, "l")
    if strat_found:
        yy = ly + len(used) * 20 + 8
        strat_items.append(scene.addLine(lx, yy + 7, lx + 18, yy + 7, strat_pen))
        strat_items.append(text("Stratigraphic boundary", lx + 24, yy - 1, 9, "l"))

    text(f"Geological cross section - {len(proj)} boreholes, length {span:.1f} m, "
         f"V:H = {ky / kx:.1f}:1", ax, Y(zmax) - 82, 11, "l", True)
    scene.setSceneRect(scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))
    # geometry of the section in scene units, used for the 3D fence model
    scene.geo = {"polys": list(polys), "cols": cols, "d0": d0, "kx": kx, "ky": ky,
                 "zmax": zmax, "line": None,
                 "strat_items": strat_items, "terrain_items": terrain_items,
                 "hatch_items": hatch_items}
    return scene


# ------------------------------------------------------------------- layers
def make_borehole_point_layer(boreholes):
    lyr = QgsVectorLayer(
        "PointZ?field=name:string&field=z:double&field=depth:double"
        "&field=azimuth:double&field=dip:double",
        "Boreholes", "memory")
    lyr.setCrs(work_crs())              # the CRS object, not an authid text in the URI
    feats = []
    for b in boreholes:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry(QgsPoint(b.x, b.y, b.z)))
        f.setAttributes([b.name, b.z, b.depth, b.azimuth, b.dip])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr
