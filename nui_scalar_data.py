import os
import sys
import threading

import PyQt5.QtWidgets as QtWidgets
from PyQt5.QtWidgets import QAction, QDockWidget, QVBoxLayout
from PyQt5.QtGui import QIcon
import PyQt5.QtCore as QtCore
from PyQt5.QtCore import pyqtSignal, pyqtSlot, QObject

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsMessageLog,
    QgsProject,
    QgsVectorDataProvider,
    QgsVectorLayer,
)

# For running on OSX. In Ubuntu, use python3.8
# I just copied directly from the Ubuntu dslmeta install ... did not build on OSX.
sys.path.append("/opt/dsl/lib/python3.11/site-packages")
# And this is where lcm got installed by default.
sys.path.append("/usr/local/lib/python3.11/site-packages")

import lcm
from ini import dive_t  # for origin_latitude, origin_longitude


import inspect

cmd_folder = os.path.split(inspect.getfile(inspect.currentframe()))[0]


class NuiScalarDataDockWidget(QDockWidget):
    def __init__(self, parent=None):
        super(NuiScalarDataDockWidget, self).__init__(parent)

        self.vbox = QVBoxLayout()
        # self.add_field(channel, msg_type, msg_field, layer_name)
        self.add_button = QtWidgets.QPushButton("Add Field")
        self.add_button.clicked.connect(self.add_button_clicked)

        self.vbox.addWidget(self.add_button)

        self.my_widget = QtWidgets.QWidget()
        self.my_widget.setLayout(self.vbox)

        self.setWidget(self.my_widget)
        self.setWindowTitle("NUI Scalar Data")

    def add_button_clicked(self, _checked):
        # TODO: query other fields, then call
        # self.add_field(channel, msg_type, msg_field, layer_name)
        print("add_button_clicked")


# Trying to inherit from QObject for now so I can set up signals/slots
class NuiScalarDataPlugin(QObject):
    received_origin = pyqtSignal(float, float)

    def __init__(self, iface):
        """
        Re-use the appropriate layers if they exist, in order to let the user
        save stylings in their QGIS project.
        QUESTION(lindzey): Does subclassing the QgsPluginLayer help with this?
        """
        print("__init__")
        super(NuiScalarDataPlugin, self).__init__()
        self.iface = iface

        # layer_name -> QgsVectorLayer to add features to
        self.layers = {}
        # layer_name -> np.array where 1st column is time and 2nd is data
        self.data = {}

        # Everything NUI does is in the AlvinXY coordinate frame, with origin
        # as defined in the DIVE_INI message. So, we can't set up layers until
        # the first message has been received.
        self.projection_initialized = False
        self.lc = lcm.LCM("udpm://239.255.76.67:7667?ttl=0")
        self.subscribers = {}
        self.subscribers["DIVE_INI"] = self.lc.subscribe(
            "DIVE_INI", self.handle_dive_ini
        )

        # I don't think initialize_origin is a slot ... how to register?
        self.received_origin.connect(self.initialize_origin)

    def handle_dive_ini(self, channel, data):
        print("handle_dive_ini")
        QgsMessageLog.logMessage("handle_dive_ini")
        msg = dive_t.decode(data)
        QgsMessageLog.logMessage(
            f"Got map origin: {msg.origin_longitude}, {msg.origin_latitude}; unsubscribing from {channel}"
        )
        self.lc.unsubscribe(self.subscribers[channel])
        self.received_origin.emit(msg.origin_longitude, msg.origin_latitude)

    @pyqtSlot(float, float)
    def initialize_origin(self, lon0, lat0):
        print("initialize_origin")
        self.lon0 = lon0
        self.lat0 = lat0
        crs = QgsCoordinateReferenceSystem()
        crs.createFromProj(
            f"+proj=ortho +lat_0={self.lat0} +lon_0={self.lon0} +ellps=WGS84"
        )
        self.crs_name = "NuiXY"
        crs.saveAsUserCrs(self.crs_name)
        self.projection_initialized = True

        self.setup_layers()

    def setup_layers(self):
        """
        Needs to be called after we've received the message with origin data.
        """
        print("setup_layers")
        # To start with, only add the cursor to the map
        self.root = QgsProject.instance().layerTreeRoot()
        print("got root. Is none? ", self.root is None)
        self.nui_group = self.root.findGroup("NUI")
        if self.nui_group is None:
            self.nui_group = self.root.insertGroup(0, "NUI")

        try:
            self.cursor_layer = self.nui_group.findLayer("Scalar Data Cursor")
        except Exception as ex:
            print(ex)
            self.cursor_layer = None
        print("Tried to find cursor_layer. Is none?", self.cursor_layer is None)
        # TODO: Probably also need to check whether it's the right type of layer...
        if self.cursor_layer is None:
            self.cursor_layer = QgsVectorLayer(
                f"Point?crs={self.crs_name}&field=time:string(30)&index=yes",
                "Scalar Data Cursor",
                "memory",
            )
            print("...Created cursor_layer")
        # TODO(lindzey): AUUUUGH. This gives a warning since parent object is in another thread. I think I need to set up signals/slots maybe?
        QgsProject.instance().addMapLayer(self.cursor_layer, False)
        self.nui_group.addLayer(self.cursor_layer)
        print("...Added cursor_layer to map")

        # TODO: Any other default ones?
        channel = "NUI_SBE49_DATA"
        msg_field = "sbe49.sbe49_t.temperature"
        layer_name = "temperature"
        # self.add_field(channel, msg_type, msg_field, layer_name)
        print("done with setup_layers")

    def initGui(self):
        """
        Required method; called when plugin loaded.
        """
        print("initGui")
        icon = os.path.join(os.path.join(cmd_folder, "nui.png"))
        self.action = QAction(
            QIcon(icon), "Display scalar data from NUI", self.iface.mainWindow()
        )
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu("&NUI Scalar Data", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        """
        Required method; called when plugin unloaded.
        """
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu("&NUI Scalar Data", self.action)
        del self.action

    def add_field(self, channel, msg_type, msg_field, layer_name):
        """
        Subscribe to specified data and plot in both map and profile view.
        """
        key = f"{channel}/{msg_field}"
        print(f"add_field for key={key}")
        self.layers[key] = QgsVectorLayer(
            f"Point?crs={self.crs_name}&field=value:double&index=yes",
            layer_name,
            "memory",
        )
        QgsProject.instance().addMapLayer(self.layer)

        # QUESTION: Can we have multiple subscriptions to the same topic?
        # (e.g. if I want temperature and salinity ...)
        self.subscribers[key] = self.lc.subscribe(
            channel,
            lambda channel, data, msg_type=msg_type, msg_field=msg_field: self.handle_data(
                msg_type, msg_field, channel, data
            ),
        )

    def handle_data(self, msg_type_str, msg_field, channel, data):
        pass

    def spin_lcm(self):
        print("spin_lcm")
        QgsMessageLog.logMessage("spin_lcm")
        count = 0
        while True:
            self.lc.handle()
            count += 1
            if count > 1000:
                QgsMessageLog.logMessage("stopping spin_lcm")
                print("stopping spin_lcm")
                break

    def run(self):
        print("run")

        self.iface.messageBar().pushMessage("Hello from Plugin")

        lcm_thread = threading.Thread(target=self.spin_lcm)
        lcm_thread.start()

        self.dockwidget = NuiScalarDataDockWidget()
        self.iface.addDockWidget(QtCore.Qt.BottomDockWidgetArea, self.dockwidget)
        self.dockwidget.show()
        print("Done with dockwidget")

        # This function MUST return, or QGIS will block

        # TODO: Add dockwidget!
