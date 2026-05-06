# -*- coding: utf-8 -*-
def classFactory(iface):
    from .plugin import CrearZonasInfluenciaPlugin
    return CrearZonasInfluenciaPlugin(iface)
