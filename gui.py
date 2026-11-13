from qgis.PyQt.QtCore import Qt, QRectF, QSize, pyqtSignal
from qgis.PyQt.QtGui import (QColor, QGuiApplication, QIcon, QImage, QPainter, QPdfWriter,
                             QPageSize, QPageLayout, QPixmap)
from qgis.PyQt.QtSvg import QSvgGenerator
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget, QGroupBox, QFormLayout,
    QComboBox, QCheckBox, QPushButton, QLabel, QPlainTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QDoubleSpinBox, QGraphicsView, QFileDialog,
    QMessageBox, QSplitter, QSpinBox, QScrollArea, QFrame, QListWidget, QListWidgetItem,
)
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
    QgsPointXY, QgsProject, QgsRasterLayer,
)
from qgis.gui import QgsColorButton, QgsFileWidget, QgsMapTool, QgsRubberBand

from dataclasses import replace

from . import core, logs, model3d, patterns


# ---------------------------------------------------------------- map tool
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
    def wheelEvent(self, e):
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.scale(f, f)


class SectionViewer(QDialog):
    def __init__(self, scene, parent=None, proj=None, rebuild=None, strat0=True,
                 title="Geological cross section"):
        super().__init__(parent)
        self.setWindowTitle(title)
        _fit_to_screen(self, 1550, 780)
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
        row.addStretch()
        row.addWidget(QLabel("Mouse wheel = zoom, drag = pan"))
        lay.addLayout(row)
        self._apply_visibility()

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
        self._apply_visibility()
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
        if r.width() <= 0 or r.height() <= 0:
            QMessageBox.warning(self, "Export", "The scene is empty - nothing to export.")
            return
        img = QImage(int(r.width() * 2), int(r.height() * 2), QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.white)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.scene.render(p, QRectF(img.rect()), r)
        p.end()
        if not img.save(path):
            QMessageBox.warning(self, "Export", f"Could not write the file:\n{path}")

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
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.scene.render(p, QRectF(0, 0, r.width(), r.height()), r)
        p.end()

    def export_pdf(self):
        path = self._ask("PDF (*.pdf)", ".pdf")
        if not path:
            return
        w = QPdfWriter(path)
        w.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        r = self.scene.sceneRect()
        w.setPageOrientation(QPageLayout.Orientation.Portrait if r.height() > r.width()
                             else QPageLayout.Orientation.Landscape)
        p = QPainter()
        if not p.begin(w):
            QMessageBox.warning(self, "Export",
                                f"Could not write the PDF (is it open in another program?):\n{path}")
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        target = QRectF(0, 0, p.device().width(), p.device().height())
        self.scene.render(p, target, self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        p.end()


class LogViewer(SectionViewer):
    """Window of borehole profiles / sedimentary records with a side panel:
    the column of the layers table with the description, the column the profile (colours,
    patterns) is made from, merging of identical lithologies and a button to redraw."""

    def __init__(self, scene, parent, title, columns, desc0, prof0, merge0, rebuild):
        super().__init__(scene, parent, title=title)
        self.log_rebuild = rebuild
        side = QWidget()
        sl = QVBoxLayout(side)
        box = QGroupBox("Borehole log")
        form = QFormLayout(box)
        self.cb_desc = QComboBox()
        self.cb_desc.addItems(columns)
        self.cb_desc.setCurrentIndex(max(columns.index(desc0), 0) if desc0 in columns else 0)
        self.cb_prof = QComboBox()
        self.cb_prof.addItems(columns)
        self.cb_prof.setCurrentIndex(max(columns.index(prof0), 0) if prof0 in columns else 0)
        self.chk_merge = QCheckBox("Merge identical lithologies")
        self.chk_merge.setChecked(merge0)
        form.addRow("Lithological description from column", self.cb_desc)
        form.addRow("Lithological profile from column", self.cb_prof)
        form.addRow(self.chk_merge)
        btn = QPushButton("Regenerate")
        btn.clicked.connect(self.regenerate_log)
        sl.addWidget(box)
        sl.addWidget(btn)
        sl.addStretch()
        side.setMinimumWidth(300)
        self.split.addWidget(side)
        self.split.setStretchFactor(0, 4)
        self.split.setStretchFactor(1, 1)

    def regenerate_log(self):
        try:
            scene = self.log_rebuild(self.cb_desc.currentText(), self.cb_prof.currentText(),
                                     self.chk_merge.isChecked())
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
             ("depth", "Total depth", ["depth", "hloub", "total", "delka"]),
             ("azimuth", "Azimuth [deg] (optional)", ["azimuth", "azimut", "azim", "az", "smer", "direction", "bearing"]),
             ("dip", "Dip / deviation [deg] (optional)", ["dip", "deviation", "inclination", "incl", "sklon", "odklon", "angle", "tilt"])]
LY_FIELDS = [("name", "Borehole name", ["name", "borehole", "vrt", "nazev", "id", "sonda", "oznaceni"]),
             ("top", "Layer from [m]", ["from", "od", "top", "start", "begin", "zacatek", "pocatek"]),
             ("bottom", "Layer to [m]", ["to", "do", "bottom", "end", "konec"]),
             ("litho", "Lithology", ["litho", "lit", "type", "druh", "typ", "hornin", "zemin", "petro"]),
             ("strat", "Stratigraphy (optional)", ["strat", "period", "perioda", "utvar", "vek", "age", "system", "geolog"])]


def _fit_to_screen(widget, width, height):
    """Resize a window to the wanted size, but never larger than 90 % of the screen."""
    try:
        scr = widget.screen() or QGuiApplication.primaryScreen()
        area = scr.availableGeometry()
        width, height = min(width, int(area.width() * 0.9)), min(height, int(area.height() * 0.85))
    except (AttributeError, RuntimeError):
        pass
    widget.resize(width, height)


def _scrolled(widget):
    """Tab content in a scroll area, so a tall tab never makes the window taller than the screen."""
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setWidget(widget)
    return sa


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
        self.line_rb = None
        self.tool = SectionLineTool(self.canvas)
        self.tool.lineFinished.connect(self._line_done)
        self.tool.cancelled.connect(self._line_cancel)
        self.tool.aborted.connect(self.show)
        self.bh_layer_id = None
        self.viewers = []
        self.forms = {}

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
        strat_help = STRAT_HELP
        self.cb_ly["strat"].setToolTip(strat_help)
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
        self.log.setMaximumHeight(90)
        lay.addWidget(self.log)
        lay.addStretch()
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
        self.tbl = QTableWidget(0, 4)
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
        self.chk_lab = QCheckBox("Show labels")
        self.chk_lab.setChecked(True)
        lay.addWidget(self.chk_corr)
        lay.addWidget(self.chk_merge)
        lay.addWidget(self.chk_dip)
        lay.addWidget(self.chk_trans)
        lay.addWidget(self.chk_lab)
        b3 = QPushButton("Create cross section")
        b3.clicked.connect(self.create_section)
        lay.addWidget(b3)
        lay.addStretch()
        return w

    def _tab_3d(self):
        w = QWidget()
        lay = QVBoxLayout(w)
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

        gn = QGroupBox("A) Fence model - network of 2D sections between neighbouring boreholes")
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
        self.chk_fill = QCheckBox("Fill the space between the sections (volume model)")
        self.chk_fill.setToolTip("Calculates the areas between the 2D sections: every triangle of "
                                 "neighbouring boreholes is interpolated from its three sections.")
        fn.addRow(self.chk_fill)
        self.sp_fillcell = QDoubleSpinBox()
        self.sp_fillcell.setRange(0, 1000)
        self.sp_fillcell.setSpecialValueText("auto")
        self.sp_fillcell.setSuffix(" m")
        fn.addRow("Cell size of the filled volume", self.sp_fillcell)
        bnet = QPushButton("Create network fence model")
        bnet.clicked.connect(self.build_network)
        fn.addRow(bnet)
        lay.addWidget(gn)

        ga = QGroupBox("B) Grid model - blocks interpolated on a square grid")
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

        lay.addWidget(self._lithology_visibility_box())
        lay.addWidget(QLabel("Open the result with View > New 3D Map View "
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
            if k in ("strat", "azimuth", "dip"):
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
            m1["azimuth"] = self.cb_bh["azimuth"].currentIndex() - 1   # -1 = none
            m1["dip"] = self.cb_bh["dip"].currentIndex() - 1           # -1 = none
            m2 = {k: c.currentIndex() for k, c in self.cb_ly.items()}
            m2["strat"] = self.cb_ly["strat"].currentIndex() - 1       # -1 = none
            self.ly_header = list(h2)
            self.litho_col = h2[m2["litho"]]
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
        self.btns, self.grain_boxes, self.pat_boxes = {}, {}, {}
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
            pb = QComboBox()
            pb.setIconSize(QSize(48, 36))
            pb.setMaxVisibleItems(12)
            pb.addItem("(none)", "")
            for code in patterns.NAMES:
                ic = patterns.icon(code)
                if ic is not None:
                    pb.addItem(ic, patterns.label(code), code)
                else:
                    pb.addItem(patterns.label(code), code)
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
        return True

    def add_points(self):
        if self._need_data():
            lyr = core.make_borehole_point_layer(self.boreholes)
            QgsProject.instance().addMapLayer(lyr)
            self.bh_layer_id = lyr.id()
            self.iface.mapCanvas().zoomToFullExtent()

    def _bh_layers(self):
        """The borehole point layer added by this plugin (by id), else layers named 'Boreholes'."""
        lyr = QgsProject.instance().mapLayer(self.bh_layer_id) if self.bh_layer_id else None
        return [lyr] if lyr is not None else QgsProject.instance().mapLayersByName("Boreholes")

    # ---- borehole logs
    def logs_from_selection(self):
        if not self._need_data():
            return
        names = []
        for lyr in self._bh_layers():
            if hasattr(lyr, "selectedFeatures"):
                names += [str(f["name"]) for f in lyr.selectedFeatures()]
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

    def _log_view(self, bhs, desc_col, prof_col, merge):
        """Copies of the boreholes: litho = value of the profile column (colours, patterns),
        desc = value of the description column; optionally identical neighbours merged."""
        def value(L, col):
            if col == self.litho_col:
                return L.litho
            return ((L.extra or {}).get(col) or "").strip()

        out = []
        for b in bhs:
            lay = []
            for L in core.clean_layers(b.layers):
                prof = value(L, prof_col) or "(unknown)"
                lay.append(core.Layer(L.top, L.bottom, prof, L.age, L.extra,
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
        show_pat, show_strat = self.chk_logpat.isChecked(), self.chk_logstrat.isChecked()
        pscale = self.sp_patscale.value()

        def build(desc_col, prof_col, merge):
            view = self._log_view(bhs, desc_col, prof_col, merge)
            colors, grains, pats = self._log_style(view)
            if kind == "profiles":
                cmds = logs.build_profiles(view, colors, pats, mode, scale, logs.qt_measure(),
                                           show_pat, show_strat)
            else:
                cmds = logs.build_records(view, colors, grains, pats, scale, logs.qt_measure(),
                                          mode=mode)
            return logs.to_scene(cmds, pscale)

        names = ", ".join(b.name for b in bhs)
        title = ("Borehole profiles: " if kind == "profiles" else "Sedimentary record: ") + names
        col = self.litho_col
        v = LogViewer(build(col, col, False), self, title, self.ly_header, col, col, False, build)
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
            if sc is not None:
                sc.removeItem(self.line_rb)
            self.line_rb = None

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
        if g.isNull() or g.isEmpty():
            QMessageBox.warning(self, "Selection", "The selected feature has an empty geometry.")
            return
        parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        pts = parts[0] if parts else []
        if len(pts) < 2:
            QMessageBox.warning(self, "Selection", "The selected line needs at least two vertices.")
            return
        self._set_line(pts, lyr.crs())

    def _set_line(self, pts, src_crs):
        dst = QgsCoordinateReferenceSystem(core.CRS_SJTSK)
        tr = QgsCoordinateTransform(src_crs, dst, QgsProject.instance())
        new_pts = [tr.transform(QgsPointXY(p)) for p in pts]
        length = QgsGeometry.fromPolylineXY(new_pts).length() if len(new_pts) >= 2 else 0.0
        if length < 1e-6:
            QMessageBox.warning(self, "Section line",
                                "The section line has zero length - click two different points.")
            return
        self.section_pts = new_pts
        self.lbl_line.setText(f"Section line: {len(pts)} vertices, length {length:.1f} m")
        self._drop_line_rb()
        self.line_rb = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
        self.line_rb.setColor(QColor(220, 0, 0))
        self.line_rb.setWidth(2)
        self.line_rb.setToGeometry(QgsGeometry.fromPolylineXY(list(pts)), None)

    def _marker_radius(self):
        """Radius (m) of the circular borehole marker on the map at the current scale."""
        size_mm = 2.0
        try:
            for lyr in self._bh_layers():
                rend = lyr.renderer() if hasattr(lyr, "renderer") else None
                sym = rend.symbol() if rend is not None and hasattr(rend, "symbol") else None
                if sym is not None and sym.size() > 0:
                    size_mm = sym.size()
                    break
            scale = self.canvas.scale()
        except Exception as e:
            core.log_warning(f"Marker size not available: {type(e).__name__}: {e}")
            return core.OFF_LINE_TOL
        return max(size_mm / 2.0 * scale / 1000.0, 0.1)

    def create_section(self):
        if not self._need_data():
            return
        if not self.section_pts:
            QMessageBox.warning(self, "No line", "Define the section line first.")
            return
        try:
            proj = core.project_boreholes(self.section_pts, self.boreholes, self.sp_off.value(),
                                          self.sp_gap.value())
        except Exception as e:
            QMessageBox.warning(self, "2D section", f"{type(e).__name__}: {e}")
            return
        if not proj:
            QMessageBox.warning(self, "No boreholes",
                                "No borehole within the given distance of the line.")
            return
        colors, opts, bedrock, strat = self._section_options()
        pats = self.current_patterns()
        line = list(self.section_pts)
        tol = self.sp_dash.value() or self._marker_radius()

        def build(faults, proj=proj, colors=colors, opts=opts, bedrock=bedrock, strat=strat,
                  line=line, pats=pats, tol=tol):
            sc = core.build_section_scene(proj, colors, *opts, faults, bedrock, True, pats,
                                          off_tol=tol)
            sc.geo["line"] = line
            return sc

        try:
            v = SectionViewer(build(None), self, proj, build, strat)
        except Exception as e:
            QMessageBox.warning(self, "2D section", f"{type(e).__name__}: {e}")
            return
        self._track_viewer(v)
        v.show()

    def _track_viewer(self, v):
        """Keep a reference while the window is open; forget it when it is closed."""
        self.viewers.append(v)
        v.finished.connect(lambda _r=0, v=v: self.viewers.remove(v) if v in self.viewers else None)

    def _section_options(self):
        colors = self.current_colors()
        opts = (self.sp_vex.value(), self.chk_corr.isChecked(), self.chk_lab.isChecked(),
                self.sp_curv.value(), self.sp_round.value(), self.chk_merge.isChecked(),
                self.chk_dip.isChecked(), self.chk_trans.isChecked())
        return colors, opts, self.sp_bed.value(), False

    # ---- 3D
    def _bh_subset(self):
        bhs = self.boreholes
        if self.chk_sel.isChecked():
            names = set()
            for lyr in self._bh_layers():
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
            "3D", f"{what}: {n} objects in {count} layers. Open View > New 3D Map View.",
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
        try:
            prisms, info = model3d.grid_model(
                self._bh_subset(), self.sp_cell.value(), self.sp_dz.value(),
                "depth" if self.cb_gmode.currentIndex() == 1 else "elevation",
                self.sp_k.value(), self.sp_maxd.value())
        except Exception as e:
            QMessageBox.warning(self, "3D grid model", str(e))
            return
        reg = {}
        colors = self.current_colors()
        count, n, err = model3d.build_layers(model3d.prisms_to_solids(prisms), colors, "3D grid model",
                                             edges=self.chk_edges.isChecked(), registry=reg)
        self._register_models(reg, colors)
        self._show_3d_result(count, n, err,
                             f"Grid model ({info['cells']} cells of {info['cell']:.1f} m)")

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
        tr = QgsCoordinateTransform(QgsCoordinateReferenceSystem(core.CRS_SJTSK), lyr.crs(),
                                    QgsProject.instance())
        prov = lyr.dataProvider()

        def f(x, y):
            try:
                v, ok = prov.sample(tr.transform(QgsPointXY(x, y)), 1)
                return v if ok else None
            except Exception:
                return None
        return f

    def build_network(self):
        if not self._need_data():
            return
        bhs = model3d.drop_coincident([b for b in self._bh_subset() if b.layers])
        try:
            edges = model3d.neighbour_edges(
                bhs, "delaunay" if self.cb_net.currentIndex() == 1 else "grid", self.sp_maxe.value())
        except Exception as e:
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
        try:
            solids, volume = model3d.network_solids(
                bhs, edges, make_geo, self.sp_pw.value(), self.sp_nslab.value(),
                self.sp_dz.value(), dem, self.sp_colw.value(), self.chk_fill.isChecked(),
                self.sp_fillcell.value(), self.sp_maxe.value())
        except Exception as e:
            QMessageBox.warning(self, "3D fence model", str(e))
            return
        cols = dict(colors)
        cols["Borehole"] = core.QColor(20, 20, 20)
        reg = {}
        count, n, err = model3d.build_layers(solids, cols, "3D fence network",
                                             edges=self.chk_edges.isChecked(), registry=reg,
                                             volume=volume)
        self._register_models(reg, cols)
        self._show_3d_result(count, n, err, f"Fence network ({len(edges)} panels"
                                            + (", DEM" if dem else "") + ")")

    def cleanup(self):
        try:
            self.tool.aborted.disconnect(self.show)
        except (TypeError, RuntimeError):
            pass
        for v in list(self.viewers):
            v.close()
        self.viewers.clear()
        self._drop_line_rb()
        self.canvas.unsetMapTool(self.tool)
        sc = self.canvas.scene()
        if sc is not None:
            sc.removeItem(self.tool.rb)
