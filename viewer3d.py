"""Own 3D viewer of the borehole models (WebGL2, no external libraries).

This module is pure Python (no QGIS / Qt imports): it converts the closed solids made by
`model3d` into compact triangle meshes and writes ONE self-contained HTML file (the viewer
`viewer3d.html` with the scene embedded).  The file is opened in the default web browser
(see `viewer3d_window.py`); nothing is downloaded and no server is needed.
"""
import base64
import contextlib
import json
import logging
import math
import os
import re
import stat
import tempfile
import time
from array import array
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "viewer3d.html")
PLACEHOLDER = "/*SCENE_JSON*/null"
_log = logging.getLogger(__name__)
MAX_EDGE_FACES = 400000          # above this number of faces the edge lines are not exported


# ------------------------------------------------------------ triangulation
def _ear_clip(P, eps=None):
    """Ear clipping of a simple polygon given as a counter-clockwise list of (u, v).
    Returns triangles as index triples into P.  Collinear vertices are dropped, degenerate
    input never loops forever.  The tolerances follow the size of the polygon (like in
    core._bridge_holes), so thin millimetre wedges are not dropped as collinear: eps is the
    tolerance of the cross products (areas), leps that of the coordinates (lengths)."""
    n = len(P)
    scale = max((max(abs(p[0]), abs(p[1])) for p in P), default=0.0)
    if P:
        scale = max(scale, max(p[0] for p in P) - min(p[0] for p in P),
                    max(p[1] for p in P) - min(p[1] for p in P))
    scale = max(scale, 1e-9)
    if eps is None:
        eps = 1e-12 * scale * scale
    leps = 1e-11 * scale
    prev = [(i - 1) % n for i in range(n)]
    nxt = [(i + 1) % n for i in range(n)]

    def cross(i):
        a, c = P[prev[i]], P[nxt[i]]
        b = P[i]
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    reflex = {i for i in range(n) if cross(i) < -eps}
    tris = []
    remaining = n
    i = 0
    stall = 0

    def remove(i):
        a, c = prev[i], nxt[i]
        nxt[a], prev[c] = c, a
        reflex.discard(i)
        for k in (a, c):                       # the neighbours change their angle: classify again
            if cross(k) < -eps:
                reflex.add(k)
            else:
                reflex.discard(k)
        return c

    while remaining > 3:
        a, c = prev[i], nxt[i]
        cr = cross(i)
        if abs(cr) <= eps:                      # collinear: drop the vertex, no triangle
            i = remove(i)
            remaining -= 1
            stall = 0
            continue
        ear = cr > 0
        if ear and reflex:
            A, B, C = P[a], P[i], P[c]
            lox, hix = min(A[0], B[0], C[0]) - leps, max(A[0], B[0], C[0]) + leps
            loy, hiy = min(A[1], B[1], C[1]) - leps, max(A[1], B[1], C[1]) + leps
            for j in reflex:
                if j == a or j == c or j == i:
                    continue
                p = P[j]
                if p[0] < lox or p[0] > hix or p[1] < loy or p[1] > hiy:
                    continue
                if any(abs(p[0] - q[0]) <= leps and abs(p[1] - q[1]) <= leps for q in (A, B, C)):
                    continue                    # a duplicate of the ear's own vertex (hole bridge)
                d1 = (B[0] - A[0]) * (p[1] - A[1]) - (B[1] - A[1]) * (p[0] - A[0])
                d2 = (C[0] - B[0]) * (p[1] - B[1]) - (C[1] - B[1]) * (p[0] - B[0])
                d3 = (A[0] - C[0]) * (p[1] - C[1]) - (A[1] - C[1]) * (p[0] - C[0])
                if d1 >= -eps and d2 >= -eps and d3 >= -eps:
                    ear = False
                    break
        if ear:
            tris.append((a, i, c))
            i = remove(i)
            remaining -= 1
            stall = 0
            continue
        i = c
        stall += 1
        if stall > remaining:                   # degenerate polygon: force progress
            j0, best, k = i, None, i
            for _ in range(remaining):
                v = cross(k)
                if best is None or v > best:
                    best, j0 = v, k
                k = nxt[k]
            tris.append((prev[j0], j0, nxt[j0]))
            i = remove(j0)
            remaining -= 1
            stall = 0
    if remaining == 3:
        a = i
        if abs(cross(a)) > eps:                 # the last three may have become collinear
            tris.append((prev[a], a, nxt[a]))
    return tris


def _newell(pts):
    """Newell normal (not normalised) of a planar polygon given as a list of (x, y, z)."""
    nx = ny = nz = 0.0
    n = len(pts)
    for k in range(n):
        x1, y1, z1 = pts[k]
        x2, y2, z2 = pts[(k + 1) % n]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    return nx, ny, nz


def _tri_ok(p, t, nrm):
    """True when the triangle t of the polygon p has the same orientation as the polygon
    (a triangle on the other side - or a zero one - means a diagonal outside a concave quad)."""
    a, b, c = (p[k] for k in t)
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    cx, cy, cz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    return cx * nrm[0] + cy * nrm[1] + cz * nrm[2] > 0.0


def triangulate_face(ring, verts):
    """Triangles (index triples into `verts`, a flat [x, y, z, ...] list) of one planar face
    given as a ring of vertex indices."""
    n = len(ring)
    if n < 3:
        return []
    if n == 3:
        j = [3 * i for i in ring]               # a triangle of collinear / coincident vertices
        a, b, c = ((verts[k], verts[k + 1], verts[k + 2]) for k in j)   # has no area: drop it
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        cx, cy, cz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        wx, wy, wz = c[0] - b[0], c[1] - b[1], c[2] - b[2]
        longest2 = max(ux * ux + uy * uy + uz * uz, vx * vx + vy * vy + vz * vz,
                       wx * wx + wy * wy + wz * wz)
        # |cross| = 2 * area; relative to the longest edge squared, so thin but real slivers stay
        if math.sqrt(cx * cx + cy * cy + cz * cz) <= 1e-9 * longest2:
            return []
        return [tuple(ring)]

    def xyz(k):
        j = 3 * ring[k]
        return verts[j], verts[j + 1], verts[j + 2]

    if n == 4:                                  # quad: split along the shorter VALID diagonal
        p = [xyz(k) for k in range(4)]
        r = ring
        nrm = _newell(p)
        ok02 = _tri_ok(p, (0, 1, 2), nrm) and _tri_ok(p, (0, 2, 3), nrm)
        ok13 = _tri_ok(p, (1, 2, 3), nrm) and _tri_ok(p, (1, 3, 0), nrm)
        if ok02 or ok13:
            d02 = sum((p[0][t] - p[2][t]) ** 2 for t in range(3))
            d13 = sum((p[1][t] - p[3][t]) ** 2 for t in range(3))
            if ok02 and (d02 <= d13 or not ok13):
                return [(r[0], r[1], r[2]), (r[0], r[2], r[3])]
            return [(r[1], r[2], r[3]), (r[1], r[3], r[0])]
        # neither diagonal passes the test: a warped (slightly twisted) quad of a smooth solid.
        # Projecting it to one coordinate plane for ear clipping could cross the triangles, so
        # it is split along the shorter diagonal; a zero-area quad gives no triangles at all.
        if max(abs(nrm[0]), abs(nrm[1]), abs(nrm[2])) < 1e-12:
            return []
        d02 = sum((p[0][t] - p[2][t]) ** 2 for t in range(3))
        d13 = sum((p[1][t] - p[3][t]) ** 2 for t in range(3))
        if d02 <= d13:
            return [(r[0], r[1], r[2]), (r[0], r[2], r[3])]
        return [(r[1], r[2], r[3]), (r[1], r[3], r[0])]
    pts = [xyz(k) for k in range(n)]
    nx = ny = nz = 0.0                          # Newell normal
    for k in range(n):
        x1, y1, z1 = pts[k]
        x2, y2, z2 = pts[(k + 1) % n]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    ax, ay, az = abs(nx), abs(ny), abs(nz)
    if max(ax, ay, az) < 1e-12:
        return []
    if az >= ax and az >= ay:
        uv = [(p[0], p[1]) for p in pts]
        sgn = 1.0 if nz > 0 else -1.0
    elif ax >= ay:
        uv = [(p[1], p[2]) for p in pts]
        sgn = 1.0 if nx > 0 else -1.0
    else:
        uv = [(p[2], p[0]) for p in pts]
        sgn = 1.0 if ny > 0 else -1.0
    if sgn < 0:                                 # make the polygon counter-clockwise
        order = list(range(n - 1, -1, -1))
    else:
        order = list(range(n))
    tris = _ear_clip([uv[k] for k in order])
    if sgn < 0:                                 # keep the winding of the ring: reverse the triangles
        return [(ring[order[a]], ring[order[c]], ring[order[b]]) for a, b, c in tris]
    return [(ring[order[a]], ring[order[b]], ring[order[c]]) for a, b, c in tris]


# ---------------------------------------------------------------- meshes
# Two tolerances are kept apart:
#  * the MERGE tolerance decides which vertices are one vertex (shared corners of neighbouring
#    solids; needed so that the walls between blocks of one lithology cancel and the solids have
#    no gaps).  It is a distance (every coordinate differs by at most the tolerance), not the
#    rounding of the coordinates, so two points 0.0001 m apart are never split by a rounding
#    boundary, and the stored coordinates are the real ones (they are not snapped to a grid);
#  * the RENDER precision is that of the float32 buffers sent to WebGL (the coordinates are
#    relative to the model centre).  A merge finer than that would only create triangles that
#    degenerate in the GPU, so the merge tolerance is never smaller than the float32 spacing at
#    the largest coordinate of the model (see merge_tolerance).
MERGE_TOL_MIN = 1e-4                 # m; 0.1 mm - thin layers (cm) are never merged
_F32_REL = 2.0 ** -23                # relative spacing of float32


def merge_tolerance(max_abs):
    """Merge tolerance [m] for a model whose largest coordinate (after the shift to the model
    centre; elevations are not shifted) is `max_abs` m: twice the float32 spacing there, at
    least MERGE_TOL_MIN."""
    try:
        m = abs(float(max_abs))
    except (TypeError, ValueError):
        m = 0.0
    if not math.isfinite(m):
        m = 0.0
    return max(MERGE_TOL_MIN, 2.0 * m * _F32_REL)


class _Mesh:
    def __init__(self, tol=MERGE_TOL_MIN):
        self.tol = float(tol) if tol and tol > 0 else MERGE_TOL_MIN
        self._h = 8.0 * self.tol                 # cell of the lookup grid; merge radius = h / 8
        self.cells = {}                          # grid cell -> indices of the vertices in it
        self.verts = []                          # flat x, y, z (float, real coordinates)
        self.faces = []                          # rings of vertex indices
        self.owner = []                          # body (solid) number of every face
        self.body = 0

    def vertex(self, x, y, z):
        h, tol, verts = self._h, self.tol, self.verts
        fx, fy, fz = x / h, y / h, z / h
        cx, cy, cz = math.floor(fx), math.floor(fy), math.floor(fz)
        # a point within tol of a cell border can have its partner in the neighbouring cell
        ox = (0, -1) if fx - cx < 0.125 else ((0, 1) if fx - cx > 0.875 else (0,))
        oy = (0, -1) if fy - cy < 0.125 else ((0, 1) if fy - cy > 0.875 else (0,))
        oz = (0, -1) if fz - cz < 0.125 else ((0, 1) if fz - cz > 0.875 else (0,))
        best, best_d = None, None               # the NEAREST vertex within tol (not the first one found:
        for dx in ox:                           # a chain of close points then joins the same representative)
            for dy in oy:
                for dz in oz:
                    for i in self.cells.get((cx + dx, cy + dy, cz + dz), ()):
                        k = 3 * i
                        if (abs(verts[k] - x) <= tol and abs(verts[k + 1] - y) <= tol
                                and abs(verts[k + 2] - z) <= tol):
                            d = (verts[k] - x) ** 2 + (verts[k + 1] - y) ** 2 + (verts[k + 2] - z) ** 2
                            if best_d is None or d < best_d:
                                best, best_d = i, d
        if best is not None:
            return best
        i = len(verts) // 3
        self.cells.setdefault((cx, cy, cz), []).append(i)
        verts.extend((float(x), float(y), float(z)))
        return i

    def add_face(self, ring3d, ox, oy):
        ring = []
        for x, y, z in ring3d:
            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                return                           # NaN / Inf: the face cannot be placed, drop it
            i = self.vertex(x - ox, y - oy, z)
            if not ring or ring[-1] != i:
                ring.append(i)
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring.pop()
        if len(ring) >= 3:
            self.faces.append(tuple(ring))
            self.owner.append(self.body)

    def _quad_vs_triangles(self, count):
        """Indices of faces forming a wall between two blocks of the same lithology where one
        block has the wall as ONE quadrilateral and the neighbouring (pinching-out) block as
        TWO triangles (either diagonal): such a wall is inside the solid and is dropped too."""
        tri_at = {}
        for i, f in enumerate(self.faces):
            if len(f) == 3:
                key = tuple(sorted(f))
                if count[key] == 1:
                    tri_at[key] = i
        drop = set()
        if not tri_at:
            return drop
        for i, f in enumerate(self.faces):
            if len(f) != 4 or len(set(f)) != 4 or count[tuple(sorted(f))] != 1:
                continue
            a, b, c, d = f
            for t1, t2 in (((a, b, c), (a, c, d)), ((a, b, d), (b, c, d))):   # the two diagonals
                i1, i2 = tri_at.get(tuple(sorted(t1))), tri_at.get(tuple(sorted(t2)))
                if i1 is not None and i2 is not None and i1 not in drop and i2 not in drop \
                        and self.owner[i] not in (self.owner[i1], self.owner[i2]):   # a wall BETWEEN bodies
                    drop.update((i, i1, i2))
                    break
        return drop

    def finish(self, edges):
        """Drop faces that appear twice (the wall between two blocks of the same lithology),
        triangulate, return (positions, triangles, edges) as arrays."""
        count = Counter(tuple(sorted(f)) for f in self.faces)
        drop = self._quad_vs_triangles(count)
        owners = {}                              # face key -> the bodies it comes from
        for f, o in zip(self.faces, self.owner):
            owners.setdefault(tuple(sorted(f)), set()).add(o)
        # faces met an even number of times are walls between two blocks (inside the solid) and
        # go all; an odd number (3 coplanar faces at a hole bridge: 2 inner + 1 outer) leaves ONE.
        # Twin faces of ONE body are no inner wall: they are the front and the back of a panel
        # so thin that its vertices were merged (width <= 2 * tol); one sheet of it stays
        # (the panel would vanish, while its bigger faces of 5+ vertices stayed).
        kept, once = [], set()
        for i, f in enumerate(self.faces):
            if i in drop:
                continue
            key = tuple(sorted(f))
            single_body = len(owners[key]) == 1
            if len(f) in (3, 4):
                if (count[key] % 2 == 0 and not single_body) or key in once:
                    continue
                once.add(key)
            elif count[key] % 2 == 0 and not single_body:
                continue                         # the wall between two blocks, any number of vertices
            elif single_body:
                if key in once:                  # the coincident back of a collapsed panel face
                    continue
                once.add(key)
            kept.append(f)
        tri = array("I")
        for f in kept:
            for t in triangulate_face(f, self.verts):
                tri.extend(t)
        lines = array("I")
        if edges:
            seen = set()
            for f in kept:
                m = len(f)
                for k in range(m):
                    a, b = f[k], f[(k + 1) % m]
                    e = (a, b) if a < b else (b, a)
                    if e not in seen:
                        seen.add(e)
                        lines.extend(e)
        pos = array("f", self.verts)
        return pos, tri, lines


def _b64(arr):
    return base64.b64encode(arr.tobytes()).decode("ascii")


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")


def _hex(c):
    """Colour as '#rrggbb' (QColor, (r, g, b) or a hex string); anything else is rejected, so a
    colour can never carry markup into the page."""
    if isinstance(c, str):
        h = c
    else:
        try:
            h = c.name()                         # QColor
        except AttributeError:
            r, g, b = c[:3]                      # (r, g, b) or (r, g, b, a)
            h = "#%02x%02x%02x" % (int(r), int(g), int(b))
    if not _HEX_RE.match(h):
        raise ValueError(f"Invalid colour: {h!r}")
    return h[:7].lower()                     # #rrggbb only: the page parses 6 digits (no alpha)


def build_scene(models, colors, boreholes=None, title="3D model", edges=True,
                progress=None, textures=None):
    """models: [(group name, [(faces, litho)])] with faces = rings of (x, y, z) map coordinates.
    colors: litho -> QColor / '#rrggbb'.  boreholes: core.Borehole objects (traces + labels).
    textures: {litho: (pattern code, PNG bytes of a square tile - black pattern on a transparent
    background, aspect = tile width / height)} for the lithology textures of the viewer.
    Returns a JSON-able dict."""
    xs0 = ys0 = math.inf
    xs1 = ys1 = -math.inf
    zabs = 0.0
    nfaces = 0
    for _g, solids in models:
        for faces, _lit in solids:
            for ring in faces:
                nfaces += 1
                for x, y, z in ring:
                    if abs(z) > zabs:
                        zabs = abs(z)
                    if x < xs0:
                        xs0 = x
                    if x > xs1:
                        xs1 = x
                    if y < ys0:
                        ys0 = y
                    if y > ys1:
                        ys1 = y
    bhs = list(boreholes or [])
    for b in bhs:
        for x, y, _z in (b.point_at(0.0), b.point_at(b.depth)):
            xs0, xs1 = min(xs0, x), max(xs1, x)
            ys0, ys1 = min(ys0, y), max(ys1, y)
    if not math.isfinite(xs0):
        raise ValueError("The model is empty.")
    ox, oy = round((xs0 + xs1) / 2.0), round((ys0 + ys1) / 2.0)
    want_edges = bool(edges) and nfaces <= MAX_EDGE_FACES   # edge buffers only when asked for (and not huge)
    # vertices are merged within a distance that is not finer than the float32 buffers of WebGL
    tol = merge_tolerance(max(xs1 - ox, ox - xs0, ys1 - oy, oy - ys0, zabs))

    meshes = []
    zmin = zmax = None
    total = sum(len(s) for _g, s in models) or 1
    done = 0
    for group, solids in models:
        by_lit = {}
        for faces, lit in solids:
            by_lit.setdefault(lit, []).append(faces)
        for lit, bodies in sorted(by_lit.items(), key=lambda kv: str(kv[0])):
            mesh = _Mesh(tol)
            for faces in bodies:
                mesh.body += 1
                for ring in faces:
                    mesh.add_face(ring, ox, oy)
                done += 1
                if progress and done % 500 == 0:
                    progress(done / total)
            pos, tri, lines = mesh.finish(want_edges)
            if not len(tri):
                continue
            zs = pos[2::3]
            lo, hi = min(zs), max(zs)
            zmin = lo if zmin is None else min(zmin, lo)
            zmax = hi if zmax is None else max(zmax, hi)
            meshes.append({"group": group, "litho": str(lit), "pos": _b64(pos),
                           "tri": _b64(tri), "edge": _b64(lines) if len(lines) else "",
                           "ntri": len(tri) // 3})
    if not meshes:
        raise ValueError("The model has no faces to show.")

    traces = []
    for b in bhs:
        n = 1 if b.dip <= 0.01 else 24
        pts = []
        for k in range(n + 1):
            x, y, z = b.point_at(b.depth * k / n)
            pts += [round(x - ox, 3), round(y - oy, 3), round(z, 3)]
        traces.append({"name": str(b.name), "pts": pts, "depth": round(b.depth, 2)})
        zmin = min(zmin, pts[-1])
        zmax = max(zmax, pts[2])
    tex, litho_tex = [], {}
    used = {m["litho"] for m in meshes}
    for lit, (code, png, asp) in sorted((textures or {}).items(), key=lambda kv: str(kv[0])):
        if str(lit) not in used or not png:
            continue
        if all(t["code"] != code for t in tex):
            tex.append({"code": code, "asp": round(float(asp), 4),
                        "png": "data:image/png;base64," + base64.b64encode(png).decode("ascii")})
        litho_tex[str(lit)] = code
    sc = {
        "title": title,
        "tex": tex, "litho_tex": litho_tex,
        "origin": [ox, oy],
        "groups": [g for g, s in models if s],
        "colors": {str(k): _hex(v) for k, v in colors.items()},
        "meshes": meshes,
        "boreholes": traces,
        "edges": bool(want_edges),
        "edges_on": bool(want_edges),
        "zmin": zmin, "zmax": zmax,
    }
    return sc


# ---------------------------------------------------------------- html output
def _json_for_html(obj):
    """JSON that can sit inside a <script>: no '<', '>', '&' or line separators survive."""
    return (json.dumps(obj, separators=(",", ":"), allow_nan=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
            # a name must not contain /* or */ (the template marks its placeholders with them)
            .replace("/*", "\\u002f*").replace("*/", "*\\u002f"))


def render_html(scene):
    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        html = fh.read()
    if PLACEHOLDER not in html:
        raise RuntimeError("viewer3d.html is damaged (scene placeholder missing).")
    # "<" only occurs inside JSON strings, where \u003c means the same: no "</script>", "<!--"
    # or "<script" sequence from the data can ever reach the HTML parser
    return html.replace(PLACEHOLDER, _json_for_html(scene), 1)


_PREFIX = "borehole_3d_"


def _same_dir(a, b):
    """True when two directories are the same one (real paths: /var -> /private/var on macOS)."""
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def is_viewer_file(path):
    """True for a viewer file made by write_html: an .html file whose name starts with the plugin
    prefix, lying directly in the viewer directory (compared by real paths), a regular file and
    not a link.  Anything else (another program, a URL, a link) is never opened or deleted."""
    try:
        return (isinstance(path, str)
                and os.path.basename(path).startswith(_PREFIX)
                and path.lower().endswith(".html")
                and _same_dir(os.path.dirname(os.path.abspath(path)), _viewer_dir())
                and stat.S_ISREG(os.lstat(path).st_mode))
    except OSError as e:                      # includes ViewerDirError
        _log.debug("%s is not a viewer file: %s", path, e)
        return False


def remove_file(path):
    """Delete a viewer file made by write_html (never any other file, see is_viewer_file)."""
    try:
        if is_viewer_file(path):
            os.remove(path)
    except OSError as e:
        _log.debug("viewer file %s was not removed: %s", path, e)


class ViewerDirError(OSError):
    """No private directory for the viewer files is available (the files hold geological data,
    so a shared directory is never used as a fallback)."""


def _viewer_dir():
    """Directory of the viewer files: the temp directory when it is private to this user (the
    per-user temp folder of macOS, Windows TEMP inside the profile), otherwise (the shared /tmp
    of Linux, whose listing shows the names and times of the files to every local user, or a
    Windows TEMP outside the profile) a folder .borehole_sections_tmp in the user's home
    directory (mode 0700), which only the user (and administrators) can read.
    When that private folder cannot be made or verified, ViewerDirError is raised: the shared
    temp directory is NOT used as a fallback."""
    d = tempfile.gettempdir()
    if _temp_dir_is_private():
        return d
    sub = os.path.join(os.path.expanduser("~"), ".borehole_sections_tmp")
    try:
        os.makedirs(sub, mode=0o700, exist_ok=True)
    except OSError as e:
        raise ViewerDirError(
            f"the private directory {sub} cannot be made ({e}); "
            "the viewer file is not written to a shared temporary directory") from e
    problem = _private_dir_problem(sub)
    if problem:
        raise ViewerDirError(f"the directory {sub} is not private ({problem}); "
                             "the viewer file is not written there")
    return sub


def _private_dir_problem(path):
    """Why `path` cannot hold private files (empty string when it can).  Checked on the
    directory itself (no link followed): a real directory; on POSIX owned by this user and not
    accessible to group or others (a looser mode of our own directory is tightened first).
    Windows ACLs are not inspected: the folder lies in the user profile, which is private."""
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return "not a regular directory"
        if hasattr(os, "getuid"):
            if st.st_uid != os.getuid():
                return "owned by another user"
            if st.st_mode & 0o077:
                os.chmod(path, 0o700)  # nosec B103 - owner-only directory permissions (rwx------)
                if os.lstat(path).st_mode & 0o077:
                    return "its permissions cannot be restricted"
    except OSError as e:
        return str(e)
    return ""


def _temp_dir_is_private():
    """Is the temp directory private to this user (nobody else can even list the names and
    times of the viewer files)?  POSIX: owned by this user and closed to group and others (the
    per-user macOS temp folder; the shared /tmp of Linux, mode 1777, is not).  Windows has no
    getuid(), so the temp directory must lie inside the user profile (a shared TEMP such as
    C:\\Temp is not private)."""
    if hasattr(os, "getuid"):
        try:
            st = os.stat(tempfile.gettempdir())
        except OSError:
            return False
        return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077
    profile = os.environ.get("USERPROFILE")
    if not profile:
        return False
    tmp, prof = (os.path.normcase(os.path.realpath(p)) for p in (tempfile.gettempdir(), profile))
    try:
        return os.path.commonpath([tmp, prof]) == prof
    except ValueError:                                # different drives
        return False


def remove_old_files(*a, **k):
    """Public name of the age cleanup (used when the plugin starts)."""
    _remove_old_files(*a, **k)


def _remove_old_files(max_age=2 * 3600):
    """Remove viewer files of this plugin older than two hours (the browser timer is one hour) from the temp directory (only
    regular files owned by the current user; links and foreign files are never touched)."""
    dirs = []
    try:
        dirs.append(_viewer_dir())
    except ViewerDirError as e:
        _log.debug("old viewer files are not removed from the private directory: %s", e)
    shared = tempfile.gettempdir()             # files of the versions that wrote there (own files only)
    if hasattr(os, "getuid") and not any(_same_dir(shared, x) for x in dirs):
        dirs.append(shared)                    # POSIX only: the owner of every file is checked
    for d in dirs:
        _remove_old_files_in(d, max_age)


def _remove_old_files_in(d, max_age):
    now = time.time()
    uid = os.getuid() if hasattr(os, "getuid") else None
    try:
        names = os.listdir(d)
    except OSError as e:
        _log.debug("temp directory cannot be listed: %s", e)
        return
    for f in names:
        if not (f.startswith(_PREFIX) and f.endswith(".html")):
            continue
        p = os.path.join(d, f)
        try:
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode) or (uid is not None and st.st_uid != uid):
                continue
            if now - st.st_mtime > max_age:
                os.remove(p)
        except OSError as e:
            _log.debug("old viewer file %s was not removed: %s", p, e)


def write_html(scene):
    """Write the viewer + scene to a NEW temporary file and return its path.  The file has a
    unique, unpredictable name and is readable only by the current user (mode 0600), so no
    other local user can pre-create it, replace it by a link or read the exported data."""
    return write_html_text(render_html(scene))


def write_html_text(html):
    """Write ready HTML text to a NEW private temporary file (see write_html); returns its path."""
    _remove_old_files()
    fh = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".html",
                                     prefix=_PREFIX, dir=_viewer_dir(), delete=False)
    name = fh.name
    try:
        with fh:
            fh.write(html)
    except BaseException:                             # e.g. disk full: do not leave a stub behind
        with contextlib.suppress(OSError):
            os.remove(name)
        raise
    return name
