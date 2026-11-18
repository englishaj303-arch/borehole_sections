"""Window of the own 3D viewer (see viewer3d.py).

The viewer is a self-contained HTML page (WebGL2).  It is shown in an embedded QtWebEngine
window when QGIS provides QtWebEngine, otherwise - or when the embedded browser has no WebGL -
the page is opened in the default web browser.
"""
import contextlib
import importlib
import os
import time

from qgis.PyQt.QtCore import Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                                 QPushButton, QVBoxLayout)


from . import core, viewer3d

_ENGINE = {}


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
            ("qgis.PyQt.QtWebEngineWidgets",), "QWebEngineView")
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
        saved = img.save(buf, "PNG")
        buf.close()
        if not saved:
            core.log_warning(f"3D viewer: pattern tile {code} could not be encoded")
            continue
        out[code] = (bytes(ba.data()), asp)
    return out


_BROWSER_FILES = {}                # path -> time when the web browser was started
BROWSER_KEEP_MS = 60 * 60 * 1000      # F5 in the browser must keep working for an hour


def open_in_browser(path):
    """Open the viewer file in the system browser (it starts asynchronously, so the file is
    deleted only an hour later, never at once; a file left by a QGIS closed in the meantime is
    removed by the age cleanup at the next start of the plugin or the next viewer)."""
    ok = QDesktopServices.openUrl(QUrl.fromLocalFile(path))
    if ok and path not in _BROWSER_FILES:
        _BROWSER_FILES[path] = time.time()

        def _later(p=path):
            _BROWSER_FILES.pop(p, None)
            viewer3d.remove_file(p)
        QTimer.singleShot(BROWSER_KEEP_MS, _later)
    return ok


def cleanup_files(min_age=60.0):
    """Plugin unload: delete the files given to a web browser at least `min_age` seconds ago
    (the browser has surely read them); younger ones are left to the timer / the age cleanup."""
    now = time.time()
    for p, t in list(_BROWSER_FILES.items()):
        if now - t >= min_age:
            _BROWSER_FILES.pop(p, None)
            viewer3d.remove_file(p)


def release_file(path):
    """The window does not need its file any more: delete it, unless a web browser was started
    with it (that file is removed by the timer of open_in_browser)."""
    if path and path not in _BROWSER_FILES:
        viewer3d.remove_file(path)


class ViewerUnavailable(RuntimeError):
    """The embedded viewer cannot be opened with the navigation control (own page with
    _navigation_allowed); the caller shows the model in the system web browser instead."""


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
        self._profile = None
        self._dl_profile = None            # profile whose downloadRequested is connected
        if not self._make_private_page():
            # The default QWebEngine page would NOT check the navigation (_navigation_allowed),
            # so the embedded viewer is not opened at all in that case.
            self.view.deleteLater()
            self.view = None
            self.setParent(None)           # let go of the parent; the unreferenced dialog is freed
            raise ViewerUnavailable("the page with the navigation control could not be created")
        self._enable_webgl()
        lay.addWidget(self.view, 1)
        self.view.loadFinished.connect(self._loaded)
        self._connect_downloads()

    def _make_private_page(self):
        """Own anonymous (off-the-record) profile: the viewer shares neither cookies, cache nor
        the download handler with the other web views of QGIS, and its page checks every
        navigation (_navigation_allowed).  Returns False when that page could not be made; the
        view then still has the default page WITHOUT the navigation control and must not be
        used."""
        profile_cls = _first_import(("qgis.PyQt.QtWebEngineCore",), "QWebEngineProfile")
        page_cls = _first_import(("qgis.PyQt.QtWebEngineCore",), "QWebEnginePage")
        if profile_cls is None or page_cls is None:
            core.log_warning("3D viewer: private web profile not available - the embedded "
                             "viewer is not opened (navigation could not be restricted)")
            return False
        try:
            self._profile = profile_cls(self)
            window = self

            class _ViewerPage(page_cls):
                """Page that shows only the viewer file itself (no navigation to other files,
                links or dropped URLs)."""

                def acceptNavigationRequest(self, url, nav_type, is_main_frame):
                    if not window._navigation_allowed(url, is_main_frame):
                        core.log_warning(f"3D viewer: navigation blocked: {url.toString()[:200]}")
                        return False
                    return super().acceptNavigationRequest(url, nav_type, is_main_frame)

                def createWindow(self, _type):
                    return None                    # window.open() / target=_blank never opens a window

            self.view.setPage(_ViewerPage(self._profile, self.view))   # page deleted before profile
            return True
        except Exception as e:
            self._profile = None
            core.log_warning(f"3D viewer: private web profile failed: {type(e).__name__}: {e} - "
                             f"the embedded viewer is not opened (navigation could not be "
                             f"restricted)")
            return False

    def _navigation_allowed(self, url, is_main_frame=True):
        """Only the loaded viewer file, about:blank and (not in the main frame) the blob: URLs
        made by the page for the PNG download."""
        scheme = url.scheme()
        if scheme == "about":                # (no data: pages - they would escape the CSP)
            return url.toString() == "about:blank"
        if scheme == "blob":
            return not is_main_frame
        if scheme == "file" and self.path:
            # realpath on both sides: /var/folders is a link to /private/var/folders on macOS
            return (os.path.normcase(os.path.realpath(url.toLocalFile()))
                    == os.path.normcase(os.path.realpath(self.path)))
        return False

    def _connect_downloads(self):
        if self._dl_profile is not None:
            return
        try:
            profile = self._profile                  # always the private profile (see __init__)
            profile.downloadRequested.connect(self._download)
            self._dl_profile = profile
        except Exception as e:                      # PNG export would then not be offered
            core.log_warning(f"3D viewer: downloads not available: {type(e).__name__}: {e}")

    def _disconnect_downloads(self):
        if self._dl_profile is None:
            return
        with contextlib.suppress(TypeError, RuntimeError):     # already disconnected / profile deleted
            self._dl_profile.downloadRequested.disconnect(self._download)
        self._dl_profile = None

    def _enable_webgl(self):
        try:
            mod = importlib.import_module(type(self.view).__module__)
            settings_cls = _first_import(
                ("qgis.PyQt.QtWebEngineCore",), "QWebEngineSettings") or getattr(
                mod, "QWebEngineSettings", None)
            if settings_cls is None:
                core.log_warning("3D viewer: QWebEngineSettings not found, WebGL setting not applied")
                return
            attr = getattr(settings_cls, "WebAttribute", settings_cls)
            st = self.view.settings()
            for name in ("WebGLEnabled", "Accelerated2dCanvasEnabled"):
                if hasattr(attr, name):
                    st.setAttribute(getattr(attr, name), True)
        except Exception as e:
            core.log_warning(f"3D viewer: WebGL setting not applied: {type(e).__name__}: {e}")

    # ---- model
    def load(self, path, title=""):
        if self.path and self.path != path:
            release_file(self.path)                 # the previous model file is not needed
        self.path = path
        self._connect_downloads()
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
        core.log_warning(f"3D viewer: the embedded browser has no WebGL 2 ({info})")
        self.hide()
        self.webgl_failed.emit()                # the dialog shows a message-bar note
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
        if not dst.lower().endswith((".html", ".htm")):
            dst += ".html"
        try:
            with open(self.path, "rb") as src, open(dst, "wb") as out:
                out.write(src.read())
        except OSError as e:
            QMessageBox.warning(self, "Save", str(e))

    def _download(self, item):
        """Screenshot (PNG) button of the viewer."""
        name = os.path.basename(item.suggestedFileName() or "") or "model3d.png"
        dst, _ = QFileDialog.getSaveFileName(self, "Save the image", name, "PNG (*.png)")
        if not dst:
            item.cancel()
            return
        if not dst.lower().endswith(".png"):
            dst += ".png"
        if hasattr(item, "setDownloadDirectory"):          # Qt 6
            item.setDownloadDirectory(os.path.dirname(dst))
            item.setDownloadFileName(os.path.basename(dst))
        else:                                              # Qt 5
            item.setPath(dst)
        item.accept()

    def reject(self):
        """Esc: QDialog.reject() only hides the window; close() runs closeEvent (WebGL context
        and temporary file are released)."""
        self.close()

    def closeEvent(self, e):
        self.view.setUrl(QUrl("about:blank"))              # frees the WebGL context and the model
        self._disconnect_downloads()
        if self.path:
            release_file(self.path)                        # big temporary file is not kept
            self.path = None
        super().closeEvent(e)

    def dispose(self):
        """Final close (plugin unload): released in the order view -> page -> profile
        (deleteLater events run in the order they were posted)."""
        self.close()
        self._disconnect_downloads()
        view = self.view
        page = view.page() if view is not None else None
        if view is not None:
            view.deleteLater()                      # 1. the view that shows the page
        if self._profile is not None:               # only our own page (the view owns the default one)
            if page is not None:
                page.deleteLater()                  # 2. the page
            self._profile.deleteLater()             # 3. the profile, when no page uses it
            self._profile = None
        self.deleteLater()
