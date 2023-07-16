from .nui_scalar_data import NuiScalarDataPlugin


def classFactory(iface):
    return NuiScalarDataPlugin(iface)
