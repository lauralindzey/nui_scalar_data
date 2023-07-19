import importlib
import typing

import PyQt5.QtCore as QtCore
import PyQt5.QtGui as QtGui
import PyQt5.QtWidgets as QtWidgets
from qgis.core import Qgis, QgsMessageLog


# Line widgets from:
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


# Vertical Label widget from:
# https://stackoverflow.com/questions/3757246/pyqt-rotate-a-qlabel-so-that-its-positioned-diagonally-instead-of-horizontally
class VerticalLabel(QtWidgets.QLabel):
    def __init__(self, *args):
        QtWidgets.QLabel.__init__(self, *args)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.translate(0, self.height())
        painter.rotate(-90)
        # calculate the size of the font
        fm = QtGui.QFontMetrics(painter.font())
        xoffset = int(fm.boundingRect(self.text()).width() / 2)
        yoffset = int(fm.boundingRect(self.text()).height() / 2)
        x = int(self.width() / 2) + yoffset
        y = int(self.height() / 2) - xoffset
        # because we rotated the label, x affects the vertical placement, and y affects the horizontal
        painter.drawText(y, x, self.text())
        painter.end()

    def minimumSizeHint(self):
        size = QtWidgets.QLabel.minimumSizeHint(self)
        return QtCore.QSize(size.height(), size.width())

    def sizeHint(self):
        size = QtWidgets.QLabel.sizeHint(self)
        return QtCore.QSize(size.height(), size.width())


class ConfigureTimeSeriesWidget(QtWidgets.QWidget):
    """
    Widget for enabling/disabling display of fields on the scalar data plot,
    as well adjusting their y-axes.
    """

    # Key, whether line is enabled
    toggle_plot = QtCore.pyqtSignal(str, bool)
    # key, ymin, ymax. None to use data min/max. PyQt doesn't support using None
    #   in signals/slots, so we use the overly-general object here.
    ylim_changed = QtCore.pyqtSignal(str, object, object)
    # whether to entirely stop tracking a given field. Removes from map and time series.
    remove_field = QtCore.pyqtSignal(str)

    VISIBLE_COLUMN = 0
    NAME_COLUMN = 1
    MIN_Y_COLUMN = 2
    MAX_Y_COLUMN = 3
    REMOVE_COLUMN = 4

    def __init__(self, iface, parent=None):
        super(ConfigureTimeSeriesWidget, self).__init__(parent)
        self.iface = iface
        self.setup_ui()

    def setup_ui(self):
        self.grid = QtWidgets.QGridLayout()

        # Before items are added, just add header row
        self.visible_label = VerticalLabel("Visible")
        self.layer_name_label = VerticalLabel("Name")
        self.min_y_label = VerticalLabel("Min Y")
        self.max_y_label = VerticalLabel("Max Y")
        self.remove_label = VerticalLabel("Remove")

        # Dict mapping key to buttons and textboxes
        self.widgets = {}

        row = 0
        self.grid.addWidget(self.visible_label, row, self.VISIBLE_COLUMN)
        self.grid.addWidget(self.layer_name_label, row, self.NAME_COLUMN)
        self.grid.addWidget(self.min_y_label, row, self.MIN_Y_COLUMN)
        self.grid.addWidget(self.max_y_label, row, self.MAX_Y_COLUMN)
        self.grid.addWidget(self.remove_label, row, self.REMOVE_COLUMN)

        self.setLayout(self.grid)
        self.remove_field.connect(self.remove_field_widgets)

    def add_field(self, key, layer_name):
        visible_checkbox = QtWidgets.QCheckBox()
        visible_checkbox.setChecked(True)
        visible_checkbox.stateChanged.connect(
            lambda state, key=key: self.toggle_plot.emit(
                key, state == QtCore.Qt.Checked
            )
        )
        name_label = QtWidgets.QLabel(layer_name)
        ymin_lineedit = QtWidgets.QLineEdit()
        ymin_lineedit.setFixedWidth(35)
        ymin_lineedit.editingFinished.connect(lambda key=key: self.on_ylim_changed(key))
        ymax_lineedit = QtWidgets.QLineEdit()
        ymax_lineedit.setFixedWidth(35)
        ymax_lineedit.editingFinished.connect(lambda key=key: self.on_ylim_changed(key))
        remove_button = QtWidgets.QPushButton("x")
        remove_button.setFixedWidth(25)
        remove_button.setStyleSheet("QPushButton {color: red;}")
        remove_button.pressed.connect(lambda key=key: self.remove_field.emit(key))

        self.widgets[key] = (
            visible_checkbox,
            name_label,
            ymin_lineedit,
            ymax_lineedit,
            remove_button,
        )
        row = self.grid.rowCount()
        self.grid.addWidget(visible_checkbox, row, self.VISIBLE_COLUMN)
        self.grid.addWidget(name_label, row, self.NAME_COLUMN)
        self.grid.addWidget(ymin_lineedit, row, self.MIN_Y_COLUMN)
        self.grid.addWidget(ymax_lineedit, row, self.MAX_Y_COLUMN)
        self.grid.addWidget(remove_button, row, self.REMOVE_COLUMN)

    def on_ylim_changed(self, key):
        # when one box changes, go ahead and send update for both
        # TODO: test empty/none case
        try:
            ymin_qstring = self.widgets[key][self.MIN_Y_COLUMN].text()
            ymin = float(str(ymin_qstring))
        except:
            ymin = None
        try:
            ymax_qstring = self.widgets[key][self.MAX_Y_COLUMN].text()
            ymax = float(str(ymax_qstring))
        except:
            ymax = None

        self.ylim_changed.emit(key, ymin, ymax)

    @QtCore.pyqtSlot(str)
    def remove_field_widgets(self, key):
        print(f"ConfigureTimeSeriesWidget.remove_field_widgets: {key}")
        if key not in self.widgets or self.widgets[key] is None:
            err = f"Cannot remove widgets for key {key} -- not in dict!"
            print(err)
            return

        for widget in self.widgets[key]:
            widget.deleteLater()
            del widget
        self.widgets[key] = None


class AddScalarDataFieldWidget(QtWidgets.QWidget):
    """
    Widget that holds all the elements for adding a new field to the scalar
    data widget.

    emits:
    * new_field(channel, msg_type_str, msg_field, sample_rate, layer_name)
        Will be validated for existence of type+field
    """

    new_field = QtCore.pyqtSignal(str, str, str, float, str)

    def __init__(self, iface, parent=None):
        super(AddScalarDataFieldWidget, self).__init__(parent)
        self.iface = iface
        self.setup_ui()

    def setup_ui(self):
        self.grid = QtWidgets.QGridLayout()

        row = 0
        self.channel_name_label = QtWidgets.QLabel("Channel:")
        self.channel_name_lineedit = QtWidgets.QLineEdit()
        self.grid.addWidget(self.channel_name_label, row, 0)
        self.grid.addWidget(self.channel_name_lineedit, row, 1)
        row += 1

        self.msg_type_label = QtWidgets.QLabel("LCM type:")
        self.msg_type_lineedit = QtWidgets.QLineEdit()
        self.grid.addWidget(self.msg_type_label, row, 0)
        self.grid.addWidget(self.msg_type_lineedit, row, 1)
        row += 1

        self.msg_field_label = QtWidgets.QLabel("Field:")
        self.msg_field_lineedit = QtWidgets.QLineEdit()
        self.grid.addWidget(self.msg_field_label, row, 0)
        self.grid.addWidget(self.msg_field_lineedit, row, 1)
        row += 1

        self.sample_rate_label = QtWidgets.QLabel("Rate (Hz):")
        self.sample_rate_lineedit = QtWidgets.QLineEdit()
        self.grid.addWidget(self.sample_rate_label, row, 0)
        self.grid.addWidget(self.sample_rate_lineedit, row, 1)
        row += 1

        self.layer_name_label = QtWidgets.QLabel("Layer name:")
        self.layer_name_lineedit = QtWidgets.QLineEdit()
        self.grid.addWidget(self.layer_name_label, row, 0)

        self.grid.addWidget(self.layer_name_lineedit, row, 1)
        row += 1

        self.add_field_button = QtWidgets.QPushButton("Add Field")
        self.add_field_button.clicked.connect(self.add_button_clicked)
        self.grid.addWidget(self.add_field_button, row, 0, 1, 2)

        self.setLayout(self.grid)

    def add_button_clicked(self, _checked):
        print("add_button_clicked")
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
            msg_module = importlib.import_module(msg_pkg)
            msg_type = getattr(msg_module, msg_class)
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

        self.new_field.emit(
            channel_name, msg_type_str, msg_field, sample_rate, layer_name
        )
