"""3D geological models for the QGIS 3D map view.

Two models are offered:

* grid model   - square grid chosen from the boreholes; every grid cell gets a
                 column of blocks whose lithology is interpolated from the
                 nearest boreholes,
* fence model  - 2D cross sections (e.g. two perpendicular families) are turned
                 into thin 3D slabs.

Both are written as extruded polygons (PolygonZ + extrusion).  Block heights are
multiples of the vertical step, a column of n steps is split into power-of-two
chunks, so only a few layers per lithology are needed and no data-defined
extrusion is required.
"""
import math

from . import core

CRS = core.CRS_SJTSK


# ------------------------------------------------------------------ helpers
def binary_chunks(n):
    """n steps -> [largest power of two, ...] that sum up to n."""
    out = []
    p = 1
    while p * 2 <= n:
        p *= 2
    while n > 0 and p >= 1:
        if p <= n:
            out.append(p)
            n -= p
        p //= 2
    return out


def stack_prisms(ring, i_bot, i_top, dz, litho):
    """Prisms filling the levels i_bot*dz .. i_top*dz above the footprint ring.
    Returns [(ring, z_base, height, litho)]."""
    out = []
    i = i_bot
    for c in binary_chunks(i_top - i_bot):
        out.append((ring, i * dz, c * dz, litho))
        i += c
    return out


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
            i_top = int(math.floor(g / dz - 0.5))
            i_min = int(math.floor(zbot / dz))
            seq = []
            for i in range(i_top, i_min - 1, -1):
                zc = (i + 0.5) * dz
                dg = g - zc
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
                    seq.append((i, None))
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
                seq.append((i, max(votes, key=votes.get) if votes else None))
            ring = [(cx - cell / 2, cy - cell / 2), (cx + cell / 2, cy - cell / 2),
                    (cx + cell / 2, cy + cell / 2), (cx - cell / 2, cy + cell / 2)]
            # merge neighbouring voxels of the same lithology
            j = 0
            while j < len(seq):
                lit = seq[j][1]
                m = j
                while m + 1 < len(seq) and seq[m + 1][1] == lit:
                    m += 1
                if lit is not None:
                    top_i, bot_i = seq[j][0], seq[m][0]
                    prisms += stack_prisms(ring, bot_i, top_i + 1, dz, lit)
                j = m + 1
            ncells += 1
            if len(prisms) > max_prisms:
                raise ValueError("The model would contain too many blocks - increase the "
                                 "cell size or the vertical step.")
    return prisms, {"cell": cell, "cells": ncells, "nx": nx, "ny": ny}


# -------------------------------------------------------------- fence model
def _slab_ring(locate, da, db, width):
    p1, p2 = locate(da), locate(db)
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    ln = math.hypot(dx, dy)
    if ln < 1e-6:
        return None
    nx, ny = -dy / ln * width / 2.0, dx / ln * width / 2.0
    return [(p1[0] + nx, p1[1] + ny), (p2[0] + nx, p2[1] + ny),
            (p2[0] - nx, p2[1] - ny), (p1[0] - nx, p1[1] - ny)]


def fence_prisms(geo, locate, length, width=4.0, slab=8.0, dz=2.0, shift=None):
    """Turn one 2D section (core.build_section_scene(...).geo) into 3D slabs.
    locate(d) -> (x, y) point on the section line at distance d.
    shift(d) -> vertical offset (terrain following, e.g. from a DEM) or None."""
    d0, kx, ky, zmax = geo["d0"], geo["kx"], geo["ky"], geo["zmax"]
    prisms = []
    for pts, lit, _age in list(geo["polys"]) + list(geo["cols"]):
        P = [(d0 + px / kx, zmax - py / ky) for px, py in pts]
        dmin = min(p[0] for p in P)
        dmax = max(p[0] for p in P)
        if dmax - dmin < 1e-6:
            continue
        n = max(1, int(math.ceil((dmax - dmin) / slab)))
        step = (dmax - dmin) / n
        for s in range(n):
            da, db = dmin + s * step, dmin + (s + 1) * step
            dc = (da + db) / 2.0
            zs = []
            m = len(P)
            for q in range(m):
                (x1, z1), (x2, z2) = P[q], P[(q + 1) % m]
                if (x1 <= dc < x2) or (x2 <= dc < x1):
                    zs.append(z1 + (z2 - z1) * (dc - x1) / (x2 - x1))
            zs.sort()
            off = shift(dc) if shift else 0.0
            ring = None
            for a, b in zip(zs[0::2], zs[1::2]):
                a, b = a + off, b + off
                ia, ib = int(round(a / dz)), int(round(b / dz))
                if ib <= ia:
                    continue
                if ring is None:
                    ring = _slab_ring(locate, da, db, width)
                    if ring is None:
                        break
                prisms += stack_prisms(ring, ia, ib, dz, lit)
    return prisms


# ----------------------------------------------------------- QGIS 3D layers
def _style_layer(layer, color, height):
    """Single-colour extruded 3D style; returns None or an error text."""
    try:
        from qgis._3d import QgsPolygon3DSymbol, QgsVectorLayer3DRenderer
        from qgis.core import Qgis, QgsPhongMaterialSettings
        sym = QgsPolygon3DSymbol()
        sym.setAltitudeClamping(Qgis.AltitudeClamping.Absolute)
        sym.setExtrusionHeight(height)
        mat = QgsPhongMaterialSettings()
        mat.setDiffuse(color)
        mat.setAmbient(color.darker(160))
        sym.setMaterialSettings(mat)
        try:
            sym.setEdgesEnabled(True)
            sym.setEdgeWidth(0.4)
            sym.setEdgeColor(color.darker(220))
        except (AttributeError, TypeError) as e:
            # edge rendering is optional and missing in some QGIS versions
            core.log_warning(f"3D edge styling not available: {type(e).__name__}: {e}")
        layer.setRenderer3D(QgsVectorLayer3DRenderer(sym))
        return None
    except Exception as e:      # report to the user instead of failing silently
        return f"{type(e).__name__}: {e}"


def build_layers(prisms, colors, group_name):
    """Create the 3D layers (one per lithology and block height) in a layer group.
    Returns (number_of_layers, number_of_prisms, error_text_or_None)."""
    from qgis.core import (QgsFeature, QgsGeometry, QgsLineString, QgsPoint, QgsPolygon,
                           QgsProject, QgsVectorLayer)
    groups = {}
    for ring, zb, h, lit in prisms:
        groups.setdefault((lit, round(h, 6)), []).append((ring, zb))
    root = QgsProject.instance().layerTreeRoot()
    group = root.insertGroup(0, group_name)
    err = None
    count = 0
    for (lit, h), items in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), -kv[0][1])):
        lyr = QgsVectorLayer(
            f"PolygonZ?crs={CRS}&field=litho:string&field=zbot:double&field=ztop:double",
            f"{lit} [{h:g} m]", "memory")
        feats = []
        for ring, zb in items:
            pts = [QgsPoint(x, y, zb) for x, y in ring] + [QgsPoint(ring[0][0], ring[0][1], zb)]
            poly = QgsPolygon()
            poly.setExteriorRing(QgsLineString(pts))
            f = QgsFeature(lyr.fields())
            f.setGeometry(QgsGeometry(poly))
            f.setAttributes([lit, zb, zb + h])
            feats.append(f)
        lyr.dataProvider().addFeatures(feats)
        lyr.updateExtents()
        e = _style_layer(lyr, colors.get(lit, core.QColor(128, 128, 128)), h)
        err = err or e
        QgsProject.instance().addMapLayer(lyr, False)
        group.addLayer(lyr)
        count += 1
    return count, len(prisms), err


# ---------------------------------------------- network of panels (fence diagram)
def _median(v):
    v = sorted(v)
    return v[len(v) // 2] if v else 0.0


def delaunay_edges(pts):
    """Edges (i, j) of the Delaunay triangulation (Bowyer-Watson, small point sets)."""
    n = len(pts)
    if n < 2:
        return set()
    if n == 2:
        return {(0, 1)}
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
    out = set()
    for t in tris:
        if max(t) >= n:
            continue
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            out.add((min(a, b), max(a, b)))
    return out


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


def column_prisms(bhs, width, dz):
    """Borehole columns (the black pillars of a fence diagram; supports inclined boreholes)."""
    out = []
    h = width / 2.0
    for b in bhs:
        i0 = int(round(b.z_at(b.depth) / dz))
        i1 = int(round(b.z / dz))
        cos_d = max(math.cos(math.radians(b.dip)), 1e-4)
        cur = None
        for i in range(i0, i1 + 1):
            zc = (i + 0.5) * dz
            md = max(0.0, min(b.depth, (b.z - zc) / cos_d))
            bx, by, _ = b.point_at(md)
            key = (round(bx, 3), round(by, 3)) if i < i1 else None
            if cur and cur[0] == key:
                cur[2] = i + 1
            else:
                if cur:
                    (cx, cy), ia, ib = cur
                    ring = [(cx - h, cy - h), (cx + h, cy - h),
                            (cx + h, cy + h), (cx - h, cy + h)]
                    out += stack_prisms(ring, ia, ib, dz, "Borehole")
                cur = [key, i, i + 1] if key else None
    return out


def network_prisms(bhs, edges, make_geo, width=1.0, slab=5.0, dz=2.0, dem=None,
                   colw=3.0, max_prisms=250000):
    """Thin vertical panels between neighbouring boreholes.
    make_geo(A, B, L) -> 2D section geometry (core.section_geo) of the pair;
    dem(x, y) -> ground elevation or None (optional terrain following)."""
    prisms = []
    for i, j in edges:
        A, B = bhs[i], bhs[j]
        L = math.hypot(B.x - A.x, B.y - A.y)
        if L < 1e-6:
            continue
        geo = make_geo(A, B, L)

        def locate(d, A=A, B=B, L=L):
            return (A.x + (B.x - A.x) * d / L, A.y + (B.y - A.y) * d / L)

        shift = _dem_shift(A, B, L, dem) if dem is not None else None
        prisms += fence_prisms(geo, locate, L, width, slab, dz, shift)
        if len(prisms) > max_prisms:
            raise ValueError("The model would contain too many blocks - increase the slab "
                             "length or the vertical step, or shorten the maximum panel length.")
    if colw > 0:
        used = {k for e in edges for k in e}
        prisms += column_prisms([bhs[k] for k in sorted(used)], colw, dz)
    return prisms
