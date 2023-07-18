import importlib

import PyQt5.QtCore as QtCore
import PyQt5.QtWidgets as QtWidgets
from qgis.core import Qgis, QgsMessageLog


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


class ScalarDataFieldWidget(QtWidgets.QWidget):
    """
    Widget that holds all the elements for adding a new field to the scalar
    data widget.

    emits:
    * new_field(channel, msg_type_str, msg_field, sample_rate, layer_name)
        Will be validated for existence of type+field
    """

    new_field = QtCore.pyqtSignal(str, str, str, float, str)

    def __init__(self, iface, parent=None):
        super(ScalarDataFieldWidget, self).__init__(parent)
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
