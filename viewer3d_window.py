"""Window of the own 3D viewer (see viewer3d.py).

The viewer is a self-contained HTML page (WebGL2).  It is shown in an embedded QtWebEngine
window when QGIS provides QtWebEngine, otherwise - or when the embedded browser has no WebGL -
the page is opened in the default web browser.
"""
import atexit
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
    except (ImportError, AttributeError, RuntimeError, OSError, SystemError) as e:   # not installed / failed to initialise / missing shared library
        core.log_warning(f"3D viewer: module {module_name} is not usable: {type(e).__name__}: {e}")
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
_BROWSER_TIMERS = {}               # path -> QTimer that deletes the file (stopped on unload)
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
            t = _BROWSER_TIMERS.pop(p, None)
            if t is not None:
                t.deleteLater()
            viewer3d.remove_file(p)
        timer = QTimer()                  # kept here (not a bare singleShot), so unload can stop it
        timer.setSingleShot(True)
        timer.timeout.connect(_later)
        _BROWSER_TIMERS[path] = timer
        timer.start(BROWSER_KEEP_MS)
    return ok


def cleanup_files(min_age=15.0):
    """Plugin unload / QGIS exit: delete the files given to a web browser at least `min_age`
    seconds ago (the browser has read them by then; F5 after QGIS is closed needs 'Save as HTML');
    younger ones are left to the timer / the age cleanup, so they never stay for hours."""
    now = time.time()
    for p, t in list(_BROWSER_FILES.items()):
        if now - t >= min_age:
            _BROWSER_FILES.pop(p, None)
            _stop_timer(p)
            viewer3d.remove_file(p)


def _stop_timer(path):
    timer = _BROWSER_TIMERS.pop(path, None)
    if timer is not None:
        with contextlib.suppress(RuntimeError):          # the C++ object may be gone already
            timer.stop()
            timer.deleteLater()


_ATEXIT = [False]


def _register_atexit():
    """cleanup_files at the end of QGIS (once; stop_timers takes it back when the plugin is
    unloaded, so a reloaded module does not leave a second registration behind)."""
    if not _ATEXIT[0]:
        atexit.register(cleanup_files)
        _ATEXIT[0] = True


def stop_timers():
    """Plugin unload: stop every pending deletion timer (nothing of the plugin stays in the Qt
    event loop); the files they would delete are removed by cleanup_files or, when they are
    younger than its limit, by the age cleanup at the next start."""
    for p in list(_BROWSER_TIMERS):
        _stop_timer(p)
    if _ATEXIT[0]:
        atexit.unregister(cleanup_files)
        _ATEXIT[0] = False


_register_atexit()                   # QGIS closed without unloading the plugin


def release_file(path):
    """The window does not need its file any more: delete it, unless a web browser was started
    with it (that file is removed by the timer of open_in_browser)."""
    _register_atexit()                   # the plugin is in use again after an unload
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
        self._page = None
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
            self._profile = profile_cls()               # no C++ parent: dispose() controls its life
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

            self._page = _ViewerPage(self._profile, self.view)   # Python reference: the overridden
            self.view.setPage(self._page)                        # virtual methods need a live wrapper
            return True
        except Exception as e:
            self._profile = None
            core.log_warning(f"3D viewer: private web profile failed: {type(e).__name__}: {e} - "
                             f"the embedded viewer is not opened (navigation could not be "
                             f"restricted)")
            return False

    def _navigation_allowed(self, url, is_main_frame=True):
        """Only the loaded viewer file and about:blank.  The PNG download needs no navigation
        (it goes through downloadRequested), so blob: URLs are refused everywhere - in
        Chromium a blob: URL shares the origin of its parent file:// page."""
        scheme = url.scheme()
        if scheme == "about":                # (no data: pages - they would escape the CSP)
            return url.toString() == "about:blank"
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
            # the page is self-contained: it needs no other file, no remote content, no storage, no plugins
            for name in ("LocalContentCanAccessFileUrls", "LocalContentCanAccessRemoteUrls",
                         "LocalStorageEnabled", "PluginsEnabled", "JavascriptCanOpenWindows",
                         "AllowRunningInsecureContent"):
                if hasattr(attr, name):
                    st.setAttribute(getattr(attr, name), False)
        except Exception as e:
            core.log_warning(f"3D viewer: WebGL setting not applied: {type(e).__name__}: {e}")

    # ---- model
    def load(self, path, title=""):
        if self.view is None:
            raise ViewerUnavailable("the viewer window was already disposed")
        old = self.path
        if old and old != path and self.view.url().scheme() == "file":
            # the faults the user added in the open window go into the new model, like when the
            # model is opened in a web browser or saved
            self._fetch_faults(lambda faults: self._load_with_faults(path, title, old, faults))
            return
        self._load_now(path, title, old)

    def _load_with_faults(self, path, title, old, faults):
        if self.view is None:                       # disposed while the page was asked
            return
        if faults and os.path.isfile(path) and not os.path.islink(path):
            try:
                with open(path, "r", encoding="utf-8") as src:
                    html = viewer3d.with_faults(src.read(), faults)
                with open(path, "w", encoding="utf-8", newline="") as out:
                    out.write(html)
            except (OSError, RuntimeError) as e:
                core.log_warning(f"3D viewer: the faults were not carried over: {e}")
        self._load_now(path, title, old)

    def _load_now(self, path, title, old):
        if old and old != path:
            release_file(old)                       # the previous model file is not needed
        self.path = path
        self._connect_downloads()
        self.lbl.setText(title)
        self.view.load(QUrl.fromLocalFile(path))
        self.show()
        self.raise_()
        self.activateWindow()

    def _loaded(self, ok):
        if self.view is None:                       # disposed meanwhile
            return
        if self.view.url().scheme() != "file":      # about:blank set when the window is closed
            return
        if not ok:
            # loadFinished(False) of an older, already replaced file must not raise the dialog
            current = self.view.url().toLocalFile()
            if (self.path and current
                    and os.path.realpath(current) == os.path.realpath(self.path)
                    and os.path.isfile(self.path)):
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
    def _fetch_faults(self, done):
        """Ask the page for the faults the user added; done(list) gets the cleaned list
        ([] when the page cannot answer)."""
        def _got(result):
            done(viewer3d.sanitize_faults(result))
        if self.view is None:
            done([])
            return
        try:
            self.view.page().runJavaScript(
                "window.__getFaults ? JSON.stringify(window.__getFaults()) : '[]'", _got)
        except Exception as e:                      # page or view already gone
            core.log_warning(f"3D viewer: faults could not be read from the page: {type(e).__name__}: {e}")
            done([])

    def in_browser(self):
        if not self.path:
            return
        path = self.path

        def _open(faults):
            if not os.path.isfile(path):            # the window was closed / a new model loaded meanwhile
                return
            if not faults:
                open_in_browser(path)
                return
            try:                                    # the browser gets a copy that includes the faults
                with open(path, "r", encoding="utf-8") as fh:
                    copy = viewer3d.write_html_text(viewer3d.with_faults(fh.read(), faults))
            except (OSError, RuntimeError) as e:
                core.log_warning(f"3D viewer: the faults were not passed to the browser: {e}")
                open_in_browser(path)
                return
            open_in_browser(copy)
        self._fetch_faults(_open)

    def _may_overwrite(self, path):
        """True when `path` does not exist or the user agrees to replace it (the file dialog asks
        only about the name as typed, so a file that exists under the name with the added suffix
        would be replaced silently)."""
        if not os.path.exists(path):
            return True
        yes = QMessageBox.StandardButton.Yes
        return QMessageBox.question(self, "Save", f"{os.path.basename(path)} already exists. Replace it?",
                                    yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) == yes

    def save_html(self):
        src_path = self.path                        # before the dialog: closing the window clears self.path
        if not src_path:
            return
        dst, _ = QFileDialog.getSaveFileName(self, "Save the 3D viewer", "borehole_model_3d.html",
                                             "HTML (*.html)")
        if not dst:
            return
        if not dst.lower().endswith((".html", ".htm")):
            dst += ".html"
            if not self._may_overwrite(dst):        # the dialog checked the name without the suffix
                return

        def _write(faults):                         # the faults added in the viewer are saved too
            if not src_path or not os.path.isfile(src_path):        # the window was closed / a new model loaded meanwhile
                QMessageBox.warning(self, "Save", "The 3D viewer file no longer exists; create the model again.")
                return
            try:
                with open(src_path, "r", encoding="utf-8") as src:
                    html = viewer3d.with_faults(src.read(), faults)
                with open(dst, "w", encoding="utf-8", newline="") as out:
                    out.write(html)
            except (OSError, RuntimeError) as e:
                QMessageBox.warning(self, "Save", str(e))
        self._fetch_faults(_write)

    def _download(self, item):
        """Screenshot (PNG) button of the viewer."""
        name = os.path.basename(item.suggestedFileName() or "") or "model3d.png"
        dst, _ = QFileDialog.getSaveFileName(self, "Save the image", name, "PNG (*.png)")
        if not dst:
            item.cancel()
            return
        if not dst.lower().endswith(".png"):
            dst += ".png"
            if not self._may_overwrite(dst):        # the dialog checked the name without the suffix
                item.cancel()
                return
        if not hasattr(item, "setDownloadDirectory"):
            core.log_warning("3D viewer: QWebEngineDownloadRequest.setDownloadDirectory is missing")
            item.cancel()
            return
        item.setDownloadDirectory(os.path.dirname(dst))
        item.setDownloadFileName(os.path.basename(dst))
        item.accept()

    def reject(self):
        """Esc: QDialog.reject() only hides the window; close() runs closeEvent (WebGL context
        and temporary file are released)."""
        self.close()

    def closeEvent(self, e):
        if self.view is not None:
            with contextlib.suppress(RuntimeError):         # the C++ view may be gone already
                self.view.setUrl(QUrl("about:blank"))       # frees the WebGL context and the model
        self._disconnect_downloads()
        if self.path:
            release_file(self.path)                        # big temporary file is not kept
            self.path = None
        super().closeEvent(e)

    def dispose(self):
        """Final close (plugin unload): released in the order view -> page -> profile
        (deleteLater events run in the order they were posted).  Safe to call twice."""
        self.close()
        self._disconnect_downloads()
        view, page, profile = self.view, self._page, self._profile
        self.view = self._page = self._profile = None      # no use of deleted C++ objects later
        if view is not None:
            if page is not None:                    # 1. detach our page from the view: PyQt6 does not
                page_cls = _first_import(("qgis.PyQt.QtWebEngineCore",), "QWebEnginePage")
                with contextlib.suppress(RuntimeError, TypeError):    # accept setPage(None), so the view
                    if page_cls is not None:                          # gets a fresh page of the default
                        view.setPage(page_cls(view))                  # profile and lets go of ours
            view.deleteLater()                      # 2. the view
        if profile is not None:                     # only our own page (the view owns the default one)
            if page is not None:
                page.deleteLater()                  # 3. the page
            profile.deleteLater()                   # 4. the profile, queued after the page
        self.deleteLater()
