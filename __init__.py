def classFactory(iface):
    from .plugin import BoreholePlugin
    return BoreholePlugin(iface)
