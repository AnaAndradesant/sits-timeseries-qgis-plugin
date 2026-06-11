# -*- coding: utf-8 -*-
import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from .sits_dockwidget import SITSDockWidget


class SITSTimeSeriesPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.dock_widget = None
        self.action = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, "SITS Time Series", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu("SITS Time Series", self.action)

    def toggle_dock(self, checked):
        if self.dock_widget is None:
            self.dock_widget = SITSDockWidget(self.iface)
            self.iface.addDockWidget(2, self.dock_widget)  # Qt.RightDockWidgetArea = 2
            self.dock_widget.visibilityChanged.connect(self.action.setChecked)

        if checked:
            self.dock_widget.show()
        else:
            self.dock_widget.hide()

    def unload(self):
        self.iface.removePluginRasterMenu("SITS Time Series", self.action)
        self.iface.removeToolBarIcon(self.action)
        if self.dock_widget:
            self.iface.removeDockWidget(self.dock_widget)
            self.dock_widget.cleanup()
            self.dock_widget = None
