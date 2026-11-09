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
    qth = {}
    for b in bhs:
        n = core._quat_run(lay[id(b)])
        qth[id(b)] = lay[id(b)][n - 1].bottom if n else 0.0
    xs = [b.x for b in bhs]
    ys = [b.y for b in bhs]
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
            qt = sum(w * qth[id(b)] for w, b in wts) / wsum        # Quaternary thickness
            zbot = min(b.z - b.depth for _, b in near)
            i_top = int(math.floor(g / dz - 0.5))
            i_min = int(math.floor(zbot / dz))
            seq = []
            for i in range(i_top, i_min - 1, -1):
                zc = (i + 0.5) * dz
                dg = g - zc
                votes = {}
                if qt > 0 and dg <= qt:                            # Quaternary cover
                    for w, b in wts:
                        q_b = qth[id(b)]
                        if q_b <= 0:
                            continue
                        L = unit_at(lay[id(b)], dg * q_b / qt)
                        if L is not None and L.age == (0, 0):
                            votes[L.litho] = votes.get(L.litho, 0.0) + w
                else:
                    for w, b in wts:
                        if mode == "depth":
                            dep = qth[id(b)] + (dg - qt)
                        else:
                            dep = b.z - zc
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


def fence_prisms(geo, locate, length, width=4.0, slab=8.0, dz=2.0):
    """Turn one 2D section (core.build_section_scene(...).geo) into 3D slabs.
    locate(d) -> (x, y) point on the section line at distance d."""
    d0, kx, ky, zmax = geo["d0"], geo["kx"], geo["ky"], geo["zmax"]
    prisms = []
    for pts, lit, _age in list(geo["polys"]) + list(geo["cols"]):
        P = [(d0 + px / kx, zmax - py / ky) for px, py in pts]
        dmin = max(min(p[0] for p in P), 0.0)
        dmax = min(max(p[0] for p in P), length)
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
            ring = None
            for a, b in zip(zs[0::2], zs[1::2]):
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
