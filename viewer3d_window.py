"""Helpers of the own 3D viewer (see viewer3d.py).

The viewer is a self-contained HTML page (WebGL2).  The plugin opens it in the default web
browser (open_in_browser) and removes the temporary file later (cleanup_files, stop_timers);
pattern_tiles prepares the lithologic pattern tiles for the page.
"""
import atexit
import contextlib
import time

from qgis.PyQt.QtCore import Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices

from . import core, viewer3d


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
        try:
            saved = img.save(buf, "PNG")
        finally:
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
    _register_atexit()                   # the plugin is in use again after an unload
    if not viewer3d.is_viewer_file(path):      # only the file made by write_html, nothing else
        return False
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
