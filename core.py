"""Data handling, section drawing and 3D layer creation (no GUI widgets)."""
import csv
import difflib
import io
import math
import re
import unicodedata
from dataclasses import dataclass, field

from qgis.PyQt.QtCore import Qt, QPointF
from qgis.PyQt.QtGui import QColor, QBrush, QPen, QPolygonF, QFont, QPainterPath
from qgis.PyQt.QtWidgets import QGraphicsScene
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsFeature, QgsGeometry, QgsLineString, QgsPoint, QgsPointXY, QgsVectorLayer,
    QgsMessageLog, Qgis,
)

from . import patterns as _patterns

LOG_TAG = "Borehole Geology"


def log_warning(msg):
    QgsMessageLog.logMessage(msg, LOG_TAG, Qgis.MessageLevel.Warning)


CRS_SJTSK = "EPSG:5514"

# key -> (authid, label, x label, y label, x keywords, y keywords)
INPUT_CRS = {
    "sjtsk": ("EPSG:5514", "S-JTSK (EPSG:5514)",
              "X (northing, latitude)", "Y (easting, longitude)", ["x"], ["y"]),
    "wgs84": ("EPSG:4326", "WGS84 geographic - lon/lat in degrees (EPSG:4326)",
              "Longitude [deg]", "Latitude [deg]",
              ["lon", "lng", "longitude", "x"], ["lat", "latitude", "y"]),
    "etrs89": ("EPSG:4258", "ETRS89 geographic - lon/lat in degrees (EPSG:4258)",
               "Longitude [deg]", "Latitude [deg]",
               ["lon", "lng", "longitude", "x"], ["lat", "latitude", "y"]),
    "etrs89_utm33": ("EPSG:25833", "ETRS89 / UTM zone 33N (EPSG:25833)",
                     "Easting (E) [m]", "Northing (N) [m]",
                     ["easting", "east", "e", "x"], ["northing", "north", "n", "y"]),
}

DELIMITERS = [("Auto-detect", None), ("Semicolon ;", ";"), ("Comma ,", ","),
              ("Tab", "\t"), ("Space", " "), ("Pipe |", "|")]


@dataclass
class Layer:
    top: float
    bottom: float
    litho: str
    age: tuple = None   # (youngest_index, oldest_index) in STRAT_NAMES, or None


@dataclass
class Borehole:
    name: str
    x: float          # QGIS easting  (EPSG:5514)
    y: float          # QGIS northing (EPSG:5514)
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

    def __post_init__(self):
        if self.d_top is None:
            self.d_top = self.dist
        if self.d_bot is None:
            self.d_bot = self.dist

    def d_at(self, md):
        """Distance along section polyline at measured along-hole depth `md`."""
        if self.bh.depth <= 1e-9:
            return self.d_top
        t = md / self.bh.depth
        return self.d_top + (self.d_bot - self.d_top) * t


# ----------------------------------------------------------------- CSV input
def _norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return s.strip().lower()


def read_table(path, delimiter=None):
    """Return (header, rows). Auto-detects encoding; delimiter=None -> auto."""
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "cp1250", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    first = text.splitlines()[0] if text else ""
    if delimiter is None:
        if ";" in first:
            delimiter = ";"
        elif "\t" in first:
            delimiter = "\t"
        else:
            delimiter = ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter,
                                  skipinitialspace=(delimiter == " "))
            if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("File is empty: " + path)
    return [h.strip() for h in rows[0]], rows[1:]


def to_float(s):
    try:
        return float(str(s).strip().replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


# Stratigraphy, youngest -> oldest (index grows with age)
STRAT_NAMES = ["kvarter", "neogen", "paleogen", "krida", "jura", "trias", "perm",
               "karbon", "devon", "silur", "ordovik", "kambrium", "proterozoikum"]
STRAT_LABELS = ["kvartér", "neogén", "paleogén", "křída", "jura", "trias", "perm",
                "karbon", "devon", "silur", "ordovik", "kambrium", "proterozoikum"]
STRAT_ENGLISH = ["Quaternary", "Neogene", "Palaeogene", "Cretaceous", "Jurassic", "Triassic",
                 "Permian", "Carboniferous", "Devonian", "Silurian", "Ordovician", "Cambrian",
                 "Proterozoic"]
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


def parse_strat(text):
    """'neogén', 'Terciér', 'Cretaceous', 'křída-jura' ... -> (youngest_idx, oldest_idx) or None.

    Upper / lower case and diacritics are ignored (é = e, ř = r, ...).  A letter that was
    lost or garbled by a machine error ('k?ida', 'kida') is tolerated as well."""
    t = _norm(str(text).replace("\ufffd", "?"))
    if not t:
        return None
    found = []
    for name in sorted(_STRAT_TERMS, key=len, reverse=True):
        if name in t:
            found.append(_STRAT_TERMS[name])
            t = t.replace(name, " ")
    # what is left: words with a lost or garbled character
    for word in re.findall(r"[a-z?*]{4,}", t):
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


def build_boreholes(h1, r1, m1, h2, r2, m2, crs_key="sjtsk", negate=True, swap=False):
    """m1: dict name,x,y,z,depth[,azimuth,dip] -> column index (-1 = none).
    m2: dict name,top,bottom,litho[,strat] -> column index (-1 = none).

    Input CRS is given by crs_key (see INPUT_CRS); all boreholes are converted
    to S-JTSK (EPSG:5514) which is used for map, section and 3D.
    S-JTSK: column 'x' = X (northing), 'y' = Y (easting), both negative in
    EPSG:5514; QGIS point = (-Y, -X). `negate` adds the minus sign.
    Other CRS: column 'x' = lon/easting, 'y' = lat/northing.
    `swap` exchanges the meaning of the two coordinate columns.
    """
    src = QgsCoordinateReferenceSystem(INPUT_CRS[crs_key][0])
    dst = QgsCoordinateReferenceSystem(CRS_SJTSK)
    xform = None if crs_key == "sjtsk" else QgsCoordinateTransform(
        src, dst, QgsProject.instance().transformContext())
    warnings, bhs, bad_strat = [], {}, set()
    az_idx = m1.get("azimuth", -1)
    dip_idx = m1.get("dip", -1)
    for n, r in enumerate(r1, start=2):
        try:
            name = r[m1["name"]].strip()
            sx, sy = to_float(r[m1["x"]]), to_float(r[m1["y"]])
            z, depth = to_float(r[m1["z"]]), to_float(r[m1["depth"]])
        except IndexError:
            warnings.append(f"Boreholes file, line {n}: too few columns - skipped")
            continue
        if not name or None in (sx, sy, z):
            warnings.append(f"Boreholes file, line {n}: invalid values - skipped")
            continue

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

        if swap:
            sx, sy = sy, sx
        if crs_key == "sjtsk":
            qx, qy = sy, sx
            if negate:
                qx, qy = -abs(qx), -abs(qy)
        else:
            try:
                p = xform.transform(QgsPointXY(sx, sy))
                qx, qy = p.x(), p.y()
            except Exception:
                warnings.append(f"Boreholes file, line {n}: coordinate transformation failed - skipped")
                continue
        if name in bhs:
            warnings.append(f"Duplicate borehole '{name}' - last one used")
        bhs[name] = Borehole(name, qx, qy, z, depth or 0.0, azimuth=az, dip=dip)

    for n, r in enumerate(r2, start=2):
        try:
            name = r[m2["name"]].strip()
            t, b = to_float(r[m2["top"]]), to_float(r[m2["bottom"]])
            lit = r[m2["litho"]].strip() or "(unknown)"
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
        age = parse_strat(raw_age) if raw_age else None
        if raw_age and age is None:
            bad_strat.add(raw_age)
        bhs[name].layers.append(Layer(t, b, lit, age))

    for bh in bhs.values():
        bh.layers.sort(key=lambda L: L.top)
        if bh.layers:
            last = bh.layers[-1].bottom
            if bh.depth < last:
                if bh.depth:
                    warnings.append(f"{bh.name}: layers go deeper ({last}) than total depth ({bh.depth})")
                bh.depth = last
            for a, b in zip(bh.layers, bh.layers[1:]):
                if a.age and b.age and b.age[1] < a.age[0]:
                    warnings.append(f"{bh.name}: younger stratigraphy ({age_label(b.age)}) below "
                                    f"older ({age_label(a.age)}) at {b.top} m - fault or data error?")
                if b.top < a.bottom - 1e-6:
                    warnings.append(f"{bh.name}: overlapping layers at {b.top} m")
                elif b.top > a.bottom + 1e-6:
                    warnings.append(f"{bh.name}: gap between {a.bottom} and {b.top} m")
        else:
            warnings.append(f"{bh.name}: no layers")
    for raw in sorted(bad_strat):
        warnings.append(f"Unknown stratigraphy '{raw}' - layer treated as unknown age")
    # sanity check: should lie within the Czech Republic (lon 11.5-19.5, lat 48.4-51.2)
    try:
        to_wgs = QgsCoordinateTransform(dst, QgsCoordinateReferenceSystem("EPSG:4326"),
                                        QgsProject.instance().transformContext())
        out = 0
        for bh in bhs.values():
            p = to_wgs.transform(QgsPointXY(bh.x, bh.y))
            if not (11.5 <= p.x() <= 19.5 and 48.4 <= p.y() <= 51.2):
                out += 1
        if out:
            warnings.insert(0, f"{out} borehole(s) lie outside the Czech Republic - check the "
                               "coordinate system, the minus-sign option and the swap option")
    except Exception as e:
        # the location check is only advisory; keep the data but record why it was skipped
        log_warning(f"Coordinate plausibility check skipped: {type(e).__name__}: {e}")
    return list(bhs.values()), warnings


# ------------------------------------------------------------------- colours
_KEYWORDS = [
    (("jil", "clay"), "#8b5a2b"), (("jilovec",), "#6b4423"),
    (("pisek", "sand"), "#f2d16b"), (("sterk", "stk", "gravel"), "#e0a458"),
    (("hlina", "silt", "loam"), "#c9a66b"), (("navaz", "fill"), "#9a9a9a"),
    (("skal", "rock", "granit", "rula", "bridlic"), "#6e7b8b"),
    (("pisk", "sandstone"), "#e6c27a"), (("slin", "sinter"), "#a0a98f"),
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


def project_boreholes(points, boreholes, max_offset):
    line = QgsGeometry.fromPolylineXY(points)
    max_dep = max([b.depth for b in boreholes] or [0.0])
    ext = max_dep + line.length() + 1000.0
    ext_pts = _extend_polyline(points, ext)
    ext_line = QgsGeometry.fromPolylineXY(ext_pts)
    has_ext = len(ext_pts) >= 2 and ext_line.length() > line.length()
    out = []
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
            out.append(Projected(b, d_mid, off, d_top, d_bot))
    out.sort(key=lambda p: p.dist)
    return out


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
            out.append(Layer(top, L.bottom, L.litho, L.age))
            run = L.bottom if run is None else max(run, L.bottom)
    return out


def merge_same_lithology(layers, tol=1e-6):
    """Merge directly neighbouring layers with the same lithology."""
    out = []
    for L in layers:
        if (out and out[-1].litho == L.litho and out[-1].age == L.age
                and abs(L.top - out[-1].bottom) <= tol):
            out[-1] = Layer(out[-1].top, L.bottom, L.litho, L.age)
        else:
            out.append(Layer(L.top, L.bottom, L.litho, L.age))
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
                return hs[i] * wf((xt - x) / (xt - xa)) if x <= xt else 0.0
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
            xl = [x for x in xs if x < xm - amp - 1e-9]
            xr = [x for x in xs if x > xm + amp + 1e-9]

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
                   G=None, top_depths=(0.0, 0.0)):
    """Two separate blocks divided by a fault.  Layers of each borehole continue
    with the dip of their own block and are cut by the fault plane.
    pos 0..1 = fault position between the columns at the surface, direction
    +1/-1 = fault plane dips to the right/left (0 = vertical).
    Returns (polygons, ((x1, y1), (x2, y2)) fault line)."""
    span = xb - xa
    if G is None:
        def G(x):
            return YA(0.0) + (YB(0.0) - YA(0.0)) * (x - xa) / span
    y_ref = min(YA(top_depths[0]), YB(top_depths[1]))
    y_bot = max(YA(max([lay.bottom for lay in LA] or [0.0])), YB(max([lay.bottom for lay in LB] or [0.0])))
    x_ft = xa + min(max(pos, 0.05), 0.95) * span
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


def _no_overlap(items):
    """Safety net: subtract everything already drawn from each polygon."""
    try:
        acc, out = None, []
        for item in items:
            pts, rest = item[0], item[1:]
            g = QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in pts]])
            if not g.isGeosValid():
                g = g.makeValid()
            if acc is not None:
                g = g.difference(acc)
            if g.isNull() or g.isEmpty():
                continue
            acc = g if acc is None else acc.combine(g)
            for part in g.asGeometryCollection():
                poly = part.asQPolygonF()
                pp = [(poly.at(k).x(), poly.at(k).y()) for k in range(poly.count())]
                area = abs(sum(pp[k][0] * pp[(k + 1) % len(pp)][1]
                               - pp[(k + 1) % len(pp)][0] * pp[k][1]
                               for k in range(len(pp)))) / 2.0 if len(pp) >= 3 else 0
                if area > 1e-6:
                    out.append((pp,) + rest)
        return out
    except Exception:
        return items


def _age_disjoint(a, b):
    return a is not None and b is not None and not ages_compatible(a, b)


def _inside(pt, poly):
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
            nx, ny = -dy / ln * offset, dx / ln * offset
            for j in cands:
                b = boxes[j]
                if not (b[0] - offset <= mx <= b[2] + offset and b[1] - offset <= my <= b[3] + offset):
                    continue
                if _inside((mx + nx, my + ny), items[j][0]) or _inside((mx - nx, my - ny), items[j][0]):
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
    x0, y0 = X(p.d_top), Y(bh.z)
    x1, y1 = X(p.d_bot), Y(bh.z_at(bh.depth))
    dx, dy = x1 - x0, y1 - y0
    ln = math.hypot(dx, dy)
    if ln > 1e-9:
        tx, ty = dx / ln, dy / ln
    else:
        tx, ty = 0.0, 1.0
    nx, ny = ty, -tx
    hw = cw / 2.0

    def left(md):
        xc, yc = X(p.d_at(md)), Y(bh.z_at(md))
        return (xc - hw * nx, yc - hw * ny)

    def right(md):
        xc, yc = X(p.d_at(md)), Y(bh.z_at(md))
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


def _fence_secant_inclined(LA, LB, YA, YB, x_A, x_B):
    """Median apparent dip (scene dy/dx) of correlated contacts between inclined walls."""
    sl = []
    for i, j in align_layers(LA, LB, 0.0, 0.0):
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
    faults.  Returns ([(points, litho, age)], fault_lines)."""
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
        mxA = gA["tx"] / tyA
        mxB = gB["tx"] / tyB

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

        def YA(d, bh=a.bh):
            return Y(bh.z_at(d))

        def YB(d, bh=b.bh):
            return Y(bh.z_at(d))

        LAk, LBk = LAY[id(a.bh)], LAY[id(b.bh)]
        faulted = k in faults
        sec = qsec = None
        if not faulted:
            sec = _fence_secant_inclined(LAk, LBk, YA, YB, x_A, x_B)
        matched = sec is not None
        dx_g = pLB0[0] - pRA0[0]
        ground = (pLB0[1] - pRA0[1]) / dx_g if abs(dx_g) > 1e-6 else 0.0
        if faulted:
            qa, qb = _quat_run(LAk), _quat_run(LBk)
            if qa and qb:
                qsec = _fence_secant_inclined(LAk[:qa], LBk[:qb], YA, YB, x_A, x_B)
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
        zA, zB = proj[k].bh.z, proj[k + 1].bh.z

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
            G, top = None, (0.0, 0.0)
            if qsec is not None:        # Quaternary cover continues across the fault
                qa, qb = _quat_run(LA), _quat_run(LB)
                dq = (LA[qa - 1].bottom, LB[qb - 1].bottom)
                dipsq = (qsec, blend_slopes(real_sec(k - 1), qsec),
                         blend_slopes(qsec, real_sec(k + 1))) if use_dip else None
                out += map_polys(fence_polygons(LA[:qa], LB[:qb], zA, zB, xa, xb, YA, YB,
                                                curvature, roundness, dipsq, transitions, None, True))
                G = _contact_curve(xa, xb, YA(dq[0]), YB(dq[1]), dipsq, curvature)
                top = dq
                LA, LB = LA[qa:], LB[qb:]
            polys, line = fault_polygons(LA, LB, xa, xb, YA, YB, f.get("pos", 0.5),
                                         f.get("dip", 80.0), f.get("dir", 1), mA, mB, aspect,
                                         G, top)
            out += map_polys(polys)
            (fx1, fy1), (fx2, fy2) = line
            u1, u2 = fx1 / xb, fx2 / xb
            lines.append((((1.0 - u1) * x_A(fy1) + u1 * x_B(fy1), fy1),
                          ((1.0 - u2) * x_A(fy2) + u2 * x_B(fy2), fy2)))
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
    zmax = max(p.bh.z for p in proj)
    zmin = min(p.bh.z_at(p.bh.depth) for p in proj)
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
                        strat_lines=True, patterns=None):
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
        for n_f, ((x1, y1), (x2, y2)) in enumerate(flines, start=1):
            ln = scene.addLine(x1, y1, x2, y2, QPen(QColor(170, 0, 0), 2.6))
            ln.setZValue(4)
            text(f"F{n_f}", x1, y1 - 18, 9, "c", True, QColor(170, 0, 0))

    # boreholes (vertical or inclined)
    col_geoms = {}
    for p in proj:
        bh = p.bh
        cg = _bh_column_geom(p, X, Y, cw)
        col_geoms[id(bh)] = cg
        q_full = QPolygonF([QPointF(px, py) for px, py in cg["quad"](0.0, bh.depth)])
        full = scene.addPolygon(q_full, QPen(Qt.GlobalColor.black, 1), QBrush(Qt.GlobalColor.white))
        full.setZValue(1)
        for L in LAY[id(bh)]:
            c = colors.get(L.litho, QColor(Qt.GlobalColor.gray))
            q_lay = QPolygonF([QPointF(px, py) for px, py in cg["quad"](L.top, L.bottom)])
            r = scene.addPolygon(q_lay, QPen(Qt.GlobalColor.black, 0.8), QBrush(c))
            r.setZValue(2)
            add_hatch(lambda b, q_lay=q_lay: scene.addPolygon(q_lay, no_pen, b), L.litho, 2.5)
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
        it = scene.addLine(X(a.d_top), Y(a.bh.z), X(b.d_top), Y(b.bh.z), pen)
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
        f"PointZ?crs={CRS_SJTSK}&field=name:string&field=z:double&field=depth:double"
        "&field=azimuth:double&field=dip:double",
        "Boreholes", "memory")
    feats = []
    for b in boreholes:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry(QgsPoint(b.x, b.y, b.z)))
        f.setAttributes([b.name, b.z, b.depth, b.azimuth, b.dip])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


def make_3d_interval_layer(boreholes):
    lyr = QgsVectorLayer(
        f"LineStringZ?crs={CRS_SJTSK}&field=name:string&field=litho:string"
        "&field=top:double&field=bottom:double",
        "Boreholes 3D (lithology)", "memory")
    feats = []
    for b in boreholes:
        for L in b.layers:
            f = QgsFeature(lyr.fields())
            f.setGeometry(QgsGeometry(QgsLineString([
                QgsPoint(*b.point_at(L.top)), QgsPoint(*b.point_at(L.bottom))])))
            f.setAttributes([b.name, L.litho, L.top, L.bottom])
            feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


def apply_3d_style(layer, colors, width):
    """Rule-based 3D renderer: one tube symbol per lithology."""
    from qgis._3d import QgsLine3DSymbol, QgsRuleBased3DRenderer
    from qgis.core import Qgis, QgsPhongMaterialSettings

    root = QgsRuleBased3DRenderer.Rule(None)
    for lit, col in colors.items():
        sym = QgsLine3DSymbol()
        sym.setWidth(width)
        sym.setAltitudeClamping(Qgis.AltitudeClamping.Absolute)
        mat = QgsPhongMaterialSettings()
        mat.setDiffuse(col)
        mat.setAmbient(col.darker(150))
        sym.setMaterialSettings(mat)
        esc = lit.replace("'", "''")
        root.appendChild(QgsRuleBased3DRenderer.Rule(sym, f"\"litho\" = '{esc}'", lit))
    layer.setRenderer3D(QgsRuleBased3DRenderer(root))
