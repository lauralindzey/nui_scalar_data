import datetime
import importlib
import math
from matplotlib.figure import Figure
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, ScalarFormatter
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
import numpy as np
import os
import sys
import threading
import time
import yaml

import PyQt5.QtWidgets as QtWidgets
import PyQt5.QtGui as QtGui
import PyQt5.QtCore as QtCore

import qgis.core
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMessageLog,
    QgsProject,
    QgsVectorDataProvider,
    QgsVectorLayer,
)

from qgis.gui import (
    QgsMessageBar,
)

# For running on OSX. In Ubuntu, use python3.8
# I just copied directly from the Ubuntu dslmeta install ... did not build on OSX.
sys.path.append("/opt/dsl/lib/python3.11/site-packages")
# And this is where lcm got installed by default.
sys.path.append("/usr/local/lib/python3.11/site-packages")

import lcm
from comms import statexy_t
from ini import dive_t  # for origin_latitude, origin_longitude


import inspect

cmd_folder = os.path.split(inspect.getfile(inspect.currentframe()))[0]


# Line classes from:
# https://stackoverflow.com/questions/5671354/how-to-programmatically-make-a-horizontal-line-in-qt
class QHLine(QtWidgets.QFrame):
    def __init__(self):
        super(QHLine, self).__init__()
        self.setFrameShape(QtWidgets.QFrame.HLine)
        self.setFrameShadow(QtWidgets.QFrame.Sunken)


class QVLine(QtWidgets.QFrame):
    def __init__(self):
        super(QVLine, self).__init__()
        self.setFrameShape(QtWidgets.QFrame.VLine)
        self.setFrameShadow(QtWidgets.QFrame.Sunken)


# I tried using the Proj4 ortho projection, but that didn't seem to match expected
# So, since our layers will all be in EPSG:4326, I'll use code ported from
# dslpp/mfiles/utils/conversions/xy2ll.m
def ll2xy(lat, lon, lat_0, lon_0):
    if lon > 180:
        lon = lon - 360
    if lon < -180:
        lon = lon + 360
    xx = (lon - lon_0) * mdeglon(lat_0)
    yy = (lat - lat_0) * mdeglat(lat_0)
    return (xx, yy)


def xy2ll(xx, yy, lat_0, lon_0):
    lon = xx / mdeglon(lat_0) + lon_0
    lat = yy / mdeglat(lat_0) + lat_0
    return lat, lon


def mdeglat(lat_deg):
    latrad = math.radians(lat_deg)
    dy = (
        111132.09
        - 566.05 * math.cos(2.0 * latrad)
        + 1.20 * math.cos(4.0 * latrad)
        - 0.002 * math.cos(6.0 * latrad)
    )
    return dy


def mdeglon(lat_deg):
    latrad = math.radians(lat_deg)
    dx = (
        111415.13 * math.cos(latrad)
        - 94.55 * math.cos(3.0 * latrad)
        + 0.12 * math.cos(5.0 * latrad)
    )
    return dx


class NuiScalarDataMainWindow(QtWidgets.QMainWindow):
    # If I understand correctly, any slots decorated with @pyqtSlot will be
    # called in the thread that created the connection, NOT the thread that
    # emitted the signal. So, use that to get data from the LCM thread into
    # the main Widget thread.

    # Otherwise, trying to add features to the layer will give a warning since parent object is in another thread.
    received_origin = QtCore.pyqtSignal(float, float)  # lon, lat in degrees
    new_data = QtCore.pyqtSignal(str, float, float)  # layer key, timestamp, value

    def __init__(self, iface, parent=None):
        super(NuiScalarDataMainWindow, self).__init__(parent)
        self.iface = iface

        try:
            config_str, success = QgsProject.instance().readEntry(
                "nui_scalar_data", "subscriptions"
            )
            if success:
                self.config = yaml.safe_load(config_str)
                print(f"Loaded config! {self.config}")
            else:
                self.config = []
        except Exception as ex:
            self.config = []

        self.setup_ui()

        # TODO: I think there's a cleaner way to dynamically import LCM types,
        #   but this works for now.
        self.msg_modules = {}

        ####
        # Moving stuff from the original QObject
        # layer_name -> QgsVectorLayer to add features to
        self.layers = {}
        # layer_name -> np.array where 1st column is time and 2nd is data
        self.data = {}
        # layer_name -> sample rate
        self.sample_rates = {}
        # layer_name -> timestamp of most recently-added feature (used for decimation)
        self.last_updated = {}
        # layer_name -> axes object for plotting
        self.data_axes = {}
        self.data_plots = {}
        self.plot_length = -1  # In seconds; if -1, plot all available data
        # We need our own instance of a color cycler because I'm using multiple axes
        # on top of each other, and by default, each axis gets its own cycler.
        self.color_cycler = plt.rcParams["axes.prop_cycle"]()

        # Everything NUI does is in the AlvinXY coordinate frame, with origin
        # as defined in the DIVE_INI message. So, we can't set up layers until
        # the first message has been received.
        self.projection_initialized = False
        self.lc = lcm.LCM("udpm://239.255.76.67:7667?ttl=0")
        self.subscribers = {}
        self.subscribers["DIVE_INI"] = self.lc.subscribe(
            "DIVE_INI", self.handle_dive_ini
        )
        self.data_locks = {}
        self.data_locks["STATEXY"] = threading.Lock()
        self.data = {}
        self.data["STATEXY"] = None
        self.subscribers["FIBER_STATEXY"] = self.lc.subscribe(
            "FIBER_STATEXY", self.handle_statexy
        )
        self.subscribers["ACOMM_STATEXY"] = self.lc.subscribe(
            "ACOMM_STATEXY", self.handle_statexy
        )

        self.received_origin.connect(self.initialize_origin)
        self.new_data.connect(self.update_data)

        self.update_timer = QtCore.QTimer()
        self.update_timer.timeout.connect(self.maybe_refresh)
        self.update_timer.setSingleShot(False)
        # QUESTION: Why doesn't this fire at a constant rate?!??
        self.update_timer.start(500)  # ms

        # TODO: consider shutting down plugin when the dockwidget is closed. Right now,
        #  the LCM callbacks just keep on being called.
        self.shutdown = False

    def setup_ui(self):
        self.subscription_grid = QtWidgets.QGridLayout()

        row = 0
        self.channel_name_label = QtWidgets.QLabel("Channel:")
        self.channel_name_lineedit = QtWidgets.QLineEdit()
        self.subscription_grid.addWidget(self.channel_name_label, row, 0)
        self.subscription_grid.addWidget(self.channel_name_lineedit, row, 1)
        row += 1

        self.msg_type_label = QtWidgets.QLabel("LCM type:")
        self.msg_type_lineedit = QtWidgets.QLineEdit()
        self.subscription_grid.addWidget(self.msg_type_label, row, 0)
        self.subscription_grid.addWidget(self.msg_type_lineedit, row, 1)
        row += 1

        self.msg_field_label = QtWidgets.QLabel("Field:")
        self.msg_field_lineedit = QtWidgets.QLineEdit()
        self.subscription_grid.addWidget(self.msg_field_label, row, 0)
        self.subscription_grid.addWidget(self.msg_field_lineedit, row, 1)
        row += 1

        self.sample_rate_label = QtWidgets.QLabel("Rate (Hz):")
        self.sample_rate_lineedit = QtWidgets.QLineEdit()
        self.subscription_grid.addWidget(self.sample_rate_label, row, 0)
        self.subscription_grid.addWidget(self.sample_rate_lineedit, row, 1)
        row += 1

        self.layer_name_label = QtWidgets.QLabel("Layer name:")
        self.layer_name_lineedit = QtWidgets.QLineEdit()
        self.subscription_grid.addWidget(self.layer_name_label, row, 0)
        self.subscription_grid.addWidget(self.layer_name_lineedit, row, 1)
        row += 1

        self.add_field_button = QtWidgets.QPushButton("Add Field")
        self.add_field_button.clicked.connect(self.add_button_clicked)
        self.subscription_grid.addWidget(self.add_field_button, row, 0, 1, 2)

        self.fig = Figure((8.0, 4.0), dpi=100)
        self.ax = self.fig.add_axes([0.1, 0.15, 0.8, 0.8])
        self.cursor_vline = self.ax.axvline(0, 0, 1, ls="--", color="grey")
        print("Functions defined for vline: ", dir(self.cursor_vline))
        # TODO: Turn off this one's y-labels/ticks?, because we won't use it, other than for twinning?
        self.time_formatter = FuncFormatter(
            lambda tt, pos: datetime.datetime.utcfromtimestamp(tt).strftime("%H:%M:%S")
        )
        self.ax.xaxis.set_major_formatter(self.time_formatter)
        self.ax.xaxis.set_tick_params(which="both", labelrotation=60)
        self.ax.tick_params(
            axis="both",
            left=False,
            top=False,
            right=False,
            bottom=True,
            labelleft=False,
            labeltop=False,
            labelright=False,
            labelbottom=True,
        )

        self.ax.get_yaxis().set_visible(False)

        self.canvas = FigureCanvas(self.fig)
        self.canvas.setFocusPolicy(QtCore.Qt.NoFocus)
        self.canvas.mpl_connect("motion_notify_event", self.on_motion_notify_event)

        # TODO: set of QRadioButtons (or checkboxes?) for displaying individual
        #   chunks of data on the plot

        self.vbox = QtWidgets.QVBoxLayout()
        self.vbox.addLayout(self.subscription_grid)
        self.vbox.addWidget(QHLine())
        self.vbox.addStretch(1.0)

        self.hbox = QtWidgets.QHBoxLayout()
        self.hbox.addLayout(self.vbox)
        self.hbox.addWidget(self.canvas, stretch=5)

        self.my_widget = QtWidgets.QWidget()
        self.my_widget.setLayout(self.hbox)

        self.setCentralWidget(self.my_widget)
        self.setWindowTitle("NUI Scalar Data")

    def on_motion_notify_event(self, event):
        """
        When the user drags the mouse across the plot, want to update the
        cursor on the map as well.
        """
        if event.inaxes is None:
            # QgsMessageLog.logMessage(
            #     f"got motion that's not in the figure axes. axes = {event.inaxes}"
            # )
            return
        else:
            # QgsMessageLog.logMessage(
            #     f"got motion in axis {event.inaxes}! mouse at {event.x}, {event.y}"
            #  )
            data_xx, data_yy = self.ax.transData.inverted().transform(
                (event.x, event.y)
            )
            # QgsMessageLog.logMessage(f"Which is at data coords {data_xx}, {data_yy}.")

        tt = data_xx
        self.cursor_vline.set_xdata(tt)

        with self.data_locks["STATEXY"]:
            xx = np.interp(tt, self.data["STATEXY"][:, 0], self.data["STATEXY"][:, 1])
            yy = np.interp(tt, self.data["STATEXY"][:, 0], self.data["STATEXY"][:, 2])
        lat, lon = xy2ll(xx, yy, self.lat0, self.lon0)
        # print(f"Trying to set cursor at x,y = {xx}, {yy} / lon,lat = {lon}, {lat}")
        pt = qgis.core.QgsPointXY(lon, lat)
        geom = qgis.core.QgsGeometry.fromPointXY(pt)
        # I tried to figure out how to just update the existing feature, but couldn't get its new coords to show in the map.
        cursor_feature = qgis.core.QgsFeature()
        cursor_feature.setGeometry(geom)
        dt = datetime.datetime.utcfromtimestamp(tt)
        cursor_feature.setAttributes([dt.strftime("%H:%M:%S.%f")])
        with qgis.core.edit(self.cursor_layer):
            for feat in self.cursor_layer.getFeatures():
                self.cursor_layer.deleteFeature(feat.id())
            self.cursor_layer.dataProvider().addFeature(cursor_feature)
        # Don't wait for the 2Hz update; user will expect something more responsive.
        self.maybe_refresh()

        # TODO: Add checkbox controlling whether the cursor is active
        # TODO: Add layer on map with cursor
        # TODO: Add cursor
        # TODO: Add second axis, with twinx?

    def add_button_clicked(self, _checked):
        print("add_button_clicked")

        if not self.projection_initialized:
            errmsg = "Waiting on DIVE_INI; cannot add layers."
            print(errmsg)
            self.iface.messageBar().pushMessage(errmsg, level=Qgis.Warning)
            QgsMessageLog.logMessage(errmsg)
            return

        channel_name = self.channel_name_lineedit.text()
        if channel_name.strip() == "":
            errmsg = "Please select non-empty channel name."
            print(errmsg)
            self.iface.messageBar().pushMessage(errmsg, level=Qgis.Warning)
            QgsMessageLog.logMessage(errmsg)
            return
        print(f"channel_name: {channel_name}")

        msg_type_str = self.msg_type_lineedit.text()
        msg_type = None
        msg = None
        try:
            msg_pkg, msg_class = msg_type_str.split(".")
            self.msg_modules[msg_pkg] = importlib.import_module(msg_pkg)
            msg_type = getattr(self.msg_modules[msg_pkg], msg_class)
            msg = msg_type()
            if not hasattr(msg, "utime"):
                errmsg = "Plotted messages must have utime field!"
                print(errmsg)
                self.iface.messageBar().pushMessage(errmsg, level=Qgis.Warning)
                QgsMessageLog.logMessage(errmsg)
                return
        except Exception as ex:
            errmsg = f"Tried to instantiate a '{msg_type_str}'. Got exception {ex}"
            print(errmsg)
            self.iface.messageBar().pushMessage(errmsg, level=Qgis.Warning)
            QgsMessageLog.logMessage(errmsg)
            return
        print(f"msg_type = {msg_type_str}")

        # QUESTION: Do we need to support nested fields?
        msg_field = self.msg_field_lineedit.text()
        if not hasattr(msg, msg_field):
            errmsg = (
                f"Message of type '{msg_type_str}' does not have field '{msg_field}'"
            )
            print(errmsg)
            self.iface.messageBar().pushMessage(errmsg, level=Qgis.Warning)
            QgsMessageLog.logMessage(errmsg)
            return
        print(f"msg_field = {msg_field}")

        sample_rate_str = self.sample_rate_lineedit.text()
        try:
            sample_rate = float(sample_rate_str)
        except Exception as ex:
            errmsg = "Couldn't convert input '{sample_rate_str}' into float."
            print(errmsg)
            self.iface.messageBar().pushMessage(errmsg, level=Qgis.Warning)
            QgsMessageLog.logMessage(errmsg)
            return

        layer_name = self.layer_name_lineedit.text()
        if layer_name.strip() == "":
            errmsg = "Please select non-empty layer name."
            print(errmsg)
            self.iface.messageBar().pushMessage(errmsg, level=Qgis.Warning)
            QgsMessageLog.logMessage(errmsg)
            return
        print(f"layer_name: {layer_name}")

        # TODO: Call function adding layer. Will need to check whether our projection has been initialized.
        # Ah! This probably can't be a function call, unless we move everything
        # into the widget.
        self.config.append(
            [channel_name, msg_type_str, msg_field, sample_rate, layer_name]
        )
        print(f"Trying to save config. type = {type(self.config)}: {self.config}")
        config_str = yaml.safe_dump(self.config)
        print(f"Resulting yaml string: {config_str}")
        QgsProject.instance().writeEntry(
            "nui_scalar_data", "subscriptions", yaml.safe_dump(self.config)
        )
        self.add_field(channel_name, msg_type_str, msg_field, sample_rate, layer_name)

    def handle_statexy(self, channel, data):
        """ "
        This is used for both Fiber and Acomms StateXY messages; only append
        data to our vector if it's more recent.

        QUESTION: Should we convert northing/easting to lat/lon immediately?
            (I don't think it matters terribly -- it's always best-estimate, and I
            don't think we'd ever want to correct for offsets.)
        """
        msg = statexy_t.decode(data)

        with self.data_locks["STATEXY"]:
            if self.data["STATEXY"] is None:
                self.data["STATEXY"] = np.array([[msg.utime / 1.0e6, msg.x, msg.y]])
            else:
                new_t = msg.utime / 1.0e6
                last_t = self.data["STATEXY"][-1][0]
                if new_t > last_t:
                    self.data["STATEXY"] = np.append(
                        self.data["STATEXY"],
                        [[msg.utime / 1.0e6, msg.x, msg.y]],
                        axis=0,
                    )
                else:
                    QgsMessageLog.logMessage(f"Received stale msg: {channel}")

    def handle_dive_ini(self, channel, data):
        print("handle_dive_ini")
        QgsMessageLog.logMessage("handle_dive_ini")
        msg = dive_t.decode(data)
        QgsMessageLog.logMessage(
            f"Got map origin: {msg.origin_longitude}, {msg.origin_latitude}; unsubscribing from {channel}"
        )
        try:
            self.lc.unsubscribe(self.subscribers[channel])
            self.received_origin.emit(msg.origin_longitude, msg.origin_latitude)
        except Exception as ex:
            print("Could not unsubscribe from DIVE_INI")

    @QtCore.pyqtSlot(str, float, float)
    def update_data(self, key, tt, val):
        """
        I'm not sure if I'm going to have issues with interpolation + stateXY.
        Will probably be more important as we start working acoustically; with statexy
        on a fiber, I'd feel pretty good about just using the most recent position
        if interpolation isn't an option.

        With longer delays, might wind up wanting to hold on to scalar data and only
        plot it after we have updated positions...but for now, adding it to layers as it comes in.
        """
        # Decimate the features that we actually show, since QGIS is displeased by
        # layers with tens or hundreds of thousands of features.
        # QUESTION: better way to get this? it's somewhere in the layer ...
        dt = tt - self.last_updated[key]
        period = 1.0 / self.sample_rates[key]
        if dt < period:
            return
        self.last_updated[key] = tt

        if self.data[key] is None:
            self.data[key] = np.array([[tt, val]])
        else:
            self.data[key] = np.append(self.data[key], np.array([[tt, val]]), axis=0)

        # Do the interpolation in NuiXY coords, then transform into lat/lon before adding the feature to the layer.
        with self.data_locks["STATEXY"]:
            xx = np.interp(tt, self.data["STATEXY"][:, 0], self.data["STATEXY"][:, 1])
            yy = np.interp(tt, self.data["STATEXY"][:, 0], self.data["STATEXY"][:, 2])
        feature = qgis.core.QgsFeature()
        lat, lon = xy2ll(xx, yy, self.lat0, self.lon0)
        pt = qgis.core.QgsPointXY(lon, lat)
        geom = qgis.core.QgsGeometry.fromPointXY(pt)
        # NOTE(lindzey): We could probably go back to this. The issue was using the wrong
        # EPSG code on the layers themselves, rather than AlvinXY vs something else.
        # geom.transform(self.tr)
        feature.setGeometry(geom)
        dt = datetime.datetime.utcfromtimestamp(tt)
        feature.setAttributes(
            [float(xx), float(yy), dt.strftime("%Y-%m-%d %H:%M:%S:%f"), val]
        )
        self.layers[key].dataProvider().addFeature(feature)

        if self.plot_length < 0:
            (idxs,) = np.where(self.data[key][:, 0] > 0)
        else:
            t0 = np.max(self.data[key][:, 0]) - self.plot_length
            (idxs,) = np.where(self.data[key][:, 0] > t0)

        self.data_plots[key].set_data(self.data[key][idxs, 0], self.data[key][idxs, 1])

        # This didn't seem to work
        # self.data_axes[key].relim()
        xlim = [np.min(self.data[key][idxs, 0]), np.max(self.data[key][idxs, 0])]
        ylim = [np.min(self.data[key][idxs, 1]), np.max(self.data[key][idxs, 1])]
        self.data_axes[key].set_xlim(xlim)
        self.data_axes[key].set_ylim(ylim)

    @QtCore.pyqtSlot()
    def maybe_refresh(self):
        """
        To avoid updating too frequently, we redraw at a fixed rate.

        In my earlier experiments, I had refresh directly called by the LCM thread,
        so needed a mutex on the layers.
        In the Widget, I'm using signals/slots to guarantee that all layer-related
        stuff happens in a single thread (I hope?)

        I considered adding a flag to see if we need to redraw, but haven't yet.
        (This will also become more important when we start drawing time series plots.)
        """
        # print(f"{time.time()}: Calling refresh on the canvas!")
        # TODO: This doesn't actually seem to refresh the layers -- maybe I needed the caching method?
        # The other problem is updating the bounds of the shading, ratehr than just not plotting points that are off the edges.
        if self.iface.mapCanvas().isCachingEnabled():
            # TODO: Should we check per-layer if it needs to be redrawn?
            #    Maybe only redraw visible layers?
            for key, layer in self.layers.items():
                # I'm not sure how this wound up getting called while layer was None.
                # I thought all things touching the layer were in the same thread,
                # and that layer creation would finish before this was called.
                if layer is not None and layer.isValid():
                    layer.triggerRepaint()
        else:
            self.iface.mapCanvas().refresh()

        # And, update the scalar data plot!
        self.canvas.draw_idle()
        self.canvas.flush_events()

    @QtCore.pyqtSlot(float, float)
    def initialize_origin(self, lon0, lat0):
        print(f"initialize_origin. lon={lon0}, lat={lat0}")
        self.lon0 = lon0
        self.lat0 = lat0
        self.crs = QgsCoordinateReferenceSystem()
        # AlvinXY uses the Clark 1866 ellipsoid; it predates WGS84
        self.crs.createFromProj4(
            f"+proj=ortho +lat_0={self.lat0} +lon_0={self.lon0} +ellps=clrk66"
        )
        print(f"Created CRS! isValid = {self.crs.isValid()}")
        self.crs_name = "NuiXY"
        self.crs.saveAsUserCrs(self.crs_name)
        # For some reason, setting this custom CRS on a layer doesn't work, but it's fine
        # for projecting points between.
        self.map_crs = QgsCoordinateReferenceSystem("epsg:4326")
        self.tr = QgsCoordinateTransform(self.crs, self.map_crs, QgsProject.instance())
        self.projection_initialized = True

        self.setup_layers()
        self.update_subscriptions()  # Activate any subscriptions from the config

    def update_subscriptions(self):
        for channel, msg_type_str, msg_field, sample_rate, layer_name in self.config:
            self.add_field(channel, msg_type_str, msg_field, sample_rate, layer_name)

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
        self.scalar_data_group = self.nui_group.findGroup("Scalar Data")
        if self.scalar_data_group is None:
            self.scalar_data_group = self.nui_group.insertGroup(0, "Scalar Data")

        # TODO: Why isn't this finding the layer?!??
        self.cursor_layer = None
        for ll in self.scalar_data_group.children():
            print(ll.name())
            if (
                isinstance(ll, qgis.core.QgsLayerTreeLayer)
                and ll.name() == "Scalar Data Cursor"
            ):
                self.cursor_layer = ll.layer()  # ll is a QgsLayerTreeLayer
        print(
            "Tried to find cursor_layer in NUI group. Is none?",
            self.cursor_layer is None,
        )
        # TODO: Probably also need to check whether it's the right type of layer...
        if self.cursor_layer is None:
            self.cursor_layer = QgsVectorLayer(
                f"Point?crs=epsg:4326&field=time:string(30)&index=yes",
                "Scalar Data Cursor",
                "memory",
            )
            print("...Created cursor_layer")
            QgsProject.instance().addMapLayer(self.cursor_layer, False)
            self.scalar_data_group.addLayer(self.cursor_layer)
        cursor_feature = qgis.core.QgsFeature()
        pt = qgis.core.QgsPointXY(self.lon0, self.lat0)
        geom = qgis.core.QgsGeometry.fromPointXY(pt)
        cursor_feature.setGeometry(geom)
        cursor_feature.setAttributes(["0"])
        self.cursor_layer.dataProvider().addFeature(cursor_feature)

        print("done with setup_layers")

    def add_field(self, channel, msg_type_str, msg_field, sample_rate, layer_name):
        """
        Subscribe to specified data and plot in both map and profile view.
        """
        key = f"{channel}/{msg_field}"
        print(f"add_field for key={key}")
        if key in self.layers:
            errmsg = f"Duplicate field '{key}'"
            print(errmsg)
            self.iface.messageBar().pushMessage(errmsg, level=Qgis.Warning)
            QgsMessageLog.logMessage(errmsg)
            return

        self.data[key] = None
        self.layers[key] = None
        self.sample_rates[key] = sample_rate
        self.last_updated[key] = 0.0
        self.data_axes[key] = self.ax.twinx()
        # Try to split labels across left/right for readability
        if len(self.fig.axes) % 2 == 0:
            self.data_axes[key].yaxis.set_label_position("left")
            self.data_axes[key].tick_params(
                axis="both",
                left=True,
                right=False,
                top=False,
                bottom=False,
                labelleft=True,
                labelright=False,
                labeltop=False,
                labelbottom=False,
            )
        else:
            self.data_axes[key].yaxis.set_label_position("right")
            self.data_axes[key].tick_params(
                axis="both",
                left=False,
                right=True,
                top=False,
                bottom=False,
                labelleft=False,
                labelright=True,
                labeltop=False,
                labelbottom=False,
            )
        # Set colors for each axes
        self.data_axes[key].set_ylabel(layer_name)
        self.data_axes[key].yaxis.set_major_formatter(ScalarFormatter(useOffset=False))
        color = next(self.color_cycler)["color"]
        self.data_axes[key].yaxis.label.set_color(color)
        self.data_axes[key].tick_params(axis="y", colors=color)
        (self.data_plots[key],) = self.data_axes[key].plot(
            [], [], ".", markersize=1, color=color, label=layer_name
        )

        print("Searching scalar data group's children...")
        for ll in self.scalar_data_group.children():
            print(ll.name())
            if isinstance(ll, qgis.core.QgsLayerTreeLayer) and ll.name() == layer_name:
                print(f"Found existing layer for {layer_name}")
                self.layers[key] = ll.layer()
        if self.layers[key] is None:
            print("...creating layer.")
            # TODO: Also need to double-check that it's the right type of layer
            self.layers[key] = QgsVectorLayer(
                f"Point?crs=epsg:4326&field=x:double&field=y:double&field=time:string(30)&field=value:double&index=yes",
                layer_name,
                "memory",
            )
            QgsProject.instance().addMapLayer(self.layers[key], False)
            self.scalar_data_group.addLayer(self.layers[key])
        print(f"Added layer '{layer_name}' to map")

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

        # Create the axis to plot onto

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
                # If we've already unsubscribed from DIVE_INI, this will fail.
                print(ex)

        event.accept()


# Trying to inherit from QObject for now so I can set up signals/slots
class NuiScalarDataPlugin(QtCore.QObject):
    def __init__(self, iface):
        """
        Re-use the appropriate layers if they exist, in order to let the user
        save stylings in their QGIS project.
        QUESTION(lindzey): Does subclassing the QgsPluginLayer help with this?
        """
        print("__init__")
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
        self.dw.setWidget(mw)
        self.iface.addDockWidget(QtCore.Qt.BottomDockWidgetArea, self.dw)
        mw.run()

        print("Done with dockwidget")
        # This function MUST return, or QGIS will block
