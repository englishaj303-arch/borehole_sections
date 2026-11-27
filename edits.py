"""Interactive editing of the 2D geological section: the edit model and its geometry helpers.

The automatic correlation (core.py) stays the starting point; the geologist has the last word.
An edit is stored in terms that survive a regeneration of the section (borehole names, layer
depths, distance along the section and elevation - never scene pixels or object ids):

  forced     correlation links that the automatic correlation must keep ("manual" = made by hand,
             "locked" = an automatic link that was frozen);
  forbidden  correlation links that were removed (the two layers are not correlated);
  short      layers whose lens (the part drawn between the boreholes) is suppressed - the layer
             ends right next to its borehole;
  shifts     moved layer boundaries (a smooth vertical bulge of the contact, zero at the boreholes);
  lenses     lenses drawn by hand (lithology, two tips and a thickness).

This module needs no QGIS classes, so the geometry can be tested on its own.
"""
import bisect
import copy
import json
import math
from dataclasses import dataclass, field

GAP = 0.75          # scene units: two polygon edges closer than this are one contact
MAX_ITEMS = 10000   # per list in an edit file
MAX_COORD = 1e7     # distance / elevation (m) in an edit file
MAX_TEXT = 256      # lithology / borehole name
MAX_FILE_BYTES = 5 * 1024 * 1024   # an edit file is a few KB; a bigger one is not an edit file


def read_edit_file(path):
    """Text of an edit file: a regular file of at most MAX_FILE_BYTES (a device, a FIFO or a huge
    file is refused; the descriptor is checked after opening, so there is no race)."""
    import stat
    import os
    with open(path, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
            raise ValueError("not an edit file (not a regular file, or larger than 5 MB)")
        raw = fh.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("not an edit file (larger than 5 MB)")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError(f"not an edit file (not UTF-8 text: {e.reason})") from e


def layer_key(bh_name, layer):
    """Stable identity of a layer: borehole name and depths (not an object id)."""
    return (str(bh_name), round(float(layer.top), 3), round(float(layer.bottom), 3))


@dataclass
class PairEdit:
    """Edits of ONE fence (two neighbouring boreholes), resolved to the Layer objects of a build."""
    forced: set = field(default_factory=set)       # {(id(layer A), id(layer B))}
    forbidden: set = field(default_factory=set)
    short: set = field(default_factory=set)        # {id(layer)}


@dataclass
class SectionEdits:
    forced: dict = field(default_factory=dict)     # {(keyA, keyB): "manual" | "locked"}
    forbidden: set = field(default_factory=set)    # {(keyA, keyB)}
    short: set = field(default_factory=set)        # {layer key}
    shifts: list = field(default_factory=list)     # [{"d", "z", "dz", "la", "lb"}]
    lenses: list = field(default_factory=list)     # [{"d1", "z1", "d2", "z2", "thick", "litho"}]

    def copy(self):
        return SectionEdits(dict(self.forced), set(self.forbidden), set(self.short),
                            copy.deepcopy(self.shifts), copy.deepcopy(self.lenses))

    def is_empty(self):
        return not (self.forced or self.forbidden or self.short or self.shifts or self.lenses)

    def count(self):
        return (len(self.forced) + len(self.forbidden) + len(self.short)
                + len(self.shifts) + len(self.lenses))

    def corr_signature(self):
        """What changes the correlation polygons (shifts and hand-drawn lenses are applied
        afterwards, so they are not part of the key of the polygon cache)."""
        return (tuple(sorted(self.forced.items())), tuple(sorted(self.forbidden)),
                tuple(sorted(self.short)))

    def resolve(self, name_a, LA, name_b, LB):
        """PairEdit for the fence (borehole A, borehole B) of the current build."""
        ka = {layer_key(name_a, L): id(L) for L in LA}
        kb = {layer_key(name_b, L): id(L) for L in LB}
        pe = PairEdit()
        for (a, b) in self.forced:
            if a in ka and b in kb:
                pe.forced.add((ka[a], kb[b]))
        for (a, b) in self.forbidden:
            if a in ka and b in kb:
                pe.forbidden.add((ka[a], kb[b]))
        for key in self.short:
            if key in ka:
                pe.short.add(ka[key])
            if key in kb:
                pe.short.add(kb[key])
        return pe

    # ---------------------------------------------------------------- JSON
    def to_json(self):
        return json.dumps({                  # allow_nan=False: never a file that from_json rejects
            "version": 1,
            "forced": [[list(a), list(b), o] for (a, b), o in sorted(self.forced.items())],
            "forbidden": [[list(a), list(b)] for a, b in sorted(self.forbidden)],
            "short": [list(k) for k in sorted(self.short)],
            "shifts": self.shifts, "lenses": self.lenses}, indent=1, allow_nan=False)

    @staticmethod
    def from_json(text):
        """Edits from a file written by to_json.  Anything that is not valid (NaN / Infinity,
        wrong types, a lens without thickness...) raises ValueError, so a damaged file never
        puts bad coordinates into the section."""
        def no_const(val):
            raise ValueError(f"invalid JSON number: {val}")

        d = json.loads(text, parse_constant=no_const)
        if not isinstance(d, dict):
            raise ValueError("not a section edit file")

        def num(v, name):
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                raise ValueError(f"'{name}' is not a number")
            x = float(v)
            if not math.isfinite(x) or abs(x) > MAX_COORD:
                raise ValueError(f"'{name}' is not a usable number")
            return x

        def text(v, name, required=False):
            if v is None and not required:
                return None
            if not isinstance(v, str) or len(v) > MAX_TEXT or (required and not v.strip()) \
                    or any(ord(ch) < 32 for ch in v):
                raise ValueError(f"'{name}' must be text of 1 to {MAX_TEXT} characters")
            return v

        def items(name):
            val = d.get(name, [])
            if not isinstance(val, list):
                raise ValueError(f"'{name}' must be a list")
            if len(val) > MAX_ITEMS:
                raise ValueError(f"'{name}' has more than {MAX_ITEMS} items")
            return val

        def key(k):
            if not isinstance(k, (list, tuple)) or len(k) != 3:
                raise ValueError("invalid layer key")
            top, bottom = num(k[1], "top"), num(k[2], "bottom")
            if top < 0 or bottom <= top:
                raise ValueError("a layer key needs 0 <= top < bottom")
            return (text(k[0], "borehole", True), round(top, 3), round(bottom, 3))

        def text_or_none(v, name):
            return text(v, name)

        ed = SectionEdits()
        for it in items("forced"):
            if not isinstance(it, (list, tuple)) or len(it) != 3:
                raise ValueError("invalid forced link")
            ed.forced[(key(it[0]), key(it[1]))] = "manual" if it[2] == "manual" else "locked"
        for it in items("forbidden"):
            if not isinstance(it, (list, tuple)) or len(it) != 2:
                raise ValueError("invalid removed link")
            ed.forbidden.add((key(it[0]), key(it[1])))
        for k in items("short"):
            ed.short.add(key(k))
        for s in items("shifts"):
            if not isinstance(s, dict):
                raise ValueError("invalid shift")
            ed.shifts.append({"d": num(s.get("d"), "d"), "z": num(s.get("z"), "z"),
                              "dz": num(s.get("dz"), "dz"),
                              "la": text_or_none(s.get("la"), "la"),
                              "lb": text_or_none(s.get("lb"), "lb")})
        for ln in items("lenses"):
            if not isinstance(ln, dict):
                raise ValueError("invalid lens")
            lens = {k: num(ln.get(k), k) for k in ("d1", "z1", "d2", "z2", "thick")}
            if not 0.0 < lens["thick"] <= 1000.0:
                raise ValueError("lens thickness must be between 0 and 1000 m")
            if abs(lens["d2"] - lens["d1"]) < 1e-6:
                raise ValueError("a lens needs two different tips")
            lens["litho"] = text(ln.get("litho"), "litho", True)
            ed.lenses.append(lens)
        ed.forbidden -= set(ed.forced)           # a link cannot be both kept and removed
        return ed


# ------------------------------------------------------------------ geometry
def stack_at(polys, x, only=None):
    """Intervals (ytop, ybottom, polygon index) of the polygons along the vertical line at x,
    sorted from the top.  Half-open edge rule: a vertex is never counted twice."""
    res = []
    for idx in (range(len(polys)) if only is None else only):
        pts = polys[idx][0]
        ys = []
        x1, y1 = pts[-1]
        for x2, y2 in pts:
            if (x1 <= x < x2) or (x2 <= x < x1):
                ys.append(y1 + (y2 - y1) * (x - x1) / (x2 - x1))
            x1, y1 = x2, y2
        ys.sort()
        for a in range(0, len(ys) - 1, 2):
            if ys[a + 1] - ys[a] > 1e-6:
                res.append((ys[a], ys[a + 1], idx))
    res.sort()
    return res


def bump(x, x0, pk, x1):
    """Smooth hill: 0 at x0 and x1, 1 at pk (x0 < pk < x1; anything else gives 0)."""
    if x <= x0 or x >= x1 or pk <= x0 or pk >= x1:
        return 0.0
    t = (x - x0) / (pk - x0) if x <= pk else (x1 - x) / (x1 - pk)
    return 0.5 * (1.0 - math.cos(math.pi * min(max(t, 0.0), 1.0)))


def _sample(polys, A, B, x):
    """(top of A, contact, bottom of B) at x where A lies directly above B, else None."""
    best = None
    for ta, ba, _ in stack_at(polys, x, (A,)):
        for tb, bb, _ in stack_at(polys, x, (B,)):
            g = abs(ba - tb)
            if g <= GAP and (best is None or g < best[0]):
                best = (g, ta, 0.5 * (ba + tb), bb)
    return None if best is None else best[1:]


def find_contact(polys, px, py, tol, la=None, lb=None, steps=120):
    """The contact (boundary between two polygons) nearest to the point (px, py).

    la / lb (lithology above / below) restrict the search (used when a stored edit is replayed).
    Returns {"A", "B", "xs", "u", "c", "l", "pk"} or None: A lies above B, xs is a grid along
    the stretch where they touch, u / c / l = top of A / contact / bottom of B at every x."""
    st = stack_at(polys, px)
    best = None
    for a in range(len(st)):               # every pair: a thin sliver may lie between two polygons
        t1, b1, i1 = st[a]
        for b in range(a + 1, len(st)):
            t2, b2, i2 = st[b]
            if t2 > b1 + GAP:              # sorted by the top: the rest starts even lower
                break
            if i1 == i2 or abs(b1 - t2) > GAP:
                continue
            if la is not None and (polys[i1][1] != la or polys[i2][1] != lb):
                continue
            d = abs(0.5 * (b1 + t2) - py)
            if d <= tol and (best is None or d < best[0]):
                best = (d, i1, i2)
    if best is None:
        return None
    _, A, B = best
    xa = [p[0] for p in polys[A][0]]
    xb = [p[0] for p in polys[B][0]]
    if not xa or not xb:
        return None
    lo, hi = max(min(xa), min(xb)), min(max(xa), max(xb))
    if hi - lo < 1e-6:
        return None
    h = (hi - lo) / steps
    # a FIXED grid of the stretch (nodes lo + k*h): repeated moves of the same contact split the
    # polygon edges at the same x again, so the number of vertices does not grow
    k0 = min(max(int(round((px - lo) / h)), 1), steps - 1)
    px = lo + k0 * h
    first = _sample(polys, A, B, px)
    if first is None:
        return None
    rows = {px: first}
    for sgn in (1, -1):
        k = k0 + sgn
        while 1 <= k <= steps - 1:
            x = lo + k * h
            s = _sample(polys, A, B, x)
            if s is None:
                break
            rows[x] = s
            k += sgn
    xs = sorted(rows)
    if len(xs) < 5:
        return None
    x0, x1 = xs[0], xs[-1]
    pk = min(max(px, x0 + 0.15 * (x1 - x0)), x1 - 0.15 * (x1 - x0))
    return {"A": A, "B": B, "xs": xs, "u": [rows[x][0] for x in xs],
            "c": [rows[x][1] for x in xs], "l": [rows[x][2] for x in xs], "pk": pk}


def shift_limits(ct):
    """(largest move up, largest move down) in scene units that keeps both layers thick."""
    up = down = math.inf
    x0, x1, pk = ct["xs"][0], ct["xs"][-1], ct["pk"]
    for x, u, c, l in zip(ct["xs"], ct["u"], ct["c"], ct["l"]):
        w = bump(x, x0, pk, x1)
        if w < 0.02:
            continue
        up = min(up, (c - u) / w)
        down = min(down, (l - c) / w)
    if not math.isfinite(up):
        up = 0.0
    if not math.isfinite(down):
        down = 0.0
    return 0.9 * max(up, 0.0), 0.9 * max(down, 0.0)


def shift_profile(ct, dy):
    """Points of the moved contact: [(x, y)] (for the live preview)."""
    x0, x1, pk = ct["xs"][0], ct["xs"][-1], ct["pk"]
    return [(x, c + dy * bump(x, x0, pk, x1)) for x, c in zip(ct["xs"], ct["c"])]


def apply_shift(polys, ct, dy):
    """New list of polygons with the contact `ct` moved by dy (scene y, + = down).

    A continuous vertical warp: the vertices on the contact move by dy * bump(x); between the
    contact and the top of the upper / the bottom of the lower layer the move fades out
    linearly, so the neighbouring contacts stay where they were, nothing overlaps and no gap
    opens.  Edges are first split on the grid of the contact, so the warp follows it."""
    xs, us, cs, ls = ct["xs"], ct["u"], ct["c"], ct["l"]
    x0, x1, pk = xs[0], xs[-1], ct["pk"]
    ymin, ymax = min(us), max(ls)

    def at(x):
        i = min(max(bisect.bisect_right(xs, x) - 1, 0), len(xs) - 2)
        t = (x - xs[i]) / (xs[i + 1] - xs[i])
        return (us[i] + (us[i + 1] - us[i]) * t, cs[i] + (cs[i + 1] - cs[i]) * t,
                ls[i] + (ls[i + 1] - ls[i]) * t)

    out = list(polys)
    for idx, item in enumerate(polys):
        pts = item[0]
        px_ = [p[0] for p in pts]
        py_ = [p[1] for p in pts]
        if max(px_) <= x0 or min(px_) >= x1 or max(py_) < ymin - 1e-6 or min(py_) > ymax + 1e-6:
            continue
        dense = []
        n = len(pts)
        for k in range(n):
            p, q = pts[k], pts[(k + 1) % n]
            dense.append((p[0], p[1], True))                 # True = an original vertex
            if p[0] != q[0]:
                lo, hi = (p[0], q[0]) if p[0] < q[0] else (q[0], p[0])
                gx = xs[bisect.bisect_right(xs, lo + 1e-6):bisect.bisect_left(xs, hi - 1e-6)]
                if p[0] > q[0]:
                    gx = gx[::-1]
                for g in gx:
                    t = (g - p[0]) / (q[0] - p[0])
                    dense.append((g, p[1] + t * (q[1] - p[1]), False))
        new, moved = [], False
        for x, y, orig in dense:
            ny = y
            if x0 < x < x1:
                u, c, l = at(x)
                if u < y < l:
                    if y <= c:
                        f = (y - u) / (c - u) if c - u > 1e-9 else 0.0
                    else:
                        f = (l - y) / (l - c) if l - c > 1e-9 else 0.0
                    ny = min(max(y + dy * bump(x, x0, pk, x1) * f, u), l)   # never past a neighbour
                    moved = moved or ny != y
            new.append((x, ny, orig))
        if moved:
            out[idx] = (_simplify(new),) + tuple(item[1:])
    return out


def _simplify(pts, tol=1e-6):
    """pts = [(x, y, original)].  Drops the vertices that were ADDED by the split on the grid
    and lie on the straight line between their neighbours (where nothing moved they are not
    needed); the original vertices of the polygon always stay.  Returns [(x, y)]."""
    n = len(pts)
    out = []
    for i in range(n):
        x, y, orig = pts[i]
        if not orig and n > 3 and out:
            a = out[-1]
            b = pts[(i + 1) % n]
            dx, dy = b[0] - a[0], b[1] - a[1]
            ln2 = dx * dx + dy * dy
            if ln2 > 1e-18:
                t = ((x - a[0]) * dx + (y - a[1]) * dy) / ln2
                if 0.0 < t < 1.0 and abs((x - a[0]) * dy - (y - a[1]) * dx) / math.sqrt(ln2) < tol:
                    continue
        out.append((x, y))
    return out if len(out) >= 3 else [(x, y) for x, y, _o in pts]


def lens_outline(p1, p2, thick, n=24):
    """Polygon of a lens between the tips p1 and p2 (scene units) with the given thickness;
    [] when it would have no area (the tips coincide or the thickness is not positive)."""
    (x1, y1), (x2, y2) = p1, p2
    if not (thick > 0.0) or abs(x2 - x1) < 1e-9 or not all(map(math.isfinite, (x1, y1, x2, y2, thick))):
        return []
    top, bot = [], []
    for k in range(n + 1):
        s = k / n
        xc, yc = x1 + (x2 - x1) * s, y1 + (y2 - y1) * s
        h = 0.5 * thick * (1.0 - (2.0 * s - 1.0) ** 2)
        top.append((xc, yc - h))
        bot.append((xc, yc + h))
    return top + bot[-2:0:-1]


def dist_to_segment(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    ln = dx * dx + dy * dy
    t = 0.0 if ln <= 1e-12 else min(max(((p[0] - ax) * dx + (p[1] - ay) * dy) / ln, 0.0), 1.0)
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))
