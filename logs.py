"""Individual borehole profiles and sedimentary records (no GUI widgets).

The drawing is computed as a plain list of primitives (rectangles, polygons, lines, texts),
so the layout does not depend on Qt; ``to_scene`` turns the list into a QGraphicsScene.

Two figures are offered:

* ``build_profiles``  - up to four borehole columns side by side with a depth ruler and the
                        description of every layer written next to the column,
* ``build_records``   - sedimentary records: the column width follows the grain size
                        (clay / silt / sand / gravel) and an "Index" legend lists the rock types.
"""
import math
import string

from . import core

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
                min(b.z - max([L.bottom for L in b.layers] or [b.depth]) for b in bhs))
    return 0.0, max(max([L.bottom for L in b.layers] or [b.depth]) for b in bhs)


# ------------------------------------------------------------- 1) profiles
COL_W = 150.0
LABEL_W = 250.0
GAP = 40.0
TOP = 70.0


def _spread(wish, hgt):
    """Boxes of the given heights as close to their wished starts as possible, not overlapping
    (neighbouring boxes that collide are shifted together around their wished position)."""
    clusters = []                       # [first index, last index, start y]

    def start_of(i0, i1):
        off, tot = 0.0, 0.0
        for j in range(i0, i1 + 1):
            tot += wish[j] - off
            off += hgt[j]
        return tot / (i1 - i0 + 1)

    for i in range(len(wish)):
        cl = [i, i, wish[i]]
        while clusters:
            p = clusters[-1]
            if p[2] + sum(hgt[p[0]:p[1] + 1]) <= cl[2] + 1e-6:
                break
            clusters.pop()
            cl = [p[0], i, start_of(p[0], i)]
        clusters.append(cl)
    out = []
    for i0, i1, y in clusters:
        for j in range(i0, i1 + 1):
            out.append([y, y + hgt[j]])
            y += hgt[j]
    return out


def _layout_labels(items, line_h):
    """items: [(top_y, bottom_y, lines)] sorted by depth.  Returns [(box_top, box_bottom)].

    A label whose text fits into its layer keeps the extent of the layer.  The labels of thin
    layers get a box centred on the layer, pushed apart from each other and kept inside the
    free space between the fitting labels (if the space is too small the lower ones move down)."""
    n = len(items)
    need = [len(lines) * line_h + 4 for _, _, lines in items]
    fits = [need[i] <= items[i][1] - items[i][0] for i in range(n)]
    boxes = [None] * n
    cursor, i = -1e9, 0
    while i < n:
        if fits[i]:
            top = max(items[i][0], cursor)
            boxes[i] = (top, top + (items[i][1] - items[i][0]))
            cursor = boxes[i][1]
            i += 1
            continue
        j = i
        while j < n and not fits[j]:
            j += 1
        hi = items[j][0] if j < n else 1e9
        hgt = [need[m] for m in range(i, j)]
        wish = [(items[m][0] + items[m][1]) / 2 - need[m] / 2 for m in range(i, j)]
        grp = _spread(wish, hgt)
        prev = cursor
        for g in grp:                           # not above the previous box
            d = max(prev - g[0], 0.0)
            g[0] += d
            g[1] += d
            prev = g[1]
        lim = hi
        for g in reversed(grp):                 # not into the next fitting box, if possible
            d = max(g[1] - lim, 0.0)
            g[0] -= d
            g[1] -= d
            lim = g[0]
        prev = cursor
        for g in grp:
            d = max(prev - g[0], 0.0)
            g[0] += d
            g[1] += d
            prev = g[1]
            boxes[i + grp.index(g)] = (g[0], g[1])
        cursor = prev
        i = j
    return boxes


def build_profiles(bhs, colors, grains, mode="depth", px_per_m=0.0, measure=approx_measure,
                   show_pattern=True):
    """Primitives of up to four borehole columns with descriptions.

    colors: litho -> '#rrggbb';  grains: litho -> grain class;  mode: 'depth' or 'alt'."""
    cmds = []
    v_top, v_bot = _extent(bhs, mode)
    span = max(v_bot - v_top if mode == "depth" else v_top - v_bot, 1e-6)
    k = px_per_m if px_per_m > 0 else max(8.0, min(60.0, 900.0 / span))
    line_h = 13.0

    def y_of(v):                           # v = depth, or altitude in 'alt' mode
        return TOP + ((v - v_top) if mode == "depth" else (v_top - v)) * k

    ruler_x = 70.0
    lo, hi = (v_top, v_bot) if mode == "depth" else (v_bot, v_top)
    _ruler(cmds, ruler_x, y_of, lo, hi, k)
    cmds.append(_text("Depth [m]" if mode == "depth" else "Altitude [m a.s.l.]",
                      ruler_x - 54, TOP - 40, 9, "l", bold=True))

    x0 = ruler_x + 70
    for bh in bhs:
        layers = _clean(bh)
        z0 = bh.z
        cmds.append(_text(bh.name, x0 + COL_W / 2, TOP - 50, 15, "c", bold=True))
        # ground marker
        ytop = y_of(0.0 if mode == "depth" else z0)
        cmds.append(_rect(x0, ytop - 5, COL_W, 5, "#ffffff", "#000000", 0.8, "ground"))
        items = []
        for L in layers:
            a = L.top if mode == "depth" else z0 - L.top
            b = L.bottom if mode == "depth" else z0 - L.bottom
            y1, y2 = y_of(a), y_of(b)
            col = colors.get(L.litho, "#cccccc")
            gr = grains.get(L.litho, DEFAULT_GRAIN)
            cmds.append(_rect(x0, y1, COL_W, y2 - y1, col, "#303030", 0.6))
            if show_pattern:
                cmds.append(_rect(x0, y1, COL_W, y2 - y1, None, "#303030", 0.0, gr))
            items.append((y1, y2, wrap_text(L.litho, LABEL_W - 40, measure, 10), L.litho))
        # descriptions
        lx0, lx1 = x0 + COL_W + 6, x0 + COL_W + LABEL_W
        boxes = _layout_labels([(a, b, ln) for a, b, ln, _ in items], line_h)
        for (y1, y2, lines, _), (b0, b1) in zip(items, boxes):
            slanted = abs(b0 - y1) > 2.5 or abs(b1 - y2) > 2.5
            if slanted:
                # trapezoid: the box is displaced, its sides lead to the layer
                cmds.append(_poly([(lx0, y1), (lx0 + 18, b0), (lx1, b0)], None, "#000000", 0.8))
                cmds.append(_poly([(lx0, y2), (lx0 + 18, b1), (lx1, b1)], None, "#000000", 0.8))
                tx0 = lx0 + 18
            else:
                cmds.append(_line(lx0, b0, lx1, b0, "#000000", 0.8))
                cmds.append(_line(lx0, b1, lx1, b1, "#000000", 0.8))
                tx0 = lx0
            mid = (b0 + b1) / 2 if slanted else (y1 + y2) / 2
            cy = mid - len(lines) * line_h / 2
            for i, s in enumerate(lines):
                cmds.append(_text(s, (tx0 + lx1) / 2, cy + i * line_h - 1, 10, "c"))
        x0 += COL_W + LABEL_W + GAP
    return cmds


# ---------------------------------------------------------- 2) sedimentary record
CELL = 22.0           # width of one grain-size class
HEAD_H = 58.0
REC_TOP = 110.0


def build_records(bhs, colors, grains, px_per_m=0.0, measure=approx_measure, letters=True,
                  legend=True, mode="depth"):
    """Sedimentary records side by side + an 'Index' legend (rock type and grain size)."""
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
        z0 = bh.z
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
            a = L.top if mode == "depth" else z0 - L.top
            b = L.bottom if mode == "depth" else z0 - L.bottom
            gr = grains.get(L.litho, DEFAULT_GRAIN)
            w = CELL * (GRAINS.index(gr) + 1)
            y1, y2 = y_of(a), y_of(b)
            cmds.append(_rect(x0, y1, w, y2 - y1, colors.get(L.litho, "#cccccc"), "#000000", 0.9))
            cmds.append(_rect(x0, y1, w, y2 - y1, None, "#000000", 0.0, gr))
            if L.litho not in used:
                used.append(L.litho)
        if letters:
            cmds.append(_text(string.ascii_lowercase[n % 26], x0 + gw / 2, bottom_px + 12, 14,
                              "c", bold=True))
        x0 += gw + 100.0

    if legend and used:
        lx = x0 + 10
        ly = REC_TOP - 8
        cmds.append(_text("Index", lx, ly - 34, 14, "l", bold=True, italic=True, underline=True))
        row = 26.0
        for i, lit in enumerate(used):
            y = ly + i * row
            gr = grains.get(lit, DEFAULT_GRAIN)
            cmds.append(_rect(lx, y, 40, 18, colors.get(lit, "#cccccc"), "#000000", 0.9))
            cmds.append(_rect(lx, y, 40, 18, None, "#000000", 0.0, gr))
            cmds.append(_text(f"{lit}  ({GRAIN_LABELS[gr].lower()})", lx + 50, y + 1, 10, "l"))
    return cmds


# ------------------------------------------------------------------- scene
def to_scene(cmds):
    """QGraphicsScene with the primitives (imported here: the module works without Qt)."""
    from qgis.PyQt.QtCore import Qt, QPointF
    from qgis.PyQt.QtGui import QBrush, QColor, QFont, QPen, QPolygonF
    from qgis.PyQt.QtWidgets import QGraphicsScene

    styles = {"clay": Qt.BrushStyle.HorPattern, "silt": Qt.BrushStyle.Dense5Pattern,
              "sand": Qt.BrushStyle.Dense6Pattern, "gravel": Qt.BrushStyle.DiagCrossPattern,
              "ground": Qt.BrushStyle.BDiagPattern}
    scene = QGraphicsScene()
    scene.setBackgroundBrush(QBrush(Qt.GlobalColor.white))
    for c in cmds:
        if c[0] == "poly":
            _, pts, fill, pen, pw, pattern = c
            poly = QPolygonF([QPointF(x, y) for x, y in pts])
            p = QPen(QColor(pen), pw) if pw > 0 else QPen(Qt.PenStyle.NoPen)
            if pattern in styles:
                color = QColor(0, 0, 0, 110)
                if fill is None:
                    b = QBrush(color, styles[pattern])
                else:
                    b = QBrush(QColor(fill), styles[pattern])
                    b.setColor(color)
                it = scene.addPolygon(poly, p, b)
                it.setZValue(2)
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
