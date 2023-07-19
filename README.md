# nui_scalar_data

The nui_scalar_data QGIS plugin supports real-tiem viewing of any scalar data published
by NUI, tying together a time series view and a map view.

No attempt has been made to make this vehicle-agnostic, though only a few modifications would be necessary:
* It is assumed that all LCM messages have a `utime` field, giving microseconds since the epoch
* Nui uses a local AlvinXY-style coordinate system. The plugin listens for a DIVE_INI message to determine the origin.
* Position data is collected from the ACOMMS_STATEXY and FIBER_STATEXY; scalar data is plotted in map view by interpolating into these coordinates.

Any top-level scalar field in a LCM message is supported.
* can't yet handle nested messages
* can't index into arrays

### Installation instructions

clone; symlink into ?? for mac and ?? for windows

After cloning, restart QGIS, then plugins->"Manage and Install Plugins..."->"Installed Plugins". Make sure "NUI Scalar Data" is checked.

assumes that the NUI LCM definitions and lcm install are on your pythonpath.

If you will be doing any development, the "plugin reloader" plugin is very useful.
I like to run qgis from the command line; then the output of `print` statements is visible. I've used `print` and `logMessage` (goes to python console in qgis) for developer-focused messages, and the messageBar for user-targeted messages.
Also note that if your changes cause the plugin to not load cleanly, you'll have to restart QGIS; if there are errors on launch, those can be fixed and then the plugin reloaded without a full restart.

Open the plugin by either clicking the NUI icon or Plugins->"Nui Scalar Data"->"Display scalar data from NUI"

### Configuration

To add a new field, type in the channel name, the message type, the field name, the rate to decimate the input data at, and the layer name.
* Use dsl-spy.sh (aka lcm-spy) to identify channel name and field of interest
* the plugin requres the message type to be class.type_t; e.g. `comms.statexy_t`. dsl-spy only shows the second half; if you don't know what package a message is in, you can find it by: `cd /path/to/dslmeta`; `find . -iname "statexy_t.msg"`
* QGIS will be displeased if you try to plot 30Hz data for a whole dive. 1Hz is usually reasonable.
* a QGIS layer (and time series axes) will be created with the input "layer name"

Use standard QGIS configuration to set the layer properties. If you save the layers in your QGIS project, the styling will be preserved. (The plugin itself does not attempt to set any styling.)

The plugin configuration (which topics/fields are monitored) is also saved in the QGIS project. Be sure to save the project (even if there are no other changes to the underlying layers) in order for the configuration to persist across restarts.

If you're using graduated symbols, be sure to edit the minimum/maximum values are outside the data range after auto-generating the classes (otherwise, QGIS is silly and just won't display data outside the range.)

### Operation

The plots will auto-refresh as new data comes in.
In order geolocate a datapoint from the time series plot, simply click within the plot -- the Scalar Data Cursor symbol will be moved to the lat,lon corresponding to the clicked timestamp.

We also have some options to configure what's shown in the time-series viewer.
* The checkboxes toggle visibility, but the data is still there
* min/max Y allow the user to set y limits to cut off fliers. Leave blank or type 'none' if you want limits to be calculated from data bounds
* the remove button will entirely remove that data source, both from the layers menu and the time series plot.

TODO: selecting time limits