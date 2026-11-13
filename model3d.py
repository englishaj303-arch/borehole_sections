"""3D geological models for the QGIS 3D map view.

Two models are offered:

* fence model  - 2D cross sections between neighbouring boreholes are turned into
                 closed 3D solids (PolygonZ faces, no extrusion): the outline of every
                 correlated layer is moved by half of the panel thickness to both sides,
                 so inclined boundaries, pinch-outs and the terrain of a DEM are smooth.
                 Optionally the space between the panels is filled: every triangle of
                 neighbouring boreholes is interpolated from its three sections,
* grid model   - square grid chosen from the boreholes; every grid cell gets a
                 column of blocks whose lithology is interpolated from the nearest
                 boreholes.  Blocks are closed 3D boxes as well, so no extrusion style
                 is needed.
"""
import math

from . import core

CRS = core.CRS_SJTSK


# ------------------------------------------------------------------ helpers
def box_solid(ring, zb, h):
    """Closed box over the footprint `ring` (counter-clockwise) from zb to zb + h."""
    bot = [(x, y, zb) for x, y in ring]
    top = [(x, y, zb + h) for x, y in ring]
    faces = [top, list(reversed(bot))]
    m = len(ring)
    for k in range(m):
        faces.append([bot[k], bot[(k + 1) % m], top[(k + 1) % m], top[k]])
    return faces


def prisms_to_solids(prisms):
    """[(ring, z_base, height, litho)] -> [(faces, litho)]"""
    return [(box_solid(ring, zb, h), lit) for ring, zb, h, lit in prisms]


def drop_coincident(bhs, tol=1.0):
    """Boreholes closer than `tol` m (collar position) to an earlier one are left out - a panel
    between them has no length (the one with more layers is kept)."""
    kept = []
    for b in sorted(bhs, key=lambda q: -len(q.layers)):
        if all(math.hypot(b.x - k.x, b.y - k.y) >= tol for k in kept):
            kept.append(b)
    keep_ids = {id(b) for b in kept}
    return [b for b in bhs if id(b) in keep_ids]


def unit_at(layers, depth):
    for L in layers:
        if L.top <= depth < L.bottom:
            return L
    return None


# --------------------------------------------------------------- grid model
def grid_model(boreholes, cell=0.0, dz=2.0, mode="elevation", k=4, maxdist=0.0,
               margin=0.1, max_prisms=80000):
    """Block model on a square grid.

    mode "elevation": layers are horizontal, "depth": layers follow the ground
    surface.  The Quaternary cover is always draped below the ground.
    Returns (prisms, info)."""
    bhs = [b for b in boreholes if b.layers]
    if len(bhs) < 2:
        raise ValueError("At least two boreholes with layers are needed.")
    lay = {id(b): core.clean_layers(b.layers) for b in bhs}
    cos_dip = {id(b): max(math.cos(math.radians(b.dip)), 1e-4) for b in bhs}
    qth = {}
    qvth = {}
    for b in bhs:
        n = core._quat_run(lay[id(b)])
        md_q = lay[id(b)][n - 1].bottom if n else 0.0
        qth[id(b)] = md_q
        qvth[id(b)] = md_q * cos_dip[id(b)]
    xs = [b.x for b in bhs] + [b.point_at(b.depth)[0] for b in bhs]
    ys = [b.y for b in bhs] + [b.point_at(b.depth)[1] for b in bhs]
    ext = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    if cell <= 0:
        cell = max(ext / 25.0, 1.0)
    pad = margin * ext
    x0, y0 = min(xs) - pad, min(ys) - pad
    nx = int(math.ceil((max(xs) + pad - x0) / cell))
    ny = int(math.ceil((max(ys) + pad - y0) / cell))
    if nx * ny > 40000:
        raise ValueError(f"Grid of {nx} x {ny} cells is too large - increase the cell size.")
    prisms = []
    eps = 1.0
    ncells = 0
    for ix in range(nx):
        for iy in range(ny):
            cx, cy = x0 + (ix + 0.5) * cell, y0 + (iy + 0.5) * cell
            dist = sorted(((math.hypot(b.x - cx, b.y - cy), b) for b in bhs),
                          key=lambda t: t[0])
            if maxdist > 0 and dist[0][0] > maxdist:
                continue
            near = [(d, b) for d, b in dist[:max(1, k)] if maxdist <= 0 or d <= maxdist]
            wts = [(1.0 / (d * d + eps), b) for d, b in near]
            wsum = sum(w for w, _ in wts)
            g = sum(w * b.z for w, b in wts) / wsum                 # ground elevation
            qt = sum(w * qvth[id(b)] for w, b in wts) / wsum       # vertical Quaternary thickness
            zbot = min(b.z_at(b.depth) for _, b in near)
            i_top = int(math.ceil(g / dz)) - 1          # the top block contains the ground surface
            i_min = int(math.floor(zbot / dz))
            seq = []
            for i in range(i_top, i_min - 1, -1):
                zc = (i + 0.5) * dz
                dg = max(g - zc, 0.0)
                # weight boreholes by horizontal distance to their trace at elevation zc
                z_dist = []
                for b in bhs:
                    cd = cos_dip[id(b)]
                    md_z = max(0.0, min(b.depth, (b.z - zc) / cd))
                    bx, by, _ = b.point_at(md_z)
                    d_z = math.hypot(bx - cx, by - cy)
                    if maxdist <= 0 or d_z <= maxdist:
                        z_dist.append((d_z, b))
                if not z_dist:
                    seq.append((i, None, False))
                    continue
                z_dist.sort(key=lambda t: t[0])
                z_wts = [(1.0 / (d * d + eps), b) for d, b in z_dist[:max(1, k)]]
                votes = {}
                if qt > 0 and dg <= qt:                            # Quaternary cover
                    for w, b in z_wts:
                        q_vb = qvth[id(b)]
                        if q_vb <= 0:
                            continue
                        cd = cos_dip[id(b)]
                        L = unit_at(lay[id(b)], (dg * q_vb / qt) / cd)
                        if L is not None and L.age == (0, 0):
                            votes[L.litho] = votes.get(L.litho, 0.0) + w
                else:
                    for w, b in z_wts:
                        cd = cos_dip[id(b)]
                        if mode == "depth":
                            dep = qth[id(b)] + (dg - qt) / cd
                        else:
                            dep = (b.z - zc) / cd
                        L = unit_at(lay[id(b)], dep)
                        if L is not None and L.age != (0, 0):
                            votes[L.litho] = votes.get(L.litho, 0.0) + w
                seq.append((i, max(votes, key=votes.get) if votes else None, qt > 0 and dg <= qt))
            ring = [(cx - cell / 2, cy - cell / 2), (cx + cell / 2, cy - cell / 2),
                    (cx + cell / 2, cy + cell / 2), (cx - cell / 2, cy + cell / 2)]
            # merge neighbouring voxels of the same lithology; the top of the column is the
            # ground surface g and the Quaternary base is exactly g - qt (not a multiple of dz)
            ztop = g
            j = 0
            while j < len(seq):
                lit = seq[j][1]
                m = j
                while m + 1 < len(seq) and seq[m + 1][1] == lit:
                    m += 1
                zbot_run = seq[m][0] * dz
                if m + 1 < len(seq) and seq[m + 1][2] != seq[m][2] and qt > 0:
                    zbot_run = g - qt                 # boundary cover / bedrock
                if m + 1 == len(seq):
                    zbot_run = seq[m][0] * dz
                if lit is not None and ztop - zbot_run > 1e-6:
                    prisms.append((ring, zbot_run, ztop - zbot_run, lit))
                ztop = zbot_run
                j = m + 1
            ncells += 1
            if len(prisms) > max_prisms:
                raise ValueError("The model would contain too many blocks - increase the "
                                 "cell size or the vertical step.")
    return prisms, {"cell": cell, "cells": ncells, "nx": nx, "ny": ny}


# -------------------------------------------------------------- fence model
def _dedupe(P, tol=1e-6):
    out = []
    for p in P:
        if not out or abs(p[0] - out[-1][0]) > tol or abs(p[1] - out[-1][1]) > tol:
            out.append(p)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) <= tol and abs(out[0][1] - out[-1][1]) <= tol:
        out.pop()
    return out


def _densify(P, step):
    """Add vertices so that no edge is longer than `step` in the distance direction."""
    out = []
    m = len(P)
    for q in range(m):
        (d1, z1), (d2, z2) = P[q], P[(q + 1) % m]
        n = max(1, int(math.ceil(abs(d2 - d1) / step)))
        for k in range(n):
            t = k / n
            out.append((d1 + (d2 - d1) * t, z1 + (z2 - z1) * t))
    return out


def solid_from_outline(front, back):
    """Closed 3D body (list of faces, each a ring of (x, y, z)) between two congruent
    outlines: the front and back face and one quadrilateral per outline edge."""
    m = len(front)
    faces = [list(front), list(reversed(back))]
    for k in range(m):
        a, b = front[k], front[(k + 1) % m]
        if math.dist(a, b) < 1e-6:
            continue
        faces.append([a, b, back[(k + 1) % m], back[k]])
    return faces


def fence_solids(geo, locate, width=4.0, step=5.0, shift=None):
    """Turn one 2D section (core.build_section_scene(...).geo) into closed 3D solids.
    locate(d) -> (x, y) point on the (straight) section line at distance d.
    shift(d) -> vertical offset (terrain following, e.g. from a DEM) or None; with it the
    outlines get vertices every `step` m so that the layers follow the terrain.
    Returns [(faces, lithology)]."""
    d0, kx, ky, zmax = geo["d0"], geo["kx"], geo["ky"], geo["zmax"]
    p0, p1 = locate(0.0), locate(1.0)
    ln = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    if ln < 1e-12:
        return []
    hx, hy = -(p1[1] - p0[1]) / ln * width / 2.0, (p1[0] - p0[0]) / ln * width / 2.0
    out = []
    for pts, lit, _age in list(geo["polys"]) + list(geo["cols"]):
        P = _dedupe([(d0 + px / kx, zmax - py / ky) for px, py in pts])
        if len(P) < 3:
            continue
        if shift:
            P = _dedupe(_densify(P, max(step, 0.1)))
            P = [(d, z + shift(d)) for d, z in P]
        front, back = [], []
        for d, z in P:
            x, y = locate(d)
            front.append((x + hx, y + hy, z))
            back.append((x - hx, y - hy, z))
        out.append((solid_from_outline(front, back), lit))
    return out


# ----------------------------------------------------------- QGIS 3D layers
def _style_layer(layer, color, edges=False):
    """Single-colour 3D style for PolygonZ faces (no extrusion); returns None or an error text."""
    try:
        from qgis._3d import QgsPolygon3DSymbol, QgsVectorLayer3DRenderer
        from qgis.core import Qgis
        try:
            from qgis._3d import QgsPhongMaterialSettings
        except ImportError:         # older / alternative bindings
            from qgis.core import QgsPhongMaterialSettings
        sym = QgsPolygon3DSymbol()
        sym.setAltitudeClamping(Qgis.AltitudeClamping.Absolute)
        sym.setExtrusionHeight(0.0)
        mat = QgsPhongMaterialSettings()
        mat.setDiffuse(color)
        mat.setAmbient(color.darker(160))
        sym.setMaterialSettings(mat)
        try:
            sym.setAddBackFaces(True)       # the faces are seen from both sides
        except (AttributeError, TypeError) as e:
            core.log_warning(f"3D back faces not available: {type(e).__name__}: {e}")
        if edges:
            try:
                sym.setEdgesEnabled(True)
                sym.setEdgeWidth(0.2)
                sym.setEdgeColor(color.darker(130))
            except (AttributeError, TypeError) as e:
                # edge rendering is optional and missing in some QGIS versions
                core.log_warning(f"3D edge styling not available: {type(e).__name__}: {e}")
        layer.setRenderer3D(QgsVectorLayer3DRenderer(sym))
        return None
    except Exception as e:      # report to the user instead of failing silently
        return f"{type(e).__name__}: {e}"


def build_layers(solids, colors, group_name, edges=False, registry=None, volume=None):
    """Create the 3D layers (one per lithology) in a layer group.
    solids: [(faces, litho)] closed bodies (PolygonZ faces); volume: the same for the filled
    space between panels (layers "<litho> - volume").
    registry: optional dict litho -> [layer ids] that receives the new layers.
    Returns (number_of_layers, number_of_objects, error_text_or_None)."""
    from qgis.core import (QgsFeature, QgsGeometry, QgsLineString, QgsMultiPolygon, QgsPoint,
                           QgsPolygon, QgsProject, QgsVectorLayer)
    root = QgsProject.instance().layerTreeRoot()
    group = root.insertGroup(0, group_name)
    state = {"err": None, "count": 0}

    def polygon(ring3d):
        pts = [QgsPoint(x, y, z) for x, y, z in ring3d] + [QgsPoint(*ring3d[0])]
        poly = QgsPolygon()
        poly.setExteriorRing(QgsLineString(pts))
        return poly

    def add(objs, suffix):
        by_lit = {}
        for faces, lit in objs:
            by_lit.setdefault(lit, []).append(faces)
        for lit, bodies in sorted(by_lit.items(), key=lambda kv: str(kv[0])):
            lyr = QgsVectorLayer(
                f"MultiPolygonZ?crs={CRS}&field=litho:string&field=zbot:double&field=ztop:double",
                f"{lit}{suffix}", "memory")
            feats = []
            for faces in bodies:
                mp = QgsMultiPolygon()
                zs = []
                for ring in faces:
                    mp.addGeometry(polygon(ring))
                    zs += [p[2] for p in ring]
                f = QgsFeature(lyr.fields())
                f.setGeometry(QgsGeometry(mp))
                f.setAttributes([lit, min(zs), max(zs)])
                feats.append(f)
            lyr.dataProvider().addFeatures(feats)
            lyr.updateExtents()
            e = _style_layer(lyr, colors.get(lit, core.QColor(128, 128, 128)), edges)
            state["err"] = state["err"] or e
            QgsProject.instance().addMapLayer(lyr, False)
            group.addLayer(lyr)
            state["count"] += 1
            if registry is not None:
                registry.setdefault(str(lit), []).append(lyr.id())

    add(solids or [], " - panels" if volume else "")
    add(volume or [], " - volume")
    return state["count"], len(solids or []) + len(volume or []), state["err"]


# ---------------------------------------------- network of panels (fence diagram)
def _median(v):
    v = sorted(v)
    return v[len(v) // 2] if v else 0.0


def delaunay_edges(pts):
    """Edges (i, j) of the Delaunay triangulation (Bowyer-Watson, small point sets)."""
    out = set()
    for t in delaunay_triangles(pts):
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            out.add((min(a, b), max(a, b)))
    if len(pts) == 2:
        out.add((0, 1))
    return out


def delaunay_triangles(pts):
    """Triangles (i, j, k) of the Delaunay triangulation (Bowyer-Watson, small point sets)."""
    n = len(pts)
    if n < 3:
        return []
    minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
    miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
    dm = max(maxx - minx, maxy - miny, 1.0) * 30.0
    mx, my = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    P = list(pts) + [(mx - dm, my - dm), (mx, my + dm), (mx + dm, my - dm)]

    def in_circle(p, t):
        (ax, ay), (bx, by), (cx, cy) = P[t[0]], P[t[1]], P[t[2]]
        d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(d) < 1e-12:
            return False
        ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
              + (cx * cx + cy * cy) * (ay - by)) / d
        uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
              + (cx * cx + cy * cy) * (bx - ax)) / d
        return (p[0] - ux) ** 2 + (p[1] - uy) ** 2 < (ax - ux) ** 2 + (ay - uy) ** 2 - 1e-9

    tris = [(n, n + 1, n + 2)]
    for i in range(n):
        bad = [t for t in tris if in_circle(P[i], t)]
        cnt = {}
        for t in bad:
            for e in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                k = tuple(sorted(e))
                cnt[k] = cnt.get(k, 0) + 1
        tris = [t for t in tris if t not in bad]
        tris += [(e[0], e[1], i) for e, c in cnt.items() if c == 1]
    return [t for t in tris if max(t) < n]


def _default_maxlen(pts):
    nn = []
    for i, p in enumerate(pts):
        nn.append(min(math.hypot(q[0] - p[0], q[1] - p[1]) for j, q in enumerate(pts) if j != i))
    return 2.5 * _median(nn)


def neighbour_edges(bhs, mode="grid", maxlen=0.0):
    """Pairs of neighbouring boreholes (indices) that get a panel.
    mode "grid": the nearest borehole in each of four directions (the grid is
    rotated to the dominant direction of the boreholes), "delaunay": triangulation.
    maxlen = longest allowed panel, 0 = 2.5 x the median nearest-neighbour distance."""
    n = len(bhs)
    pts = [(b.x, b.y) for b in bhs]
    if n < 2:
        return []
    nn, vec = [], []
    for i in range(n):
        best = None
        for j in range(n):
            if i != j:
                d = math.hypot(pts[j][0] - pts[i][0], pts[j][1] - pts[i][1])
                if best is None or d < best[0]:
                    best = (d, j)
        nn.append(best[0])
        vec.append(math.atan2(pts[best[1]][1] - pts[i][1], pts[best[1]][0] - pts[i][0]))
    if maxlen <= 0:
        maxlen = 2.5 * _median(nn)
    if mode == "delaunay":
        return sorted(e for e in delaunay_edges(pts)
                      if math.hypot(pts[e[0]][0] - pts[e[1]][0], pts[e[0]][1] - pts[e[1]][1]) <= maxlen)
    theta = 0.25 * math.atan2(sum(math.sin(4 * v) for v in vec), sum(math.cos(4 * v) for v in vec))
    edges = set()
    for i in range(n):
        best = {}
        for j in range(n):
            if i == j:
                continue
            dx, dy = pts[j][0] - pts[i][0], pts[j][1] - pts[i][1]
            d = math.hypot(dx, dy)
            if d > maxlen:
                continue
            sector = int(((math.atan2(dy, dx) - theta + math.pi / 4) % (2 * math.pi)) // (math.pi / 2))
            if sector not in best or d < best[sector][0]:
                best[sector] = (d, j)
        for d, j in best.values():
            edges.add((min(i, j), max(i, j)))
    return sorted(edges)


def _dem_shift(A, B, L, dem, n=40):
    """Terrain-following offset along the panel A-B: the DEM profile minus the straight
    line between the collars, with zero offset at the two boreholes."""
    ds = [L * k / n for k in range(n + 1)]
    hs = []
    for d in ds:
        h = dem(A.x + (B.x - A.x) * d / L, A.y + (B.y - A.y) * d / L)
        if h is None:
            return None
        hs.append(h)
    e0, e1 = hs[0] - A.z, hs[-1] - B.z
    offs = [hs[k] - (A.z + (B.z - A.z) * ds[k] / L) - (e0 * (1 - ds[k] / L) + e1 * ds[k] / L)
            for k in range(n + 1)]

    def f(d):
        t = min(max(d / L, 0.0), 1.0) * n
        i = min(int(t), n - 1)
        return offs[i] + (offs[i + 1] - offs[i]) * (t - i)
    return f


def column_solids(bhs, width, dz):
    """Borehole columns (the black pillars of a fence diagram) as closed bodies that follow
    the real trajectory, also of inclined boreholes (no rounding to dz)."""
    out = []
    h = width / 2.0

    def square(b, md):
        x, y, _ = b.point_at(md)
        z = b.z_at(md)
        return [(x - h, y - h, z), (x + h, y - h, z), (x + h, y + h, z), (x - h, y + h, z)]

    for b in bhs:
        n = 1 if b.dip <= 0.01 else max(2, int(math.ceil(b.depth / max(dz, 0.5))))
        rings = [square(b, b.depth * k / n) for k in range(n + 1)]
        faces = [list(rings[0]), list(reversed(rings[-1]))]
        for r1, r2 in zip(rings, rings[1:]):
            for k in range(4):
                faces.append([r1[k], r1[(k + 1) % 4], r2[(k + 1) % 4], r2[k]])
        out.append((faces, "Borehole"))
    return out


class _Sampler:
    """Lithology at (distance along a panel, elevation) from a 2D section geometry."""

    def __init__(self, geo, shift=None):
        d0, kx, ky, zmax = geo["d0"], geo["kx"], geo["ky"], geo["zmax"]
        self.shift = shift
        self.polys = []
        for pts, lit, _age in list(geo["polys"]) + list(geo["cols"]):
            P = [(d0 + px / kx, zmax - py / ky) for px, py in pts]
            if len(P) >= 3:
                ds, zs = [p[0] for p in P], [p[1] for p in P]
                self.polys.append((min(ds), max(ds), min(zs), max(zs), P, lit))

    def lit_at(self, d, z):
        if self.shift:
            z -= self.shift(d)
        for d1, d2, z1, z2, P, lit in self.polys:
            if d1 <= d <= d2 and z1 <= z <= z2 and _inside(P, d, z):
                return lit
        return None


def _inside(P, x, y):
    inside = False
    m = len(P)
    for k in range(m):
        (x1, y1), (x2, y2) = P[k], P[(k + 1) % m]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _clip_convex(poly, tri):
    """Sutherland-Hodgman: polygon `poly` clipped by the counter-clockwise triangle `tri`."""
    out = poly
    for k in range(3):
        (ax, ay), (bx, by) = tri[k], tri[(k + 1) % 3]
        inp, out = out, []
        if not inp:
            break

        def side(p):
            return (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)
        for q in range(len(inp)):
            p1, p2 = inp[q], inp[(q + 1) % len(inp)]
            s1, s2 = side(p1), side(p2)
            if s1 >= 0:
                out.append(p1)
            if (s1 >= 0) != (s2 >= 0):
                t = s1 / (s1 - s2)
                out.append((p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t))
    return out


def fill_between_sections(bhs, edge_of, dz=2.0, cell=0.0, maxlen=0.0, dem=None, max_columns=30000):
    """Volume between the panels: every Delaunay triangle of neighbouring boreholes is cut
    into small columns; the lithology of a column at depth z is interpolated from the three
    sections around the triangle (the line from each corner through the column hits the
    opposite section, and the sections vote with a weight growing towards them).
    edge_of(i, j) (i < j) -> (_Sampler, length).  Returns [(faces, litho)]."""
    pts = [(b.x, b.y) for b in bhs]
    if len(bhs) < 3:
        return []
    if maxlen <= 0:
        maxlen = _default_maxlen(pts)
    tris = []
    for t in delaunay_triangles(pts):
        if all(math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1]) <= maxlen
               for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0]))):
            tris.append(t)
    if not tris:
        return []
    if cell <= 0:
        lens = [math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1])
                for t in tris for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0]))]
        cell = max(_median(lens) / 8.0, 0.5)
    out = []
    ncol = 0
    for t in tris:
        idx = list(t)
        A, B, C = (pts[k] for k in idx)
        det = (B[0] - A[0]) * (C[1] - A[1]) - (B[1] - A[1]) * (C[0] - A[0])
        if abs(det) < 1e-9:
            continue
        if det < 0:
            idx[1], idx[2] = idx[2], idx[1]
            A, B, C = (pts[k] for k in idx)
            det = -det
        tri = [A, B, C]
        bh3 = [bhs[k] for k in idx]
        x0 = math.floor(min(p[0] for p in tri) / cell) * cell
        y0 = math.floor(min(p[1] for p in tri) / cell) * cell
        nx = int(math.ceil((max(p[0] for p in tri) - x0) / cell))
        ny = int(math.ceil((max(p[1] for p in tri) - y0) / cell))
        for ix in range(nx):
            for iy in range(ny):
                sq = [(x0 + ix * cell, y0 + iy * cell), (x0 + (ix + 1) * cell, y0 + iy * cell),
                      (x0 + (ix + 1) * cell, y0 + (iy + 1) * cell), (x0 + ix * cell, y0 + (iy + 1) * cell)]
                ring = _clip_convex(sq, tri)
                if len(ring) < 3:
                    continue
                area = 0.5 * abs(sum(ring[q][0] * ring[(q + 1) % len(ring)][1]
                                     - ring[(q + 1) % len(ring)][0] * ring[q][1]
                                     for q in range(len(ring))))
                if area < 0.01 * cell * cell:
                    continue
                ncol += 1
                if ncol > max_columns:
                    raise ValueError("The filled model would contain too many columns - "
                                     "increase the fill cell size.")
                cx = sum(p[0] for p in ring) / len(ring)
                cy = sum(p[1] for p in ring) / len(ring)
                wa = ((B[0] - cx) * (C[1] - cy) - (B[1] - cy) * (C[0] - cx)) / det
                wb = ((C[0] - cx) * (A[1] - cy) - (C[1] - cy) * (A[0] - cx)) / det
                w3 = [max(wa, 0.0), max(wb, 0.0), max(1.0 - wa - wb, 0.0)]
                g = None
                if dem is not None:
                    g = dem(cx, cy)
                if g is None:
                    g = sum(w * b.z for w, b in zip(w3, bh3))
                zbot = sum(w * b.z_at(b.depth) for w, b in zip(w3, bh3))
                refs = []                       # (sampler, distance d, weight) for the 3 sections
                for v in range(3):
                    u, w = (v + 1) % 3, (v + 2) % 3
                    den = w3[u] + w3[w]
                    tt = w3[w] / den if den > 1e-9 else 0.5
                    i1, i2 = idx[u], idx[w]
                    sampler, length = edge_of(min(i1, i2), max(i1, i2))
                    if i1 > i2:
                        tt = 1.0 - tt
                    if sampler is None:
                        continue
                    refs.append((sampler, tt * length, 1.0 / (w3[v] ** 2 + 0.02)))

                if not refs:
                    continue

                def vote(z, refs=refs):
                    votes = {}
                    for sampler, d, wt in refs:
                        lit = sampler.lit_at(d, z)
                        if lit is not None:
                            votes[lit] = votes.get(lit, 0.0) + wt
                    return max(votes, key=votes.get) if votes else None

                for top, bot, lit in _column_runs(vote, g, zbot, dz):
                    out.append((box_solid(ring, bot, top - bot), lit))
    return out


def _column_runs(vote, ztop, zbot, dz):
    """Runs (top, bottom, lithology) of one column; boundaries are refined by bisection."""
    if ztop - zbot < 1e-6:
        return []
    n = max(1, int(math.ceil((ztop - zbot) / dz)))
    step = (ztop - zbot) / n
    zc = [ztop - (k + 0.5) * step for k in range(n)]
    lits = [vote(z) for z in zc]
    runs = []
    top = ztop
    for k in range(n):
        if k + 1 < n and lits[k + 1] == lits[k]:
            continue
        if k + 1 < n:
            hi, lo = zc[k], zc[k + 1]
            for _ in range(7):
                mid = 0.5 * (hi + lo)
                if vote(mid) == lits[k]:
                    hi = mid
                else:
                    lo = mid
            bottom = 0.5 * (hi + lo)
        else:
            bottom = zbot
        if lits[k] is not None and top - bottom > 1e-6:
            runs.append((top, bottom, lits[k]))
        top = bottom
    return runs


def network_solids(bhs, edges, make_geo, width=1.0, step=5.0, dz=2.0, dem=None,
                   colw=3.0, fill=False, cell=0.0, maxlen=0.0, max_solids=60000):
    """Thin vertical panels between neighbouring boreholes as 3D solids [(faces, litho)] and,
    with fill=True, the volume between the panels.
    make_geo(A, B, L) -> 2D section geometry (core.section_geo) of the pair;
    dem(x, y) -> ground elevation or None (optional terrain following, sampled every `step` m).
    Returns (panel_and_column_solids, volume_solids)."""
    solids = []
    cache = {}

    def edge_of(i, j):
        if (i, j) not in cache:
            A, B = bhs[i], bhs[j]
            L = math.hypot(B.x - A.x, B.y - A.y)
            if L < 1e-3:                       # coincident boreholes: no section between them
                cache[(i, j)] = (None, None, L)
                return cache[(i, j)]
            geo = make_geo(A, B, L)
            shift = _dem_shift(A, B, L, dem) if dem is not None else None
            cache[(i, j)] = (geo, shift, L)
        return cache[(i, j)]

    for i, j in edges:
        A, B = bhs[i], bhs[j]
        geo, shift, L = edge_of(i, j)
        if geo is None:
            continue

        def locate(d, A=A, B=B, L=L):
            return (A.x + (B.x - A.x) * d / L, A.y + (B.y - A.y) * d / L)

        solids += fence_solids(geo, locate, width, step, shift)
        if len(solids) > max_solids:
            raise ValueError("The model would contain too many objects - shorten the maximum "
                             "panel length or select fewer boreholes.")
    volume = []
    if fill:
        def sampler_of(i, j):
            geo, shift, L = edge_of(i, j)
            return (_Sampler(geo, shift) if geo is not None else None), L
        samplers = {}

        def cached(i, j):
            if (i, j) not in samplers:
                samplers[(i, j)] = sampler_of(i, j)
            return samplers[(i, j)]
        volume = fill_between_sections(bhs, cached, dz, cell, maxlen, dem)
    if colw > 0:
        used = {k for e in edges for k in e}
        solids += column_solids([bhs[k] for k in sorted(used)], colw, dz)
    return solids, volume
