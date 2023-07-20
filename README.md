# nui_scalar_data

The nui_scalar_data QGIS plugin supports real-time viewing of any scalar data published
by NUI, tying together a time series view and a map view.

No attempt has been made to make this vehicle-agnostic, though only a few modifications would be necessary:
* It is assumed that all LCM messages have a `utime` field, giving microseconds since the epoch
* Nui uses a local AlvinXY-style coordinate system. The plugin listens for a DIVE_INI message to determine the origin.
* Position data is collected from the ACOMMS_STATEXY and FIBER_STATEXY channels; time-stamped scalar data is plotted in map view by interpolating into these coordinates.

Any top-level scalar field in a LCM message is supported. However, support for some fields are not yet implemented:
* nested messages
* indexing into arrays

## Installation instructions

### Prerequisites
* Install QGIS: follow the instructions at qgis.org to instal. The plugin has been used with versions 3.22 and 3.26. (I don't know of any reason it won't work with any 3.X)
* Install LCM: Follow the instructions at http://lcm-proj.github.io/lcm/
    * sudo apt remove openjdk-8-*
    * sudo apt install openjdk-11-jdk  # LCM will compile on 20.04 with openjdk-8, but lcm-spy won't work
    * sudo apt install build-essential cmake libglib2.0-dev python3-dev
    * cd ~/code
    * git clone https://github.com/lcm-proj/lcm.git
    * cd lcm
    * git checkout v1.5.0
    * mkdir build && cd build && cmake .. && make && sudo make install
    * edit `~/.bashrc` so the LCM install location is on your path. e.g. `export PYTHONPATH=${PYTHONPATH}:/usr/local/lib/python3.8/site-packages`
* Install NUI's LCM message definitions:
    * cd ~/code
    * git clone ssh://git@bitbucket.org/whoidsl/dslmeta-git.git
      * OR if you're external to DSL, use the github mirror: `git clone http://github.com/lauralindzey/dslmeta.git`
    * cd dslmeta
    * git checkout feature/python3  # NUI uses python2, but we needed to install python3 definitions for QGIS
    * cmake -DTARGET_VEHICLE:STRING=NUI ../ && make && sudo make install
    * The messages re-define a number of common module names, so it's a bad idea to have them on your path by default. I use a few bash aliases:
      * alias dsl-spy='for JAR in /opt/dsl/share/java/*.jar; do CLASSPATH=${CLASSPATH}:${JAR}; done; export CLASSPATH; lcm-spy'
      * alias dsl-qgis='export PYTHONPATH=${PYTHONPATH}:/opt/dsl/lib/python3.8/site-packages; qgis'

Start QGIS with "dsl-qgis"

If you will be doing any development, the "Plugin Reloader" plugin is very useful: it allows you to reload a plugin whose code has changed without having to restart QGIS.
* Plugins -> manage and Install Plugins...
* Click "All", search for "Plugin Reloader", select and click "Install"
Also note that if your changes cause the plugin to not load cleanly, you'll have to restart QGIS; if there are errors on launch, those can be fixed and then the plugin reloaded without a full restart.
I like to run qgis from the command line; then the output of `print` statements is visible. I've used `print` and `logMessage` (goes to python console in qgis) for developer-focused messages, and the messageBar for user-targeted messages.

### Plugin Install

`git clone git@github.com:lauralindzey/nui_scalar_data.git`

On a Mac, QGIS expects plugins to be installed to
`~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins`

On Linux, QGIS plugins are installed to
`~/.local/share/QGIS/QGIS3/profiles/default/python/plugins`
(This directory is create the first time you install a plugin via the QGIS menu.)

Either clone directly to the plugin directory, or create a symbolic link to your checkout from the appropriate directory. (If you will be developing the plugin, use a symlink -- if you have to uninstall the plugin, QGIS will delete that directory.)

After cloning and linking, restart QGIS, then install it:
* plugins->"Manage and Install Plugins..."
* click "Installed"
* Make sure "NUI Scalar Data" is checked.

Open the plugin by either clicking the NUI icon or Plugins->"Nui Scalar Data"->"Display scalar data from NUI"

## Configuration

To add a new field, type in the channel name, the message type, the field name, the rate to decimate the input data at, and the layer name.
* Use dsl-spy.sh (aka lcm-spy) to identify channel name and field of interest
* the plugin requres the message type to be class.type_t; e.g. `comms.statexy_t`. dsl-spy only shows the second half; if you don't know what package a message is in, you can find it by: `cd /path/to/dslmeta`; `find . -iname "statexy_t.msg"`
* QGIS will be displeased if you try to plot 30Hz data for a whole dive. 1Hz is usually reasonable.
* Uncheck the "Create layer?" checkbox if you only want to see the data in time series view (this saves memory vs. creating a QGIS layer for every scalar value).
* a QGIS layer (and time series axes) will be created with the input "layer name"

Use standard QGIS configuration to set the layer properties. If you save the layers in your QGIS project, the styling will be preserved. (The plugin itself does not attempt to set any styling.)

The plugin configuration (which topics/fields are monitored) is also saved in the QGIS project. Be sure to save the project (even if there are no other changes to the underlying layers) in order for the configuration to persist across restarts.

If you're using graduated symbols, be sure to edit the minimum/maximum values are outside the data range after auto-generating the classes (otherwise, QGIS is silly and just won't display data outside the range.)

## Operation

The plots will auto-refresh as new data comes in.
In order geolocate a datapoint from the time series plot, simply click within the plot -- the Scalar Data Cursor symbol will be moved to the lat,lon corresponding to the clicked timestamp.

We also have some options to configure what's shown in the time-series viewer.
* The checkboxes toggle visibility, but the data is still there
* min/max Y allow the user to set y limits to cut off fliers. Leave blank or type 'none' if you want limits to be calculated from data bounds
* the remove button will entirely remove that data source, both from the layers menu and the time series plot.
* the clear button will remove all points from the QGIS layer; this is useful because the previous dive's data may be saved in the project file.

The plugin provides two ways of selecting the time range:
1) right-click-and-drag across desired range
2) text entry of either moving window or starting time
Whichever has been more recently set will take precedence.

The text entry is grossly overloaded:
* Enter a single number, and it will be interpreted as a moving window, and will display the last N seconds of data, as a moving window.
* Leave the box empty (or enter something that doesn't parse), and the xaxis will cover the full span of data.
* A timestamp formatted in "YYYY-mm-dd HH:MM:SS" will show all data since that timestamp, making it possible to cut off the descent and then view all data at depth.

Adding persistent markers is currently a little clunky to set up, but :
* Select the NUI group, then "Layer"->"Create Layer"->"New Shapefile Layer"
  * choose a filename alongside your other QGIS layers for the project
  * Select "Geometry Type" -> "Point"
  * Make sure it's using the EPSG:4326 CRS
  * Create a new field, with Name="notes", Type="text data", then click "Add to Fields List"
  * click OK
* Configure layer to display annotations
  * right-click on layer in the menu
  * click "labels", then select "Single Labels", then "OK"
* With the new layer selected, enable editing by clicking the pencil
* Select the "Add Point Feature Tool" (icon with three dots, or command+.)
* Click on area of interest; fill in note in popup, then click OK
* disable editing by clicking pencil again; save when prompted
