from qgis.PyQt.QtCore import Qt, QRectF, pyqtSignal
from qgis.PyQt.QtGui import (QColor, QImage, QPainter, QPdfWriter, QPageSize, QPageLayout)
from qgis.PyQt.QtSvg import QSvgGenerator
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget, QGroupBox, QFormLayout,
    QComboBox, QCheckBox, QPushButton, QLabel, QPlainTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QDoubleSpinBox, QGraphicsView, QFileDialog,
    QMessageBox, QLineEdit, QSplitter, QSpinBox,
)
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
    QgsPointXY, QgsProject,
)
from qgis.gui import QgsColorButton, QgsFileWidget, QgsMapTool, QgsRubberBand

from . import core, model3d


# ---------------------------------------------------------------- map tool
class SectionLineTool(QgsMapTool):
    lineFinished = pyqtSignal(list)
    cancelled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.points = []
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
            pts, self.points = self.points, []
            self.rb.reset(Qgis.GeometryType.Line)
            if len(pts) >= 2:
                self.lineFinished.emit(pts)
            else:
                self.cancelled.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.points = []
            self.rb.reset(Qgis.GeometryType.Line)
            self.cancelled.emit()

    def deactivate(self):
        self.rb.reset(Qgis.GeometryType.Line)
        super().deactivate()


# ------------------------------------------------------------ section view
class _View(QGraphicsView):
    def wheelEvent(self, e):
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.scale(f, f)


class SectionViewer(QDialog):
    def __init__(self, scene, parent=None, proj=None, rebuild=None, add3d=None):
        super().__init__(parent)
        self.add3d = add3d
        self.setWindowTitle("Geological cross section")
        self.resize(1550, 780)
        self.scene = scene
        self.rebuild = rebuild
        self.view = _View(scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.view)
        self.panel = None
        if proj is not None and rebuild is not None and len(proj) > 1:
            side = QWidget()
            sl = QVBoxLayout(side)
            self.panel = FaultPanel(proj)
            sl.addWidget(self.panel)
            btn = QPushButton("Regenerate section")
            btn.clicked.connect(self.regenerate)
            sl.addWidget(btn)
            split.addWidget(side)
            split.setStretchFactor(0, 3)
            split.setStretchFactor(1, 2)
        lay.addWidget(split)
        row = QHBoxLayout()
        for label, fn in (("Fit", self.fit), ("Export PNG", self.export_png),
                          ("Export SVG", self.export_svg), ("Export PDF", self.export_pdf)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        if add3d is not None:
            b3 = QPushButton("Add to 3D fence model")
            b3.clicked.connect(self._add3d)
            row.addWidget(b3)
            self.lbl_add = QLabel("")
            row.addWidget(self.lbl_add)
        row.addStretch()
        row.addWidget(QLabel("Mouse wheel = zoom, drag = pan"))
        lay.addLayout(row)

    def _add3d(self):
        n = self.add3d(self.scene.geo)
        self.lbl_add.setText(f"added - {n} section(s) in the 3D fence model")

    def regenerate(self):
        faults = self.panel.faults() or None
        try:
            scene = self.rebuild(faults)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        old = self.scene
        self.scene = scene
        self.view.setScene(scene)
        self.fit()
        old.deleteLater()

    def showEvent(self, e):
        super().showEvent(e)
        self.fit()

    def fit(self):
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _ask(self, filt, ext):
        path, _ = QFileDialog.getSaveFileName(self, "Export", "", filt)
        if path and not path.lower().endswith(ext):
            path += ext
        return path

    def export_png(self):
        path = self._ask("PNG (*.png)", ".png")
        if not path:
            return
        r = self.scene.sceneRect()
        img = QImage(int(r.width() * 2), int(r.height() * 2), QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.white)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.scene.render(p, QRectF(img.rect()), r)
        p.end()
        img.save(path)

    def export_svg(self):
        path = self._ask("SVG (*.svg)", ".svg")
        if not path:
            return
        r = self.scene.sceneRect()
        g = QSvgGenerator()
        g.setFileName(path)
        g.setSize(r.size().toSize())
        g.setViewBox(QRectF(0, 0, r.width(), r.height()))
        p = QPainter(g)
        self.scene.render(p, QRectF(0, 0, r.width(), r.height()), r)
        p.end()

    def export_pdf(self):
        path = self._ask("PDF (*.pdf)", ".pdf")
        if not path:
            return
        w = QPdfWriter(path)
        w.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        w.setPageOrientation(QPageLayout.Orientation.Landscape)
        p = QPainter(w)
        target = QRectF(0, 0, p.device().width(), p.device().height())
        self.scene.render(p, target, self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        p.end()


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
        self.setMinimumWidth(560)

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
             ("depth", "Total depth", ["depth", "hloub", "total", "delka"])]
LY_FIELDS = [("name", "Borehole name", ["name", "borehole", "vrt", "nazev", "id", "sonda", "oznaceni"]),
             ("top", "Layer from [m]", ["from", "od", "top", "start", "begin", "zacatek", "pocatek"]),
             ("bottom", "Layer to [m]", ["to", "do", "bottom", "end", "konec"]),
             ("litho", "Lithology", ["litho", "lit", "type", "druh", "typ", "hornin", "zemin", "petro"]),
             ("strat", "Stratigraphy (optional)", ["strat", "period", "perioda", "utvar", "vek", "age", "system", "geolog"])]


class BoreholeDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.setWindowTitle("Borehole Geology Sections")
        self.resize(640, 640)
        self.boreholes, self.colors, self.section_pts = [], {}, None
        self.hdr = {"bh": None, "ly": None}
        self.line_rb = None
        self.tool = SectionLineTool(self.canvas)
        self.tool.lineFinished.connect(self._line_done)
        self.tool.cancelled.connect(self._line_cancel)
        self.viewers = []
        self.forms = {}

        tabs = QTabWidget()
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        tabs.addTab(self._tab_data(), "1. Data")
        tabs.addTab(self._tab_colors(), "2. Lithology colours")
        tabs.addTab(self._tab_2d(), "3. 2D section")
        tabs.addTab(self._tab_3d(), "4. 3D model")

    # ---- tabs
    def _file_group(self, title, fields, key):
        box = QGroupBox(title)
        form = QFormLayout(box)
        fw = QgsFileWidget()
        fw.setFilter("CSV files (*.csv *.txt);;All files (*)")
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
        lay.addWidget(b1)
        lay.addWidget(b2)
        self.chk_neg = QCheckBox("Coordinates are given without minus sign (add it - S-JTSK only)")
        self.chk_neg.setChecked(True)
        self.chk_swap = QCheckBox("Swap the two coordinate columns (X<->Y / lon<->lat)")
        lay.addWidget(self.chk_neg)
        lay.addWidget(self.chk_swap)
        self.cb_crs.currentIndexChanged.connect(self._crs_changed)
        self._crs_changed()
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
        lay.addWidget(self.log)
        return w

    def _crs_changed(self, *_):
        key = self.cb_crs.currentData()
        _, _, lx, ly, kx, ky = core.INPUT_CRS[key]
        form, combos = self.forms["bh"]
        form.labelForField(combos["x"]).setText(lx)
        form.labelForField(combos["y"]).setText(ly)
        self.chk_neg.setEnabled(key == "sjtsk")
        hdr = self.hdr.get("bh")
        if hdr:
            combos["x"].setCurrentIndex(core.guess_column(hdr, kx))
            combos["y"].setCurrentIndex(core.guess_column(hdr, ky))

    def _tab_colors(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.tbl = QTableWidget(0, 2)
        self.tbl.setHorizontalHeaderLabels(["Lithology", "Colour"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.tbl)
        lay.addWidget(QLabel("Colours are filled after loading data and used in 2D and 3D."))
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
        self.chk_dip = QCheckBox("Follow the dip of neighbouring boreholes")
        self.chk_dip.setChecked(True)
        self.chk_trans = QCheckBox("Jagged boundary for lateral change of lithology")
        self.chk_trans.setChecked(True)
        self.chk_strat = QCheckBox("Highlight boundaries between stratigraphic sequences")
        self.chk_strat.setChecked(True)
        self.chk_lab = QCheckBox("Show labels")
        self.chk_lab.setChecked(True)
        lay.addWidget(self.chk_corr)
        lay.addWidget(self.chk_merge)
        lay.addWidget(self.chk_dip)
        lay.addWidget(self.chk_trans)
        lay.addWidget(self.chk_strat)
        lay.addWidget(self.chk_lab)
        b3 = QPushButton("Create cross section")
        b3.clicked.connect(self.create_section)
        lay.addWidget(b3)
        lay.addStretch()
        return w

    def _tab_3d(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.fences = []
        com = QGroupBox("Common settings")
        cf = QFormLayout(com)
        self.sp_dz = QDoubleSpinBox()
        self.sp_dz.setRange(0.25, 50)
        self.sp_dz.setValue(2.0)
        self.sp_dz.setSuffix(" m")
        cf.addRow("Vertical step of the blocks", self.sp_dz)
        self.chk_sel = QCheckBox("Use only boreholes selected in the 'Boreholes' map layer")
        cf.addRow(self.chk_sel)
        lay.addWidget(com)

        ga = QGroupBox("A) Grid model - blocks interpolated on a square grid")
        fa = QFormLayout(ga)
        self.sp_cell = QDoubleSpinBox()
        self.sp_cell.setRange(0, 5000)
        self.sp_cell.setSpecialValueText("auto")
        self.sp_cell.setSuffix(" m")
        fa.addRow("Grid cell size", self.sp_cell)
        self.cb_gmode = QComboBox()
        self.cb_gmode.addItems(["Horizontal layers", "Layers parallel to the ground surface"])
        fa.addRow("Layer geometry (Quaternary always follows the surface)", self.cb_gmode)
        self.sp_k = QSpinBox()
        self.sp_k.setRange(1, 12)
        self.sp_k.setValue(4)
        fa.addRow("Nearest boreholes used for a cell", self.sp_k)
        self.sp_maxd = QDoubleSpinBox()
        self.sp_maxd.setRange(0, 100000)
        self.sp_maxd.setSpecialValueText("unlimited")
        self.sp_maxd.setSuffix(" m")
        fa.addRow("Max. distance from a borehole", self.sp_maxd)
        bg = QPushButton("Create grid model")
        bg.clicked.connect(self.create_grid)
        fa.addRow(bg)
        lay.addWidget(ga)

        gb = QGroupBox("B) Fence model - 3D from 2D cross sections")
        fb = QFormLayout(gb)
        fb.addRow(QLabel("Add sections with 'Add to 3D fence model' in the section window,\n"
                         "or create perpendicular E-W and N-S sections automatically:"))
        self.sp_nx = QSpinBox()
        self.sp_nx.setRange(0, 20)
        self.sp_nx.setValue(2)
        fb.addRow("Number of N-S sections", self.sp_nx)
        self.sp_ny = QSpinBox()
        self.sp_ny.setRange(0, 20)
        self.sp_ny.setValue(2)
        fb.addRow("Number of E-W sections", self.sp_ny)
        self.sp_fd = QDoubleSpinBox()
        self.sp_fd.setRange(1, 100000)
        self.sp_fd.setValue(80)
        self.sp_fd.setSuffix(" m")
        fb.addRow("Max. distance of boreholes from an automatic section", self.sp_fd)
        ba = QPushButton("Create automatic sections")
        ba.clicked.connect(self.auto_fences)
        fb.addRow(ba)
        self.sp_slab = QDoubleSpinBox()
        self.sp_slab.setRange(0.5, 500)
        self.sp_slab.setValue(8)
        self.sp_slab.setSuffix(" m")
        fb.addRow("Slab length (horizontal resolution)", self.sp_slab)
        self.sp_width = QDoubleSpinBox()
        self.sp_width.setRange(0.2, 200)
        self.sp_width.setValue(4)
        self.sp_width.setSuffix(" m")
        fb.addRow("Slab width (thickness of the fence)", self.sp_width)
        self.lbl_fence = QLabel("0 sections prepared")
        fb.addRow(self.lbl_fence)
        row = QHBoxLayout()
        bb = QPushButton("Build fence model")
        bb.clicked.connect(self.build_fence)
        bc = QPushButton("Clear sections")
        bc.clicked.connect(self.clear_fences)
        row.addWidget(bb)
        row.addWidget(bc)
        fb.addRow(row)
        lay.addWidget(gb)
        lay.addWidget(QLabel("Open the result with View > New 3D Map View "
                             "(raise the vertical scale in the 3D view settings)."))
        lay.addStretch()
        return w

    # ---- data
    def _msg(self, s):
        self.log.appendPlainText(s)

    def _file_changed(self, key, path):
        if not path:
            return
        try:
            header, _ = core.read_table(path, self.delims[key].currentData())
        except Exception as e:
            self._msg(f"Cannot read {path}: {e}")
            return
        self.hdr[key] = header
        combos = self.forms[key][1]
        crs_key = self.cb_crs.currentData() if hasattr(self, "cb_crs") else "sjtsk"
        for k, _, kw in self.fields[key]:
            if key == "bh" and k == "x":
                kw = core.INPUT_CRS[crs_key][4]
            elif key == "bh" and k == "y":
                kw = core.INPUT_CRS[crs_key][5]
            cb = combos[k]
            cb.clear()
            if k == "strat":
                cb.addItem("(none)")
                cb.addItems(header)
                cb.setCurrentIndex(core.guess_column(header, kw, default=-1) + 1)
            else:
                cb.addItems(header)
                cb.setCurrentIndex(core.guess_column(header, kw))

    def load_data(self):
        p1, p2 = self.fw_bh.filePath(), self.fw_ly.filePath()
        if not p1 or not p2:
            QMessageBox.warning(self, "Missing file", "Select both CSV files.")
            return
        try:
            h1, r1 = core.read_table(p1, self.delims["bh"].currentData())
            h2, r2 = core.read_table(p2, self.delims["ly"].currentData())
            m1 = {k: c.currentIndex() for k, c in self.cb_bh.items()}
            m2 = {k: c.currentIndex() for k, c in self.cb_ly.items()}
            m2["strat"] = self.cb_ly["strat"].currentIndex() - 1   # -1 = none
            self.boreholes, warns = core.build_boreholes(
                h1, r1, m1, h2, r2, m2, self.cb_crs.currentData(),
                self.chk_neg.isChecked(), self.chk_swap.isChecked())
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        self.log.clear()
        n_l = sum(len(b.layers) for b in self.boreholes)
        self._msg(f"Loaded {len(self.boreholes)} boreholes, {n_l} layers "
                  f"(input CRS: {self.cb_crs.currentText()}).")
        if self.boreholes:
            b = self.boreholes[0]
            self._msg(f"First borehole {b.name}: S-JTSK QGIS x={b.x:.2f}, y={b.y:.2f} (EPSG:5514)")
        for w in warns[:200]:
            self._msg("WARNING: " + w)
        lithos = core.lithologies(self.boreholes)
        defaults = core.default_colors(lithos)
        self.tbl.setRowCount(len(lithos))
        self.btns = {}
        for i, lit in enumerate(lithos):
            it = QTableWidgetItem(lit)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tbl.setItem(i, 0, it)
            cb = QgsColorButton()
            cb.setColor(defaults[lit])
            self.tbl.setCellWidget(i, 1, cb)
            self.btns[lit] = cb

    def current_colors(self):
        return {lit: b.color() for lit, b in getattr(self, "btns", {}).items()}

    def _need_data(self):
        if not self.boreholes:
            QMessageBox.warning(self, "No data", "Load data first (tab 1).")
            return False
        return True

    def add_points(self):
        if self._need_data():
            QgsProject.instance().addMapLayer(core.make_borehole_point_layer(self.boreholes))
            self.iface.mapCanvas().zoomToFullExtent()

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
        if self.line_rb:
            self.line_rb.reset(Qgis.GeometryType.Line)
            self.line_rb = None
        self.tool.points = []
        self.tool.rb.reset(Qgis.GeometryType.Line)
        self.lbl_line.setText("No section line defined.")

    def _line_cancel(self):
        self.canvas.unsetMapTool(self.tool)
        self.show()

    def _line_done(self, pts):
        self.canvas.unsetMapTool(self.tool)
        src = self.canvas.mapSettings().destinationCrs()
        self._set_line(pts, src)
        self.show()

    def use_selected(self):
        lyr = self.iface.activeLayer()
        if lyr is None or not hasattr(lyr, "selectedFeatures") or not lyr.selectedFeatures():
            QMessageBox.warning(self, "Selection", "Select a line feature in the active layer.")
            return
        g = lyr.selectedFeatures()[0].geometry()
        if g.type() != Qgis.GeometryType.Line:
            QMessageBox.warning(self, "Selection", "Selected feature is not a line.")
            return
        pts = g.asMultiPolyline()[0] if g.isMultipart() else g.asPolyline()
        self._set_line(pts, lyr.crs())

    def _set_line(self, pts, src_crs):
        dst = QgsCoordinateReferenceSystem(core.CRS_SJTSK)
        tr = QgsCoordinateTransform(src_crs, dst, QgsProject.instance())
        self.section_pts = [tr.transform(QgsPointXY(p)) for p in pts]
        length = QgsGeometry.fromPolylineXY(self.section_pts).length()
        self.lbl_line.setText(f"Section line: {len(pts)} vertices, length {length:.1f} m")
        if self.line_rb:
            self.line_rb.reset(Qgis.GeometryType.Line)
        self.line_rb = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
        self.line_rb.setColor(QColor(220, 0, 0))
        self.line_rb.setWidth(2)
        self.line_rb.setToGeometry(QgsGeometry.fromPolylineXY(list(pts)), None)

    def create_section(self):
        if not self._need_data():
            return
        if not self.section_pts:
            QMessageBox.warning(self, "No line", "Define the section line first.")
            return
        proj = core.project_boreholes(self.section_pts, self.boreholes, self.sp_off.value())
        if not proj:
            QMessageBox.warning(self, "No boreholes",
                                "No borehole within the given distance of the line.")
            return
        colors, opts, bedrock, strat = self._section_options()
        line = list(self.section_pts)

        def build(faults, proj=proj, colors=colors, opts=opts, bedrock=bedrock, strat=strat,
                  line=line):
            sc = core.build_section_scene(proj, colors, *opts, faults, bedrock, strat)
            sc.geo["line"] = line
            return sc

        v = SectionViewer(build(None), self, proj, build, self.add_fence)
        self.viewers.append(v)
        v.show()

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
            for lyr in QgsProject.instance().mapLayersByName("Boreholes"):
                if hasattr(lyr, "selectedFeatures"):
                    names |= {str(f["name"]) for f in lyr.selectedFeatures()}
            sub = [b for b in bhs if b.name in names]
            if len(sub) >= 2:
                return sub
            QMessageBox.information(self, "Selection",
                                    "Fewer than two boreholes are selected in the 'Boreholes' "
                                    "layer - all boreholes are used.")
        return bhs

    def _show_3d_result(self, count, n, err, what):
        self.iface.messageBar().pushMessage(
            "3D", f"{what}: {n} blocks in {count} layers. Open View > New 3D Map View.",
            level=Qgis.MessageLevel.Success, duration=10)
        if err:
            QMessageBox.warning(
                self, "3D style",
                "The layers were created, but the 3D style could not be applied:\n\n" + err +
                "\n\nSet it manually: Layer Properties > 3D View > single symbol, "
                "altitude clamping 'absolute', extrusion = the height in the layer name.")

    def create_grid(self):
        if not self._need_data():
            return
        try:
            prisms, info = model3d.grid_model(
                self._bh_subset(), self.sp_cell.value(), self.sp_dz.value(),
                "depth" if self.cb_gmode.currentIndex() == 1 else "elevation",
                self.sp_k.value(), self.sp_maxd.value())
        except Exception as e:
            QMessageBox.warning(self, "3D grid model", str(e))
            return
        count, n, err = model3d.build_layers(prisms, self.current_colors(), "3D grid model")
        self._show_3d_result(count, n, err,
                             f"Grid model ({info['cells']} cells of {info['cell']:.1f} m)")

    def add_fence(self, geo):
        if not geo.get("line"):
            return len(self.fences)
        self.fences.append(geo)
        self._fence_label()
        return len(self.fences)

    def _fence_label(self):
        self.lbl_fence.setText(f"{len(self.fences)} section(s) prepared for the fence model")

    def clear_fences(self):
        self.fences = []
        self._fence_label()

    def auto_fences(self):
        if not self._need_data():
            return
        bhs = self._bh_subset()
        xs, ys = [b.x for b in bhs], [b.y for b in bhs]
        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
        pad = 0.05 * max(xmax - xmin, ymax - ymin) + 10
        lines = []
        ny, nx = self.sp_ny.value(), self.sp_nx.value()
        for i in range(ny):
            y = ymin + (i + 1) * (ymax - ymin) / (ny + 1)
            lines.append([QgsPointXY(xmin - pad, y), QgsPointXY(xmax + pad, y)])
        for i in range(nx):
            x = xmin + (i + 1) * (xmax - xmin) / (nx + 1)
            lines.append([QgsPointXY(x, ymin - pad), QgsPointXY(x, ymax + pad)])
        colors, opts, bedrock, strat = self._section_options()
        made = 0
        for pts in lines:
            proj = core.project_boreholes(pts, bhs, self.sp_fd.value())
            if len(proj) < 2:
                continue
            sc = core.build_section_scene(proj, colors, *opts, None, bedrock, strat)
            sc.geo["line"] = pts
            self.fences.append(sc.geo)
            made += 1
        self._fence_label()
        self.iface.messageBar().pushMessage(
            "3D", f"{made} of {len(lines)} automatic sections created "
                  "(sections with fewer than two boreholes are skipped).",
            level=Qgis.MessageLevel.Info, duration=8)

    def build_fence(self):
        if not self.fences:
            QMessageBox.warning(self, "3D fence model",
                                "No sections prepared. Add sections from the section window "
                                "or create automatic sections.")
            return
        prisms = []
        for geo in self.fences:
            line = QgsGeometry.fromPolylineXY(geo["line"])

            def locate(d, line=line):
                p = line.interpolate(d).asPoint()
                return (p.x(), p.y())
            prisms += model3d.fence_prisms(geo, locate, line.length(), self.sp_width.value(),
                                           self.sp_slab.value(), self.sp_dz.value())
            if len(prisms) > 200000:
                QMessageBox.warning(self, "3D fence model",
                                    "Too many blocks - increase the slab length or the "
                                    "vertical step.")
                return
        if not prisms:
            QMessageBox.warning(self, "3D fence model", "The sections contain no polygons.")
            return
        count, n, err = model3d.build_layers(prisms, self.current_colors(), "3D fence model")
        self._show_3d_result(count, n, err, f"Fence model ({len(self.fences)} sections)")

    def cleanup(self):
        if self.line_rb:
            self.line_rb.reset(Qgis.GeometryType.Line)
        self.canvas.unsetMapTool(self.tool)
