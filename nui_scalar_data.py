import importlib
import os
import sys
import threading
import yaml

import PyQt5.QtWidgets as QtWidgets
import PyQt5.QtGui as QtGui
import PyQt5.QtCore as QtCore

import qgis.core
from qgis.core import (
    Qgis,
    QgsMessageLog,
    QgsProject,
)

# For running on OSX. In Ubuntu, use python3.8
# And this is where lcm got installed by default.
sys.path.append("/usr/local/lib/python3.11/site-packages")

import lcm

from .nui_scalar_data_widgets import (
    AddScalarDataFieldWidget,
    ConfigureTimeLimitsWidget,
    ConfigureTimeSeriesWidget,
    QHLine,
    QVLine,
)
from .nui_scalar_data_plotters import (
    MapLayerPlotter,
    TimeSeriesPlotter,
)

import inspect

cmd_folder = os.path.split(inspect.getfile(inspect.currentframe()))[0]


class NuiScalarDataMainWindow(QtWidgets.QMainWindow):
    # If I understand correctly, any slots decorated with @pyqtSlot will be
    # called in the thread that created the connection, NOT the thread that
    # emitted the signal. So, use that to get data from the LCM thread into
    # the main Widget thread.

    # Otherwise, trying to add features to the layer will give a warning since parent object is in another thread.
    new_data = QtCore.pyqtSignal(str, float, float)  # layer key, timestamp, value

    def __init__(self, iface, parent=None):
        super(NuiScalarDataMainWindow, self).__init__(parent)
        self.iface = iface
        self.lc = lcm.LCM("udpm://239.255.76.67:7667?ttl=0")

        self.map_layer_plotter = MapLayerPlotter(self.iface, self.lc)

        self.add_field_widget = AddScalarDataFieldWidget(self.iface)
        self.add_field_widget.new_field.connect(self.add_field)

        self.time_series_plotter = TimeSeriesPlotter()
        self.time_series_plotter.cursor_moved.connect(
            self.map_layer_plotter.update_cursor
        )

        self.time_limits_widget = ConfigureTimeLimitsWidget(self.iface)
        self.time_limits_widget.time_limits_changed.connect(
            self.time_series_plotter.set_time_limits
        )

        self.time_series_widget = ConfigureTimeSeriesWidget(self.iface)
        self.time_series_widget.toggle_plot.connect(
            self.time_series_plotter.toggle_visibility
        )
        self.time_series_widget.ylim_changed.connect(self.time_series_plotter.set_ylim)
        self.time_series_widget.remove_field.connect(self.remove_field)
        self.time_series_widget.remove_field.connect(
            self.time_series_plotter.remove_field
        )
        self.time_series_widget.remove_field.connect(
            self.map_layer_plotter.remove_field
        )

        self.config = {}  # This gets updated by the add_field method
        try:
            config_str, success = QgsProject.instance().readEntry(
                "nui_scalar_data", "subscriptions"
            )
            if success:
                self.loaded_config = yaml.safe_load(config_str)
                print(f"Loaded config! {self.loaded_config}")
            else:
                self.loaded_config = {}
        except Exception as ex:
            self.loaded_config = {}

        self.setup_ui()

        # TODO: I think there's a cleaner way to dynamically import LCM types,
        #   but this works for now.
        self.msg_modules = {}

        # TODO: We don't actually need this dict; information is available in self.config
        # layer_name -> sample rate
        self.sample_rates = {}
        # layer_name -> timestamp of most recently-added feature (used for decimation)
        self.last_updated = {}

        self.subscribers = {}

        self.new_data.connect(self.update_data)
        # For now, the parent class is handling throttling. Might make sense
        # to push that down into the child classes when I finish refactoring.
        # self.new_data.connect(self.map_layer_manager.update_data)
        # self.new_data.connect(self.scalar_plotter.update_data)

        self.update_timer = QtCore.QTimer()
        self.update_timer.timeout.connect(self.time_series_plotter.maybe_refresh)
        self.update_timer.setSingleShot(False)
        self.update_timer.start(500)  # ms

        self.shutdown = False

        self.update_subscriptions()  # Activate any subscriptions from the config

    def setup_ui(self):
        # TODO: set of QRadioButtons (or checkboxes?) for displaying individual
        #   chunks of data on the plot

        self.vbox = QtWidgets.QVBoxLayout()
        self.vbox.addWidget(self.add_field_widget)
        self.vbox.addWidget(QHLine())
        self.vbox.addWidget(self.time_limits_widget)
        self.vbox.addWidget(QHLine())
        self.vbox.addWidget(self.time_series_widget)
        self.vbox.addStretch(1.0)

        self.hbox = QtWidgets.QHBoxLayout()
        self.hbox.addLayout(self.vbox)
        self.hbox.addWidget(self.time_series_plotter.canvas, stretch=5)

        self.my_widget = QtWidgets.QWidget()
        self.my_widget.setLayout(self.hbox)

        self.setCentralWidget(self.my_widget)
        self.setWindowTitle("NUI Scalar Data")

    @QtCore.pyqtSlot(str, float, float)
    def update_data(self, key, tt, val):
        """ """
        # Decimate the features that we actually show, since QGIS is displeased by
        # layers with tens or hundreds of thousands of features.
        # QUESTION: better way to get this timestamp? it's somewhere in the layer ...
        dt = tt - self.last_updated[key]
        period = 1.0 / self.sample_rates[key]
        if dt < period:
            return
        self.last_updated[key] = tt

        # NOTE(lindzey): I expect this to be replaced by a singal/slot
        #   when I finish the refactoring and also pull out the time series plots.
        # TODO: Directly attach these slots to the original signal, after pushing
        #    throttling logic into them?
        self.map_layer_plotter.update_data(key, tt, val)
        self.time_series_plotter.update_data(key, tt, val)

    def update_subscriptions(self):
        for key, config in self.loaded_config.items():
            (channel, msg_type_str, msg_field, sample_rate, layer_name) = config
            self.add_field(channel, msg_type_str, msg_field, sample_rate, layer_name)

    @QtCore.pyqtSlot(str)
    def remove_field(self, key):
        self.sample_rates.pop(key)
        self.last_updated.pop(key)
        self.config.pop(key)
        self.lc.unsubscribe(self.subscribers[key])
        self.subscribers.pop(key)

    @QtCore.pyqtSlot(str, str, str, float, str)
    def add_field(self, channel, msg_type_str, msg_field, sample_rate, layer_name):
        """
        Subscribe to specified data and plot in both map and profile view.
        """
        key = f"{channel}/{msg_field}"
        print(f"add_field for key={key}")
        if key in self.config:
            errmsg = f"Duplicate field '{key}'"
            print(errmsg)
            self.iface.messageBar().pushMessage(errmsg, level=Qgis.Warning)
            QgsMessageLog.logMessage(errmsg)
            return
        self.config[key] = [channel, msg_type_str, msg_field, sample_rate, layer_name]

        self.sample_rates[key] = sample_rate
        self.last_updated[key] = 0.0

        self.time_series_plotter.add_field(key, layer_name)
        self.map_layer_plotter.add_field(key, layer_name)
        self.time_series_widget.add_field(key, layer_name)

        # QUESTION: Can we have multiple subscriptions to the same topic?
        # (e.g. if I want temperature and salinity ...)
        msg_pkg, msg_class = msg_type_str.split(".")
        # If reading from config, won't already be loaded.
        if msg_pkg not in self.msg_modules:
            self.msg_modules[msg_pkg] = importlib.import_module(msg_pkg)
        msg_type = getattr(self.msg_modules[msg_pkg], msg_class)
        self.subscribers[key] = self.lc.subscribe(
            channel,
            lambda channel, data, msg_type=msg_type, msg_field=msg_field: self.handle_data(
                msg_type, msg_field, channel, data
            ),
        )

    def handle_data(self, msg_type, msg_field, channel, data):
        key = f"{channel}/{msg_field}"
        try:
            msg = msg_type.decode(data)
            tt = msg.utime / 1.0e6
            vv = getattr(msg, msg_field)
            self.new_data.emit(key, tt, vv)
        except ValueError as ex:
            errmsg = f"Could not decode message of type {msg_type} from channel {channel}. Exception = {ex}"
            print(errmsg)
            QgsMessageLog.logMessage(errmsg)
        except AttributeError as ex:
            errmsg = f"Couldn't parse data from message: {ex}"
            print(errmsg)
            QgsMessageLog.logMessage(errmsg)

    def spin_lcm(self):
        print("spin_lcm")
        QgsMessageLog.logMessage("spin_lcm")
        while not self.shutdown:
            self.lc.handle()
        print("stopping spin_lcm")

    def run(self):
        lcm_thread = threading.Thread(target=self.spin_lcm)
        lcm_thread.start()
        # This function MUST return, or QGIS will block

    def closeEvent(self, event):
        print("handle_close_event")
        self.shutdown = True
        self.update_timer.stop()
        for key, sub in self.subscribers.items():
            print(f"Unsubscribing from {key}")
            try:
                self.lc.unsubscribe(sub)
            except Exception as ex:
                print(ex)

        self.map_layer_plotter.closeEvent(event)
        self.time_series_plotter.closeEvent(event)

        print(f"Saving updated config! {yaml.safe_dump(self.config)}")
        QgsProject.instance().writeEntry(
            "nui_scalar_data", "subscriptions", yaml.safe_dump(self.config)
        )
        event.accept()


# Needs to be a QObject to use signals/slots
class NuiScalarDataPlugin(QtCore.QObject):
    def __init__(self, iface):
        super(NuiScalarDataPlugin, self).__init__()
        self.iface = iface

    def initGui(self):
        """
        Required method; called when plugin loaded.
        """
        print("initGui")
        icon = os.path.join(os.path.join(cmd_folder, "nui.png"))
        self.action = QtWidgets.QAction(
            QtGui.QIcon(icon), "Display scalar data from NUI", self.iface.mainWindow()
        )
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu("&NUI Scalar Data", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        """
        Required method; called when plugin unloaded.
        """
        print("unload")
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu("&NUI Scalar Data", self.action)
        del self.action

    def run(self):
        print("run")

        # I actually prefer this, because multiple windows are easier to deal
        # with than a dockable window that won't go to the background.
        # self.mainwindow = NuiScalarDataMainWindow(self.iface)
        # self.mainwindow.show()
        # self.mainwindow.run()

        # However, it's possible to wrap the MainWindow in a DockWidget...
        mw = NuiScalarDataMainWindow(self.iface)
        self.dw = QtWidgets.QDockWidget("NUI Scalar Data")
        # Need to unsubscribe from LCM callbacks when the dock widget is closed.
        self.dw.closeEvent = lambda event: mw.closeEvent(event)
        self.dw.setWidget(mw)
        self.iface.addDockWidget(QtCore.Qt.BottomDockWidgetArea, self.dw)
        mw.run()

        print("Done with dockwidget")
        # This function MUST return, or QGIS will block
