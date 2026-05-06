# -*- coding: utf-8 -*-
import os
from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon
from .crear_bufer import CrearBuferPLP

class CrearZonasInfluenciaProvider(QgsProcessingProvider):
    PROVIDER_ID = 'crear_zonas_influencia'
    def id(self): return self.PROVIDER_ID
    def name(self): return 'Crear Zonas de Influencia Personalizadas'
    def longName(self): return 'Herramientas de análisis de zonas de influencia (búferes avanzados)'
    def icon(self):
        p = os.path.join(os.path.dirname(__file__), 'icon.png')
        return QIcon(p) if os.path.isfile(p) else super().icon()
    def versionInfo(self):
        try:
            from . import crear_bufer
            return getattr(crear_bufer, '__version__', '1.0.0')
        except Exception:
            return '1.0.0'
    def loadAlgorithms(self): self.addAlgorithm(CrearBuferPLP())
    def supportsNonFileBasedOutput(self): return True
