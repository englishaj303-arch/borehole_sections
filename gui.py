from qgis.PyQt.QtCore import Qt, QEvent, QRectF, QSize, QSortFilterProxyModel, QTimer, pyqtSignal
from qgis.PyQt.QtGui import (QColor, QGuiApplication, QIcon, QImage, QPainter, QPdfWriter,
                             QStandardItem, QStandardItemModel,
                             QPageSize, QPageLayout, QPixmap)
from qgis.PyQt.QtSvg import QSvgGenerator
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget, QGroupBox, QFormLayout,
    QComboBox, QCheckBox, QPushButton, QLabel, QPlainTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QDoubleSpinBox, QGraphicsView, QFileDialog,
    QMessageBox, QSplitter, QSpinBox, QScrollArea, QFrame, QListWidget, QListWidgetItem,
    QApplication, QInputDialog, QLineEdit, QListView,
)
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsGeometry,
    QgsPointXY, QgsProject, QgsRasterLayer,
)
from qgis.gui import QgsColorButton, QgsFileWidget, QgsMapTool, QgsRubberBand

try:
    from qgis.gui import QgsCollapsibleGroupBox
except ImportError:                     # not expected; the groups are then plain boxes
    QgsCollapsibleGroupBox = None

import contextlib
import os
import math
from dataclasses import replace

from . import core, logs, model3d, patterns, viewer3d, viewer3d_window
from . import edits as _edits
from .section_editor import SectionEditor


@contextlib.contextmanager
def _busy():
    """Wait cursor that is always restored, whatever happens inside."""
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


# ---------------------------------------------------------------- map tool
class _PatternPopup(QFrame):
    """Drop-down of PatternCombo: a search line above the list of the patterns (with icons).
    Typing filters the list (case-insensitive, anywhere in the code or the name); Up / Down
    move in the list, Enter or a click chooses, Esc closes."""

    def __init__(self, combo):
        super().__init__(combo, Qt.WindowType.Popup)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._combo = combo
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search pattern (code or name)...")
        self.search.setClearButtonEnabled(True)
        lay.addWidget(self.search)
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(combo.model())
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterRole(Qt.ItemDataRole.DisplayRole)
        self.view = QListView()
        self.view.setModel(self._proxy)
        self.view.setIconSize(QSize(48, 36))
        self.view.setUniformItemSizes(True)
        self.view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        lay.addWidget(self.view)
        self.search.textChanged.connect(self._filter)
        self.search.installEventFilter(self)
        self.view.clicked.connect(self._choose)
        self.view.activated.connect(self._choose)

    def _filter(self, text):
        self._proxy.setFilterFixedString(text.strip())
        if self._proxy.rowCount():
            self.view.setCurrentIndex(self._proxy.index(0, 0))
            self.view.scrollToTop()

    def _choose(self, index):
        if not index.isValid():
            return
        row = self._proxy.mapToSource(index).row()
        self.close()
        self._combo.setCurrentIndex(row)
        self._combo.activated.emit(row)

    def eventFilter(self, obj, ev):
        if obj is self.search and ev.type() == QEvent.Type.KeyPress:
            key = ev.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                self.view.setFocus()
                QApplication.sendEvent(self.view, ev)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._choose(self.view.currentIndex())
                return True
        return super().eventFilter(obj, ev)

    def popup(self):
        combo = self._combo
        w = max(combo.width(), 420)
        h = 8 + self.search.sizeHint().height() + 4 + 12 * 40 + 6
        below = combo.mapToGlobal(combo.rect().bottomLeft())
        above = combo.mapToGlobal(combo.rect().topLeft())
        scr = combo.screen().availableGeometry() if combo.screen() else None
        x, y = below.x(), below.y()
        if scr is not None:
            if y + h > scr.bottom():
                y = above.y() - h if above.y() - h >= scr.top() else max(scr.top(), scr.bottom() - h)
            x = max(scr.left(), min(x, scr.right() - w))
        self.setGeometry(x, y, w, h)
        self.search.clear()
        self._proxy.setFilterFixedString("")
        cur = self._proxy.index(max(combo.currentIndex(), 0), 0)
        self.view.setCurrentIndex(cur)
        self.show()
        self.view.scrollTo(cur, QListView.ScrollHint.PositionAtCenter)
        self.search.setFocus()


class PatternCombo(QComboBox):
    """Combo box of the lithologic patterns: the closed box shows the icon and the name, the
    drop-down has a search line (see _PatternPopup) above the list."""

    def showPopup(self):
        if self.model() is None or self.model().rowCount() == 0:
            return
        pop = getattr(self, "_popup", None)
        if pop is None:
            pop = self._popup = _PatternPopup(self)
        pop.popup()

    def hidePopup(self):
        pop = getattr(self, "_popup", None)
        if pop is not None and pop.isVisible():
            pop.close()
        super().hidePopup()


class SectionLineTool(QgsMapTool):
    lineFinished = pyqtSignal(list)
    cancelled = pyqtSignal()
    aborted = pyqtSignal()      # tool was replaced (e.g. by Pan) before the line was finished

    def __init__(self, canvas):
        super().__init__(canvas)
        self.points = []
        self._finished = False
        self.rb = QgsRubberBand(canvas, Qgis.GeometryType.Line)
        self.rb.setColor(QColor(220, 0, 0))
        self.rb.setWidth(2)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _redraw(self, extra=None):
        self.rb.reset(Qgis.GeometryType.Line)
        pts = list(self.points) + ([extra] if extra is not None else [])
        for i, p in enumerate(pts):
            self.rb.addPoint(QgsPointXY(p), i == len(pts) - 1)

    def canvasMoveEvent(self, e):
        if self.points:
            self._redraw(e.mapPoint())

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.points.append(QgsPointXY(e.mapPoint()))
            self._redraw()
        elif e.button() == Qt.MouseButton.RightButton:
            self._finished = True
            pts, self.points = self.points, []
            self.rb.reset(Qgis.GeometryType.Line)
            if len(pts) >= 2:
                self.lineFinished.emit(pts)
            else:
                self.cancelled.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self._finished = True
            self.points = []
            self.rb.reset(Qgis.GeometryType.Line)
            self.cancelled.emit()

    def activate(self):
        self._finished = False
        self.points = []
        super().activate()

    def deactivate(self):
        unfinished = not self._finished
        self.points = []
        self.rb.reset(Qgis.GeometryType.Line)
        super().deactivate()
        if unfinished:
            self.aborted.emit()


# ------------------------------------------------------------ section view
class _View(QGraphicsView):
    """Section view: wheel = zoom; with an editing tool active the left button belongs to the
    tool (SectionEditor) and the middle button pans."""
    editor = None
    _pan_last = None

    def wheelEvent(self, e):
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.scale(f, f)

    def _tool(self):
        return self.editor is not None and self.editor.tool_active()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton:
            self._pan_last = e.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            e.accept()
            return
        if self._tool():
            if e.button() == Qt.MouseButton.RightButton:
                self.editor.cancel()
                e.accept()
                return
            if e.button() == Qt.MouseButton.LeftButton:
                self.editor.press(self.mapToScene(e.position().toPoint()))
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._pan_last is not None:
            d = e.position() - self._pan_last
            self._pan_last = e.position()
            hb, vb = self.horizontalScrollBar(), self.verticalScrollBar()
            hb.setValue(hb.value() - int(d.x()))
            vb.setValue(vb.value() - int(d.y()))
            e.accept()
            return
        if self._tool() and self.editor.dragging():
            if not (e.buttons() & Qt.MouseButton.LeftButton):
                self.editor.cancel()              # the release happened outside the window
                e.accept()
                return
            self.editor.move(self.mapToScene(e.position().toPoint()))
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton and self._pan_last is not None:
            self._pan_last = None
            if self._tool():
                self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.viewport().unsetCursor()
            e.accept()
            return
        if self._tool() and e.button() == Qt.MouseButton.LeftButton and self.editor.dragging():
            self.editor.release(self.mapToScene(e.position().toPoint()))
            e.accept()
            return
        super().mouseReleaseEvent(e)


class SectionViewer(QDialog):
    def __init__(self, scene, parent=None, proj=None, rebuild=None, strat0=True,
                 title="Geological cross section"):
        super().__init__(parent)
        self.setWindowTitle(title)
        editable = proj is not None and rebuild is not None and len(proj) > 1
        # the window with the editing panel is wider: the panel needs ~620 px, the section
        # keeps about the width it had
        _fit_to_screen(self, 1900 if editable else 1550, 780, 0.97 if editable else 0.9)
        self.scene = scene
        self.rebuild = rebuild
        self.view = _View(scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.split = split
        split.addWidget(self.view)
        self.panel = None
        self.btn_regen = None
        self.editor = None
        self.applied_faults = None                # the faults of the last (re)generation
        self._strat0 = strat0
        if proj is not None and rebuild is not None and len(proj) > 1:
            side = QWidget()
            sl = QVBoxLayout(side)
            self.chk_v_strat = QCheckBox("Show stratigraphic boundaries")
            self.chk_v_strat.setChecked(strat0)
            self.chk_v_ground = QCheckBox("Show ground surface line (green)")
            self.chk_v_ground.setChecked(False)
            self.chk_v_hatch = QCheckBox("Lithologic patterns (FGDC)")
            self.chk_v_hatch.setChecked(False)
            self.chk_v_strat.toggled.connect(self._apply_visibility)
            self.chk_v_ground.toggled.connect(self._apply_visibility)
            self.chk_v_hatch.toggled.connect(self._apply_visibility)
            sl.addWidget(self.chk_v_strat)
            sl.addWidget(self.chk_v_ground)
            sl.addWidget(self.chk_v_hatch)
            self.editor = SectionEditor(self, proj)
            self.view.editor = self.editor
            sl.addWidget(self.editor)
            self.panel = FaultPanel(proj)
            sl.addWidget(self.panel)
            btn = QPushButton("Regenerate section")     # placed in the bottom row (below)
            btn.clicked.connect(self.regenerate)
            self.btn_regen = btn
            sl.addStretch()
            scroll = QScrollArea()                 # the panel with the editing tools is tall
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(side)
            scroll.setMinimumWidth(430)
            split.addWidget(scroll)
            split.setChildrenCollapsible(False)
            split.setStretchFactor(0, 3)
            split.setStretchFactor(1, 2)
            split.setSizes([max(600, self.width() - 470), 470])
        lay.addWidget(split, 1)
        row = QHBoxLayout()
        for label, fn in (("Fit", self.fit), ("Export PNG", self.export_png),
                          ("Export SVG", self.export_svg), ("Export PDF", self.export_pdf)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addWidget(QLabel("Mouse wheel = zoom, drag = pan (editing tools: middle button = pan)"))
        row.addStretch()
        if self.panel is not None and self.btn_regen is not None:
            row.addWidget(self.btn_regen)               # always visible, not in the scrolling panel
        lay.addLayout(row)
        self._apply_visibility()
        if self.editor is not None:
            self.editor.scene_changed()

    def _apply_visibility(self, *_):
        geo = getattr(self.scene, "geo", None)
        if not geo:
            return
        if self.panel is None:      # single-borehole section: no side panel
            for it in geo.get("strat_items", []):
                it.setVisible(self._strat0)
            return
        for it in geo.get("strat_items", []):
            it.setVisible(self.chk_v_strat.isChecked())
        for it in geo.get("terrain_items", []):
            it.setVisible(self.chk_v_ground.isChecked())
        for it in geo.get("hatch_items", []):
            it.setVisible(self.chk_v_hatch.isChecked())

    def mark_stale(self):
        """The data were loaded again: Regenerate would redraw the OLD set of boreholes, so it is
        switched off; the drawing stays available (view, export) and a new section can be drawn."""
        if self.editor is not None:
            self.editor.mark_stale()
        if self.btn_regen is not None:
            self.btn_regen.setEnabled(False)
            self.btn_regen.setToolTip("The data were loaded again after this section was drawn. "
                                      "Draw the section again to use the new data.")
            self.btn_regen.setText("Regenerate section (data reloaded - draw the section again)")
        self.setWindowTitle(self.windowTitle() + " [old data]")

    def regenerate(self, keep_view=False, quiet=False):
        """Build the section again (faults of the panel + the edits of the editing tools).
        keep_view: the zoom and the position are kept (used after every edit).
        Returns False when the section could not be built."""
        if keep_view and self.editor is not None:
            faults = self.applied_faults          # an edit does not pick up unfinished fault inputs
        else:
            faults = (self.panel.faults() or None) if self.panel is not None else None
            self.applied_faults = faults
        edits = self.editor.state if self.editor is not None else None
        try:
            with _busy():
                scene = self.rebuild(faults, edits)
        except Exception as e:
            if not quiet:                       # quiet: the second try after a failed edit
                QMessageBox.critical(self, "Error", f"{type(e).__name__}: {e}")
            return False
        old = self.scene
        tr = self.view.transform()
        centre = self.view.mapToScene(self.view.viewport().rect().center())
        rect = old.sceneRect()
        self.scene = scene
        self.view.setScene(scene)
        if keep_view:
            scene.setSceneRect(rect.united(scene.sceneRect()))
            self.view.setTransform(tr)
            self.view.centerOn(centre)
        old.deleteLater()
        self._apply_visibility()
        if not keep_view:
            self.fit()
        if self.editor is not None:
            self.editor.scene_changed()
        return True

    def showEvent(self, e):
        super().showEvent(e)
        self.fit()
        QTimer.singleShot(0, self._trim_height)

    def _trim_height(self):
        """The section is wide and low: shrink the window so that no empty band is left below
        the drawing and below the buttons."""
        if self.isMaximized() or self.isFullScreen():
            return
        r = self.scene.sceneRect()
        vp = self.view.viewport()
        if r.width() <= 0 or r.height() <= 0 or vp.width() <= 0:
            return
        extra = vp.height() - int(vp.width() * r.height() / r.width()) - 6
        if extra > 20:
            side = getattr(self, "_side_panel", None)      # the side panel of the log windows
            floor = 300 if side is None else max(300, side.sizeHint().height() + 60)
            self.resize(self.width(), max(self.height() - extra, floor))
            self.fit()

    def fit(self):
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _ask(self, filt, ext):
        path, _ = QFileDialog.getSaveFileName(self, "Export", "", filt)
        if path and not path.lower().endswith(ext):
            path += ext
        return path

    MAX_PNG_PIXELS = 100_000_000          # 400 MB of ARGB32; more would freeze or crash QGIS
    MAX_PNG_SIDE = 30000                  # the raster paint engine works up to 32767 px

    def export_png(self):
        r = self.scene.sceneRect()
        if not (math.isfinite(r.width()) and math.isfinite(r.height())) \
                or r.width() <= 0 or r.height() <= 0:
            QMessageBox.warning(self, "Export", "The scene is empty - nothing to export.")
            return
        items = ["1x (screen size)", "2x (default)", "3x", "4x"]
        choice, ok = QInputDialog.getItem(self, "PNG export", "Resolution:", items, 1, False)
        if not ok:
            return
        scale = float(items.index(choice) + 1)
        # the largest scale that keeps the image within the pixel and side limits
        fit = min(scale, math.sqrt(self.MAX_PNG_PIXELS / (r.width() * r.height())),
                  self.MAX_PNG_SIDE / max(r.width(), r.height()))
        if fit < 0.25:
            QMessageBox.warning(self, "Export", "The section is too large for a PNG image even "
                                "at a low resolution. Export it to SVG or PDF instead.")
            return
        if fit < scale:
            w, h = int(r.width() * fit), int(r.height() * fit)
            if QMessageBox.question(
                    self, "Export",
                    f"At {scale:g}x the image would be too large. Export it at a reduced "
                    f"resolution ({w} x {h} px) instead?") != QMessageBox.StandardButton.Yes:
                return
            scale = fit
        path = self._ask("PNG (*.png)", ".png")
        if not path:
            return
        w, h = max(1, int(r.width() * scale)), max(1, int(r.height() * scale))
        try:
            img = QImage(w, h, QImage.Format.Format_ARGB32)
        except (MemoryError, ValueError, OverflowError):
            img = None
        if img is None or img.isNull():                  # no memory / over the QImage limit
            QMessageBox.warning(self, "Export", "The image could not be created (not enough "
                                "memory). Choose a lower resolution or export to SVG or PDF.")
            return
        img.fill(Qt.GlobalColor.white)
        p = QPainter(img)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            self.scene.render(p, QRectF(img.rect()), r)
        finally:
            p.end()
        if not img.save(path):
            QMessageBox.warning(self, "Export", f"Could not write the file:\n{path}")

    def export_svg(self):
        path = self._ask("SVG (*.svg)", ".svg")
        if not path:
            return
        r = self.scene.sceneRect()
        if not (math.isfinite(r.width()) and math.isfinite(r.height())) \
                or r.width() <= 0 or r.height() <= 0:
            QMessageBox.warning(self, "Export", "The scene is empty - nothing to export.")
            return
        existed = os.path.exists(path)
        try:
            with open(path, "ab"):                  # can the file be written (not locked)?
                pass
        except OSError as e:
            QMessageBox.warning(self, "Export", f"Could not write the file:\n{path}\n{e}")
            return
        g = QSvgGenerator()
        g.setFileName(path)
        g.setSize(r.size().toSize())
        g.setViewBox(QRectF(0, 0, r.width(), r.height()))
        p = QPainter(g)
        if not p.isActive():
            if not existed:                          # the probe above created an empty file
                with contextlib.suppress(OSError):
                    os.remove(path)
            QMessageBox.warning(self, "Export", f"Could not write the SVG:\n{path}")
            return
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            self.scene.render(p, QRectF(0, 0, r.width(), r.height()), r)
        finally:
            p.end()                                  # also when the rendering fails

    def export_pdf(self):
        path = self._ask("PDF (*.pdf)", ".pdf")
        if not path:
            return
        r = self.scene.sceneRect()
        if not (math.isfinite(r.width()) and math.isfinite(r.height())) \
                or r.width() <= 0 or r.height() <= 0:
            QMessageBox.warning(self, "Export", "The scene is empty - nothing to export.")
            return
        w = QPdfWriter(path)
        w.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        w.setPageOrientation(QPageLayout.Orientation.Portrait if r.height() > r.width()
                             else QPageLayout.Orientation.Landscape)
        p = QPainter()
        if not p.begin(w):
            QMessageBox.warning(self, "Export",
                                f"Could not write the PDF (is it open in another program?):\n{path}")
            return
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            target = QRectF(0, 0, p.device().width(), p.device().height())
            self.scene.render(p, target, self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        finally:
            p.end()                                  # also when the rendering fails


NO_STRAT = "(none)"


class LogViewer(SectionViewer):
    """Window of borehole profiles / sedimentary records with a side panel. Every drop-down
    changes only its own item; the log is redrawn by the Regenerate button:
      profiles:  Lithology (column of the profile + merging), Lithological description,
                 Stratigraphy (column of the stratigraphy column, none = hidden);
      records:   Lithology (column of the record + merging), Legend (text of the legend)."""

    def __init__(self, scene, parent, title, kind, columns, start, rebuild):
        super().__init__(scene, parent, title=title)
        self.log_rebuild = rebuild
        side = QWidget()
        sl = QVBoxLayout(side)

        def combo(items, current):
            cb = QComboBox()
            cb.addItems(items)
            cb.setCurrentIndex(max(items.index(current), 0) if current in items else 0)
            return cb

        box = QGroupBox("Lithology")
        form = QFormLayout(box)
        self.cb_prof = combo(columns, start["prof"])
        self.chk_merge = QCheckBox("Merge identical lithologies")
        self.chk_merge.setChecked(start["merge"])
        form.addRow("Lithology from column", self.cb_prof)
        form.addRow(self.chk_merge)
        sl.addWidget(box)
        box = QGroupBox("Lithological description" if kind == "profiles" else "Legend")
        form = QFormLayout(box)
        self.cb_desc = combo(columns, start["desc"])
        form.addRow("Description from column" if kind == "profiles" else "Legend text from column",
                    self.cb_desc)
        sl.addWidget(box)
        self.cb_strat = None
        if kind == "profiles":
            box = QGroupBox("Stratigraphy")
            form = QFormLayout(box)
            self.cb_strat = combo([NO_STRAT] + list(columns), start["strat"])
            form.addRow("Stratigraphy from column", self.cb_strat)
            sl.addWidget(box)
        self._side_panel = side
        btn = QPushButton("Regenerate")
        btn.clicked.connect(self.regenerate_log)
        self.btn_regen = btn                # mark_stale() disables it when the data are reloaded
        sl.addWidget(btn)
        sl.addStretch()
        side.setMinimumWidth(300)
        self.split.addWidget(side)
        self.split.setStretchFactor(0, 4)
        self.split.setStretchFactor(1, 1)
        # the log is redrawn only by the Regenerate button, not when a parameter is changed

    def regenerate_log(self, *_):
        try:
            scene = self.log_rebuild(self.cb_desc.currentText(), self.cb_prof.currentText(),
                                     self.chk_merge.isChecked(),
                                     self.cb_strat.currentText() if self.cb_strat else NO_STRAT)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        old = self.scene
        self.scene = scene
        self.view.setScene(scene)
        self.fit()
        old.deleteLater()


# ------------------------------------------------------------- main dialog
class FaultPanel(QGroupBox):
    """Table of faults shown next to the section (blocks are not correlated)."""

    def __init__(self, proj, parent=None):
        super().__init__("Faults", parent)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Tick the pairs of neighbouring boreholes separated by a fault, then press\n"
            "'Regenerate section'. Position = place of the fault between the boreholes\n"
            "at the surface."))
        n = len(proj) - 1
        self.tbl = QTableWidget(n, 6)
        self.tbl.setHorizontalHeaderLabels(
            ["Fault", "Between boreholes", "Distance [m]", "Position [% from left]",
             "Dip [deg]", "Fault plane dips"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.rows = []
        for k in range(n):
            a, b = proj[k], proj[k + 1]
            chk = QCheckBox()
            self.tbl.setCellWidget(k, 0, chk)
            for col, txt in ((1, f"{a.bh.name}  -  {b.bh.name}"),
                             (2, f"{a.dist:.0f} - {b.dist:.0f}  ({b.dist - a.dist:.0f})")):
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.tbl.setItem(k, col, it)
            pos = QDoubleSpinBox()
            pos.setRange(5, 95)
            pos.setValue(50)
            pos.setSuffix(" %")
            dip = QDoubleSpinBox()
            dip.setRange(30, 90)
            dip.setValue(80)
            dirc = QComboBox()
            dirc.addItems(["to the right", "to the left"])
            for col, w in ((3, pos), (4, dip), (5, dirc)):
                self.tbl.setCellWidget(k, col, w)
            self.rows.append((chk, pos, dip, dirc))
        lay.addWidget(self.tbl)
        self.tbl.horizontalHeader().setMinimumSectionSize(56)
        self.setMinimumWidth(420)

    def faults(self):
        out = {}
        for k, (chk, pos, dip, dirc) in enumerate(self.rows):
            if chk.isChecked():
                out[k] = {"pos": pos.value() / 100.0, "dip": dip.value(),
                          "dir": 1 if dirc.currentIndex() == 0 else -1}
        return out


BH_FIELDS = [("name", "Borehole name", ["name", "borehole", "vrt", "nazev", "id", "sonda", "oznaceni"]),
             ("x", "X", ["x"]),
             ("y", "Y", ["y"]),
             ("z", "Altitude Z", ["z", "alt", "altitude", "kota", "elev", "vyska"]),
             ("depth", "Total depth", ["depth", "hloub", "total", "delka"]),
             ("azimuth", "Azimuth [deg] (optional)", ["azimuth", "azimut", "azim", "az", "smer", "direction", "bearing"]),
             ("dip", "Dip / deviation [deg] (optional)", ["dip", "deviation", "inclination", "incl", "sklon", "odklon", "angle", "tilt"])]
LY_FIELDS = [("name", "Borehole name", ["name", "borehole", "vrt", "nazev", "id", "sonda", "oznaceni"]),
             ("top", "Layer from [m]", ["from", "od", "top", "start", "begin", "zacatek", "pocatek"]),
             ("bottom", "Layer to [m]", ["to", "do", "bottom", "end", "konec"]),
             ("litho", "Lithology", ["litho", "lit", "type", "druh", "typ", "hornin", "zemin", "petro"]),
             ("strat", "Stratigraphy (optional)", ["strat", "period", "perioda", "utvar", "vek", "age", "system", "geolog"])]


def _fit_to_screen(widget, width, height, max_width=0.9):
    """Resize a window to the wanted size, but never larger than 90 % of the screen
    (max_width: the share of the screen width that may be used)."""
    with contextlib.suppress(AttributeError, RuntimeError):      # no screen (yet): keep the size
        scr = widget.screen() or QGuiApplication.primaryScreen()
        area = scr.availableGeometry()
        width, height = min(width, int(area.width() * max_width)), min(height, int(area.height() * 0.85))
    widget.resize(width, height)


def _scrolled(widget):
    """Tab content in a scroll area, so a tall tab never makes the window taller than the screen."""
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setWidget(widget)
    return sa


def _collapsible(title):
    """A group box with a fold-away arrow (QGIS's own look), a plain one when it is not available.
    The state is not remembered between sessions: the dialog always opens the same way.  Fold it
    with _fold() only AFTER its content has been added."""
    if QgsCollapsibleGroupBox is None:
        return QGroupBox(title)
    box = QgsCollapsibleGroupBox(title)
    box.setSaveCollapsedState(False)
    return box


def _fold(box):
    if hasattr(box, "setCollapsed"):
        box.setCollapsed(True)


def _accordion(*boxes):
    """Opening one of the boxes folds the others away (only one long list of settings at a time)."""
    def opened(box, collapsed):
        if not collapsed:
            for other in boxes:
                if other is not box and hasattr(other, "setCollapsed"):
                    other.setCollapsed(True)
    for b in boxes:
        if hasattr(b, "collapsedStateChanged"):
            b.collapsedStateChanged.connect(lambda c, b=b: opened(b, c))


def _strat_help():
    cz = ", ".join(f"{c} ({e})" for c, e in zip(core.STRAT_LABELS, core.STRAT_ENGLISH))
    return (
        "Stratigraphy (optional): the age of each layer. It is used to correlate the layers and "
        "to decide what is older (bedrock) and what is younger. Periods from the youngest to "
        "the oldest: " + cz + ".\n"
        "Also accepted: terciér (Tertiary = Neogene + Palaeogene), mesozoikum (Mesozoic = "
        "Cretaceous + Jurassic + Triassic), paleozoikum (Palaeozoic = Permian, Carboniferous, "
        "Devonian, Silurian, Ordovician, Cambrian), permokarbon (Permian + Carboniferous).\n"
        "It does not matter whether the first letter is upper or lower case; 'e' instead of 'é' "
        "and 'r' instead of 'ř' (krida) are accepted too.")


STRAT_HELP = _strat_help()

# Czech names shown in brackets in the grain-size menu of tab 2
GRAIN_CZ = {"silt": "prach", "clay": "jíl", "sand": "písek", "gravel": "štěrk"}


class BoreholeDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.setWindowTitle("Borehole Geology Sections")
        _fit_to_screen(self, 720, 600)
        self.boreholes, self.colors, self.section_pts = [], {}, None
        self.hdr = {"bh": None, "ly": None}
        self.hdr_idx = {"bh": None, "ly": None}      # file column of every drop-down entry
        self.line_rb = None
        self.tool = SectionLineTool(self.canvas)
        self.tool.lineFinished.connect(self._line_done)
        self.tool.cancelled.connect(self._line_cancel)
        self.tool.aborted.connect(self.show)
        self.bh_layer_id = None
        QgsProject.instance().cleared.connect(self._project_cleared)
        QgsProject.instance().layerWillBeRemoved.connect(self._layer_will_be_removed)
        self.viewers = []
        self.forms = {}
        self.scene3d, self.scene3d_colors, self.scene3d_bhs = {}, {}, {}   # models of the own 3D viewer

        tabs = QTabWidget()
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        tabs.addTab(_scrolled(self._tab_data()), "1. Data")
        tabs.addTab(_scrolled(self._tab_colors()), "2. Lithology (colour, grain size)")
        tabs.addTab(_scrolled(self._tab_logs()), "3. Borehole logs")
        tabs.addTab(_scrolled(self._tab_2d()), "4. 2D section")
        tabs.addTab(_scrolled(self._tab_3d()), "5. 3D model")

    # ---- tabs
    def _file_group(self, title, fields, key):
        box = QGroupBox(title)
        form = QFormLayout(box)
        fw = QgsFileWidget()
        fw.setFilter("CSV files (*.csv *.tsv *.txt);;All files (*)")
        form.addRow("File", fw)
        dl = QComboBox()
        for label, val in core.DELIMITERS:
            dl.addItem(label, val)
        form.addRow("Delimiter", dl)
        combos = {}
        for k, label, _ in fields:
            cb = QComboBox()
            combos[k] = cb
            form.addRow(label, cb)
        fw.fileChanged.connect(lambda path, k=key: self._file_changed(k, path))
        dl.currentIndexChanged.connect(lambda _i, k=key, f=fw: self._file_changed(k, f.filePath()))
        self.delims[key] = dl
        self.fields[key] = fields
        self.forms[key] = (form, combos)
        return box, fw, combos

    def _tab_data(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.delims, self.fields, self.forms = {}, {}, {}
        b1, self.fw_bh, self.cb_bh = self._file_group("Boreholes CSV", BH_FIELDS, "bh")
        b2, self.fw_ly, self.cb_ly = self._file_group("Layers CSV", LY_FIELDS, "ly")
        # coordinate system selector (inside boreholes group, after delimiter)
        self.cb_crs = QComboBox()
        for k, v in core.INPUT_CRS.items():
            self.cb_crs.addItem(v[1], k)
        self.forms["bh"][0].insertRow(2, "Input coordinate system", self.cb_crs)
        # UTM zone (shown only for the UTM entries)
        self.cb_utm_zone = QComboBox()      # only the zones that exist for the datum (HARN has gaps)
        self.cb_utm_hemi = QComboBox()
        self.cb_utm_hemi.addItem("N (northern hemisphere)", False)
        self.cb_utm_hemi.addItem("S (southern hemisphere)", True)
        self.utm_row = QWidget()
        ul = QHBoxLayout(self.utm_row)
        ul.setContentsMargins(0, 0, 0, 0)
        ul.addWidget(self.cb_utm_zone)
        ul.addWidget(self.cb_utm_hemi)
        ul.addStretch()
        self.forms["bh"][0].insertRow(3, "UTM zone", self.utm_row)
        lay.addWidget(b1)
        lay.addWidget(b2)
        strat_help = STRAT_HELP
        self.cb_ly["strat"].setToolTip(strat_help)
        self.chk_proj = QCheckBox("Set the QGIS project coordinate system to that of the CSV file "
                                  "(when the boreholes are added to the map)")
        self.chk_proj.setChecked(True)
        lay.addWidget(self.chk_proj)
        self._loaded_sig = None            # input settings of the loaded data
        self._stale_warned = False
        self.cb_crs.currentIndexChanged.connect(self._crs_changed)
        self._crs_changed()
        for sig in (self.cb_crs.currentIndexChanged, self.cb_utm_zone.currentIndexChanged,
                    self.cb_utm_hemi.currentIndexChanged):
            sig.connect(self._crs_settings_changed)
        row = QHBoxLayout()
        b = QPushButton("Load data")
        b.clicked.connect(self.load_data)
        b3 = QPushButton("Add boreholes to map")
        b3.clicked.connect(self.add_points)
        row.addWidget(b)
        row.addWidget(b3)
        lay.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(90)
        lay.addWidget(self.log)
        lay.addStretch()
        return w

    def _utm_zone(self):
        z = self.cb_utm_zone.currentData()
        return int(z) if z is not None else None

    def _crs_signature(self):
        """Input settings that decide the coordinates of the loaded boreholes."""
        return (self.cb_crs.currentData(), self._utm_zone(), bool(self.cb_utm_hemi.currentData()))

    def _crs_settings_changed(self, *_):
        """The coordinate settings were changed after Load data: the boreholes (and the map layer,
        sections, 3D models) still have the old coordinates, so say that they must be reloaded."""
        if self.boreholes and self._loaded_sig is not None \
                and self._crs_signature() != self._loaded_sig and not self._stale_warned:
            self._stale_warned = True
            self._msg("WARNING: the coordinate system settings were changed after the data were "
                      "loaded - press Load data again, otherwise the old coordinates are used.")

    def _source_crs_text(self):
        """Name of the coordinate system chosen for the CSV file (with the UTM zone)."""
        key = self.cb_crs.currentData()
        if not core.is_utm(key):
            return self.cb_crs.currentText()
        try:
            return core.crs_authid(key, self._utm_zone(), bool(self.cb_utm_hemi.currentData()))
        except ValueError:
            return self.cb_crs.currentText()

    def _crs_changed(self, *_):
        key = self.cb_crs.currentData()
        _, _, lx, ly, kx, ky = core.INPUT_CRS[key]
        form, combos = self.forms["bh"]
        form.labelForField(combos["x"]).setText(lx)
        form.labelForField(combos["y"]).setText(ly)
        utm = core.is_utm(key)
        form.labelForField(self.utm_row).setVisible(utm)
        self.utm_row.setVisible(utm)
        if utm:
            z0, z1, default, south_ok = core.utm_zone_info(key)
            self.cb_utm_zone.blockSignals(True)
            self.cb_utm_zone.clear()
            for z in core.utm_valid_zones(key):
                self.cb_utm_zone.addItem(f"{z}", z)
            self.cb_utm_zone.setCurrentIndex(max(self.cb_utm_zone.findData(default), 0))
            self.cb_utm_zone.blockSignals(False)
            if not south_ok:
                self.cb_utm_hemi.setCurrentIndex(0)
            self.cb_utm_hemi.setEnabled(south_ok)
        hdr = self.hdr.get("bh")
        if hdr:
            combos["x"].setCurrentIndex(core.guess_column(hdr, kx))
            combos["y"].setCurrentIndex(core.guess_column(hdr, ky))

    def _tab_colors(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.tbl = QTableWidget(0, 4)
        self.tbl.verticalHeader().setDefaultSectionSize(30)     # compact rows (32x24 pattern icons)
        self.tbl.setHorizontalHeaderLabels(["Lithology", "Colour", "Grain size", "Pattern"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.tbl)
        lay.addWidget(QLabel("Colours, grain sizes and patterns are filled after loading data. Colours are\n"
                             "used in all outputs. The grain size (guessed from the name - check it) is\n"
                             "used in the sedimentary records of tab 3. The pattern (FGDC lithologic\n"
                             "patterns) is used in the logs of tab 3 and in the 2D section."))
        return w

    def _tab_logs(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        box = QGroupBox("Boreholes (one to four)")
        form = QFormLayout(box)
        explain = QLabel("Choose the boreholes in one of two ways:\n"
                         "1) on the map - select them in the 'Boreholes' layer (any QGIS selection "
                         "tool) and press the button below;\n"
                         "2) from the drop-down lists below - pick each borehole by its name.")
        explain.setWordWrap(True)
        form.addRow(explain)
        b_sel = QPushButton("Take selected boreholes from the map (max. 4)")
        b_sel.clicked.connect(self.logs_from_selection)
        form.addRow(b_sel)
        self.cb_logs = []
        for i in range(4):
            cb = QComboBox()
            cb.addItem("(none)", None)
            self.cb_logs.append(cb)
            form.addRow(f"Borehole {i + 1}", cb)
        lay.addWidget(box)

        opt = QGroupBox("Settings")
        of = QFormLayout(opt)
        self.cb_logmode = QComboBox()
        self.cb_logmode.addItem("Depth below the surface [m]", "depth")
        self.cb_logmode.addItem("Altitude [m a.s.l.]", "alt")
        of.addRow("Vertical axis", self.cb_logmode)
        self.sp_logscale = QDoubleSpinBox()
        self.sp_logscale.setRange(0, 500)
        self.sp_logscale.setSpecialValueText("auto")
        self.sp_logscale.setSuffix(" px/m")
        of.addRow("Vertical scale", self.sp_logscale)
        self.chk_logpat = QCheckBox("Lithologic patterns (FGDC) in the profiles")
        self.chk_logpat.setChecked(True)
        of.addRow(self.chk_logpat)
        self.chk_logstrat = QCheckBox("Stratigraphy column next to the lithology column")
        self.chk_logstrat.setChecked(True)
        of.addRow(self.chk_logstrat)
        self.sp_patscale = QDoubleSpinBox()
        self.sp_patscale.setRange(0.1, 2.0)
        self.sp_patscale.setSingleStep(0.05)
        self.sp_patscale.setValue(0.45)
        of.addRow("Size of the patterns", self.sp_patscale)
        self.chk_recsep = QCheckBox("A separate window for each borehole (profiles and sedimentary records)")
        of.addRow(self.chk_recsep)
        lay.addWidget(opt)

        b1 = QPushButton("Create profiles (columns with layer descriptions)")
        b1.clicked.connect(self.create_profiles)
        b2 = QPushButton("Create sedimentary records (grain size + rock type legend)")
        b2.clicked.connect(self.create_records)
        lay.addWidget(b1)
        lay.addWidget(b2)
        lay.addWidget(QLabel("Colours and grain sizes of the rocks are set in tab 2."))
        lay.addStretch()
        return w

    def _tab_2d(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        b = QPushButton("Draw section line on map")
        b.clicked.connect(self.draw_line)
        b2 = QPushButton("Use selected line feature (active layer)")
        b2.clicked.connect(self.use_selected)
        row.addWidget(b)
        row.addWidget(b2)
        lay.addLayout(row)
        b_clr = QPushButton("Clear section line")
        b_clr.clicked.connect(self.clear_line)
        lay.addWidget(b_clr)
        self.lbl_line = QLabel("No section line defined.")
        lay.addWidget(self.lbl_line)
        form = QFormLayout()
        self.sp_off = QDoubleSpinBox()
        self.sp_off.setRange(0, 1e6)
        self.sp_off.setValue(50)
        self.sp_off.setSuffix(" m")
        form.addRow("Max. distance of borehole from line", self.sp_off)
        self.sp_gap = QDoubleSpinBox()
        self.sp_gap.setRange(0, 1e5)
        self.sp_gap.setValue(5)
        self.sp_gap.setSuffix(" m")
        self.sp_gap.setSpecialValueText("off")
        self.sp_gap.setToolTip("Boreholes closer together than this: only the one nearer to the "
                               "section line is used (0 = use all). Boreholes off the line are "
                               "drawn with dashed edges.")
        form.addRow("Min. spacing of boreholes", self.sp_gap)
        self.sp_dash = QDoubleSpinBox()
        self.sp_dash.setRange(0, 1e5)
        self.sp_dash.setValue(0)
        self.sp_dash.setSuffix(" m")
        self.sp_dash.setSpecialValueText("auto (size of the map marker)")
        self.sp_dash.setToolTip("A borehole whose distance from the section line is larger than "
                                "this is drawn with a dashed outline. Auto: the radius of the "
                                "circular marker of the 'Boreholes' layer at the current map "
                                "scale, i.e. the line passes through the marker.")
        form.addRow("Dashed outline farther than", self.sp_dash)
        self.cb_xdip = QComboBox()
        self.cb_xdip.addItem("Auto (ground slope across the line)", "auto")
        self.cb_xdip.addItem("Manual (angle below)", "manual")
        self.cb_xdip.addItem("Off (real elevations)", "off")
        self.cb_xdip.setToolTip(
            "An inclined borehole crosses the section plane in one point only; its layers lie "
            "at a distance from the plane (known from the azimuth, the inclination and the "
            "distances of the collar and of the bottom from the line). The elevations of the "
            "layer boundaries are moved into the plane along the strata, which rise "
            "perpendicular to the section by the chosen cross slope.")
        form.addRow("Heights of layers in the section plane", self.cb_xdip)
        self.sp_xdip = QDoubleSpinBox()
        self.sp_xdip.setRange(-80, 80)
        self.sp_xdip.setDecimals(1)
        self.sp_xdip.setSuffix(" deg")
        self.sp_xdip.setToolTip("Manual cross slope of the strata: + = rising to the left of "
                                "the section line (looking from its start to its end).")
        form.addRow("Cross slope of the strata (manual)", self.sp_xdip)

        def _xdip_mode(*_):                       # the angle matters only in the manual mode
            self.sp_xdip.setEnabled(self.cb_xdip.currentData() == "manual")
        self.cb_xdip.currentIndexChanged.connect(_xdip_mode)
        _xdip_mode()
        self.sp_vex = QDoubleSpinBox()
        self.sp_vex.setRange(0, 100)
        self.sp_vex.setSpecialValueText("Auto")
        self.sp_vex.setValue(0)
        form.addRow("Vertical exaggeration", self.sp_vex)
        self.sp_curv = QDoubleSpinBox()
        self.sp_curv.setRange(0, 1)
        self.sp_curv.setSingleStep(0.1)
        self.sp_curv.setValue(0.6)
        form.addRow("Boundary curvature (0 = straight, 1 = follow dip)", self.sp_curv)
        self.sp_bed = QDoubleSpinBox()
        self.sp_bed.setRange(0, 60)
        self.sp_bed.setValue(0)
        self.sp_bed.setSuffix(" deg")
        self.sp_bed.setSpecialValueText("off")
        form.addRow("Bedrock surface dip when not reached (off = connect borehole bottoms)", self.sp_bed)
        self.sp_round = QDoubleSpinBox()
        self.sp_round.setRange(0, 1)
        self.sp_round.setSingleStep(0.1)
        self.sp_round.setValue(0.6)
        form.addRow("Pinch-out roundness (0 = pointed, 1 = blunt)", self.sp_round)
        lay.addLayout(form)
        self.chk_corr = QCheckBox("Correlate same lithology between neighbouring boreholes")
        self.chk_corr.setChecked(True)
        self.chk_merge = QCheckBox("Merge neighbouring layers of the same lithology")
        self.chk_merge.setChecked(True)
        self.chk_dip = QCheckBox("Follow the dip of neighbouring boreholes")
        self.chk_dip.setChecked(True)
        self.chk_trans = QCheckBox("Jagged boundary for lateral change of lithology")
        self.chk_trans.setChecked(True)
        self.chk_lab = QCheckBox("Show labels")
        self.chk_lab.setChecked(True)
        self.chk_strat = QCheckBox("Show stratigraphic boundaries when the section opens")
        self.chk_strat.setChecked(False)
        lay.addWidget(self.chk_corr)
        lay.addWidget(self.chk_merge)
        lay.addWidget(self.chk_dip)
        lay.addWidget(self.chk_trans)
        lay.addWidget(self.chk_lab)
        lay.addWidget(self.chk_strat)
        b3 = QPushButton("Create cross section")
        b3.clicked.connect(self.create_section)
        lay.addWidget(b3)
        lay.addStretch()
        return w

    def _tab_3d(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        vw = QGroupBox("Output and 3D viewer")
        vf = QFormLayout(vw)
        self.cb_out = QComboBox()
        self.cb_out.addItems(["Plugin's 3D viewer (in the web browser)",
                              "QGIS 3D layers (for the QGIS 3D Map View)"])
        self.cb_out.setCurrentIndex(0)                 # default: the plugin's viewer
        self.cb_out.setToolTip("The model is created for the viewer chosen here: the plugin's own "
                               "viewer (WebGL, in the web browser) or the native QGIS 3D Map View with the "
                               "3D layers of the model. The model is created only "
                               "for the viewer chosen here and opens in it automatically.")
        vf.addRow("Open the created model in", self.cb_out)
        note = QLabel("Before creating the model, select the 3D viewer in which the results "
                      "will be displayed")
        note.setWordWrap(True)
        vf.addRow(note)
        bview = QPushButton("Reopen 3D viewer (models created in this session)")
        bview.setToolTip("Opens again the models created in this session for the viewer chosen in 'Open the created model in'.")
        bview.clicked.connect(self.open_viewer)
        vf.addRow(bview)
        lay.addWidget(vw)
        com = QGroupBox("Common settings")
        cf = QFormLayout(com)
        self.sp_dz = QDoubleSpinBox()
        self.sp_dz.setRange(0.25, 50)
        self.sp_dz.setValue(2.0)
        self.sp_dz.setSuffix(" m")
        cf.addRow("Vertical step (grid model, inclined borehole columns)", self.sp_dz)
        self.chk_edges = QCheckBox("Show block edges")
        self.chk_edges.setToolTip("Thin outlines of the blocks / panels in the 3D view "
                                  "(off = smooth surfaces without a mesh).")
        cf.addRow(self.chk_edges)
        self.chk_sel = QCheckBox("Use only boreholes selected in the 'Boreholes' map layer")
        cf.addRow(self.chk_sel)
        lay.addWidget(com)

        gn = _collapsible("A) Fence model - network of 2D sections between neighbouring boreholes")
        fn = QFormLayout(gn)
        self.cb_net = QComboBox()
        self.cb_net.addItems(["Grid-like (nearest borehole in each direction)",
                              "Delaunay triangulation"])
        fn.addRow("Connect boreholes", self.cb_net)
        self.sp_maxe = QDoubleSpinBox()
        self.sp_maxe.setRange(0, 100000)
        self.sp_maxe.setSpecialValueText("auto")
        self.sp_maxe.setSuffix(" m")
        fn.addRow("Longest panel", self.sp_maxe)
        self.sp_pw = QDoubleSpinBox()
        self.sp_pw.setRange(0.2, 50)
        self.sp_pw.setValue(1.0)
        self.sp_pw.setSuffix(" m")
        fn.addRow("Panel thickness", self.sp_pw)
        self.sp_nslab = QDoubleSpinBox()
        self.sp_nslab.setRange(0.5, 200)
        self.sp_nslab.setValue(5.0)
        self.sp_nslab.setSuffix(" m")
        fn.addRow("Terrain sampling step (with a DEM)", self.sp_nslab)
        self.sp_colw = QDoubleSpinBox()
        self.sp_colw.setRange(0, 50)
        self.sp_colw.setValue(3.0)
        self.sp_colw.setSuffix(" m")
        self.sp_colw.setSpecialValueText("off")
        fn.addRow("Borehole columns (black pillars), width", self.sp_colw)
        drow = QHBoxLayout()
        self.cb_dem = QComboBox()
        drow.addWidget(self.cb_dem, 1)
        bdem = QPushButton("Refresh")
        bdem.clicked.connect(self._refresh_dem)
        drow.addWidget(bdem)
        fn.addRow("DEM (ground between boreholes)", drow)
        self._refresh_dem()
        self.chk_fill = QCheckBox("Interpolate layers between the sections (volume model)")
        self.chk_fill.setToolTip("Calculates the layers between the 2D sections: every triangle of \n"
                                 "neighbouring boreholes is interpolated from its three sections.")
        fn.addRow(self.chk_fill)
        self.cb_fillmode = QComboBox()
        self.cb_fillmode.addItems(["Smooth layers (sloped solids)", "Blocks (columns of boxes, old method)"])
        self.cb_fillmode.setToolTip("Smooth layers: layer boundaries are interpolated surfaces, dipping layers stay\n"
                                    "inclined and layers pinch out smoothly. Blocks: the method of versions up to 0.18.")
        fn.addRow("Interpolation", self.cb_fillmode)
        self.sp_fillcell = QDoubleSpinBox()
        self.sp_fillcell.setRange(0, 1000)
        self.sp_fillcell.setSpecialValueText("auto")
        self.sp_fillcell.setSuffix(" m")
        self.sp_fillcell.setToolTip("Mesh size of the interpolated volume (auto = 1/8 of the median distance of boreholes).")
        fn.addRow("Mesh size of the interpolated volume", self.sp_fillcell)
        bnet = QPushButton("Create network fence model")
        bnet.clicked.connect(self.build_network)
        fn.addRow(bnet)
        lay.addWidget(gn)

        ga = _collapsible("B) Full model from boreholes (all boreholes interpolated, no sections needed)")
        _accordion(gn, ga)                 # one of the two long lists of settings is open at a time
        fa = QFormLayout(ga)
        self.cb_gmethod = QComboBox()
        self.cb_gmethod.addItems(["Smooth layers (sloped solids)", "Blocks (bricks on a square grid)"])
        self.cb_gmethod.setToolTip("Smooth layers: the layer stack is interpolated at the nodes of a fine mesh and the \n"
                                   "layers are joined into smooth bodies - layer boundaries are exact surfaces and \n"
                                   "dipping layers stay inclined. Blocks: the older brick model.")
        fa.addRow("Method", self.cb_gmethod)
        self.cb_extent = QComboBox()
        self.cb_extent.addItems(["Between the boreholes (their hull)", "Rectangle around the boreholes"])
        self.cb_extent.setToolTip("Hull: only the triangles between neighbouring boreholes (no extrapolation).\n"
                                  "Rectangle: the model is extended around the boreholes (limited by the\n"
                                  "maximum distance from a borehole).")
        fa.addRow("Extent of the smooth model", self.cb_extent)
        self.cb_gmethod.currentIndexChanged.connect(lambda i: self.cb_extent.setEnabled(i == 0))
        self.sp_cell = QDoubleSpinBox()
        self.sp_cell.setRange(0, 5000)
        self.sp_cell.setSpecialValueText("auto")
        self.sp_cell.setSuffix(" m")
        self.sp_cell.setToolTip("Mesh size (smooth layers) or cell size (blocks); auto = 1/25 of the extent.")
        fa.addRow("Mesh / grid cell size", self.sp_cell)
        self.cb_gmode = QComboBox()
        self.cb_gmode.addItems(["Horizontal layers", "Layers parallel to the ground surface"])
        fa.addRow("Layer geometry (Quaternary always follows the surface)", self.cb_gmode)
        self.sp_k = QSpinBox()
        self.sp_k.setRange(1, 12)
        self.sp_k.setValue(4)
        fa.addRow("Nearest boreholes used for a node / cell", self.sp_k)
        self.sp_maxd = QDoubleSpinBox()
        self.sp_maxd.setRange(0, 100000)
        self.sp_maxd.setSpecialValueText("unlimited")
        self.sp_maxd.setSuffix(" m")
        self.sp_maxd.setToolTip("Hull: triangles with a side longer than twice this distance are left out.\n"
                                "Rectangle / blocks: nodes or cells farther than this from a borehole are left out.")
        fa.addRow("Max. distance from a borehole", self.sp_maxd)
        bg = QPushButton("Create full model")
        bg.clicked.connect(self.create_grid)
        fa.addRow(bg)
        lay.addWidget(ga)
        _fold(ga)                          # only A) is open at first (see _accordion)

        lay.addWidget(self._lithology_visibility_box())
        lay.addWidget(QLabel("QGIS layers: open them with View > New 3D Map View "
                             "(raise the vertical scale in the 3D view settings)."))
        lay.addStretch()
        return w

    # ---- visibility of lithologies in the 3D models
    def _lithology_visibility_box(self):
        box = QGroupBox("Lithologies in the 3D models (tick = shown)")
        v = QVBoxLayout(box)
        self.lst_lit = QListWidget()
        self.lst_lit.setMaximumHeight(170)
        self.lst_lit.itemChanged.connect(self._apply_lithology_visibility)
        v.addWidget(self.lst_lit)
        row = QHBoxLayout()
        for text, state in (("Show all", True), ("Hide all", False)):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, s=state: self._set_all_lithologies(s))
            row.addWidget(b)
        v.addLayout(row)
        self.model_layers = {}              # lithology -> ids of the 3D layers
        return box

    def _register_models(self, registry, colors):
        """Add the lithologies of a new 3D model to the list."""
        self.lst_lit.blockSignals(True)
        for lit, ids in registry.items():
            self.model_layers.setdefault(lit, []).extend(ids)
            if not self.lst_lit.findItems(lit, Qt.MatchFlag.MatchExactly):
                it = QListWidgetItem(lit)
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Checked)
                c = colors.get(lit)
                if c is not None:
                    pm = QPixmap(14, 14)
                    pm.fill(QColor(c))
                    it.setIcon(QIcon(pm))
                self.lst_lit.addItem(it)
        self.lst_lit.blockSignals(False)
        self._apply_lithology_visibility()

    def _apply_lithology_visibility(self, _item=None):
        root = QgsProject.instance().layerTreeRoot()
        for i in range(self.lst_lit.count()):
            it = self.lst_lit.item(i)
            on = it.checkState() == Qt.CheckState.Checked
            for lid in self.model_layers.get(it.text(), []):
                node = root.findLayer(lid)
                if node is not None:
                    node.setItemVisibilityChecked(on)

    def _set_all_lithologies(self, on):
        self.lst_lit.blockSignals(True)
        for i in range(self.lst_lit.count()):
            self.lst_lit.item(i).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.lst_lit.blockSignals(False)
        self._apply_lithology_visibility()

    # ---- data
    def _msg(self, s):
        self.log.appendPlainText(s)

    def _file_changed(self, key, path):
        if not path or not os.path.isfile(path):       # e.g. a path that is being typed
            return
        try:
            header, _ = core.read_table(path, self.delims[key].currentData(), max_rows=1)
        except Exception as e:
            self.hdr[key] = None
            self.hdr_idx[key] = None
            for cb in self.forms[key][1].values():     # no column names of the previous file
                cb.clear()
            self._msg(f"Cannot read {path}: {e}")
            return
        # only columns with a heading are offered (blank headers were named "(column N)")
        kept = [i for i, h in enumerate(header) if not core.is_blank_header(h)]
        header = [header[i] for i in kept]
        self.hdr[key] = header
        self.hdr_idx[key] = kept
        combos = self.forms[key][1]
        crs_key = self.cb_crs.currentData() if hasattr(self, "cb_crs") else "sjtsk"
        for k, _, kw in self.fields[key]:
            if key == "bh" and k == "x":
                kw = core.INPUT_CRS[crs_key][4]
            elif key == "bh" and k == "y":
                kw = core.INPUT_CRS[crs_key][5]
            cb = combos[k]
            cb.clear()
            if k in ("strat", "azimuth", "dip"):
                cb.addItem("(none)")
                cb.addItems(header)
                cb.setCurrentIndex(core.guess_column(header, kw, default=-1) + 1)
            else:
                cb.addItems(header)
                cb.setCurrentIndex(core.guess_column(header, kw))

    def _file_col(self, key, i):
        """Column of the file for the index of a drop-down entry (-1 stays -1 = none)."""
        idx = self.hdr_idx.get(key)
        if i < 0:
            return -1
        return idx[i] if idx is not None and i < len(idx) else i

    def load_data(self):
        p1, p2 = self.fw_bh.filePath(), self.fw_ly.filePath()
        if not p1 or not p2:
            QMessageBox.warning(self, "Missing file", "Select both CSV files.")
            return
        try:
            h1, r1 = core.read_table(p1, self.delims["bh"].currentData())
            h2, r2 = core.read_table(p2, self.delims["ly"].currentData())
            if any(c.currentIndex() < 0 for c in list(self.cb_bh.values()) + list(self.cb_ly.values())
                   if c not in (self.cb_bh["azimuth"], self.cb_bh["dip"], self.cb_ly["strat"])):
                raise ValueError("Choose a column for every required field (a drop-down is empty).")
            m1 = {k: self._file_col("bh", c.currentIndex()) for k, c in self.cb_bh.items()}
            m1["azimuth"] = self._file_col("bh", self.cb_bh["azimuth"].currentIndex() - 1)  # -1 = none
            m1["dip"] = self._file_col("bh", self.cb_bh["dip"].currentIndex() - 1)
            m2 = {k: self._file_col("ly", c.currentIndex()) for k, c in self.cb_ly.items()}
            m2["strat"] = self._file_col("ly", self.cb_ly["strat"].currentIndex() - 1)
            bhs, warns = core.build_boreholes(
                h1, r1, m1, h2, r2, m2, self.cb_crs.currentData(),
                self._utm_zone(), bool(self.cb_utm_hemi.currentData()))
            if not bhs:                      # nothing valid: the previous data stay as they were
                msg = "\n".join(warns[:10]) if warns else "No valid boreholes found in the CSV file."
                QMessageBox.warning(self, "No boreholes loaded", msg)
                return
            # the column names are replaced only when the new data were built successfully
            self.boreholes = bhs
            self.bh_layer_id = None          # the map layer of the previous data set is not ours any more
            # the columns offered in tab 1 (no blank headers), like in the windows of the logs
            self.ly_header = [h for h in h2 if not core.is_blank_header(h)]
            self.litho_col = h2[m2["litho"]]
            self.strat_col = h2[m2["strat"]] if m2["strat"] >= 0 else ""
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        self.clear_line()                    # the old line is in the CRS of the previous data
        self._loaded_sig = self._crs_signature()
        self._stale_warned = False
        self.log.clear()
        for v in list(self.viewers):         # open windows drawn from the previous data set
            stale = getattr(v, "mark_stale", None)
            if stale is not None:
                stale()
        self.scene3d.clear()                 # models of the previous data set
        self.scene3d_colors.clear()
        self.scene3d_bhs.clear()
        self.model_layers.clear()
        self.lst_lit.clear()
        n_l = sum(len(b.layers) for b in self.boreholes)
        self._msg(f"Loaded {len(self.boreholes)} boreholes, {n_l} layers "
                  f"(input CRS: {self._source_crs_text()}).")
        if core.work_crs_authid() != core.source_crs_authid():
            self._msg(f"The file is in degrees or Web Mercator, which cannot be used for distances "
                      f"in metres: the plugin works in {core.work_crs_authid()} (UTM zone of the "
                      f"boreholes); the QGIS project keeps {core.source_crs_authid()}.")
        if self.boreholes:
            b = self.boreholes[0]
            self._msg(f"First borehole {b.name}: x={b.x:.2f}, y={b.y:.2f} ({core.work_crs_authid()})")
        for w in warns[:200]:
            self._msg("WARNING: " + w)
        lithos = core.lithologies(self.boreholes)
        defaults = core.default_colors(lithos)
        self.tbl.setRowCount(len(lithos))
        self.btns, self.grain_boxes, self.pat_boxes = {}, {}, {}
        pat_model = self._pattern_model()
        for i, lit in enumerate(lithos):
            it = QTableWidgetItem(lit)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tbl.setItem(i, 0, it)
            cb = QgsColorButton()
            cb.setColor(defaults[lit])
            self.tbl.setCellWidget(i, 1, cb)
            self.btns[lit] = cb
            gb = QComboBox()
            for g in logs.GRAINS:
                gb.addItem(f"{logs.GRAIN_LABELS[g]} ({GRAIN_CZ[g]})", g)
            gb.setCurrentIndex(logs.GRAINS.index(logs.guess_grain(lit)))
            self.tbl.setCellWidget(i, 2, gb)
            self.grain_boxes[lit] = gb
            pb = PatternCombo()
            pb.setIconSize(QSize(32, 24))
            pb.setModel(pat_model)               # one model (133 icons) for all rows
            pb.setCurrentIndex(max(pb.findData(patterns.default_pattern(lit)), 0))
            self.tbl.setCellWidget(i, 3, pb)
            self.pat_boxes[lit] = pb
        for i, cb in enumerate(self.cb_logs):
            cb.clear()
            cb.addItem("(none)", None)
            for b in self.boreholes:
                cb.addItem(b.name, b.name)
            if i == 0 and self.boreholes:
                cb.setCurrentIndex(1)

    def _pattern_model(self):
        """ONE item model with the pattern icons, shared by the combos of all lithologies."""
        model = QStandardItemModel(self)
        none = QStandardItem("(none)")
        none.setData("", Qt.ItemDataRole.UserRole)
        model.appendRow(none)
        for code in patterns.NAMES:
            ic = patterns.icon(code)
            item = QStandardItem(ic, patterns.label(code)) if ic is not None \
                else QStandardItem(patterns.label(code))
            item.setData(code, Qt.ItemDataRole.UserRole)
            model.appendRow(item)
        old = getattr(self, "_pat_model", None)
        if old is not None:
            old.deleteLater()                # the previous set of combos is being replaced
        self._pat_model = model
        return model

    def current_colors(self):
        return {lit: b.color() for lit, b in getattr(self, "btns", {}).items()}

    def current_grains(self):
        return {lit: b.currentData() for lit, b in getattr(self, "grain_boxes", {}).items()}

    def current_patterns(self):
        return {lit: b.currentData() for lit, b in getattr(self, "pat_boxes", {}).items()}

    def _need_data(self):
        if not self.boreholes:
            QMessageBox.warning(self, "No data", "Load data first (tab 1).")
            return False
        if self._loaded_sig is not None and self._crs_signature() != self._loaded_sig:
            QMessageBox.warning(self, "Coordinate system changed",
                                "The coordinate system settings (tab 1) were changed after the data "
                                "were loaded. Press Load data again so that the boreholes, the map "
                                "layer, the sections and the 3D models use the new settings.")
            return False
        return True

    def add_points(self):
        if self._need_data():
            lyr = core.make_borehole_point_layer(self.boreholes)
            if self.chk_proj.isChecked():
                crs = QgsCoordinateReferenceSystem(core.source_crs_authid())
                if crs.isValid():
                    QgsProject.instance().setCrs(crs)     # the project uses the CRS of the CSV file
            QgsProject.instance().addMapLayer(lyr)
            self.bh_layer_id = lyr.id()
            self.iface.mapCanvas().zoomToFullExtent()

    def _bh_layers(self):
        """The borehole point layer added by this plugin (by id), else layers named 'Boreholes'."""
        lyr = QgsProject.instance().mapLayer(self.bh_layer_id) if self.bh_layer_id else None
        return [lyr] if lyr is not None else QgsProject.instance().mapLayersByName("Boreholes")

    @staticmethod
    def _selected_names(lyr):
        """Names (attribute 'name') of the selected features; a foreign layer called
        'Boreholes' without such an attribute gives nothing."""
        if not hasattr(lyr, "selectedFeatures") or lyr.fields().indexFromName("name") < 0:
            return []
        names = []
        for f in lyr.selectedFeatures():
            v = f["name"]
            if v is None or (hasattr(v, "isNull") and v.isNull()):     # NULL in the table
                continue
            names.append(str(v))
        return names

    # ---- borehole logs
    def logs_from_selection(self):
        if not self._need_data():
            return
        names = []
        for lyr in self._bh_layers():
            names += self._selected_names(lyr)
        names = [n for n in dict.fromkeys(names) if any(b.name == n for b in self.boreholes)]
        if not names:
            QMessageBox.information(self, "Selection",
                                    "Select boreholes in the 'Boreholes' map layer "
                                    "(tab 1: Add boreholes to map).")
            return
        if len(names) > 4:
            QMessageBox.information(self, "Selection",
                                    f"{len(names)} boreholes are selected - the first four are used.")
        for i, cb in enumerate(self.cb_logs):
            cb.setCurrentIndex(cb.findData(names[i]) if i < len(names) else 0)

    def _log_boreholes(self):
        if not self._need_data():
            return []
        picked = []
        for cb in self.cb_logs:
            name = cb.currentData()
            if name is not None and name not in [b.name for b in picked]:
                picked += [b for b in self.boreholes if b.name == name][:1]
        picked = [b for b in picked if b.layers]
        if not picked:
            QMessageBox.warning(self, "No borehole", "Select at least one borehole with layers.")
        return picked

    def _log_view(self, bhs, desc_col, prof_col, merge, strat_col):
        """Copies of the boreholes: litho = value of the lithology column (colours, patterns),
        desc = value of the description column, age = from the stratigraphy column; each item
        is taken from its own column; optionally identical neighbours are merged."""
        def value(L, col):
            if not col:                       # column without a header name: no value
                return ""
            if col == self.litho_col:
                return L.litho
            return ((L.extra or {}).get(col) or "").strip()

        def age_of(L):
            if strat_col == NO_STRAT:
                return None
            if self.strat_col and strat_col == self.strat_col:
                return L.age
            raw = value(L, strat_col)
            return core.parse_strat(raw) if raw else None

        out = []
        for b in bhs:
            lay = []
            for L in core.clean_layers(b.layers):
                prof = value(L, prof_col) or "(unknown)"
                lay.append(core.Layer(L.top, L.bottom, prof, age_of(L), L.extra,
                                      value(L, desc_col) or prof))
            out.append(replace(b, layers=core.merge_same_lithology(lay) if merge else lay))
        return out

    def _log_style(self, bhs):
        """Colours, grain sizes and patterns: those of tab 2, defaults for other values."""
        lithos = core.lithologies(bhs)
        colors = {lit: c.name() for lit, c in self.current_colors().items()}
        for lit, c in core.default_colors([lit for lit in lithos if lit not in colors]).items():
            colors[lit] = c.name()
        grains = self.current_grains()
        pats = self.current_patterns()
        for lit in lithos:
            grains.setdefault(lit, logs.guess_grain(lit))
            pats.setdefault(lit, patterns.default_pattern(lit))
        return colors, grains, pats

    def _open_log(self, kind, bhs):
        """Window with the profiles (kind 'profiles') or the sedimentary records of `bhs`."""
        mode, scale = self.cb_logmode.currentData(), self.sp_logscale.value()
        show_pat = self.chk_logpat.isChecked()
        pscale = self.sp_patscale.value()

        def build(desc_col, prof_col, merge, strat_col):
            view = self._log_view(bhs, desc_col, prof_col, merge, strat_col)
            colors, grains, pats = self._log_style(view)
            if kind == "profiles":
                cmds = logs.build_profiles(view, colors, pats, mode, scale, logs.qt_measure(),
                                           show_pat, strat_col != NO_STRAT)
            else:
                cmds = logs.build_records(view, colors, grains, pats, scale, logs.qt_measure(),
                                          mode=mode)
            return logs.to_scene(cmds, pscale)

        names = ", ".join(b.name for b in bhs)
        title = ("Borehole profiles: " if kind == "profiles" else "Sedimentary record: ") + names
        col = self.litho_col
        strat = self.strat_col if kind == "profiles" and self.chk_logstrat.isChecked() and self.strat_col else NO_STRAT
        v = LogViewer(build(col, col, False, strat), self, title, kind, self.ly_header,
                      {"prof": col, "desc": col, "merge": False, "strat": strat}, build)
        self._track_viewer(v)
        v.show()

    def _create_logs(self, kind):
        bhs = self._log_boreholes()
        if not bhs:
            return
        for grp in ([[b] for b in bhs] if self.chk_recsep.isChecked() else [bhs]):
            self._open_log(kind, grp)

    def create_profiles(self):
        self._create_logs("profiles")

    def create_records(self):
        self._create_logs("records")

    # ---- 2D
    def draw_line(self):
        self.hide()
        self.canvas.setMapTool(self.tool)
        self.iface.messageBar().pushMessage(
            "Section line", "Left-click = vertex, right-click = finish, Esc = cancel",
            level=Qgis.MessageLevel.Info, duration=8)

    def clear_line(self):
        """Remove the current section line (from the map and from memory)."""
        self.section_pts = None
        self._drop_line_rb()
        self.tool.points = []
        self.tool.rb.reset(Qgis.GeometryType.Line)
        self.lbl_line.setText("No section line defined.")

    def _drop_line_rb(self):
        """Reset the section-line rubber band and remove it from the canvas scene."""
        if self.line_rb:
            self.line_rb.reset(Qgis.GeometryType.Line)
            sc = self.canvas.scene()
            if sc is not None and self.line_rb.scene() is sc:
                sc.removeItem(self.line_rb)      # a QgsRubberBand is a graphics item, not a QObject
            self.line_rb = None

    def _line_cancel(self):
        self.canvas.unsetMapTool(self.tool)
        self._to_front()

    def _to_front(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _line_done(self, pts):
        self.canvas.unsetMapTool(self.tool)
        src = self.canvas.mapSettings().destinationCrs()
        self._set_line(pts, src)
        self._to_front()

    def use_selected(self):
        lyr = self.iface.activeLayer()
        if lyr is None or not hasattr(lyr, "selectedFeatures") or not lyr.selectedFeatures():
            QMessageBox.warning(self, "Selection", "Select a line feature in the active layer.")
            return
        g = lyr.selectedFeatures()[0].geometry()
        if g is None or g.isNull() or g.isEmpty():
            QMessageBox.warning(self, "Selection", "The selected feature has an empty geometry.")
            return
        if g.type() != Qgis.GeometryType.Line:
            QMessageBox.warning(self, "Selection", "Selected feature is not a line.")
            return
        g = QgsGeometry(g)                      # arcs (CircularString, CompoundCurve) -> segments
        if g.constGet() is not None and g.constGet().hasCurvedSegments():
            g = g.convertToStraightSegment()    # returns a NEW geometry
        parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        pts = next((p for p in parts if len(p) >= 2), [])      # first usable part
        if len(pts) < 2:
            QMessageBox.warning(self, "Selection", "The selected line needs at least two vertices.")
            return
        self._set_line(pts, lyr.crs())
        n_sel = len(lyr.selectedFeatures())
        if n_sel > 1 and self.section_pts:
            self.lbl_line.setText(self.lbl_line.text() + f"  (the first of {n_sel} selected features)")

    def _set_line(self, pts, src_crs):
        dst = core.work_crs()                  # the coordinate system of the boreholes
        tr = QgsCoordinateTransform(src_crs, dst, QgsProject.instance().transformContext())
        if not src_crs.isValid() or not tr.isValid():       # invalid CRS would pass unchanged
            QMessageBox.warning(self, "Section line",
                                "The layer (or the project) has no valid coordinate system, so "
                                "the section line cannot be transformed to the coordinate system "
                                f"of the boreholes ({core.work_crs_authid()}). Assign a CRS "
                                "to the layer first.")
            return
        try:
            new_pts = [tr.transform(QgsPointXY(p)) for p in pts]
        except (QgsCsException, ValueError) as e:
            QMessageBox.warning(self, "Section line",
                                "The section line could not be transformed to "
                                f"{core.work_crs_authid()} "
                                f"(layer or project without a valid CRS?):\n{e}")
            return
        if not all(math.isfinite(p.x()) and math.isfinite(p.y()) for p in new_pts):
            QMessageBox.warning(self, "Section line",
                                "The section line lies outside the valid area of "
                                f"{core.work_crs_authid()}.")
            return
        length = QgsGeometry.fromPolylineXY(new_pts).length() if len(new_pts) >= 2 else 0.0
        if not math.isfinite(length) or length < 1e-6:
            QMessageBox.warning(self, "Section line",
                                "The section line has zero length - click two different points.")
            return
        self.section_pts = new_pts
        self.lbl_line.setText(f"Section line: {len(pts)} vertices, length {length:.1f} m")
        self._drop_line_rb()
        self.line_rb = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
        self.line_rb.setColor(QColor(220, 0, 0))
        self.line_rb.setWidth(2)
        self.line_rb.setToGeometry(QgsGeometry.fromPolylineXY(list(pts)), src_crs)

    def _marker_radius(self):
        """Radius (m) of the circular borehole marker on the map, measured in the working CRS
        of the boreholes (metres of UTM / S-JTSK): the pixel size of the canvas is in the units
        of the project CRS (degrees for EPSG:4326, stretched metres for Web Mercator), so two
        points one marker radius apart are transformed into the working CRS and measured there."""
        size_mm = 2.0
        try:
            for lyr in self._bh_layers():
                rend = lyr.renderer() if hasattr(lyr, "renderer") else None
                sym = rend.symbol() if rend is not None and hasattr(rend, "symbol") else None
                if sym is not None and sym.size() > 0:
                    size_mm = sym.size()
                    break
            ms = self.canvas.mapSettings()
            r_px = size_mm / 2.0 * ms.outputDpi() / 25.4
            r_map = r_px * self.canvas.mapUnitsPerPixel()          # in the units of the canvas CRS
            c = self.canvas.center()
            canvas_crs = ms.destinationCrs()
            tr = QgsCoordinateTransform(canvas_crs, core.work_crs(),
                                       QgsProject.instance().transformContext())
            if not tr.isValid():                 # no valid CRS yet: no transformation, no log noise
                return core.OFF_LINE_TOL
            p1 = tr.transform(QgsPointXY(c.x(), c.y()))
            dx = -r_map if c.x() > 0 else r_map      # towards the middle of the axis: no step beyond +-180
            p2 = tr.transform(QgsPointXY(c.x() + dx, c.y()))
            r = math.hypot(p2.x() - p1.x(), p2.y() - p1.y())
            if not math.isfinite(r) or r <= 0:
                raise ValueError("marker radius is not finite")
        except Exception as e:
            core.log_warning(f"Marker size not available: {type(e).__name__}: {e}")
            return core.OFF_LINE_TOL
        return max(r, 0.1)

    def create_section(self):
        if not self._need_data():
            return
        if not self.section_pts:
            QMessageBox.warning(self, "No line", "Define the section line first.")
            return
        try:
            proj = core.project_boreholes(self.section_pts, self.boreholes, self.sp_off.value(),
                                          self.sp_gap.value())
            core.apply_cross_dip(proj, self.cb_xdip.currentData(), self.sp_xdip.value())
        except Exception as e:
            QMessageBox.warning(self, "2D section", f"{type(e).__name__}: {e}")
            return
        if not proj:
            QMessageBox.warning(self, "No boreholes",
                                "No borehole within the given distance of the line.")
            return
        _colors, opts, bedrock, strat = self._section_options()
        line = list(self.section_pts)
        tol = self.sp_dash.value() or self._marker_radius()

        poly_cache = {}                                  # correlation polygons of the edit steps

        def build(faults, edits=None, proj=proj, opts=opts, bedrock=bedrock, strat=strat,
                  line=line, tol=tol, poly_cache=poly_cache):
            core.take_geometry_issues()                  # forget the problems of older builds
            # colours and patterns of tab 2 are read again at every (re)generation
            sc = core.build_section_scene(proj, self.current_colors(), *opts, faults, bedrock, True,
                                          self.current_patterns(), off_tol=tol,
                                          edits=edits, poly_cache=poly_cache)
            sc.geo["line"] = line
            self._report_geometry_issues()
            return sc

        try:
            with _busy():                                # the correlation can take seconds
                v = SectionViewer(build(None, _edits.SectionEdits() if len(proj) > 1 else None),
                                  self, proj, build, strat)
        except Exception as e:
            QMessageBox.warning(self, "2D section", f"{type(e).__name__}: {e}")
            return
        self._track_viewer(v)
        v.show()

    def _report_geometry_issues(self):
        """Tell the user that a geometric correction failed (core.geometry_issue)."""
        issues = core.take_geometry_issues()
        if not issues:
            return
        text = "; ".join(m + (f" (x{n})" if n > 1 else "") for m, n in issues)
        self.iface.messageBar().pushMessage(
            "Geometry", text + ". Details are in the QGIS log (Borehole Geology).",
            level=Qgis.MessageLevel.Warning, duration=20)

    def _track_viewer(self, v):
        """Keep a reference while the window is open; forget it when it is closed."""
        self.viewers.append(v)
        # closed = deleted (with its scene and pattern bitmaps), not hidden and kept as a child
        v.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        def _drop_scene(_r=0, v=v):
            # the CURRENT scene of the window: Regenerate replaces (and deletes) the first one
            sc = getattr(v, "scene", None)
            if sc is not None and hasattr(sc, "deleteLater"):
                with contextlib.suppress(RuntimeError):      # already deleted
                    sc.deleteLater()

        v.finished.connect(_drop_scene)
        v.finished.connect(lambda _r=0, v=v: self.viewers.remove(v) if v in self.viewers else None)

    def _section_options(self):
        colors = self.current_colors()
        opts = (self.sp_vex.value(), self.chk_corr.isChecked(), self.chk_lab.isChecked(),
                self.sp_curv.value(), self.sp_round.value(), self.chk_merge.isChecked(),
                self.chk_dip.isChecked(), self.chk_trans.isChecked())
        return colors, opts, self.sp_bed.value(), self.chk_strat.isChecked()

    # ---- 3D
    def _bh_subset(self):
        bhs = self.boreholes
        if self.chk_sel.isChecked():
            names = set()
            for lyr in self._bh_layers():
                names |= set(self._selected_names(lyr))
            sub = [b for b in bhs if b.name in names]
            if len(sub) >= 2:
                return sub
            QMessageBox.information(self, "Selection",
                                    "Fewer than two boreholes are selected in the 'Boreholes' "
                                    "layer - all boreholes are used.")
        return bhs

    def _show_3d_result(self, count, n, err, what):
        self.iface.messageBar().pushMessage(
            "3D", f"{what}: {n} objects in {count} layers.",
            level=Qgis.MessageLevel.Success, duration=10)
        if err:
            QMessageBox.warning(
                self, "3D style",
                "The layers were created, but the 3D style could not be applied:\n\n" + err +
                "\n\nSet it manually: Layer Properties > 3D View > single symbol, "
                "altitude clamping 'absolute', extrusion 0 (the objects are closed bodies "
                "made of PolygonZ faces).")

    def create_grid(self):
        if not self._need_data():
            return
        bhs = self._bh_subset()
        mode = "depth" if self.cb_gmode.currentIndex() == 1 else "elevation"
        failure = None
        with _busy():
            try:
                if self.cb_gmethod.currentIndex() == 0:
                    solids, info = model3d.smooth_model(
                        bhs, self.sp_cell.value(), self.sp_dz.value(), mode, self.sp_k.value(),
                        self.sp_maxd.value(),
                        "rectangle" if self.cb_extent.currentIndex() == 1 else "hull")
                    name, layer_group = "Full model", "3D full model"
                    what = (f"Full model ({info['bodies']} smooth bodies, mesh {info['cell']:.1f} m)")
                else:
                    prisms, info = model3d.grid_model(bhs, self.sp_cell.value(), self.sp_dz.value(),
                                                      mode, self.sp_k.value(), self.sp_maxd.value())
                    solids = model3d.prisms_to_solids(prisms)
                    name, layer_group = "Grid blocks", "3D grid model"
                    what = f"Grid model ({info['cells']} cells of {info['cell']:.1f} m)"
            except Exception as e:
                failure = e
        if failure is not None:
            e = failure
            QMessageBox.warning(self, "3D full model", str(e))
            return
        # one of the two methods replaces the other result in the viewer
        for old in ("Full model", "Grid blocks"):
            if old != name:
                self.scene3d.pop(old, None)
        self._finish_model({name: solids}, self.current_colors(), layer_group, solids, None, bhs, what)

    def _textures(self, lithos):
        """Hatching patterns (tab 2) of the lithologies for the 3D viewer: {litho: (code, PNG, aspect)}."""
        pats = self.current_patterns()
        codes = {lit: pats.get(lit, patterns.default_pattern(lit)) for lit in lithos}
        try:
            tiles = viewer3d_window.pattern_tiles([c for c in codes.values() if c])
        except Exception as e:                       # textures are optional
            core.log_warning(f"3D viewer textures not available: {type(e).__name__}: {e}")
            return {}
        return {lit: (c, *tiles[c]) for lit, c in codes.items() if c in tiles}

    def _finish_model(self, groups, colors, layer_group, solids, volume, bhs, what):
        """Put a new model into the viewer chosen in 'Open the created model in' only
        (0 = plugin's viewer in the web browser, 1 = QGIS 3D layers) and open it."""
        if self.cb_out.currentIndex() == 0:
            for name, sol in groups.items():
                if sol:
                    self.scene3d[name] = sol
                else:
                    self.scene3d.pop(name, None)
            self.scene3d_colors.update(colors)
            self.scene3d_bhs.update({b.name: b for b in bhs})
            self.iface.messageBar().pushMessage(
                "3D", f"{what} - opening the plugin's 3D viewer.",
                level=Qgis.MessageLevel.Success, duration=8)
            self.open_web_viewer()
            return
        reg = {}
        with _busy():
            count, n, err = model3d.build_layers(solids, colors, layer_group,
                                                 edges=self.chk_edges.isChecked(),
                                                 registry=reg, volume=volume)
            self._register_models(reg, colors)
        self._show_3d_result(count, n, err, what)
        self.open_qgis_3d_view()

    def open_viewer(self, _checked=False):
        """Button 'Reopen 3D viewer': the models created in this session for the viewer chosen in
        'Open the created model in' (0 = plugin's viewer in the web browser, 1 = QGIS 3D Map View)."""
        web = self.cb_out.currentIndex() == 0
        has_qgis_layers = any(QgsProject.instance().mapLayer(lid) is not None
                              for ids in self.model_layers.values() for lid in ids)
        if not (self.scene3d if web else has_qgis_layers):
            if has_qgis_layers or self.scene3d:       # a model exists, but for the other viewer
                msg = "Previous model was created for a different viewer; please generate the model again."
            else:
                msg = "Create a fence model or a full model first."
            QMessageBox.information(self, "3D viewer", msg)
            return
        if web:
            self.open_web_viewer()                  # the plugin's own WebGL viewer
        else:
            self.open_qgis_3d_view()                # the native QGIS 3D Map View

    def _zoom_to_model(self):
        """Zoom the main canvas to the 3D layers: a new 3D Map View takes this extent."""
        try:
            from qgis.core import QgsCoordinateTransform, QgsRectangle
            canvas = self.iface.mapCanvas()
            dest = canvas.mapSettings().destinationCrs()
            box = QgsRectangle()
            box.setNull()
            for ids in self.model_layers.values():
                for lid in ids:
                    lyr = QgsProject.instance().mapLayer(lid)
                    if lyr is None or lyr.extent().isNull():
                        continue
                    ext = lyr.extent()
                    if lyr.crs() != dest and lyr.crs().isValid() and dest.isValid():
                        ext = QgsCoordinateTransform(lyr.crs(), dest,
                                                     QgsProject.instance().transformContext()
                                                     ).transformBoundingBox(ext)
                    box.combineExtentWith(ext)
            if not box.isNull() and not box.isEmpty():
                box.scale(1.15)
                canvas.setExtent(box)
                canvas.refresh()
        except Exception as e:                       # framing is a convenience only
            core.log_warning(f"3D Map View: zoom to the model failed: {type(e).__name__}: {e}")

    def open_qgis_3d_view(self):
        """Open the native QGIS 3D Map View (Qt3D on OpenGL/Metal; no WebGL, no QtWebEngine)."""
        from qgis.PyQt.QtGui import QAction
        with _busy():
            self._apply_lithology_visibility()
            self._zoom_to_model()
        action = None
        # QgisInterface.actionNew3DMapCanvas() where the build has it, else the action of the
        # main window by its object name (View > New 3D Map View)
        getter = getattr(self.iface, "actionNew3DMapCanvas", None)
        if callable(getter):
            try:
                action = getter()
            except Exception as e:
                core.log_warning(f"3D Map View: iface.actionNew3DMapCanvas failed: "
                                 f"{type(e).__name__}: {e}")
        if action is None:
            action = self.iface.mainWindow().findChild(QAction, "mActionNew3DMapCanvas")
        if action is None or not action.isEnabled():
            self.iface.messageBar().pushMessage(
                "3D", "The 3D layers are in the project, but this QGIS cannot open the 3D Map View "
                "from the plugin (QGIS built without 3D support?). Try View > New 3D Map View.",
                level=Qgis.MessageLevel.Warning, duration=15)
            return
        action.trigger()

    def open_web_viewer(self):
        """Show all models created in this session in the plugin's own WebGL viewer."""
        failure = None
        with _busy():
            try:
                cols = dict(self.scene3d_colors)
                cols.setdefault("Borehole", core.QColor(20, 20, 20))
                title = "Boreholes - " + " + ".join(self.scene3d)
                lithos = {lit for sol in self.scene3d.values() for _faces, lit in sol}
                scene = viewer3d.build_scene(list(self.scene3d.items()), cols,
                                             list(self.scene3d_bhs.values()), title,
                                             edges=self.chk_edges.isChecked(),
                                             textures=self._textures(lithos))
                path = viewer3d.write_html(scene)
            except Exception as e:
                failure = e
        if failure is not None:
            e = failure
            QMessageBox.warning(self, "3D viewer", f"{type(e).__name__}: {e}")
            return
        if not viewer3d_window.open_in_browser(path):
            QMessageBox.warning(self, "3D viewer", f"The web browser could not be started. "
                                                   f"Open this file manually:\n{path}")

    def _refresh_dem(self):
        self.cb_dem.clear()
        self.cb_dem.addItem("(none - straight line between the borehole collars)", None)
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsRasterLayer):
                self.cb_dem.addItem(lyr.name(), lyr.id())

    def _dem_func(self):
        lid = self.cb_dem.currentData()
        if not lid:
            return None
        lyr = QgsProject.instance().mapLayer(lid)
        if lyr is None:
            return None
        prov = lyr.dataProvider() if lyr.isValid() else None
        tr = None
        if prov is not None and lyr.crs().isValid():
            tr = QgsCoordinateTransform(core.work_crs(), lyr.crs(),
                                        QgsProject.instance().transformContext())
        if prov is None or tr is None or not tr.isValid():
            QMessageBox.warning(self, "DEM", f"The raster layer \"{lyr.name()}\" is not usable "
                                "(invalid layer, data provider or coordinate system) - "
                                "the model is built without the DEM.")
            return None

        warned = []

        def f(x, y):
            try:
                v, ok = prov.sample(tr.transform(QgsPointXY(x, y)), 1)
                return float(v) if (ok and math.isfinite(v)) else None   # NoData may be NaN
            except (QgsCsException, ValueError, RuntimeError) as e:
                if not warned:
                    warned.append(1)
                    core.log_warning(f"DEM: a point could not be sampled ({type(e).__name__}: {e})")
                return None
        # does the raster cover the boreholes at all? (another region / only NoData: the model
        # would silently follow the straight line between the collars)
        collars = [(b.x, b.y) for b in self.boreholes]
        if collars and all(f(x, y) is None for x, y in collars):
            QMessageBox.warning(self, "DEM", f"The raster layer \"{lyr.name()}\" gives no elevation at "
                                "any borehole (it lies in another area, or has only NoData there) - "
                                "the model is built without the DEM.")
            return None
        return f

    def build_network(self):
        if not self._need_data():
            return
        bhs = model3d.drop_coincident([b for b in self._bh_subset() if b.layers])
        failure = None
        with _busy():
            try:
                edges = model3d.neighbour_edges(
                    bhs, "delaunay" if self.cb_net.currentIndex() == 1 else "grid", self.sp_maxe.value())
            except Exception as e:
                failure = e
        if failure is not None:
            e = failure
            QMessageBox.warning(self, "3D fence model", f"{type(e).__name__}: {e}")
            return
        if not edges:
            QMessageBox.warning(self, "3D fence model",
                                "No pair of neighbouring boreholes found - increase the longest panel.")
            return
        colors, opts, bedrock, _strat = self._section_options()

        def make_geo(A, B, L):
            proj = core.project_boreholes(
                [QgsPointXY(A.x, A.y), QgsPointXY(B.x, B.y)], [A, B], 1e9)
            return core.section_geo(proj, opts[0], opts[3], opts[4], opts[5], opts[6], opts[7],
                                    bedrock)
        dem = self._dem_func()
        failure = None
        core.take_geometry_issues()
        with _busy():
            try:
                solids, volume = model3d.network_solids(
                    bhs, edges, make_geo, self.sp_pw.value(), self.sp_nslab.value(),
                    self.sp_dz.value(), dem, self.sp_colw.value(), self.chk_fill.isChecked(),
                    self.sp_fillcell.value(), self.sp_maxe.value(), 60000,
                    "blocks" if self.cb_fillmode.currentIndex() == 1 else "smooth")
            except Exception as e:
                failure = e
        if failure is not None:
            e = failure
            QMessageBox.warning(self, "3D fence model", str(e))
            return
        self._report_geometry_issues()
        cols = dict(colors)
        cols["Borehole"] = core.QColor(20, 20, 20)
        self._finish_model({"Fence panels": solids, "Fence volume": volume}, cols,
                           "3D fence network", solids, volume, bhs,
                           f"Fence network ({len(edges)} panels" + (", DEM" if dem else "") + ")")

    # ---- closing / reopening the dialog: the red section line must not stay on the map
    def _leave_map(self):
        if self.canvas.mapTool() is self.tool:
            self.tool._finished = True     # a deliberate leave: deactivate() must not emit aborted
            self.canvas.unsetMapTool(self.tool)    # (it would show the dialog being closed again)
        self._drop_line_rb()

    def reject(self):                      # Esc
        self._leave_map()
        super().reject()

    def closeEvent(self, e):               # the [X] button
        self._leave_map()
        super().closeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        if self.section_pts and self.line_rb is None and self.canvas.mapTool() is not self.tool:
            # the line stays defined: draw it again when the dialog is reopened
            self.line_rb = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
            self.line_rb.setColor(QColor(220, 0, 0))
            self.line_rb.setWidth(2)
            self.line_rb.setToGeometry(QgsGeometry.fromPolylineXY(list(self.section_pts)),
                                       core.work_crs())

    def _project_cleared(self):
        """A new / closed project: the borehole layer and the line on the canvas are gone."""
        with contextlib.suppress(RuntimeError):
            self._drop_line_rb()
        self.bh_layer_id = None

    def _layer_will_be_removed(self, layer_id):
        if layer_id == self.bh_layer_id:
            self.bh_layer_id = None

    def cleanup(self):
        with contextlib.suppress(TypeError, RuntimeError):      # already disconnected / deleted
            QgsProject.instance().cleared.disconnect(self._project_cleared)
            QgsProject.instance().layerWillBeRemoved.disconnect(self._layer_will_be_removed)
        with contextlib.suppress(TypeError, RuntimeError):      # already disconnected / deleted
            self.tool.aborted.disconnect(self.show)
        for v in list(self.viewers):
            v.close()
        self.viewers.clear()
        viewer3d_window.cleanup_files()             # files given to a web browser a while ago
        viewer3d_window.stop_timers()               # no deletion timer stays in the Qt event loop
        with contextlib.suppress(RuntimeError):       # the canvas may already be deleted when QGIS is closing
            self._drop_line_rb()
            if self.canvas is not None:
                self.tool._finished = True
                self.canvas.unsetMapTool(self.tool)
                sc = self.canvas.scene()
                if sc is not None:
                    sc.removeItem(self.tool.rb)
