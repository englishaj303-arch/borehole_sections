"""Window of the own 3D viewer (see viewer3d.py).

The viewer is a self-contained HTML page (WebGL2).  It is shown in an embedded QtWebEngine
window when QGIS provides QtWebEngine, otherwise - or when the embedded browser has no WebGL -
the page is opened in the default web browser.
"""
import importlib
import os

from qgis.PyQt.QtCore import Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                                 QPushButton, QVBoxLayout)

from . import core

_ENGINE = {}


def _webengine_flags():
    """Recent Chromium versions switch WebGL off when there is no usable GPU driver (remote
    desktops, virtual machines, old / blocklisted graphics cards) unless the software
    rasteriser (SwiftShader) is allowed explicitly.  The flags are read when QtWebEngine starts,
    so they work only if nothing in this QGIS session has started it yet (restart QGIS once)."""
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    for f in ("--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"):
        if f not in flags:
            flags += " " + f
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = flags.strip()


_webengine_flags()


def _try_import(module_name, attr):
    """Attribute `attr` of module `module_name`, or None when the module is not installed or
    fails to initialise."""
    try:
        return getattr(importlib.import_module(module_name), attr, None)
    except Exception:                              # not installed / failed to initialise
        return None


def _first_import(module_names, attr):
    """First non-None `attr` found in the given modules (tried in order), else None."""
    for name in module_names:
        found = _try_import(name, attr)
        if found is not None:
            return found
    return None


def _engine():
    """QWebEngineView class or None when QtWebEngine is not installed."""
    if "view" not in _ENGINE:
        _ENGINE["view"] = _first_import(
            ("qgis.PyQt.QtWebEngineWidgets", "PyQt6.QtWebEngineWidgets",
             "PyQt5.QtWebEngineWidgets"), "QWebEngineView")
    return _ENGINE["view"]


def webengine_available():
    return _engine() is not None


def pattern_tiles(codes, size=256):
    """FGDC pattern tiles for the viewer: {code: (PNG bytes of a size x size tile with the black
    pattern on a transparent background, aspect = width / height of the original tile)}."""
    from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice
    from qgis.PyQt.QtGui import QImage
    from . import patterns
    out = {}
    for code in set(codes):
        if code not in patterns.NAMES:
            continue
        img = QImage(patterns.tile_path(code))
        if img.isNull():
            continue
        asp = img.width() / max(img.height(), 1)
        img = img.convertToFormat(QImage.Format.Format_ARGB32).scaled(
            size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        out[code] = (bytes(ba.data()), asp)
    return out


def open_in_browser(path):
    return QDesktopServices.openUrl(QUrl.fromLocalFile(path))


class Viewer3DWindow(QDialog):
    """Non-modal window with the embedded viewer; one window is reused for new models."""

    webgl_failed = pyqtSignal()          # the embedded browser has no WebGL 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("3D viewer - Borehole Geology")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinMaxButtonsHint)
        self.resize(1100, 740)
        self.path = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        bar = QHBoxLayout()
        self.lbl = QLabel("")
        bar.addWidget(self.lbl, 1)
        for text, tip, fn in (
                ("Open in web browser", "Open the same viewer in your default browser "
                                       "(useful when the embedded view is slow).", self.in_browser),
                ("Save as HTML...", "Save the viewer with the model as ONE self-contained file "
                                    "that works in any browser and can be sent to colleagues.",
                 self.save_html)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
        lay.addLayout(bar)
        self.view = _engine()(self)
        self._enable_webgl()
        lay.addWidget(self.view, 1)
        self.view.loadFinished.connect(self._loaded)
        try:
            self.view.page().profile().downloadRequested.connect(self._download)
        except Exception as e:                      # PNG export would then not be offered
            core.log_warning(f"3D viewer: downloads not available: {type(e).__name__}: {e}")

    def _enable_webgl(self):
        try:
            mod = importlib.import_module(type(self.view).__module__)
            settings_cls = _first_import(
                ("qgis.PyQt.QtWebEngineCore", "PyQt6.QtWebEngineCore",
                 "PyQt5.QtWebEngineWidgets"), "QWebEngineSettings") or getattr(
                mod, "QWebEngineSettings")
            attr = getattr(settings_cls, "WebAttribute", settings_cls)
            st = self.view.settings()
            for name in ("WebGLEnabled", "Accelerated2dCanvasEnabled"):
                if hasattr(attr, name):
                    st.setAttribute(getattr(attr, name), True)
        except Exception as e:
            core.log_warning(f"3D viewer: WebGL setting not applied: {type(e).__name__}: {e}")

    # ---- model
    def load(self, path, title=""):
        self.path = path
        self.lbl.setText(title)
        self.view.load(QUrl.fromLocalFile(path))
        self.show()
        self.raise_()
        self.activateWindow()

    def _loaded(self, ok):
        if self.view.url().scheme() != "file":      # about:blank set when the window is closed
            return
        if not ok:
            QMessageBox.warning(self, "3D viewer", "The viewer page could not be loaded.")
            return
        self.view.page().runJavaScript(
            "window.__webgl2_ok === true ? 'ok' : 'fail: ' + (window.__webgl_info || '')",
            self._webgl_checked)

    def _webgl_checked(self, result):
        if result == "ok":
            return
        info = str(result or "")
        core.log_warning(f"3D viewer: the embedded browser has no WebGL 2 ({info}); "
                         f"QTWEBENGINE_CHROMIUM_FLAGS={os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')}")
        self.hide()
        self.webgl_failed.emit()
        QMessageBox.information(
            self.parent() or None, "3D viewer",
            "The embedded browser of QGIS has no WebGL 2 on this computer, so the model is "
            "opened in your web browser instead (the next models will go there directly).\n\n"
            "If you want the embedded window: restart QGIS once (the plugin now allows the "
            "software renderer when QGIS starts its embedded browser) and untick "
            "'Open the viewer in the web browser' in tab 3D model.")
        if self.path:
            open_in_browser(self.path)

    # ---- buttons
    def in_browser(self):
        if self.path:
            open_in_browser(self.path)

    def save_html(self):
        if not self.path:
            return
        dst, _ = QFileDialog.getSaveFileName(self, "Save the 3D viewer", "borehole_model_3d.html",
                                             "HTML (*.html)")
        if not dst:
            return
        try:
            with open(self.path, "rb") as src, open(dst, "wb") as out:
                out.write(src.read())
        except OSError as e:
            QMessageBox.warning(self, "Save", str(e))

    def _download(self, item):
        """Screenshot (PNG) button of the viewer."""
        if not self.isActiveWindow():
            return
        name = item.suggestedFileName() or "model3d.png"
        dst, _ = QFileDialog.getSaveFileName(self, "Save the image", name, "PNG (*.png)")
        if not dst:
            item.cancel()
            return
        if hasattr(item, "setDownloadDirectory"):          # Qt 6
            item.setDownloadDirectory(os.path.dirname(dst))
            item.setDownloadFileName(os.path.basename(dst))
        else:                                              # Qt 5
            item.setPath(dst)
        item.accept()

    def closeEvent(self, e):
        self.view.setUrl(QUrl("about:blank"))              # frees the WebGL context and the model
        super().closeEvent(e)
