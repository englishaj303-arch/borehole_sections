"""Interactive editing of the 2D geological section (the panel and the mouse tools).

The automatic correlation is the starting point, the geologist has the last word:

  Move boundary   press on a layer boundary and drag it up or down (a smooth bulge that is zero
                  at the boreholes; the neighbouring boundaries stay where they are);
  Links           the correlation links between neighbouring boreholes are drawn as lines: click
                  one to select it, then lock / unlock it or remove / restore it; drag from a
                  layer of one borehole to a layer of the neighbouring one to make a link;
  Lens            click a lens (or a layer of a borehole that has one) to suppress it - the layer
                  then ends right next to its borehole - or to bring it back; click a hand-drawn
                  lens to delete it;
  Add lens        drag from the left tip to the right tip of a new lens (lithology and thickness
                  are set in the panel);
  Undo / Redo     every change is one step (Ctrl+Z, Ctrl+Y); the edits can be saved to a file.

The edits (edits.SectionEdits) are kept apart from the geometry: the section is built again from
the data and the edits after every change, so a Regenerate keeps them.
"""
import contextlib
import os
import shutil
import tempfile

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QKeySequence, QPainterPath, QPen, QPolygonF, QShortcut
from qgis.PyQt.QtCore import QPointF
from qgis.PyQt.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QApplication, QDialog, QDialogButtonBox, QGridLayout, QLineEdit, QSizePolicy, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGraphicsPathItem, QGraphicsView, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QToolButton,
    QVBoxLayout,
)

from . import core
from . import edits as _edits

MAX_HISTORY = 100        # undo steps kept (every step is a whole copy of the edits)

HINTS = {
    "pan": "Pan / zoom only. Pick a tool to edit the section.",
    "shift": "Press on a layer boundary and drag it up or down, release to apply. "
             "The boundary bulges smoothly and stays fixed at the boreholes.",
    "link": "Click a correlation line to select it. Drag from a layer of one borehole to a "
            "layer of the neighbouring borehole to make a link. Blue = manual, orange = locked, "
            "grey = automatic, red dashed = removed.",
    "lens": "Click a lens (or its layer in the borehole) to suppress or restore it. "
            "Click a hand-drawn lens to delete it.",
    "addlens": "Drag from the left tip to the right tip of the new lens, then choose "
               "its lithology and thickness.",
}


class SectionEditor(QGroupBox):
    def __init__(self, viewer, proj):
        super().__init__("Edit section (geologist's corrections)")
        self.viewer = viewer
        self.proj = proj
        self.state = _edits.SectionEdits()
        self.hist = [self.state.copy()]
        self.pos = 0
        self.tool = "pan"
        self.sel = None                  # (k, key a, key b) of the selected correlation link
        self.drag = None
        self.sel_link = None
        self._tmp = []                   # temporary graphics (preview, selection) of the scene
        self._busy = False

        lay = QVBoxLayout(self)
        row = QGridLayout()                      # 3 + 2 buttons: the panel stays narrow
        self.tool_btns = {}
        grp = QButtonGroup(self)
        for n, (key, text, tip) in enumerate((
                ("pan", "Pan", "Pan and zoom only (no editing)"),
                ("shift", "Move boundary", "Drag a layer boundary up or down"),
                ("link", "Links", "Correlation links: select, lock, remove, make by hand"),
                ("lens", "Lens", "Suppress / restore a lens, delete a hand-drawn lens"),
                ("addlens", "Add lens", "Draw a lens by hand"))):
            b = QToolButton()
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setChecked(key == "pan")
            grp.addButton(b)
            b.clicked.connect(lambda _c=False, k=key: self.set_tool(k))
            row.addWidget(b, n // 3, n % 3)
            self.tool_btns[key] = b
        grp.setExclusive(True)
        lay.addLayout(row)
        self.lbl_hint = QLabel(HINTS["pan"])
        self.lbl_hint.setWordWrap(True)
        lay.addWidget(self.lbl_hint)

        row = QHBoxLayout()
        self.btn_undo = QPushButton("Undo")
        self.btn_redo = QPushButton("Redo")
        self.btn_reset = QPushButton("Reset edits")
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo.clicked.connect(self.redo)
        self.btn_reset.clicked.connect(self.reset)
        for b in (self.btn_undo, self.btn_redo, self.btn_reset):
            row.addWidget(b)
        lay.addLayout(row)

        box = QGroupBox("Correlation links")
        bl = QVBoxLayout(box)
        self.chk_links = QCheckBox("Show correlation links")
        self.chk_links.toggled.connect(self._links_visibility)
        bl.addWidget(self.chk_links)
        row = QHBoxLayout()
        self.btn_lock = QPushButton("Lock / unlock")
        self.btn_lock.setToolTip("A locked link is kept by the automatic correlation, whatever "
                                 "else changes (layers, faults, other edits).")
        self.btn_remove = QPushButton("Remove / restore")
        self.btn_remove.setToolTip("A removed link is never made by the automatic correlation: "
                                   "the two layers end as lenses instead.")
        self.btn_lock.clicked.connect(self.toggle_lock)
        self.btn_remove.clicked.connect(self.toggle_remove)
        row.addWidget(self.btn_lock)
        row.addWidget(self.btn_remove)
        bl.addLayout(row)
        row = QHBoxLayout()
        b1, b2 = QPushButton("Lock all links"), QPushButton("Unlock all")
        b1.clicked.connect(self.lock_all)
        b2.clicked.connect(self.unlock_all)
        row.addWidget(b1)
        row.addWidget(b2)
        bl.addLayout(row)
        self.lbl_sel = QLabel("No link selected.")
        self.lbl_sel.setWordWrap(True)
        bl.addWidget(self.lbl_sel)
        lay.addWidget(box)

        # a hand-drawn lens asks for its lithology and thickness when the drag ends
        self._lithos = list(core.lithologies([p.bh for p in proj]))
        zs = [p.z_at(0.0) for p in proj] + [p.z_at(p.bh.depth) for p in proj]
        zrange = (max(zs) - min(zs)) if zs else 10.0
        self._lens_thick = min(1000.0, max(0.5, round(zrange / 12.0, 1)))
        self._lens_litho = self._lithos[0] if self._lithos else ""

        row = QHBoxLayout()
        bs, bo = QPushButton("Save edits..."), QPushButton("Load edits...")
        bs.clicked.connect(self.save_edits)
        bo.clicked.connect(self.load_edits)
        row.addWidget(bs)
        row.addWidget(bo)
        lay.addLayout(row)
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)

        for seq, fn in (("Ctrl+Z", self._on_undo), ("Ctrl+Y", self._on_redo),
                        ("Ctrl+Shift+Z", self._on_redo), ("Delete", self._on_delete)):
            QShortcut(QKeySequence(seq), viewer).activated.connect(fn)
        self._refresh()

    # ------------------------------------------------------------ helpers
    @property
    def scene(self):
        return self.viewer.scene

    @property
    def geo(self):
        return getattr(self.viewer.scene, "geo", None) or {}

    def tool_active(self):
        return self.tool != "pan" and self.isEnabled()

    def dragging(self):
        return self.drag is not None

    def _say(self, text):
        self.lbl_status.setText(text)

    def _scale(self):
        return max(self.viewer.view.transform().m11(), 1e-6)

    def _d_z(self, x, y):
        g = self.geo
        return x / g["kx"] + g["d0"], g["zmax"] - y / g["ky"]

    def _x_y(self, d, z):
        g = self.geo
        return (d - g["d0"]) * g["kx"], (g["zmax"] - z) * g["ky"]

    def set_tool(self, tool):
        self.cancel()
        self.tool = tool
        self.tool_btns[tool].setChecked(True)
        self.lbl_hint.setText(HINTS[tool])
        view = self.viewer.view
        if tool == "pan":
            view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            view.viewport().unsetCursor()
        else:
            view.setDragMode(QGraphicsView.DragMode.NoDrag)
            view.viewport().setCursor(Qt.CursorShape.CrossCursor)
        if tool == "link":
            self.chk_links.setChecked(True)
        self._links_visibility()

    def _links_visibility(self, *_):
        show = self.chk_links.isChecked()
        for it in self.geo.get("link_items", []):
            it.setVisible(show)
        if self.drag is None:                  # the yellow selection follows the links
            self._clear_tmp()
            self._highlight_selected()

    def _clear_tmp(self):
        for it in self._tmp:
            if it.scene() is not None:
                it.scene().removeItem(it)
        self._tmp = []

    def _add_tmp(self, path, color, width, dashed=False, z=8):
        it = QGraphicsPathItem(path)
        pen = QPen(color, width)
        pen.setCosmetic(False)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        it.setPen(pen)
        it.setZValue(z)
        self.scene.addItem(it)
        self._tmp.append(it)
        return it

    # ------------------------------------------------------- scene changes
    def scene_changed(self):
        """The viewer built a new scene: drop the temporary items, redraw the selection."""
        self._tmp = []
        self.drag = None
        self._links_visibility()
        miss = self.geo.get("edits_missing", 0)
        if miss:
            self._say(f"{miss} edit(s) no longer match the section (the layers or the correlation "
                      "changed) and were not applied.")

    def _highlight_selected(self):
        self.sel_link = None
        if self.sel is not None:
            for lk in self.geo.get("links", []):
                if (lk["k"], lk["a"], lk["b"]) == self.sel:
                    self.sel_link = lk
                    if self.chk_links.isChecked():       # the links are drawn: so is the selection
                        path = QPainterPath()
                        path.moveTo(*lk["p1"])
                        path.lineTo(*lk["p2"])
                        self._add_tmp(path, QColor(255, 210, 0), 5.0, z=5.5)
                    break
            if self.sel_link is None:
                self.sel = None
        self._describe_selection()

    def _layer_text(self, key):
        for cl in self.geo.get("col_layers", []):
            if cl["key"] == key:
                return f"{key[0]} {cl['litho']} ({key[1]:g}-{key[2]:g} m)"
        return f"{key[0]} ({key[1]:g}-{key[2]:g} m)"

    def _describe_selection(self):
        lk = getattr(self, "sel_link", None)
        self.btn_lock.setEnabled(lk is not None and lk["state"] in ("auto", "locked"))
        self.btn_remove.setEnabled(lk is not None)
        if lk is None:
            self.lbl_sel.setText("No link selected.")
            return
        st = {"auto": "automatic", "locked": "locked", "manual": "manual (locked)",
              "removed": "removed"}[lk["state"]]
        self.lbl_sel.setText(f"{self._layer_text(lk['a'])}  <->  {self._layer_text(lk['b'])}\n[{st}]")

    def _refresh(self):
        self.btn_undo.setEnabled(self.pos > 0)
        self.btn_redo.setEnabled(self.pos < len(self.hist) - 1)
        self.btn_reset.setEnabled(not self.state.is_empty())
        self.btn_undo.setToolTip("Undo (Ctrl+Z)")
        self.btn_redo.setToolTip("Redo (Ctrl+Y)")

    def mark_stale(self):
        self.setEnabled(False)

    # ------------------------------------------------------------- history
    def _commit(self, new, message=""):
        """Apply a changed copy of the edits: rebuild the section, remember it for Undo."""
        if self._busy:
            return False
        self._busy = True
        old = self.state
        self.state = new
        try:
            ok = self.viewer.regenerate(keep_view=True)
        finally:
            self._busy = False
        if not ok:
            self.state = old
            self.viewer.regenerate(keep_view=True, quiet=True)   # back to what worked, no second message
            return False
        del self.hist[self.pos + 1:]
        self.hist.append(new.copy())
        self.pos += 1
        if len(self.hist) > MAX_HISTORY:           # the oldest steps go: a long session stays small
            drop = len(self.hist) - MAX_HISTORY
            del self.hist[:drop]
            self.pos -= drop
        self._refresh()
        if message:
            self._say(message)
        return True

    def _goto(self, pos, message):
        if self._busy or not (0 <= pos < len(self.hist)):
            return
        self._busy = True
        old, old_pos = self.state, self.pos
        self.state, self.pos = self.hist[pos].copy(), pos
        try:
            ok = self.viewer.regenerate(keep_view=True)
        finally:
            self._busy = False
        if not ok:
            self.state, self.pos = old, old_pos
            self.viewer.regenerate(keep_view=True, quiet=True)
        self._refresh()
        if ok:
            self._say(message)

    def undo(self):
        if self.isEnabled() and self.pos > 0:
            self._goto(self.pos - 1, "Undone.")

    def redo(self):
        if self.isEnabled() and self.pos < len(self.hist) - 1:
            self._goto(self.pos + 1, "Redone.")

    def reset(self):
        if self.state.is_empty():
            return
        if QMessageBox.question(self.viewer, "Reset edits",
                                "Remove all your edits of this section? (Undo brings them back.)"
                                ) != QMessageBox.StandardButton.Yes:
            return
        self.sel = None
        self._commit(_edits.SectionEdits(), "All edits removed.")

    # --------------------------------------------------------------- files
    def save_edits(self):
        path, _ = QFileDialog.getSaveFileName(self.viewer, "Save section edits", "",
                                              "Section edits (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        tmp = None
        try:
            # a NEW file with an unpredictable name in the same folder (a ready link called
            # <name>.json.tmp could not redirect the writing), then put in place: whole file or
            # the old one, never half
            fd, tmp = tempfile.mkstemp(prefix=".edits_", suffix=".tmp",
                                       dir=os.path.dirname(os.path.abspath(path)))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.state.to_json())
            if os.path.exists(path):
                with contextlib.suppress(OSError):
                    shutil.copymode(path, tmp)         # the file keeps its permissions
            os.replace(tmp, path)
        except (OSError, ValueError) as e:
            if tmp:
                with contextlib.suppress(OSError):
                    os.remove(tmp)
            QMessageBox.warning(self.viewer, "Save edits", f"Could not write the file:\n{e}")
            return
        self._say(f"{self.state.count()} edit(s) saved.")

    def load_edits(self):
        path, _ = QFileDialog.getOpenFileName(self.viewer, "Load section edits", "",
                                              "Section edits (*.json)")
        if not path:
            return
        try:
            new = _edits.SectionEdits.from_json(_edits.read_edit_file(path))
        except (OSError, ValueError, KeyError, TypeError) as e:
            QMessageBox.warning(self.viewer, "Load edits", f"Could not read the edits:\n{e}")
            return
        self.sel = None
        self._commit(new, f"{new.count()} edit(s) loaded.")

    # -------------------------------------------------------------- hit tests
    def _col_at(self, p):
        for cl in self.geo.get("col_layers", []):
            if core.point_in_polygon((p.x(), p.y()), cl["quad"]):
                return cl
        return None

    def _link_at(self, p, tol):
        best, bd = None, tol
        for lk in self.geo.get("links", []):
            d = _edits.dist_to_segment((p.x(), p.y()), lk["p1"], lk["p2"])
            if d <= bd:
                best, bd = lk, d
        return best

    def _poly_at(self, p):
        g = self.geo
        for poly, meta in zip(g.get("polys", []), g.get("meta", [])):
            if core.point_in_polygon((p.x(), p.y()), poly[0]):
                return poly, meta
        return None, None

    # ------------------------------------------------------------ mouse tools
    def cancel(self):
        self.drag = None
        self._clear_tmp()
        self._highlight_selected_only()

    def _highlight_selected_only(self):
        if self.sel is not None:
            self._highlight_selected()

    def press(self, p):
        if self._busy or not self.geo:           # no section drawn (the last build failed)
            return True
        fn = getattr(self, f"_press_{self.tool}", None)
        if fn is not None:
            fn(p)
        return True

    def move(self, p):
        fn = getattr(self, f"_move_{self.tool}", None)
        if fn is not None and self.drag is not None and self.geo:
            fn(p)

    def release(self, p):
        fn = getattr(self, f"_release_{self.tool}", None)
        if fn is not None and self.drag is not None and self.geo:
            fn(p)
        self.drag = None

    # --- Move boundary
    def _press_shift(self, p):
        polys = self.geo.get("polys", [])
        ct = _edits.find_contact(polys, p.x(), p.y(), 8.0 / self._scale())
        if ct is None:
            self._say("No layer boundary here - press right on a boundary between two layers.")
            return
        up, down = _edits.shift_limits(ct)
        if up <= 0.5 and down <= 0.5:
            self._say("This boundary cannot be moved (the layers next to it are too thin).")
            return
        self._clear_tmp()
        self._highlight_selected_only()
        item = self._add_tmp(QPainterPath(), QColor(255, 120, 0), 3.0)
        self.drag = {"ct": ct, "y0": p.y(), "x0": p.x(), "up": up, "down": down, "dy": 0.0,
                     "item": item, "la": polys[ct["A"]][1], "lb": polys[ct["B"]][1]}
        self._move_shift(p)

    def _move_shift(self, p):
        d = self.drag
        d["dy"] = max(-d["up"], min(d["down"], p.y() - d["y0"]))
        pts = _edits.shift_profile(d["ct"], d["dy"])
        path = QPainterPath()
        path.moveTo(*pts[0])
        for q in pts[1:]:
            path.lineTo(*q)
        d["item"].setPath(path)
        self._say(f"Boundary {d['la']} / {d['lb']}: {-d['dy'] / self.geo['ky']:+.2f} m "
                  "(release to apply, right button cancels)")

    def _release_shift(self, p):
        d = self.drag
        dz = -d["dy"] / self.geo["ky"]
        if abs(d["dy"]) < 1.0 / self._scale() or abs(dz) < 1e-3:
            self._clear_tmp()
            self._highlight_selected_only()
            return
        ct = d["ct"]
        i = min(range(len(ct["xs"])), key=lambda n: abs(ct["xs"][n] - d["x0"]))
        dd, zz = self._d_z(ct["xs"][i], ct["c"][i])
        new = self.state.copy()
        new.shifts.append({"d": dd, "z": zz, "dz": dz, "la": d["la"], "lb": d["lb"]})
        self._commit(new, f"Boundary {d['la']} / {d['lb']} moved by {dz:+.2f} m.")

    # --- Links
    def _press_link(self, p):
        cl = self._col_at(p)
        if cl is not None:
            path = QPainterPath()
            path.moveTo(p)
            path.lineTo(p)
            self.drag = {"src": cl, "item": self._add_tmp(path, QColor(0, 110, 220), 2.4, True)}
            return
        lk = self._link_at(p, 7.0 / self._scale())
        self.sel = None if lk is None else (lk["k"], lk["a"], lk["b"])
        self._clear_tmp()
        self._highlight_selected()

    def _move_link(self, p):
        src = self.drag["src"]
        quad = src["quad"]
        cx = sum(q[0] for q in quad) / 4.0
        cy = sum(q[1] for q in quad) / 4.0
        path = QPainterPath()
        path.moveTo(cx, cy)
        path.lineTo(p)
        self.drag["item"].setPath(path)

    def _release_link(self, p):
        src, dst = self.drag["src"], self._col_at(p)
        self._clear_tmp()
        self._highlight_selected_only()
        if dst is None or dst["i"] == src["i"]:
            return
        if abs(dst["i"] - src["i"]) != 1:
            self._say("Link layers of two NEIGHBOURING boreholes.")
            return
        left, right = (src, dst) if src["i"] < dst["i"] else (dst, src)
        k = left["i"]
        if k in (self.viewer.applied_faults or {}):
            self._say("These boreholes are separated by a fault - blocks are not correlated.")
            return
        key = (left["key"], right["key"])
        new = self.state.copy()
        new.forbidden.discard(key)
        new.forced[key] = "manual"
        self.sel = (k, key[0], key[1])
        note = "" if left["litho"] == right["litho"] else \
            f" Different lithologies: the link is drawn in the colour of {left['litho']}."
        self._commit(new, "Correlation link made." + note)

    def _selected(self):
        lk = getattr(self, "sel_link", None)
        if lk is None:
            self._say("Select a correlation link first (tool Links, click a line).")
        return lk

    def toggle_lock(self):
        lk = self._selected()
        if lk is None:
            return
        key = (lk["a"], lk["b"])
        new = self.state.copy()
        if lk["state"] == "auto":
            new.forced[key] = "locked"
            msg = "Link locked."
        elif lk["state"] == "locked":
            new.forced.pop(key, None)
            msg = "Link unlocked."
        elif lk["state"] == "manual":
            self._say("A manual link is always kept; use Remove to delete it.")
            return
        else:
            self._say("Restore the removed link first.")
            return
        self._commit(new, msg)

    def _typing(self):
        """True while a text / number field has the focus (the key belongs to the field) or
        another window is active (the shortcut is window-wide)."""
        if not self.viewer.isActiveWindow():
            return True
        fw = QApplication.focusWidget()
        if fw is None or fw is self.viewer or fw is self.viewer.view \
                or fw is self.viewer.view.viewport():
            return False
        # a button of this panel is fine; any other widget (the table of faults, a check box,
        # a field) keeps its own keys
        return not (isinstance(fw, QAbstractButton) and self.isAncestorOf(fw))

    def _on_undo(self):
        if not self._typing():
            self.undo()

    def _on_redo(self):
        if not self._typing():
            self.redo()

    def _on_delete(self):
        if self._typing():
            return
        if self.tool == "link" and self.isEnabled():
            self.toggle_remove()

    def toggle_remove(self):
        lk = self._selected()
        if lk is None:
            return
        key = (lk["a"], lk["b"])
        new = self.state.copy()
        if lk["state"] == "removed":
            new.forbidden.discard(key)
            msg = "Link restored."
        else:
            new.forbidden.add(key)
            new.forced.pop(key, None)
            msg = "Link removed - the two layers are no longer correlated (they end as lenses or a lateral change)."
        self._commit(new, msg)

    def lock_all(self):
        new = self.state.copy()
        n = 0
        for lk in self.geo.get("links", []):
            if lk["state"] == "auto":
                new.forced[(lk["a"], lk["b"])] = "locked"
                n += 1
        if n:
            self._commit(new, f"{n} link(s) locked.")
        else:
            self._say("Nothing to lock.")

    def unlock_all(self):
        new = self.state.copy()
        n = 0
        for key in [k for k, v in new.forced.items() if v == "locked"]:
            del new.forced[key]
            n += 1
        if n:
            self._commit(new, f"{n} link(s) unlocked.")
        else:
            self._say("No locked links.")

    # --- Lens (suppress / restore / delete)
    def _press_lens(self, p):
        poly, meta = self._poly_at(p)
        key = None
        if meta is not None and meta.get("kind") == "manual":
            new = self.state.copy()
            if 0 <= meta["idx"] < len(new.lenses):
                del new.lenses[meta["idx"]]
                self._commit(new, "Hand-drawn lens deleted.")
            return
        if meta is not None and meta.get("kind") in ("lens", "trans", "fan", "stack"):
            key = meta.get("layer")
        elif meta is not None and meta.get("kind") == "pair":
            self._say("This layer is correlated between the boreholes. Remove its link "
                      "(tool Links) to make it a lens.")
            return
        if key is None:
            cl = self._col_at(p)
            if cl is not None:
                linked = any(cl["key"] in (lk["a"], lk["b"]) for lk in self.geo.get("links", [])
                             if lk["state"] != "removed")
                if linked:
                    self._say("This layer is correlated with a neighbouring borehole. Remove "
                              "its link (tool Links) to make it a lens.")
                    return
                key = cl["key"]
        if key is None:
            self._say("Click on a lens or on a layer of a borehole.")
            return
        new = self.state.copy()
        if key in new.short:
            new.short.discard(key)
            msg = f"Lens of {self._layer_text(key)} restored."
        else:
            new.short.add(key)
            msg = f"Lens of {self._layer_text(key)} suppressed - the layer ends next to its borehole."
        self._commit(new, msg)

    # --- Add lens
    def _press_addlens(self, p):
        if not self._lithos:
            return
        self._clear_tmp()
        self._highlight_selected_only()
        self.drag = {"p1": (p.x(), p.y()),
                     "item": self._add_tmp(QPainterPath(), QColor(0, 110, 220), 2.0, True)}

    def _move_addlens(self, p):
        p1 = self.drag["p1"]
        ky = self.geo["ky"]
        pts = _edits.lens_outline(p1, (p.x(), p.y()), self._lens_thick * ky)
        path = QPainterPath()
        path.addPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))
        path.closeSubpath()
        self.drag["item"].setPath(path)

    def _release_addlens(self, p):
        p1, p2 = self.drag["p1"], (p.x(), p.y())
        self.drag = None              # the dialog below is modal: no move event may use the preview
        self._clear_tmp()
        self._highlight_selected_only()
        if abs(p2[0] - p1[0]) < 12.0 / self._scale():
            self._say("Drag from the left to the right tip of the lens.")
            return
        if p2[0] < p1[0]:
            p1, p2 = p2, p1
        d1, z1 = self._d_z(*p1)
        d2, z2 = self._d_z(*p2)
        answer = self._ask_lens()
        if answer is None:
            self._say("Lens cancelled.")
            return
        self._lens_litho, self._lens_thick = answer
        new = self.state.copy()
        new.lenses.append({"d1": d1, "z1": z1, "d2": d2, "z2": z2,
                           "thick": self._lens_thick, "litho": self._lens_litho})
        self._commit(new, f"Lens of {self._lens_litho} added.")

    def _ask_lens(self):
        """Small dialog: lithology and thickness of the lens that was just drawn."""
        dlg = QDialog(self.viewer)
        dlg.setWindowTitle("New lens")
        form = QFormLayout(dlg)
        cb = QComboBox()
        cb.addItems(self._lithos)
        if self._lens_litho in self._lithos:
            cb.setCurrentIndex(self._lithos.index(self._lens_litho))
        sp = QDoubleSpinBox()
        sp.setRange(0.05, 1000.0)
        sp.setDecimals(2)
        sp.setSuffix(" m")
        sp.setValue(self._lens_thick)
        form.addRow("Lithology", cb)
        form.addRow("Thickness", sp)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return cb.currentText(), sp.value()
