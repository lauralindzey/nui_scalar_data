import datetime
import math
import numpy as np
import sys
import threading

from matplotlib.figure import Figure
from matplotlib.backend_bases import MouseButton
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, ScalarFormatter
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas

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


# I just copied directly from the Ubuntu dslmeta install ... did not build on OSX.
sys.path.append("/opt/dsl/lib/python3.11/site-packages")

from comms import statexy_t
from ini import dive_t  # for origin_latitude, origin_longitude


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
        self.crs.createFromProj(
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
            # msg = "Origin not initialized; cannot update cursor"
            # print(msg)
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
            self.cursor_layer.dataProvider().truncate()
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
                    # Ignore stale data. Will occasionally get out-of-order
                    # FIBER_STATEXY messages, but the real intent here it to not have
                    # ACOMMS_STATEXY overwrite newer FIBER_STATEXY ones.
                    # QgsMessageLog.logMessage(f"Received stale msg: {channel}")
                    pass

    @QtCore.pyqtSlot(str)
    def clear_field(self, key):
        print(f"MapLayerPlotter.clear_field: {key}")
        with qgis.core.edit(self.layers[key]):
            self.layers[key].dataProvider().truncate()

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
                # Don't auto-delete existing features; user has button to do so if desired
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

        # We have two ways of selecting the time range:
        # 1) click-and-drag across desired range
        # 2) text entry of either moving window or starting time
        # Whichever has been more recently set will take precedence

        # Used for selecting a range of times with mouse
        self.right_click_t0 = None
        self.right_click_t1 = None
        # Grossly overloaded value, in seconds
        # * If None, plot all available data
        # * if negative, plot that many seconds
        # * if positive, plot all data sense that timestamp
        self.time_limit = None

        # We need our own instance of a color cycler because I'm using multiple axes
        # on top of each other, and by default, each axis gets its own cycler.
        self.color_cycler = plt.rcParams["axes.prop_cycle"]()

        ######
        # Handle GUI stuff
        self.fig = Figure((8.0, 4.0), dpi=100)
        self.ax = self.fig.add_axes([0.1, 0.2, 0.8, 0.75])
        self.cursor_vline = self.ax.axvline(0, 0, 1, ls="--", color="grey")
        self.time_formatter = FuncFormatter(
            lambda tt, pos: datetime.datetime.utcfromtimestamp(tt).strftime(
                "%Y-%m-%d\n%H:%M:%S"
            )
        )
        self.ax.xaxis.set_major_formatter(self.time_formatter)
        self.ax.xaxis.set_tick_params(which="both", labelrotation=45)
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
        self.canvas.mpl_connect("button_release_event", self.on_button_release_event)

    def closeEvent(self, event):
        pass

    def on_button_release_event(self, event):
        if event.inaxes is None:
            return

        data_xx, data_yy = self.ax.transData.inverted().transform((event.x, event.y))
        if event.button == MouseButton.RIGHT and self.right_click_t0 is not None:
            self.right_click_t1 = data_xx
        else:
            print(f"Got unhandled button release event: {event}")

    def on_button_press_event(self, event):
        """
        When the user drags the mouse across the plot, want to update the
        cursor on the map as well.

        # TODO: This should be a click and not a drag -- if the user wants it to stay
        #   put, that's hard to do while dragging.
        """
        if event.inaxes is None:
            return
        else:
            data_xx, data_yy = self.ax.transData.inverted().transform(
                (event.x, event.y)
            )

        if event.button == MouseButton.LEFT:
            tt = data_xx
            self.cursor_vline.set_xdata(tt)
            self.cursor_moved.emit(tt)
            # Don't wait for the 2Hz update; user will expect something more responsive.
            self.maybe_refresh()
        elif event.button == MouseButton.RIGHT:
            # TODO: Store the time for setting left/right time
            self.right_click_t0 = data_xx

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
        # This somewhat duplicates the logic in update_data (which needs to figure
        # out which points are in the time bounds in order to not plot unnecessarily
        # large numbers of points), but here we look at all datasets.
        if self.right_click_t0 is not None and self.right_click_t1 is not None:
            t0 = self.right_click_t0
            t1 = self.right_click_t1
        else:
            tmin = np.inf
            tmax = -np.inf
            for key, data in self.data.items():
                if data is None:
                    continue
                tmin = min(tmin, np.min(data[:, 0]))
                tmax = max(tmax, np.max(data[:, 0]))

            if self.time_limit is None:
                t0 = tmin
            elif self.time_limit < 0:
                t0 = tmax + self.time_limit
            else:
                t0 = self.time_limit
            t1 = tmax

        # If we don't have data yet, will be nan, which isn't valid. EAFP.
        try:
            self.ax.set_xlim([t0, t1])
        except Exception as ex:
            pass

        self.canvas.draw_idle()
        self.canvas.flush_events()

    @QtCore.pyqtSlot(str, bool)
    def toggle_visibility(self, key, visible):
        self.data_axes[key].set_visible(visible)

    @QtCore.pyqtSlot(str, object, object)
    def set_ylim(self, key, ymin, ymax):
        self.ylims[key] = [ymin, ymax]

    @QtCore.pyqtSlot(object)
    def set_time_limits(self, timestamp):
        self.time_limit = timestamp
        # If the lineedit is used to set time window, clear values from mouse
        self.right_click_t0 = None
        self.right_click_t1 = None

    @QtCore.pyqtSlot(str, float, float)
    def update_data(self, key, tt, val):
        if self.data[key] is None:
            self.data[key] = np.array([[tt, val]])
        else:
            self.data[key] = np.append(self.data[key], np.array([[tt, val]]), axis=0)

        if self.right_click_t0 is not None and self.right_click_t1 is not None:
            t0 = self.right_click_t0
            t1 = self.right_click_t1
        else:
            if self.time_limit is None:
                t0 = np.min(self.data[key][:, 0])
            elif self.time_limit < 0:
                t0 = np.max(self.data[key][:, 0]) + self.time_limit
            else:
                t0 = self.time_limit
            t1 = np.max(self.data[key][:, 0])

        (gt_idxs,) = np.where(self.data[key][:, 0] >= t0)
        (lt_idxs,) = np.where(self.data[key][:, 0] <= t1)
        idxs = np.intersect1d(gt_idxs, lt_idxs)

        self.data_plots[key].set_data(self.data[key][idxs, 0], self.data[key][idxs, 1])

        # Intentionally do NOT set xlim here -- that needs to be set only once,
        # on self.ax, or different-length time histories will fight.

        # Calculate axis limits based on _visible_ data points, not full history.
        ymin, ymax = self.ylims[key]
        if ymin is None and len(idxs > 0):
            ymin = np.min(self.data[key][idxs, 1])
        if ymax is None and len(idxs > 0):
            ymax = np.max(self.data[key][idxs, 1])

        self.data_axes[key].set_ylim([ymin, ymax])
