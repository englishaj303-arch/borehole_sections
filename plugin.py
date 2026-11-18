import os

from qgis.PyQt.QtGui import QAction, QIcon
from qgis.core import Qgis, QgsApplication, QgsMessageLog

from .gui import BoreholeDialog
from . import patterns, viewer3d

MENU = "&Borehole Geology"


class BoreholePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dlg = None

    def initGui(self):
        try:
            viewer3d.remove_old_files()      # viewer files left by a QGIS closed within the hour
        except Exception as e:               # advisory cleanup only
            QgsMessageLog.logMessage(
                f"Cleanup of old 3D viewer files failed: {e}",
                "Borehole Geology",
                Qgis.MessageLevel.Warning,
            )
        path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(path) if os.path.exists(path) else QgsApplication.getThemeIcon("/mIconLineLayer.svg")
        self.action = QAction(icon, "Borehole Geology Sections", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu(MENU, self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu(MENU, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action.deleteLater()
            self.action = None
        if self.dlg:
            self.dlg.cleanup()
            self.dlg.deleteLater()
            self.dlg = None
        patterns.clear_cache()              # no QPixmap / QIcon may outlive QGuiApplication

    def run(self):
        if self.dlg is None:
            self.dlg = BoreholeDialog(self.iface, self.iface.mainWindow())
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
