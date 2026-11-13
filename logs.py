"""Individual borehole profiles and sedimentary records (no GUI widgets).

The drawing is computed as a plain list of primitives (rectangles, polygons, lines, texts),
so the layout does not depend on Qt; ``to_scene`` turns the list into a QGraphicsScene.

Two figures are offered:

* ``build_profiles``  - up to four borehole columns side by side with a depth ruler, a
                        lithology and a stratigraphy column and the description of every layer,
* ``build_records``   - sedimentary records: the column width follows the grain size
                        (clay / silt / sand / gravel) and a "Legend" lists the rock types.
"""
import math
import string

from . import core
from . import patterns as pat

GRAINS = ["clay", "silt", "sand", "gravel"]
GRAIN_LABELS = {"clay": "Clay", "silt": "Silt", "sand": "Sand", "gravel": "Gravel"}

# keyword (normalised, no diacritics) -> grain size.  The keyword that stands FIRST in the
# lithology text decides ("jil piscity" = clay, "pisek jilovity" = sand).
_GRAIN_KEYWORDS = [
    ("gravel", ("sterk", "gravel", "valoun", "pebble", "cobble", "boulder")),
    ("sand", ("pisek", "pisk", "sand", "arkoz")),
    ("silt", ("prach", "silt", "hlin", "loam", "loess", "spras", "slin", "marl")),
    ("clay", ("jil", "clay", "bridlic", "shale", "lupek", "mud", "kaolin")),
]
DEFAULT_GRAIN = "silt"


def guess_grain(litho):
    """Grain-size class of a lithology text (the user can change it in the plugin)."""
    n = core._norm(litho)
    best = None
    for grain, keys in _GRAIN_KEYWORDS:
        for k in keys:
            pos = n.find(k)
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, grain)
    return best[1] if best else DEFAULT_GRAIN


# ------------------------------------------------------------------ primitives
def _rect(x, y, w, h, fill=None, pen="#000000", pw=1.0, pattern=None):
    return ("poly", [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], fill, pen, pw, pattern)


def _poly(pts, fill=None, pen="#000000", pw=1.0, pattern=None):
    return ("poly", list(pts), fill, pen, pw, pattern)


def _line(x1, y1, x2, y2, pen="#000000", pw=1.0, dash=False):
    return ("line", x1, y1, x2, y2, pen, pw, dash)


def _text(s, x, y, size=9.0, align="l", bold=False, italic=False, rot=0.0, underline=False):
    return ("text", s, x, y, size, align, bold, italic, rot, underline)


def approx_measure(s, size=9.0, bold=False):
    """Text width estimate (used when no Qt font metrics are given)."""
    return len(s) * size * (0.62 if bold else 0.56) * 1.33


def wrap_text(s, width, measure, size=9.0):
    """Break the text into lines no wider than `width` (words are never split)."""
    lines, cur = [], ""
    for word in str(s).split():
        trial = word if not cur else cur + " " + word
        if cur and measure(trial, size) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


# ----------------------------------------------------------------- ruler
def _nice_steps(px_per_m, min_px=38.0):
    """(major, minor) tick step in metres; major ticks are at least `min_px` apart."""
    raw = min_px / max(px_per_m, 1e-9)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            major = m * mag
            break
    mant = round(major / 10 ** math.floor(math.log10(major)))
    minor = major / (4 if mant == 2 else 5)
    return major, minor


def _fmt(v, step):
    dec = 0 if step >= 1 else (1 if step >= 0.1 else 2)
    return f"{v:.{dec}f}"


def _ruler(cmds, x, y_of, v0, v1, px_per_m, label_fn=None):
    """Vertical ruler at x for values v0..v1 (y_of maps a value to y); returns nothing."""
    major, minor = _nice_steps(px_per_m)
    first = math.ceil(v0 / minor - 1e-9)
    last = math.floor(v1 / minor + 1e-9)
    cmds.append(_line(x, y_of(v0), x, y_of(v1)))
    for i in range(first, last + 1):
        v = i * minor
        is_major = abs(v / major - round(v / major)) < 1e-6
        y = y_of(v)
        cmds.append(_line(x - (14 if is_major else 7), y, x, y))
        if is_major:
            txt = label_fn(v) if label_fn else _fmt(v, major)
            cmds.append(_text(txt, x - 18, y - 7, 9, "r"))


# -------------------------------------------------------------- shared prep
def _clean(bh):
    """Layers of a borehole without overlaps (the upper layer wins)."""
    return core.clean_layers(bh.layers)


def _extent(bhs, mode):
    """(value of the top, value of the bottom) covering all boreholes."""
    if mode == "alt":
        return (max(b.z for b in bhs),
                min(b.z_at(max([L.bottom for L in b.layers] or [b.depth])) for b in bhs))
    return 0.0, max(max([L.bottom for L in b.layers] or [b.depth]) for b in bhs)


# ------------------------------------------------------------- 1) profiles
COL_W = 150.0
LABEL_W = 250.0
GAP = 40.0
TOP = 70.0


def _layout_labels(items, line_h):
    """items: [(top_y, bottom_y, lines)] sorted by depth.  Returns [(box_top, box_bottom)].

    A label box starts at the top of its layer and is at least as high as its text; when
    the text does not fit into a thin layer the box hangs below the layer and the next
    boxes are pushed down (like the descriptions in a classical borehole log)."""
    boxes, prev = [], -1e9
    for top, bot, lines in items:
        need = len(lines) * line_h + 4
        b0 = max(top, prev)
        b1 = max(bot, b0 + need)
        boxes.append((b0, b1))
        prev = b1
    return boxes


# colours of the geological periods (ICS), same order as core.STRAT_NAMES
STRAT_COLORS = ["#F9F97F", "#FFE619", "#FD9A52", "#7FC64E", "#34B2C9", "#812B92", "#F04028",
                "#67A599", "#CB8C37", "#B3E1B6", "#009270", "#7FA056", "#F74370"]
STRAT_W = 66.0


def strat_color(age):
    """Colour of a period; a span of several periods gets the mean of its two ends."""
    a, b = STRAT_COLORS[age[0]], STRAT_COLORS[age[1]]
    mix = [(int(a[i:i + 2], 16) + int(b[i:i + 2], 16)) // 2 for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(mix)


def strat_blocks(layers):
    """[(top, bottom, age)] - neighbouring layers of the same age form one block."""
    blocks = []
    for L in layers:
        if L.age is None:
            continue
        if blocks and blocks[-1][2] == L.age and abs(blocks[-1][1] - L.top) < 1e-6:
            blocks[-1][1] = L.bottom
        else:
            blocks.append([L.top, L.bottom, L.age])
    return blocks


def build_profiles(bhs, colors, patterns, mode="depth", px_per_m=0.0, measure=approx_measure,
                   show_pattern=True, show_strat=True):
    """Primitives of up to four borehole columns with descriptions.

    colors: litho -> '#rrggbb';  patterns: litho -> FGDC pattern code ('' = none);
    mode: 'depth' or 'alt'.  Left to right: the lithology column, the description of the
    layers and (when the layers have a stratigraphy) the stratigraphy column."""
    cmds = []
    v_top, v_bot = _extent(bhs, mode)
    span = max(v_bot - v_top if mode == "depth" else v_top - v_bot, 1e-6)
    k = px_per_m if px_per_m > 0 else max(8.0, min(60.0, 900.0 / span))
    line_h = 13.0
    slant = 14.0

    def y_of(v):                           # v = depth, or altitude in 'alt' mode
        return TOP + ((v - v_top) if mode == "depth" else (v_top - v)) * k

    ruler_x = 70.0
    lo, hi = (v_top, v_bot) if mode == "depth" else (v_bot, v_top)
    _ruler(cmds, ruler_x, y_of, lo, hi, k)
    cmds.append(_text("Depth [m]" if mode == "depth" else "Altitude [m a.s.l.]",
                      ruler_x - 54, TOP - 40, 9, "l", bold=True))

    sw = STRAT_W if show_strat and any(L.age for b in bhs for L in _clean(b)) else 0.0
    x0 = ruler_x + 70
    for bh in bhs:
        layers = _clean(bh)
        z0 = bh.z
        lx0, lx1 = x0 + COL_W + 6, x0 + COL_W + LABEL_W          # description column
        sx = lx1 + 6                                             # stratigraphy column
        cmds.append(_text(bh.name, x0 + COL_W / 2, TOP - 50, 15, "c", bold=True))
        if bh.dip > 0:
            cmds.append(_text(f"az {bh.azimuth:g}°, dip {bh.dip:g}°",
                              x0 + COL_W / 2, TOP - 32, 8, "c"))
        cmds.append(_text("Lithology", x0 + COL_W / 2, TOP - 20, 8, "c"))
        # ground marker
        ytop = y_of(0.0 if mode == "depth" else z0)
        cmds.append(_rect(x0, ytop - 5, COL_W, 5, "#ffffff", "#000000", 0.8, "ground"))
        items = []
        for L in layers:
            a = L.top if mode == "depth" else bh.z_at(L.top)
            b = L.bottom if mode == "depth" else bh.z_at(L.bottom)
            y1, y2 = y_of(a), y_of(b)
            cmds.append(_rect(x0, y1, COL_W, y2 - y1, colors.get(L.litho, "#cccccc"), "#303030", 0.6))
            if show_pattern and patterns.get(L.litho):
                cmds.append(_rect(x0, y1, COL_W, y2 - y1, None, "#303030", 0.0, patterns[L.litho]))
            items.append((y1, y2, wrap_text(L.desc or L.litho, LABEL_W - 2 * slant - 8, measure, 10)))
        # descriptions: separating lines; a label that does not fit into a thin layer
        # hangs below it as a trapezoid (narrower at the bottom)
        boxes = _layout_labels(items, line_h)
        for (y1, y2, lines), (b0, b1) in zip(items, boxes):
            if len(lines) * line_h + 4 > y2 - y1:      # the text does not fit into the layer
                cmds.append(_poly([(lx0, b0), (lx1, b0), (lx1 - slant, b1), (lx0 + slant, b1)],
                                  None, "#000000", 0.8))
            else:
                cmds.append(_line(lx0, b0, lx1, b0, "#000000", 0.8))
                cmds.append(_line(lx0, b1, lx1, b1, "#000000", 0.8))
            cy = (b0 + b1) / 2 - len(lines) * line_h / 2
            for i, s in enumerate(lines):
                cmds.append(_text(s, (lx0 + lx1) / 2, cy + i * line_h - 1, 10, "c"))
        # stratigraphy column
        if sw:
            cmds.append(_text("Stratigraphy", sx + sw / 2, TOP - 20, 8, "c"))
            for t, b, age in strat_blocks(layers):
                a = t if mode == "depth" else bh.z_at(t)
                bb = b if mode == "depth" else bh.z_at(b)
                y1, y2 = y_of(a), y_of(bb)
                cmds.append(_rect(sx, y1, sw, y2 - y1, strat_color(age), "#303030", 0.8))
                txt = core.age_label(age).capitalize()
                lines = [txt] if measure(txt, 8) <= sw - 6 else wrap_text(
                    txt.replace("-", "- "), sw - 6, measure, 8)
                if len(lines) * 11.0 <= y2 - y1:
                    for i, s in enumerate(lines):
                        cmds.append(_text(s, sx + sw / 2,
                                          (y1 + y2) / 2 - len(lines) * 5.5 + i * 11.0, 8, "c"))
        x0 = sx + sw + GAP + (6 if sw else 0)
    return cmds


# ---------------------------------------------------------- 2) sedimentary record
CELL = 22.0           # width of one grain-size class
HEAD_H = 58.0
REC_TOP = 110.0


def build_records(bhs, colors, grains, patterns=None, px_per_m=0.0, measure=approx_measure,
                  letters=True, legend=True, mode="depth"):
    """Sedimentary records side by side + a 'Legend' (rock type and grain size).

    grains: litho -> grain class (column width);  patterns: litho -> FGDC pattern code."""
    patterns = patterns or {}
    cmds = []
    v_top, v_bot = _extent(bhs, mode)
    span = max(v_bot - v_top if mode == "depth" else v_top - v_bot, 1e-6)
    k = px_per_m if px_per_m > 0 else max(8.0, min(60.0, 600.0 / span))

    def y_of(v):
        return REC_TOP + ((v - v_top) if mode == "depth" else (v_top - v)) * k

    gw = CELL * len(GRAINS)
    x0 = 100.0
    bottom_px = y_of(v_bot)
    used = []
    for n, bh in enumerate(bhs):
        layers = _clean(bh)
        lo, hi = (v_top, v_bot) if mode == "depth" else (v_bot, v_top)
        _ruler(cmds, x0 - 6, y_of, lo, hi, k,
               lambda v: f"{v:g}")
        if n == 0:
            cmds.append(_text("Depth [m]" if mode == "depth" else "Altitude [m a.s.l.]",
                              x0 - 60, REC_TOP - HEAD_H - 40, 9, "l", bold=True))
        cmds.append(_text(bh.name, x0 + gw / 2, REC_TOP - HEAD_H - 40, 14, "c", bold=True))
        # header: four grain-size cells with vertical captions
        for i, g in enumerate(GRAINS):
            cmds.append(_rect(x0 + i * CELL, REC_TOP - HEAD_H, CELL, HEAD_H, "#ffffff", "#000000", 0.9))
            cmds.append(_text(GRAIN_LABELS[g], x0 + i * CELL + CELL / 2 - 7, REC_TOP - 4, 10,
                              "l", rot=-90))
        # faint class boundaries through the record
        for i in range(1, len(GRAINS)):
            cmds.append(_line(x0 + i * CELL, REC_TOP, x0 + i * CELL, bottom_px,
                              "#b0b0b0", 0.6, True))
        for L in layers:
            a = L.top if mode == "depth" else bh.z_at(L.top)
            b = L.bottom if mode == "depth" else bh.z_at(L.bottom)
            gr = grains.get(L.litho, DEFAULT_GRAIN)
            w = CELL * (GRAINS.index(gr) + 1)
            y1, y2 = y_of(a), y_of(b)
            cmds.append(_rect(x0, y1, w, y2 - y1, colors.get(L.litho, "#cccccc"), "#000000", 0.9))
            if patterns.get(L.litho):
                cmds.append(_rect(x0, y1, w, y2 - y1, None, "#000000", 0.0, patterns[L.litho]))
            if (L.litho, L.desc or L.litho) not in used:
                used.append((L.litho, L.desc or L.litho))
        if letters:
            cmds.append(_text(string.ascii_lowercase[n % 26], x0 + gw / 2, bottom_px + 12, 14,
                              "c", bold=True))
        x0 += gw + 100.0

    if legend and used:
        lx = x0 + 10
        ly = REC_TOP - 8
        cmds.append(_text("Legend", lx, ly - 34, 14, "l", bold=True, italic=True, underline=True))
        row = 26.0
        for i, (lit, text) in enumerate(used):
            y = ly + i * row
            gr = grains.get(lit, DEFAULT_GRAIN)
            cmds.append(_rect(lx, y, 40, 18, colors.get(lit, "#cccccc"), "#000000", 0.9))
            if patterns.get(lit):
                cmds.append(_rect(lx, y, 40, 18, None, "#000000", 0.0, patterns[lit]))
            cmds.append(_text(f"{text}  ({GRAIN_LABELS[gr].lower()})", lx + 50, y + 1, 10, "l"))
    return cmds


# ------------------------------------------------------------------- scene
def to_scene(cmds, pattern_scale=0.45):
    """QGraphicsScene with the primitives (imported here: the module works without Qt).

    A polygon with a pattern code is drawn with the FGDC pattern tile (see patterns.py);
    'ground' is the hatched ground marker.  pattern_scale = size of the tiles (1 = 300 dpi)."""
    from qgis.PyQt.QtCore import Qt, QPointF
    from qgis.PyQt.QtGui import QBrush, QColor, QFont, QPen, QPolygonF
    from qgis.PyQt.QtWidgets import QGraphicsScene

    scene = QGraphicsScene()
    scene.setBackgroundBrush(QBrush(Qt.GlobalColor.white))
    for c in cmds:
        if c[0] == "poly":
            _, pts, fill, pen, pw, pattern = c
            poly = QPolygonF([QPointF(x, y) for x, y in pts])
            p = QPen(QColor(pen), pw) if pw > 0 else QPen(Qt.PenStyle.NoPen)
            if pattern:
                if pattern == "ground":
                    b = QBrush(QColor(0, 0, 0), Qt.BrushStyle.BDiagPattern)
                else:
                    b = pat.pattern_brush(pattern, pattern_scale)
                if b is None:
                    continue
                it = scene.addPolygon(poly, p, b)
                it.setZValue(2)
                it.setOpacity(0.85)
            else:
                b = QBrush(QColor(fill)) if fill else QBrush(Qt.BrushStyle.NoBrush)
                it = scene.addPolygon(poly, p, b)
                it.setZValue(1)
        elif c[0] == "line":
            _, x1, y1, x2, y2, pen, pw, dash = c
            p = QPen(QColor(pen), pw)
            if dash:
                p.setStyle(Qt.PenStyle.DotLine)
            scene.addLine(x1, y1, x2, y2, p).setZValue(3)
        elif c[0] == "text":
            _, s, x, y, size, align, bold, italic, rot, underline = c
            it = scene.addSimpleText(s)
            f = QFont()
            f.setPointSizeF(size)
            f.setBold(bold)
            f.setItalic(italic)
            f.setUnderline(underline)
            it.setFont(f)
            w = it.boundingRect().width()
            it.setZValue(5)
            if rot:
                it.setPos(x, y)
                it.setRotation(rot)
            else:
                it.setPos(x - (w / 2 if align == "c" else w if align == "r" else 0), y)
    r = scene.itemsBoundingRect().adjusted(-30, -30, 30, 30)
    scene.setSceneRect(r)
    return scene


def qt_measure():
    """Text width with the real font metrics."""
    from qgis.PyQt.QtGui import QFont, QFontMetricsF

    def measure(s, size=9.0, bold=False):
        f = QFont()
        f.setPointSizeF(size)
        f.setBold(bold)
        return QFontMetricsF(f).horizontalAdvance(s)
    return measure
