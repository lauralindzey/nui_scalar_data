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
    QgsVectorLayer,
)

# For running on OSX. In Ubuntu, use python3.8
# I just copied directly from the Ubuntu dslmeta install ... did not build on OSX.
sys.path.append("/opt/dsl/lib/python3.11/site-packages")
# And this is where lcm got installed by default.
sys.path.append("/usr/local/lib/python3.11/site-packages")

import lcm
from comms import statexy_t
from ini import dive_t  # for origin_latitude, origin_longitude

from .nui_scalar_data_widgets import (
    AddScalarDataFieldWidget,
    ConfigureTimeSeriesWidget,
    QHLine,
    QVLine,
)

import inspect

cmd_folder = os.path.split(inspect.getfile(inspect.currentframe()))[0]


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


class MapLayerPlotter(QtCore.QObject):
    """
    Class in charge of managing all QGIS map/layer/etc. interfaces.

    This is the only part of the code that needs to know about map projections,
    so it also owns the LCM subscription to statexy.

    Interfaces with the rest of the QGIS plugin via:
    * update_cursor -- should be connected to signal emitted by the time series plot
    * update_data -- connected to signal emitted when new LCM message with data is received
    * add_field -- currently directly called by main program's add_field;
          should probably connect to signal emitted by the ScalarDataField widget.
    """

    # Any LCM handlers need to emit signals rather than directly call functions
    # that manipulate the QGIS elements, in order to avoid threading issues.
    received_origin = QtCore.pyqtSignal(float, float)  # lon, lat in degrees

    def __init__(self, iface, lc):
        super(MapLayerPlotter, self).__init__()
        self.iface = iface
        self.lc = lc
        # layer_name -> QgsVectorLayer to add features to
        self.layers = {}

        # Everything NUI does is in the AlvinXY coordinate frame, with origin
        # as defined in the DIVE_INI message. So, we can't add data to layers
        # until the first message has been received.
        self.lat0 = None
        self.lon0 = None
        self.projection_initialized = False  # TODO: use 'self.lat0 is None' instead?
        self.received_origin.connect(self.initialize_origin)

        self.subscribers = {}
        self.subscribers["DIVE_INI"] = self.lc.subscribe(
            "DIVE_INI", self.handle_dive_ini
        )

        self.statexy_lock = threading.Lock()
        self.statexy_data = None
        self.subscribers["FIBER_STATEXY"] = self.lc.subscribe(
            "FIBER_STATEXY", self.handle_statexy
        )
        self.subscribers["ACOMM_STATEXY"] = self.lc.subscribe(
            "ACOMM_STATEXY", self.handle_statexy
        )

        self.setup_groups()
        self.setup_cursor_layer()

        self.update_timer = QtCore.QTimer()
        self.update_timer.timeout.connect(self.maybe_refresh)
        self.update_timer.setSingleShot(False)
        self.update_timer.start(500)  # ms

    def closeEvent(self, _event):
        print("MapLayerPlotter.closeEvent()")
        self.update_timer.stop()

        for key, sub in self.subscribers.items():
            print(f"Unsubscribing from {key}")
            try:
                self.lc.unsubscribe(sub)
            except Exception as ex:
                # If we've already unsubscribed from DIVE_INI, this will fail.
                print(ex)

    def setup_groups(self):
        """
        Initializes NUI's root group / the scalar data root.

        Re-use the appropriate groups and layers if they exist, in order to
        let the user save stylings in their QGIS project.
        """
        print("setup_groups")
        # To start with, only add the cursor to the map
        self.root = QgsProject.instance().layerTreeRoot()
        print("got root. Is none? ", self.root is None)
        self.nui_group = self.root.findGroup("NUI")
        if self.nui_group is None:
            self.nui_group = self.root.insertGroup(0, "NUI")
        self.scalar_data_group = self.nui_group.findGroup("Scalar Data")
        if self.scalar_data_group is None:
            self.scalar_data_group = self.nui_group.insertGroup(0, "Scalar Data")

    def setup_cursor_layer(self):
        print("setup_cursor_layer")
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
                "Point?crs=epsg:4326&field=time:string(30)&index=yes",
                "Scalar Data Cursor",
                "memory",
            )
            print("...Created cursor_layer")
            QgsProject.instance().addMapLayer(self.cursor_layer, False)
            self.scalar_data_group.addLayer(self.cursor_layer)

    @QtCore.pyqtSlot()
    def maybe_refresh(self):
        """
        To avoid updating too frequently, we redraw data layers at a fixed rate.
        The cursor is handled separately, as part of its own callback, in an
        attempt to reduce latency experienced by the user.

        In my earlier experiments, I had refresh directly called by the LCM thread,
        so needed a mutex on the layers.
        In the Widget, I'm using signals/slots to guarantee that all layer-related
        stuff happens in a single thread (I hope?)

        I considered adding a flag to see if we need to redraw, but haven't yet.
        (This will also become more important when we start drawing time series plots.)
        """
        # The other problem is updating the bounds of the shading, ratehr than just not plotting points that are off the edges.
        if self.iface.mapCanvas().isCachingEnabled():
            # TODO: Should we check per-layer if it needs to be redrawn?
            #    Maybe only redraw visible layers?
            for key, layer in self.layers.items():
                # I'm not sure how this wound up getting called while layer was None.
                # I thought all things touching the layer were in the same thread,
                # and that layer creation would finish before this was called.
                if layer is not None and layer.isValid():
                    try:
                        layer.triggerRepaint()
                    except Exception as ex:
                        print(f"Failed to repaint layer {key}")
        else:
            self.iface.mapCanvas().refresh()

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

    @QtCore.pyqtSlot(float)
    def update_cursor(self, tt):
        if self.lon0 is None:
            msg = "Origin not initialized; cannot update cursor"
            print(msg)
            return

        with self.statexy_lock:
            xx = np.interp(tt, self.statexy_data[:, 0], self.statexy_data[:, 1])
            yy = np.interp(tt, self.statexy_data[:, 0], self.statexy_data[:, 2])
        lat, lon = xy2ll(xx, yy, self.lat0, self.lon0)
        pt = qgis.core.QgsPointXY(lon, lat)
        geom = qgis.core.QgsGeometry.fromPointXY(pt)
        # I tried to figure out how to just update the existing feature,
        # but couldn't get its new coords to show in the map.
        cursor_feature = qgis.core.QgsFeature()
        cursor_feature.setGeometry(geom)
        dt = datetime.datetime.utcfromtimestamp(tt)
        cursor_feature.setAttributes([dt.strftime("%H:%M:%S.%f")])
        with qgis.core.edit(self.cursor_layer):
            for feat in self.cursor_layer.getFeatures():
                self.cursor_layer.deleteFeature(feat.id())
            self.cursor_layer.dataProvider().addFeature(cursor_feature)

        # If possible, just update this layer. Otherwise, wait for global refresh.
        if self.iface.mapCanvas().isCachingEnabled():
            self.cursor_layer.triggerRepaint()

    @QtCore.pyqtSlot(str, float, float)
    def update_data(self, key, tt, val):
        if self.lon0 is None:
            msg = "Origin not initialized; cannot plot data"
            print(msg)
            return
        if key not in self.layers:
            msg = f"No layer matching {key}; cannot plot data"
            print(msg)
            return
        # Do the interpolation in NuiXY coords, then transform into lat/lon
        # before adding the feature to the layer.
        # This is usually OK, but will lead to smearing data when we have nav shifts.
        with self.statexy_lock:
            xx = np.interp(tt, self.statexy_data[:, 0], self.statexy_data[:, 1])
            yy = np.interp(tt, self.statexy_data[:, 0], self.statexy_data[:, 2])
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

    def handle_statexy(self, channel, data):
        """ "
        This is used for both Fiber and Acomms StateXY messages; only append
        data to our vector if it's more recent.

        QUESTION: Should we convert northing/easting to lat/lon immediately?
            (I don't think it matters terribly -- it's always best-estimate, and I
            don't think we'd ever want to correct for offsets.)
        """
        msg = statexy_t.decode(data)

        with self.statexy_lock:
            if self.statexy_data is None:
                self.statexy_data = np.array([[msg.utime / 1.0e6, msg.x, msg.y]])
            else:
                new_t = msg.utime / 1.0e6
                last_t = self.statexy_data[-1][0]
                if new_t > last_t:
                    self.statexy_data = np.append(
                        self.statexy_data,
                        [[msg.utime / 1.0e6, msg.x, msg.y]],
                        axis=0,
                    )
                else:
                    QgsMessageLog.logMessage(f"Received stale msg: {channel}")

    @QtCore.pyqtSlot(str)
    def remove_field(self, key):
        print(f"MapLayerPlotter.remove_field: {key}")
        layer_id = self.layers[key].id()
        self.layers.pop(key)
        QgsProject.instance().removeMapLayers([layer_id])

    # QUESTION: should this be a slot too?
    def add_field(self, key, layer_name):
        self.layers[key] = None

        print("Searching scalar data group's children...")
        for ll in self.scalar_data_group.children():
            print(ll.name())
            if isinstance(ll, qgis.core.QgsLayerTreeLayer) and ll.name() == layer_name:
                print(f"Found existing layer for {layer_name}")
                self.layers[key] = ll.layer()
                # I'm not sure whether we want to delete features or not ...
                # For now, don't, in order to support restarting the plugin during a dive.
                print("... deleting existing features.")
                with qgis.core.edit(self.layers[key]):
                    for feat in self.layers[key].getFeatures():
                        self.layers[key].deleteFeature(feat.id())
                        pass
        if self.layers[key] is None:
            print("...creating layer.")
            # TODO: Also need to double-check that it's the right type of layer
            self.layers[key] = QgsVectorLayer(
                "Point?crs=epsg:4326&field=x:double&field=y:double&field=time:string(30)&field=value:double&index=yes",
                layer_name,
                "memory",
            )
            QgsProject.instance().addMapLayer(self.layers[key], False)
            self.scalar_data_group.addLayer(self.layers[key])
        print(f"Added layer '{layer_name}' to map")


class TimeSeriesPlotter(QtCore.QObject):
    cursor_moved = QtCore.pyqtSignal(float)  # new timestamp, in seconds since epoch

    def __init__(self):
        super(TimeSeriesPlotter, self).__init__()
        ######
        # Handle data stuff

        # layer_name -> np.array where 1st column is time and 2nd is data
        self.data = {}
        # layer_name -> axes object for plotting
        self.data_axes = {}
        self.data_plots = {}
        self.ylims = {}
        self.plot_length = -1  # In seconds; if -1, plot all available data
        # We need our own instance of a color cycler because I'm using multiple axes
        # on top of each other, and by default, each axis gets its own cycler.
        self.color_cycler = plt.rcParams["axes.prop_cycle"]()

        ######
        # Handle GUI stuff
        self.fig = Figure((8.0, 4.0), dpi=100)
        self.ax = self.fig.add_axes([0.1, 0.15, 0.8, 0.8])
        self.cursor_vline = self.ax.axvline(0, 0, 1, ls="--", color="grey")
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
        self.canvas.mpl_connect("button_press_event", self.on_button_press_event)

    def closeEvent(self, event):
        pass

    def on_button_press_event(self, event):
        """
        When the user drags the mouse across the plot, want to update the
        cursor on the map as well.

        # TODO: This should be a click and not a drag -- if the user wants it to stay
        #   put, that's hard to do while dragging.
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
        self.cursor_moved.emit(tt)

        # Don't wait for the 2Hz update; user will expect something more responsive.
        self.maybe_refresh()

        # TODO: Add checkbox controlling whether the cursor is active
        # TODO: Add layer on map with cursor
        # TODO: Add cursor
        # TODO: Add second axis, with twinx?

    def add_field(self, key, layer_name):
        self.data[key] = None
        self.data_axes[key] = self.ax.twinx()
        self.ylims[key] = [None, None]

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

    @QtCore.pyqtSlot(str)
    def remove_field(self, key):
        print(f"TimeSeriesPlotter.remove_field: {key}")
        self.data_axes[key].remove()
        self.data_axes.pop(key)
        self.data_plots.pop(key)
        self.data.pop(key)
        self.ylims.pop(key)

    @QtCore.pyqtSlot()
    def maybe_refresh(self):
        """
        To avoid updating too frequently, we redraw at a fixed rate.
        This one just updates the scalar data plot.
        """
        # And, update the scalar data plot!
        self.canvas.draw_idle()
        self.canvas.flush_events()

    @QtCore.pyqtSlot(str, bool)
    def toggle_visibility(self, key, visible):
        self.data_axes[key].set_visible(visible)

    @QtCore.pyqtSlot(str, object, object)
    def set_ylim(self, key, ymin, ymax):
        self.ylims[key] = [ymin, ymax]

    @QtCore.pyqtSlot(str, float, float)
    def update_data(self, key, tt, val):
        if self.data[key] is None:
            self.data[key] = np.array([[tt, val]])
        else:
            self.data[key] = np.append(self.data[key], np.array([[tt, val]]), axis=0)

        if self.plot_length < 0:
            (idxs,) = np.where(self.data[key][:, 0] > 0)
        else:
            t0 = np.max(self.data[key][:, 0]) - self.plot_length
            (idxs,) = np.where(self.data[key][:, 0] > t0)

        self.data_plots[key].set_data(self.data[key][idxs, 0], self.data[key][idxs, 1])

        xlim = [np.min(self.data[key][idxs, 0]), np.max(self.data[key][idxs, 0])]
        self.data_axes[key].set_xlim(xlim)

        # Calculate axis limits based on _visible_ data points, not full history.
        ymin, ymax = self.ylims[key]
        if ymin is None:
            ymin = np.min(self.data[key][idxs, 1])
        if ymax is None:
            ymax = np.max(self.data[key][idxs, 1])

        self.data_axes[key].set_ylim([ymin, ymax])


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

        self.configure_time_series_widget = ConfigureTimeSeriesWidget(self.iface)
        self.configure_time_series_widget.toggle_plot.connect(
            self.time_series_plotter.toggle_visibility
        )

        self.configure_time_series_widget.ylim_changed.connect(
            self.time_series_plotter.set_ylim
        )

        self.configure_time_series_widget.remove_field.connect(self.remove_field)
        self.configure_time_series_widget.remove_field.connect(
            self.time_series_plotter.remove_field
        )
        self.configure_time_series_widget.remove_field.connect(
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
        self.vbox.addWidget(QHLine())
        self.vbox.addWidget(self.configure_time_series_widget)
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
        self.configure_time_series_widget.add_field(key, layer_name)

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
