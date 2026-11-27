"""3D geological models for the QGIS 3D map view.

Two models are offered:

* fence model  - 2D cross sections between neighbouring boreholes are turned into
                 closed 3D solids (PolygonZ faces, no extrusion): the outline of every
                 correlated layer is moved by half of the panel thickness to both sides,
                 so inclined boundaries, pinch-outs and the terrain of a DEM are smooth.
                 Optionally the layers are interpolated between the panels: every triangle of
                 neighbouring boreholes is interpolated from its three sections into smooth
                 solids with sloped tops and bottoms (the older block method is still offered),
* grid model   - square grid chosen from the boreholes; every grid cell gets a
                 column of blocks whose lithology is interpolated from the nearest
                 boreholes.  Blocks are closed 3D boxes as well, so no extrusion style
                 is needed.
"""
import math

from qgis.PyQt.QtGui import QColor

from . import core


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


def _unique_indices(bhs, tol=1.0):
    """Indices (ascending) of the boreholes left after dropping those closer than `tol` m
    (collar position) to another one with more layers."""
    kept = []
    for i in sorted(range(len(bhs)), key=lambda q: -len(bhs[q].layers)):
        b = bhs[i]
        if all(math.hypot(b.x - bhs[k].x, b.y - bhs[k].y) >= tol for k in kept):
            kept.append(i)
    return sorted(kept)


def drop_coincident(bhs, tol=1.0):
    """Boreholes closer than `tol` m (collar position) to an earlier one are left out - a panel
    between them has no length (the one with more layers is kept)."""
    return [bhs[i] for i in _unique_indices(bhs, tol)]


def unit_at(layers, depth):
    for L in layers:
        if L.top <= depth < L.bottom:
            return L
    return None


# --------------------------------------------------------------- grid model
def _plan_dist(b, x, y):
    """Horizontal distance of the point (x, y) from the trace of the borehole (collar for a
    vertical hole, the segment collar - bottom for an inclined one)."""
    if b.dip <= 0.0 or b.depth <= 0.0:
        return math.hypot(b.x - x, b.y - y)
    ex, ey, _ = b.point_at(b.depth)
    dx, dy = ex - b.x, ey - b.y
    ll = dx * dx + dy * dy
    t = 0.0 if ll <= 0.0 else max(0.0, min(1.0, ((x - b.x) * dx + (y - b.y) * dy) / ll))
    return math.hypot(b.x + t * dx - x, b.y + t * dy - y)


def grid_model(boreholes, cell=0.0, dz=2.0, mode="elevation", k=4, maxdist=0.0,
               margin=0.1, max_prisms=80000):
    """Block model on a square grid.

    mode "elevation": layers are horizontal, "depth": layers follow the ground
    surface.  The Quaternary cover is always draped below the ground.
    Returns (prisms, info)."""
    bhs = drop_coincident([b for b in boreholes if b.layers])     # two holes at one spot would count twice
    if len(bhs) < 2:
        raise ValueError("At least two boreholes with layers are needed.")
    lay = {id(b): core.clean_layers(b.layers) for b in bhs}
    cos_dip = {id(b): max(math.cos(math.radians(b.dip)), 1e-4) for b in bhs}
    qth = {}
    qvth = {}
    # boreholes without any stratigraphy do not say how thick the cover is
    known = {id(b): any(L.age is not None for L in lay[id(b)]) for b in bhs}
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
            if maxdist > 0 and min(_plan_dist(b, cx, cy) for b in bhs) > maxdist:
                continue                    # (an inclined hole counts with its whole trace, not the collar)
            near = [(d, b) for d, b in dist[:max(1, k)] if maxdist <= 0 or d <= maxdist]
            if not near:                    # only the deeper part of an inclined hole is near
                near = [dist[0]]
            wts = [(1.0 / (d * d + eps), b) for d, b in near]
            wsum = sum(w for w, _ in wts)
            g = sum(w * b.z for w, b in wts) / wsum                 # ground elevation
            wk = [(w, b) for w, b in wts if known[id(b)]]
            qt = (sum(w * qvth[id(b)] for w, b in wk) / sum(w for w, _ in wk)
                  if wk else 0.0)                                   # vertical Quaternary thickness
            # the same weighted base as smooth_model: with the minimum, a change of the set of the k
            # nearest boreholes made the bottom jump from column to column (stepped overhangs)
            zbot = sum(w * b.z_at(b.depth) for w, b in wts) / wsum
            i_top = int(math.ceil(g / dz)) - 1          # the top block contains the ground surface
            i_min = int(math.floor(zbot / dz))
            seq = []
            for i in range(i_top, i_min - 1, -1):
                zc = (i + 0.5) * dz
                dg = max(g - zc, 0.0)
                if i == i_top and qt > 0:
                    dg = 0.0        # the top block holds the ground surface: a Quaternary cover
                                    # thinner than dz / 2 must not vanish
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
                        cd = cos_dip[id(b)]
                        if not known[id(b)]:        # no stratigraphy: the cover is not marked, so
                            L = unit_at(lay[id(b)], dg / cd)   # the layers count by depth (as below)
                            ok = L is not None
                        else:
                            q_vb = qvth[id(b)]
                            if q_vb <= 0:
                                continue
                            L = unit_at(lay[id(b)], (dg * q_vb / qt) / cd)
                            ok = L is not None and L.age == (0, 0)
                        if ok:
                            votes[L.litho] = votes.get(L.litho, 0.0) + w
                else:
                    for w, b in z_wts:
                        cd = cos_dip[id(b)]
                        if mode == "depth":
                            qb = qth[id(b)] if known[id(b)] else qt / cd
                            dep = qb + (dg - qt) / cd
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
                # voxels of one lithology are merged only inside the cover or only below it,
                # so the Quaternary base g - qt is kept even when both sides are the same rock
                while m + 1 < len(seq) and seq[m + 1][1] == lit and seq[m + 1][2] == seq[j][2]:
                    m += 1
                zbot_run = seq[m][0] * dz
                if m + 1 < len(seq):
                    if seq[m + 1][2] != seq[m][2] and qt > 0:
                        zbot_run = g - qt             # boundary cover / bedrock
                elif seq[m][2] and qt > 0:
                    zbot_run = max(zbot_run, g - qt)  # the column ends in the cover: its true base
                zbot_run = min(zbot_run, ztop)        # never above the previous block's bottom
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
        out.append((d1, z1))
        # the inner points are computed from the lexicographically smaller end, so an edge and
        # its reverse (the two sides of a bridge to a hole) give bitwise identical points
        forward = (d1, z1) <= (d2, z2)
        (da, za), (db, zb) = ((d1, z1), (d2, z2)) if forward else ((d2, z2), (d1, z1))
        for k in range(1, n):
            j = k if forward else n - k
            out.append((da + (db - da) * j / n, za + (zb - za) * j / n))
    return out


def solid_from_outline(front, back, thickness=None):
    """Closed 3D body (list of faces, each a ring of (x, y, z)) between two congruent
    outlines: the front and back face and one quadrilateral per outline edge.
    `thickness` is the horizontal vector (hx, hy) from the back to the front outline; when it
    is not given it is taken from the first vertices (which fails for a wedge of zero width)."""
    m = len(front)
    # all faces get normals pointing OUT of the body, whatever the direction of the outline
    # (the 2D section has the Y axis pointing down, which turns the outline over)
    if thickness is None:
        hx, hy = front[0][0] - back[0][0], front[0][1] - back[0][1]
    else:
        hx, hy = thickness
    hl = math.hypot(hx, hy)

    # points closer than _BR_TOL are ONE point (a distance, not a rounding of the coordinates, so a
    # value lying on a rounding boundary cannot split two equal points)
    _BR_TOL = 1e-6
    _cell = 4.0 * _BR_TOL

    def _cells(p):
        c = [math.floor(v / _cell) for v in p]
        return [(c[0] + i, c[1] + j, c[2] + k) for i in (-1, 0, 1) for j in (-1, 0, 1) for k in (-1, 0, 1)]

    def _same(p, q):
        return abs(p[0] - q[0]) <= _BR_TOL and abs(p[1] - q[1]) <= _BR_TOL and abs(p[2] - q[2]) <= _BR_TOL

    # a polygon with a hole is joined by a bridge: the outline runs along it there and back, so
    # the walls on both sides of the zero-width bridge coincide inside the body (Z-fighting in the
    # 3D view): an edge whose reverse is also in the outline gets no wall
    starts = {}                                      # grid cell of an edge start -> [(start, end)]
    for k in range(m):
        a, b = front[k], front[(k + 1) % m]
        starts.setdefault(tuple(math.floor(v / _cell) for v in a), []).append((a, b))

    def _bridge(a, b):                               # is the reverse edge b -> a in the outline?
        for c in _cells(b):
            for s0, s1 in starts.get(c, ()):
                if _same(s0, b) and _same(s1, a):
                    return True
        return False

    nfx, nfy = (hx / hl, hy / hl) if hl >= 1e-12 else (0.0, 0.0)   # horizontal normal of the front face
    tx, ty = nfy, -nfx                               # horizontal direction along the panel

    def newell(ring):
        nx = ny = nz = 0.0
        for q in range(len(ring)):
            x1, y1, z1 = ring[q]
            x2, y2, z2 = ring[(q + 1) % len(ring)]
            nx += (y1 - y2) * (z1 + z2)
            ny += (z1 - z2) * (x1 + x2)
            nz += (x1 - x2) * (y1 + y2)
        return nx, ny, nz

    def outward(ring, vx, vy, vz):
        n = newell(ring)
        return ring if n[0] * vx + n[1] * vy + n[2] * vz >= 0 else list(reversed(ring))

    if hl < 1e-12:                                   # no thickness: a flat body (front = back)
        # the front keeps its winding, the back is turned over, and every wall is oriented away
        # from the outline: outward direction of an edge a -> b is (b - a) x n of the front
        nf = newell(front)
        faces = [list(front), list(reversed(back))]
        for k in range(m):
            a, b = front[k], front[(k + 1) % m]
            if math.dist(a, b) < 1e-6 or _bridge(a, b):
                continue
            wall = [a, b, back[(k + 1) % m], back[k]]
            e = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
            ox = e[1] * nf[2] - e[2] * nf[1]
            oy = e[2] * nf[0] - e[0] * nf[2]
            oz = e[0] * nf[1] - e[1] * nf[0]
            faces.append(outward(wall, ox, oy, oz) if (ox or oy or oz) else wall)
        return faces
    faces = [outward(list(front), nfx, nfy, 0.0), outward(list(back), -nfx, -nfy, 0.0)]
    s = [(p[0] * tx + p[1] * ty, p[2]) for p in front]           # outline in (s, z)
    area = sum(s[q][0] * s[(q + 1) % m][1] - s[(q + 1) % m][0] * s[q][1] for q in range(m))
    sg = 1.0 if area >= 0 else -1.0                  # counter-clockwise or clockwise
    for k in range(m):
        a, b = front[k], front[(k + 1) % m]
        if math.dist(a, b) < 1e-6 or _bridge(a, b):
            continue
        ds = (b[0] - a[0]) * tx + (b[1] - a[1]) * ty
        dz = b[2] - a[2]
        # outward normal of the edge in (s, z): (dz, -ds) for a CCW outline, the opposite for CW
        ox, oy, oz = sg * dz * tx, sg * dz * ty, -sg * ds
        faces.append(outward([a, b, back[(k + 1) % m], back[k]], ox, oy, oz))
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
        out.append((solid_from_outline(front, back, (2.0 * hx, 2.0 * hy)), lit))
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
                "MultiPolygonZ?field=litho:string&field=zbot:double&field=ztop:double",
                f"{lit}{suffix}", "memory")
            lyr.setCrs(core.work_crs())     # the CRS object, not an authid text in the URI
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
            ok, _added = lyr.dataProvider().addFeatures(feats)
            if not ok:
                core.log_warning(f"3D layer '{lit}' could not be filled (the provider refused the features)")
            lyr.updateExtents()
            e = _style_layer(lyr, colors.get(lit, QColor(128, 128, 128)), edges)
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
    dm = max(maxx - minx, maxy - miny, 1.0) * 1000.0     # long thin sets of boreholes (roads, tunnels)
    mx, my = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    # work in a local origin: projected coordinates are often ~1e6 m, their squares ~1e12 would eat the
    # significant digits of float64 in the in-circle test
    P = [(p[0] - mx, p[1] - my) for p in pts] + [(-dm, -dm), (0.0, dm), (dm, -dm)]

    def in_circle(p, t):
        (ax, ay), (bx, by), (cx, cy) = P[t[0]], P[t[1]], P[t[2]]
        d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        # degenerate (collinear) triangle: the threshold scales with the size of the triangle
        if abs(d) < 1e-12 * ((ax - bx) ** 2 + (ay - by) ** 2 + (bx - cx) ** 2 + (by - cy) ** 2
                             + 1e-300):
            return False
        ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
              + (cx * cx + cy * cy) * (ay - by)) / d
        uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
              + (cx * cx + cy * cy) * (bx - ax)) / d
        r2 = (ax - ux) ** 2 + (ay - uy) ** 2
        # relative tolerance: a fixed one in m^2 is too coarse for tiny models and below the
        # float64 rounding for large ones
        return (p[0] - ux) ** 2 + (p[1] - uy) ** 2 < r2 * (1.0 - 1e-12)

    tris = [(n, n + 1, n + 2)]
    skipped = 0
    for i in range(n):
        bad = [t for t in tris if in_circle(P[i], t)]
        if not bad:                              # rounding on a degenerate set: the point is left out
            skipped += 1
            continue
        cnt = {}
        for t in bad:
            for e in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                k = tuple(sorted(e))
                cnt[k] = cnt.get(k, 0) + 1
        tris = [t for t in tris if t not in bad]
        tris += [(e[0], e[1], i) for e, c in cnt.items() if c == 1]
    if skipped:
        core.log_warning(f"Triangulation: {skipped} point(s) could not be inserted "
                         "(degenerate geometry) and were left out")
    return [t for t in tris if max(t) < n]


def _default_maxlen(pts):
    nn = []
    for i, p in enumerate(pts):
        nn.append(min(math.hypot(q[0] - p[0], q[1] - p[1]) for j, q in enumerate(pts) if j != i))
    return 2.5 * _median(nn)


def _chain_edges(pts):
    """Boreholes lying on one line: the points sorted along the line, each joined to the next
    one (Delaunay has no triangle there).  Returns [] when they are not collinear."""
    n = len(pts)
    if n < 2:
        return []
    ax, ay = min(pts)                    # the end points of the line: the farthest pair of the
    bx, by = max(pts)                    # lexicographic extremes (exact for collinear points)
    dx, dy = bx - ax, by - ay
    ln = math.hypot(dx, dy)
    if ln < 1e-9:
        return []
    tol = max(1e-3, 1e-6 * ln)
    if any(abs((p[0] - ax) * dy - (p[1] - ay) * dx) / ln > tol for p in pts):
        return []                        # not on one line
    order = sorted(range(n), key=lambda i: (pts[i][0] - ax) * dx + (pts[i][1] - ay) * dy)
    return [(min(a, b), max(a, b)) for a, b in zip(order, order[1:])]


def neighbour_edges(bhs, mode="grid", maxlen=0.0):
    """Pairs of neighbouring boreholes (indices of `bhs`) that get a panel; boreholes less than
    1 m from another one (repeated or inclined holes from one spot) are not joined.
    See _neighbour_edges for the modes."""
    keep = _unique_indices(bhs)
    if len(keep) == len(bhs):
        return _neighbour_edges(bhs, mode, maxlen)
    return sorted((keep[a], keep[b]) for a, b in _neighbour_edges([bhs[i] for i in keep], mode, maxlen))


def _neighbour_edges(bhs, mode="grid", maxlen=0.0):
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
        edges = delaunay_edges(pts) if n >= 3 else []
        if not edges:                    # collinear boreholes (road, railway, pipeline): no triangle
            edges = _chain_edges(pts)
        return sorted(e for e in edges
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
    line between the collars, with zero offset at the two boreholes.
    A panel of no length (L <= 0, NaN) has no profile: None (no terrain shift)."""
    if not L > 0.0:
        return None
    ds = [L * k / n for k in range(n + 1)]
    hs = [dem(A.x + (B.x - A.x) * d / L, A.y + (B.y - A.y) * d / L) for d in ds]
    hs = [h if (h is not None and math.isfinite(h)) else None for h in hs]     # NaN / Inf = NoData
    if all(h is None for h in hs):
        return None
    # NoData samples: linear interpolation between the neighbouring valid samples (the collars
    # at the two ends), so the panel follows the DEM like the volume does
    hs[0] = A.z if hs[0] is None else hs[0]
    hs[-1] = B.z if hs[-1] is None else hs[-1]
    k = 1
    while k < n:
        if hs[k] is None:
            k2 = k
            while hs[k2] is None:
                k2 += 1
            for m in range(k, k2):
                t = (m - (k - 1)) / float(k2 - (k - 1))
                hs[m] = hs[k - 1] + (hs[k2] - hs[k - 1]) * t
            k = k2
        k += 1
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

    def levels(self, d):
        """Elevations (terrain-shifted like lit_at) in the middle of every stretch of the vertical
        line at distance d that lies inside a polygon: samples inside thin layers."""
        out = []
        sh = self.shift(d) if self.shift else 0.0
        for d1, d2, _z1, _z2, P, _lit in self.polys:
            if not d1 <= d <= d2:
                continue
            zs = []
            for q in range(len(P)):
                (x1, y1), (x2, y2) = P[q], P[(q + 1) % len(P)]
                if (x1 <= d < x2) or (x2 <= d < x1):
                    zs.append(y1 + (y2 - y1) * (d - x1) / (x2 - x1))
            zs.sort()
            out.extend(0.5 * (a + b) + sh for a, b in zip(zs[0::2], zs[1::2]))
        return out

    def lit_at(self, d, z):
        if self.shift:
            z -= self.shift(d)
        for d1, d2, z1, z2, P, lit in self.polys:
            if d1 <= d <= d2 and z1 <= z <= z2 and core.point_in_polygon((d, z), P):
                return lit
        return None


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
    res = []                                  # a vertex on the clipping line can appear twice
    for p in out:
        if not res or abs(p[0] - res[-1][0]) > 1e-12 or abs(p[1] - res[-1][1]) > 1e-12:
            res.append(p)
    if len(res) > 1 and abs(res[0][0] - res[-1][0]) <= 1e-12 and abs(res[0][1] - res[-1][1]) <= 1e-12:
        res.pop()
    return res


def fill_between_sections(bhs, edge_of, dz=2.0, cell=0.0, maxlen=0.0, dem=None, max_columns=30000):
    """Volume between the panels (boreholes less than 1 m apart count once, the indices given to
    edge_of are those of `bhs`).  See _fill_between_sections."""
    keep = _unique_indices(bhs)
    if len(keep) == len(bhs):
        return _fill_between_sections(bhs, edge_of, dz, cell, maxlen, dem, max_columns)
    return _fill_between_sections([bhs[i] for i in keep],
                                  lambda i, j: edge_of(keep[i], keep[j]),
                                  dz, cell, maxlen, dem, max_columns)


def _fill_between_sections(bhs, edge_of, dz=2.0, cell=0.0, maxlen=0.0, dem=None, max_columns=30000):
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
                # centroid of the polygon (area-weighted): the mean of the vertices is pulled to the
                # side where the clipping by the triangle put more vertices
                # (computed from the first vertex: coordinates of ~1e6 m would lose the digits)
                ox, oy = ring[0]
                a2 = cx = cy = 0.0
                for q in range(len(ring)):
                    x1, y1 = ring[q][0] - ox, ring[q][1] - oy
                    x2, y2 = ring[(q + 1) % len(ring)][0] - ox, ring[(q + 1) % len(ring)][1] - oy
                    c = x1 * y2 - x2 * y1
                    a2 += c
                    cx += (x1 + x2) * c
                    cy += (y1 + y2) * c
                if abs(a2) > 1e-12 * cell * cell:
                    cx = ox + cx / (3.0 * a2)
                    cy = oy + cy / (3.0 * a2)
                else:
                    cx = sum(p[0] for p in ring) / len(ring)
                    cy = sum(p[1] for p in ring) / len(ring)
                wa = ((B[0] - cx) * (C[1] - cy) - (B[1] - cy) * (C[0] - cx)) / det
                wb = ((C[0] - cx) * (A[1] - cy) - (C[1] - cy) * (A[0] - cx)) / det
                w3 = [max(wa, 0.0), max(wb, 0.0), max(1.0 - wa - wb, 0.0)]
                g = None
                if dem is not None:
                    g = dem(cx, cy)
                gz = sum(w * b.z for w, b in zip(w3, bh3))
                if g is None or not math.isfinite(g):      # NoData may come as NaN / Inf
                    g = gz
                # the panels are shifted with the terrain (fence_solids), bottom included: the bottom
                # of the column follows by the same offset, or there are gaps / cut-offs under hills
                zbot = sum(w * b.z_at(b.depth) for w, b in zip(w3, bh3)) + (g - gz)
                refs = _section_refs(w3, idx, edge_of)   # (sampler, distance d, weight) of 3 sections

                if not refs:
                    continue

                def vote(z, refs=refs):
                    votes = {}
                    for sampler, d, wt in refs:
                        lit = sampler.lit_at(d, z)
                        if lit is not None:
                            votes[lit] = votes.get(lit, 0.0) + wt
                    return max(votes, key=votes.get) if votes else None

                extra = [z for sampler, d, _wt in refs for z in sampler.levels(d)]
                for top, bot, lit in _column_runs(vote, g, zbot, dz, extra):
                    out.append((box_solid(ring, bot, top - bot), lit))
    return out


def _section_refs(w3, idx, edge_of):
    """The three sections around a triangle of boreholes as (sampler, distance d, weight) for the
    point with barycentric coordinates w3 (the line from each corner through the point hits the
    opposite section)."""
    refs = []
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
    return refs


def _clean_ring(ring, tol=1e-6):
    """Ring of (x, y, z) without repeated neighbouring points; [] when it has no area."""
    out = []
    for p in ring:
        if not out or math.dist(p, out[-1]) > tol:
            out.append(p)
    while len(out) > 1 and math.dist(out[0], out[-1]) <= tol:
        out.pop()
    if len(out) < 3:
        return []
    nx = ny = nz = 0.0
    for k in range(len(out)):
        x1, y1, z1 = out[k]
        x2, y2, z2 = out[(k + 1) % len(out)]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    return out if math.sqrt(nx * nx + ny * ny + nz * nz) > 1e-9 else []


def _wedge(xy, tb):
    """Closed body over a triangle: sloped top and bottom, tb = [(top, bottom)] per corner."""
    top = [(x, y, t[0]) for (x, y), t in zip(xy, tb)]
    bot = [(x, y, t[1]) for (x, y), t in zip(xy, tb)]
    faces = [top, list(reversed(bot))]
    for k in range(3):
        k2 = (k + 1) % 3
        ring = _clean_ring([bot[k], bot[k2], top[k2], top[k]])
        if ring:
            faces.append(ring)
    return faces


MAX_SMOOTH_CELLS = 250_000      # triangle subdivisions of the smooth meshes (Python loops)


def fill_smooth(bhs, edge_of, dz=2.0, cell=0.0, maxlen=0.0, dem=None, max_solids=60000):
    """Interpolated layers between the sections WITHOUT blocks (boreholes less than 1 m apart count
    once, the indices given to edge_of are those of `bhs`).  See _fill_smooth."""
    keep = _unique_indices(bhs)
    if len(keep) == len(bhs):
        return _fill_smooth(bhs, edge_of, dz, cell, maxlen, dem, max_solids)
    return _fill_smooth([bhs[i] for i in keep], lambda i, j: edge_of(keep[i], keep[j]),
                        dz, cell, maxlen, dem, max_solids)


def _fill_smooth(bhs, edge_of, dz=2.0, cell=0.0, maxlen=0.0, dem=None, max_solids=60000):
    """Interpolated layers between the sections WITHOUT blocks.

    Every Delaunay triangle of neighbouring boreholes is divided into a fine triangular mesh.
    At every mesh node the stack of layers is read from the three surrounding sections (the
    same voting as the block method; layer boundaries are refined by bisection, so they are
    exact and not rounded to dz).  Neighbouring nodes are then joined layer by layer (the k-th
    occurrence of a lithology in a column continues into the k-th occurrence in the next one)
    into closed bodies with SLOPED top and bottom faces, so boundaries are smooth surfaces,
    dipping layers stay inclined and a layer that is missing at one node pinches out
    over one mesh cell.  Nodes on the border of two triangles are shared, so the volumes
    meet without gaps (all triangles use the same number of divisions n, so the nodes on a
    shared edge coincide; no T-junctions).  Returns [(faces, litho)]."""
    pts = [(b.x, b.y) for b in bhs]
    if len(bhs) < 3:
        return []
    if maxlen <= 0:
        maxlen = _default_maxlen(pts)
    tris = [t for t in delaunay_triangles(pts)
            if all(math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1]) <= maxlen
                   for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])))]
    if not tris:
        return []
    if cell <= 0:
        lens = [math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1])
                for t in tris for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0]))]
        cell = max(_median(lens) / 8.0, 0.5)
    # ONE division for every triangle (from the longest side of the whole mesh): two triangles
    # that share an edge then have the same nodes on it
    longest = max(math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1])
                  for t in tris for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])))
    n = max(1, int(math.ceil(longest / cell)))
    if len(tris) * n * n > MAX_SMOOTH_CELLS:       # before any loop: a tiny cell would freeze QGIS
        raise ValueError(f"The mesh would have about {len(tris) * n * n:,} cells - increase the "
                         "mesh size (the limit is {:,}).".format(MAX_SMOOTH_CELLS))
    cache = {}              # node position -> (x, y, {lithology: [(top, bottom), ...]}, g, zbot)
    out = []

    def column(w3, x, y, bh3, idx):
        key = (round(x, 6), round(y, 6))
        if key in cache:
            return cache[key]
        gz = sum(w * b.z for w, b in zip(w3, bh3))
        g = dem(x, y) if dem is not None else None
        if g is None or not math.isfinite(g):          # NoData may come as NaN / Inf
            g = gz
        # the panels follow the terrain including their bottom edge: so does the volume
        zbot = sum(w * b.z_at(b.depth) for w, b in zip(w3, bh3)) + (g - gz)
        refs = _section_refs(w3, idx, edge_of)
        by = {}
        if refs:
            def vote(z, refs=refs):
                votes = {}
                for sampler, d, wt in refs:
                    lit = sampler.lit_at(d, z)
                    if lit is not None:
                        votes[lit] = votes.get(lit, 0.0) + wt
                return max(votes, key=votes.get) if votes else None
            extra = [z for sampler, d, _wt in refs for z in sampler.levels(d)]
            for top, bot, lit in _column_runs(vote, g, zbot, dz, extra):
                by.setdefault(lit, []).append((top, bot))
        cache[key] = (x, y, by, g, zbot)     # the x, y of the first triangle that reached the node
        return cache[key]

    def join(v3):
        _join_nodes(v3, out)
        if len(out) > max_solids:
            raise ValueError("The interpolated volume would contain too many bodies - "
                             "increase the mesh size of the volume.")

    for t in tris:
        idx = list(t)
        A, B, C = (pts[k] for k in idx)
        det = (B[0] - A[0]) * (C[1] - A[1]) - (B[1] - A[1]) * (C[0] - A[0])
        if abs(det) < 1e-9:
            continue
        if det < 0:
            idx[1], idx[2] = idx[2], idx[1]
            A, B, C = (pts[k] for k in idx)
        bh3 = [bhs[k] for k in idx]
        nodes = {}

        def node(i, j, A=A, B=B, C=C, bh3=bh3, idx=idx, nodes=nodes):
            if (i, j) not in nodes:
                a, b, c = (n - i - j) / n, i / n, j / n
                x = a * A[0] + b * B[0] + c * C[0]
                y = a * A[1] + b * B[1] + c * C[1]
                # a node shared by two triangles has ONE position (the cached one), not two that
                # differ by rounding of the barycentric sums
                nodes[(i, j)] = column((a, b, c), x, y, bh3, idx)
            return nodes[(i, j)]

        for i in range(n):
            for j in range(n - i):
                join([node(i, j), node(i + 1, j), node(i, j + 1)])
                if i + j < n - 1:
                    join([node(i + 1, j), node(i + 1, j + 1), node(i, j + 1)])
    return out


def _match_runs(lists):
    """Group the runs (top, bottom) of ONE lithology of the nodes into bodies.  Nodes with the
    same number of runs are joined in order (k-th with k-th).  When the numbers differ (sand -
    clay - sand in one column, a single sand in the next), the runs of the shorter columns are
    matched in order to the runs of the longest one with the nearest middle elevation (the
    cheapest order-preserving matching), so the single sand joins the nearer one and the other
    pinches out instead of cutting through it.  Returns a list of groups, each a list with
    one run or None per node."""
    n_max = max(len(q) for q in lists)
    ref = next(i for i, q in enumerate(lists) if len(q) == n_max)
    groups = [[None] * len(lists) for _ in range(n_max)]

    def mid(r):
        return (r[0] + r[1]) / 2.0

    for j, q in enumerate(lists):
        if j == ref or len(q) == n_max:
            for k, r in enumerate(q):
                groups[k][j] = r
            continue
        m, n = len(q), n_max
        INF = float("inf")
        # dp[i][k]: cheapest cost of matching the first i runs of q within ref runs 0..k-1
        dp = [[INF] * (n + 1) for _ in range(m + 1)]
        for k in range(n + 1):
            dp[0][k] = 0.0
        for i in range(1, m + 1):
            for k in range(i, n + 1):
                dp[i][k] = min(dp[i][k - 1],
                               dp[i - 1][k - 1] + abs(mid(q[i - 1]) - mid(lists[ref][k - 1])))
        i, k = m, n
        while i > 0:                              # backtrack
            if dp[i][k] == dp[i][k - 1] and k > i:
                k -= 1
            else:
                groups[k - 1][j] = q[i - 1]
                i -= 1
                k -= 1
    return groups


def _tip_level(v, z0):
    """Elevation of the pinch-out tip of a layer missing at node v: the boundary of the
    node's own layers nearest to z0, so the tip lies BETWEEN two layers and the wedge does not
    cut through a layer that continues here (never above the ground, never below the bottom)."""
    bounds = [z for runs in v[2].values() for r in runs for z in (r[0], r[1])]
    if len(v) > 4:
        bounds += [v[3], v[4]]
    if bounds:
        z0 = min(bounds, key=lambda z: abs(z - z0))
    if len(v) > 4:
        z0 = max(min(z0, v[3]), min(v[4], v[3]))
    return z0


def _join_nodes(v3, out):
    """Join three neighbouring mesh nodes (x, y, {lithology: [(top, bottom), ...]}) layer by
    layer into closed bodies with sloped top and bottom faces and append them to `out`.
    The k-th occurrence of a lithology in a column continues into the k-th occurrence in the
    next one; a layer missing at one node pinches out towards it (inside the node's ground /
    bottom range, so the wedge never sticks out above the terrain).
    Nodes are (x, y, layers[, ground, bottom])."""
    lits = set()
    for v in v3:
        lits.update(v[2])
    xy = [(v[0], v[1]) for v in v3]
    for lit in sorted(lits, key=str):
        lists = [v[2].get(lit, []) for v in v3]
        groups = _match_runs(lists)
        for runs in groups:
            have = [r for r in runs if r]
            mid = sum((r[0] + r[1]) / 2.0 for r in have) / len(have)
            tb = []
            for r, v in zip(runs, v3):
                if r:
                    tb.append(r)
                    continue
                z0 = _tip_level(v, mid)          # pinch-out of the missing layer at this node
                tb.append((z0, z0))
            if all(r[0] - r[1] < 1e-6 for r in tb):
                continue
            out.append((_wedge(xy, tb), lit))


def _shepard(dists, k):
    """Weights of the nearest boreholes that fall to ZERO at the distance of the (k+1)-th one
    (modified Shepard), so the interpolated layers change smoothly when the set of nearest
    boreholes changes.  dists = [(distance, borehole)] sorted; returns [(weight, borehole)]."""
    R = dists[k][0] if len(dists) > k else dists[-1][0] * 1.5 + 1.0
    out = []
    for d, b in dists:
        if d >= R:
            break
        out.append((((R - d) / (R * math.hypot(d, 1.0))) ** 2, b))
    return out or [(1.0, dists[0][1])]


def smooth_model(boreholes, cell=0.0, dz=2.0, mode="elevation", k=4, maxdist=0.0,
                 extent="hull", maxlen=0.0, margin=0.1, max_solids=80000):
    """FULL 3D model made directly from the boreholes (no 2D sections needed) with SMOOTH layers.

    The layer stack of a mesh node is interpolated from the nearest boreholes with the same
    rules as the block model (mode "elevation" = horizontal layers, "depth" = layers parallel to
    the ground; the Quaternary cover always follows the ground), but with continuous weights, and
    layer boundaries are refined by bisection - they are exact surfaces, not multiples of dz.
    Neighbouring nodes are joined layer by layer into closed bodies with sloped tops and bottoms
    (see _join_nodes).  extent "hull": the triangles between the boreholes (Delaunay,
    triangles with a side longer than `maxlen` - 0 = 2 x maxdist or automatic - are left out);
    extent "rectangle": a rectangle around the boreholes extended by `margin`, nodes farther than
    `maxdist` from a borehole are left out.  Returns (solids [(faces, litho)], info)."""
    bhs = drop_coincident([b for b in boreholes if b.layers])
    if len(bhs) < 3:
        raise ValueError("At least three boreholes with layers (at different places) are needed.")
    lay = {id(b): core.clean_layers(b.layers) for b in bhs}
    cos_dip = {id(b): max(math.cos(math.radians(b.dip)), 1e-4) for b in bhs}
    qth, qvth = {}, {}
    known = {id(b): any(L.age is not None for L in lay[id(b)]) for b in bhs}
    for b in bhs:
        n = core._quat_run(lay[id(b)])
        qth[id(b)] = lay[id(b)][n - 1].bottom if n else 0.0
        qvth[id(b)] = qth[id(b)] * cos_dip[id(b)]
    pts = [(b.x, b.y) for b in bhs]
    xs = [b.x for b in bhs] + [b.point_at(b.depth)[0] for b in bhs]
    ys = [b.y for b in bhs] + [b.point_at(b.depth)[1] for b in bhs]
    ext = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    if cell <= 0:
        cell = max(ext / 25.0, 1.0)
    cand_n = max(2 * k, 8)
    # how far an inclined hole can lie from its collar (horizontally)
    reach = {id(b): b.depth * math.sin(math.radians(b.dip)) for b in bhs}
    cache = {}

    def column(x, y):
        key = (round(x, 6), round(y, 6))
        if key in cache:
            return cache[key]
        dl = sorted(((math.hypot(b.x - x, b.y - y), b) for b in bhs), key=lambda t: t[0])
        cands = [b for _, b in dl[:cand_n]]
        # a long inclined hole whose collar is farther than the candidates but whose trace can
        # pass under the point (as in grid_model, which looks at every hole) is a candidate too
        if len(dl) > cand_n:
            dmax = dl[cand_n - 1][0]
            cands += [b for d, b in dl[cand_n:] if reach[id(b)] > 0 and d - reach[id(b)] <= dmax]
        wc = _shepard(dl, max(1, k))
        ws = sum(w for w, _ in wc)
        g = sum(w * b.z for w, b in wc) / ws                       # ground elevation
        wk = [(w, b) for w, b in wc if known[id(b)]]
        qt = (sum(w * qvth[id(b)] for w, b in wk) / sum(w for w, _ in wk)
              if wk else 0.0)                                       # vertical Quaternary thickness
        zbot = sum(w * b.z_at(b.depth) for w, b in wc) / ws

        def vote(z):
            dg = max(g - z, 0.0)
            zd = []
            for b in cands:                       # distance of the borehole trace at elevation z
                md = max(0.0, min(b.depth, (b.z - z) / cos_dip[id(b)]))
                bx, by, _ = b.point_at(md)
                zd.append((math.hypot(bx - x, by - y), b))
            zd.sort(key=lambda t: t[0])
            votes = {}
            quat = qt > 0 and dg <= qt
            for w, b in _shepard(zd, max(1, k)):
                cd = cos_dip[id(b)]
                if quat and not known[id(b)]:       # no stratigraphy: the cover is not marked, so
                    L = unit_at(lay[id(b)], dg / cd)   # the layers count by depth (as below)
                    ok = L is not None
                elif quat:                                          # Quaternary cover
                    qv = qvth[id(b)]
                    if qv <= 0:
                        continue
                    L = unit_at(lay[id(b)], (dg * qv / qt) / cd)
                    ok = L is not None and L.age == (0, 0)
                else:
                    qb = qth[id(b)] if known[id(b)] else qt / cd
                    dep = qb + (dg - qt) / cd if mode == "depth" else (b.z - z) / cd
                    L = unit_at(lay[id(b)], dep)
                    ok = L is not None and L.age != (0, 0)
                if ok:
                    votes[L.litho] = votes.get(L.litho, 0.0) + w
            return max(votes, key=votes.get) if votes else None

        # samples in the middle of every layer of the nearest boreholes (in the elevation of each
        # rule of vote), so a layer thinner than dz is not stepped over
        extra = []
        for _, b in dl[:max(1, k)]:
            cd = cos_dip[id(b)]
            qb = qth[id(b)] if known[id(b)] else qt / cd
            for L in lay[id(b)]:
                dm = 0.5 * (L.top + L.bottom)
                extra.append(b.z - dm * cd)                                # elevation mode
                if qt > 0 and qvth[id(b)] > 0:
                    extra.append(g - dm * cd * qt / qvth[id(b)])           # Quaternary cover
                if mode == "depth":
                    extra.append(g - (qt + (dm - qb) * cd))                # layers follow the ground
        by = {}
        for top, bot, lit in _column_runs(vote, g, zbot, dz, extra):
            by.setdefault(lit, []).append((top, bot))
        cache[key] = (x, y, by, g, zbot)
        return cache[key]

    out = []
    ntri = [0]

    def join(a, b, c):
        _join_nodes([a, b, c], out)
        ntri[0] += 1
        if len(out) > max_solids:
            raise ValueError("The model would contain too many bodies - increase the mesh "
                             "size or the vertical step.")

    if extent == "rectangle":
        pad = margin * ext
        x0, y0 = min(xs) - pad, min(ys) - pad
        nx = int(math.ceil((max(xs) + pad - x0) / cell))
        ny = int(math.ceil((max(ys) + pad - y0) / cell))
        if nx * ny > 40000:
            raise ValueError(f"Mesh of {nx} x {ny} cells is too large - increase the mesh size.")
        ok = {}

        def valid(i, j):
            if (i, j) not in ok:
                x, y = x0 + i * cell, y0 + j * cell
                ok[(i, j)] = (maxdist <= 0
                              or min(_plan_dist(b, x, y) for b in bhs) <= maxdist)
            return ok[(i, j)]

        def node(i, j):
            return column(x0 + i * cell, y0 + j * cell)
        for i in range(nx):
            for j in range(ny):
                if not (valid(i, j) and valid(i + 1, j) and valid(i + 1, j + 1) and valid(i, j + 1)):
                    continue
                join(node(i, j), node(i + 1, j), node(i + 1, j + 1))
                join(node(i, j), node(i + 1, j + 1), node(i, j + 1))
    else:
        if maxlen <= 0:
            maxlen = 2.0 * maxdist if maxdist > 0 else _default_maxlen(pts)
        tris = [t for t in delaunay_triangles(pts)
                if all(math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1]) <= maxlen
                       for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])))]
        if not tris:
            raise ValueError("No triangle of neighbouring boreholes - increase the maximum "
                             "distance from a borehole.")
        # Neighbouring triangles must divide their shared side into the same number of parts, or
        # the nodes on it differ and gaps (T-junctions) appear: n is the same for every triangle,
        # from the longest side of the whole mesh (as in fill_smooth).
        longest = 0.0
        for t in tris:
            A, B, C = (pts[q] for q in t)
            longest = max(longest, math.hypot(B[0] - A[0], B[1] - A[1]),
                          math.hypot(C[0] - B[0], C[1] - B[1]), math.hypot(A[0] - C[0], A[1] - C[1]))
        est = len(tris) * max(1, int(math.ceil(longest / cell))) ** 2   # cells, checked before the loops
        if est > MAX_SMOOTH_CELLS:
            raise ValueError(f"The mesh would have about {est:,} cells - increase the mesh size "
                             f"(the limit is {MAX_SMOOTH_CELLS:,}).")
        n_all = max(1, int(math.ceil(longest / cell)))   # ONE division for the whole mesh
        for t in tris:
            idx = list(t)
            A, B, C = (pts[q] for q in idx)
            det = (B[0] - A[0]) * (C[1] - A[1]) - (B[1] - A[1]) * (C[0] - A[0])
            if abs(det) < 1e-9:
                continue
            if det < 0:
                idx[1], idx[2] = idx[2], idx[1]
                A, B, C = (pts[q] for q in idx)
            n = n_all

            def node(i, j, n=n, A=A, B=B, C=C):
                a, b, c = (n - i - j) / n, i / n, j / n
                return column(a * A[0] + b * B[0] + c * C[0], a * A[1] + b * B[1] + c * C[1])
            for i in range(n):
                for j in range(n - i):
                    join(node(i, j), node(i + 1, j), node(i, j + 1))
                    if i + j < n - 1:
                        join(node(i + 1, j), node(i + 1, j + 1), node(i, j + 1))
    return out, {"cell": cell, "nodes": len(cache), "triangles": ntri[0], "bodies": len(out),
                 "extent": extent}


def _column_runs(vote, ztop, zbot, dz, extra=()):
    """Runs (top, bottom, lithology) of one column; boundaries are refined by bisection.
    The column is sampled every ~dz and at the elevations in `extra` (e.g. the middle of every
    layer of the neighbouring boreholes or sections): a layer thinner than dz that lies between
    two equidistant samples would otherwise be skipped and swallowed by its neighbour."""
    if ztop - zbot < 1e-6:
        return []
    n = max(1, int(math.ceil((ztop - zbot) / dz)))
    step = (ztop - zbot) / n
    zc = [ztop - (k + 0.5) * step for k in range(n)]
    if extra:
        seen = {round(z, 6) for z in zc}
        for z in extra:
            if zbot + 1e-6 < z < ztop - 1e-6 and round(z, 6) not in seen:
                seen.add(round(z, 6))
                zc.append(z)
        zc.sort(reverse=True)
        n = len(zc)
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
                   colw=3.0, fill=False, cell=0.0, maxlen=0.0, max_solids=60000,
                   fill_mode="smooth"):
    """Thin vertical panels between neighbouring boreholes as 3D solids [(faces, litho)] and,
    with fill=True, the volume between the panels.
    make_geo(A, B, L) -> 2D section geometry (core.section_geo) of the pair;
    dem(x, y) -> ground elevation or None (optional terrain following, sampled every `step` m).
    fill_mode "smooth" (sloped solids, default) or "blocks" (columns of boxes, versions <= 0.18.2).
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
        if fill_mode == "blocks":
            volume = fill_between_sections(bhs, cached, dz, cell, maxlen, dem,
                                           max_columns=max(1, min(30000, max_solids - len(solids))))
        else:
            volume = fill_smooth(bhs, cached, dz, cell, maxlen, dem,
                                 max_solids=max(1, max_solids - len(solids)))   # panels + volume together
    if colw > 0:
        used = {k for e in edges for k in e}
        solids += column_solids([bhs[k] for k in sorted(used)], colw, dz)
    if len(solids) + len(volume) > max_solids:
        raise ValueError("The model would contain too many objects - shorten the maximum "
                         "panel length or select fewer boreholes.")
    return solids, volume
