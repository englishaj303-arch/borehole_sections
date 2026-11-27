import os

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QAction, QIcon
from qgis.core import Qgis, QgsApplication, QgsMessageLog

from .gui import BoreholeDialog
from . import patterns, viewer3d, viewer3d_window

MENU = "&Borehole Geology"


class BoreholePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dlg = None

    def initGui(self):
        # viewer files left by a QGIS closed within the hour: removed a few seconds after the start,
        # so that a slow disk (network profile) does not delay the start of QGIS
        QTimer.singleShot(5000, self._remove_old_files)
        path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(path) if os.path.exists(path) else QgsApplication.getThemeIcon("/mIconLineLayer.svg")
        self.action = QAction(icon, "Borehole Geology Sections", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu(MENU, self.action)
        self.iface.addToolBarIcon(self.action)

    def _remove_old_files(self):
        if self.action is None:              # the plugin was unloaded in the meantime
            return
        try:
            viewer3d.remove_old_files()
        except Exception as e:               # advisory cleanup only
            QgsMessageLog.logMessage(
                f"Cleanup of old 3D viewer files failed: {e}",
                "Borehole Geology",
                Qgis.MessageLevel.Warning,
            )

    def unload(self):
        # every step on its own: a failing one (a locked temporary file on Windows...) must not
        # stop the rest, above all the release of the pixmaps
        def step(what, fn):
            try:
                fn()
            except Exception as e:
                QgsMessageLog.logMessage(f"Unload: {what} failed: {type(e).__name__}: {e}",
                                         "Borehole Geology", Qgis.MessageLevel.Warning)

        def remove_action():
            if self.action:
                self.iface.removePluginMenu(MENU, self.action)
                self.iface.removeToolBarIcon(self.action)
                self.action.deleteLater()
                self.action = None

        def close_dialog():
            if self.dlg:
                dlg, self.dlg = self.dlg, None
                dlg.cleanup()
                dlg.deleteLater()

        step("removing the menu item", remove_action)
        step("closing the dialog", close_dialog)
        step("stopping the timers", viewer3d_window.stop_timers)     # no QTimer stays in the event loop
        step("deleting temporary files", lambda: viewer3d_window.cleanup_files(min_age=0))
        step("clearing the pattern cache", patterns.clear_cache)     # no QPixmap may outlive QGuiApplication

    def _dlg_destroyed(self, *_):
        self.dlg = None              # destroyed from outside (QGIS closing): do not keep the reference

    def run(self):
        if self.dlg is None:
            self.dlg = BoreholeDialog(self.iface, self.iface.mainWindow())
            self.dlg.destroyed.connect(self._dlg_destroyed)
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
