"""Own 3D viewer of the borehole models (WebGL2, no external libraries).

This module is pure Python (no QGIS / Qt imports): it converts the closed solids made by
`model3d` into compact triangle meshes and writes ONE self-contained HTML file (the viewer
`viewer3d.html` with the scene embedded).  The file is shown in an embedded QtWebEngine window
(see `viewer3d_window.py`) or opened in any modern web browser; nothing is downloaded and no
server is needed.
"""
import base64
import json
import math
import os
import stat
import tempfile
import time
from array import array
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "viewer3d.html")
PLACEHOLDER = "/*SCENE_JSON*/null"
MAX_EDGE_FACES = 400000          # above this number of faces the edge lines are not exported


# ------------------------------------------------------------ triangulation
def _ear_clip(P, eps=1e-9):
    """Ear clipping of a simple polygon given as a counter-clockwise list of (u, v).
    Returns triangles as index triples into P.  Collinear vertices are dropped, degenerate
    input never loops forever."""
    n = len(P)
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
        for k in (a, c):
            if k in reflex and cross(k) > eps:
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
            lox, hix = min(A[0], B[0], C[0]) - eps, max(A[0], B[0], C[0]) + eps
            loy, hiy = min(A[1], B[1], C[1]) - eps, max(A[1], B[1], C[1]) + eps
            for j in reflex:
                if j == a or j == c or j == i:
                    continue
                p = P[j]
                if p[0] < lox or p[0] > hix or p[1] < loy or p[1] > hiy:
                    continue
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
        tris.append((prev[a], a, nxt[a]))
    return tris


def triangulate_face(ring, verts):
    """Triangles (index triples into `verts`, a flat [x, y, z, ...] list) of one planar face
    given as a ring of vertex indices."""
    n = len(ring)
    if n < 3:
        return []
    if n == 3:
        return [tuple(ring)]

    def xyz(k):
        j = 3 * ring[k]
        return verts[j], verts[j + 1], verts[j + 2]

    if n == 4:                                  # quad: split along the shorter diagonal
        p = [xyz(k) for k in range(4)]
        d02 = sum((p[0][t] - p[2][t]) ** 2 for t in range(3))
        d13 = sum((p[1][t] - p[3][t]) ** 2 for t in range(3))
        r = ring
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
    return [(ring[order[a]], ring[order[b]], ring[order[c]]) for a, b, c in tris]


# ---------------------------------------------------------------- meshes
class _Mesh:
    def __init__(self):
        self.vmap = {}
        self.verts = []                          # flat x, y, z (float)
        self.faces = []                          # rings of vertex indices

    def vertex(self, x, y, z):
        key = (round(x, 3), round(y, 3), round(z, 3))
        i = self.vmap.get(key)
        if i is None:
            i = len(self.verts) // 3
            self.vmap[key] = i
            self.verts.extend(key)
        return i

    def add_face(self, ring3d, ox, oy):
        ring = []
        for x, y, z in ring3d:
            i = self.vertex(x - ox, y - oy, z)
            if not ring or ring[-1] != i:
                ring.append(i)
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring.pop()
        if len(ring) >= 3:
            self.faces.append(tuple(ring))

    def finish(self, edges):
        """Drop faces that appear twice (the wall between two blocks of the same lithology),
        triangulate, return (positions, triangles, edges) as arrays."""
        count = Counter(tuple(sorted(f)) for f in self.faces if len(f) in (3, 4))
        kept = [f for f in self.faces if len(f) not in (3, 4) or count[tuple(sorted(f))] == 1]
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


def _hex(c):
    if isinstance(c, str):
        return c
    try:
        return c.name()                          # QColor
    except AttributeError:
        r, g, b = c
        return "#%02x%02x%02x" % (r, g, b)


def build_scene(models, colors, boreholes=None, title="3D model", edges=True,
                progress=None, textures=None):
    """models: [(group name, [(faces, litho)])] with faces = rings of (x, y, z) map coordinates.
    colors: litho -> QColor / '#rrggbb'.  boreholes: core.Borehole objects (traces + labels).
    textures: {litho: (pattern code, PNG bytes of a square tile - black pattern on a transparent
    background, aspect = tile width / height)} for the lithology textures of the viewer.
    Returns a JSON-able dict."""
    xs0 = ys0 = math.inf
    xs1 = ys1 = -math.inf
    nfaces = 0
    for _g, solids in models:
        for faces, _lit in solids:
            for ring in faces:
                nfaces += 1
                for x, y, _z in ring:
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
    want_edges = nfaces <= MAX_EDGE_FACES          # edge lines are exported unless the model is huge

    meshes = []
    zmin = zmax = None
    total = sum(len(s) for _g, s in models) or 1
    done = 0
    for group, solids in models:
        by_lit = {}
        for faces, lit in solids:
            by_lit.setdefault(lit, []).append(faces)
        for lit, bodies in sorted(by_lit.items(), key=lambda kv: str(kv[0])):
            mesh = _Mesh()
            for faces in bodies:
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
        "edges_on": bool(edges and want_edges),
        "zmin": zmin, "zmax": zmax,
    }
    return sc


# ---------------------------------------------------------------- html output
def render_html(scene):
    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        html = fh.read()
    if PLACEHOLDER not in html:
        raise RuntimeError("viewer3d.html is damaged (scene placeholder missing).")
    data = json.dumps(scene, separators=(",", ":")).replace("</", "<\\/")
    return html.replace(PLACEHOLDER, data, 1)


_PREFIX = "borehole_3d_"


def _remove_old_files(max_age=2 * 86400):
    """Remove viewer files of this plugin older than two days from the temp directory (only
    regular files owned by the current user; links and foreign files are never touched)."""
    d = tempfile.gettempdir()
    now = time.time()
    uid = os.getuid() if hasattr(os, "getuid") else None
    try:
        names = os.listdir(d)
    except OSError:
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
        except OSError:
            pass


def write_html(scene):
    """Write the viewer + scene to a NEW temporary file and return its path.  The file has a
    unique, unpredictable name and is readable only by the current user (mode 0600), so no
    other local user can pre-create it, replace it by a link or read the exported data."""
    _remove_old_files()
    html = render_html(scene)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".html",
                                     prefix=_PREFIX, delete=False) as fh:
        fh.write(html)
        return fh.name
