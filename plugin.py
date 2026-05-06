# -*- coding: utf-8 -*-
"""
Crear Zonas de Influencia Personalizadas — Clase principal del complemento.
"""

import os

from qgis.core import QgsApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .provider import CrearZonasInfluenciaProvider

# ID completo del algoritmo: proveedor:nombre
_ALGORITHM_ID = f'{CrearZonasInfluenciaProvider.PROVIDER_ID}:zonas_influencia_personalizadas'


class CrearZonasInfluenciaPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self._action = None

    def initGui(self):
        # 1. Registrar proveedor de Processing
        self.provider = CrearZonasInfluenciaProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

        # 2. Crear acción de menú/barra
        if self.iface is not None:
            icon_path = os.path.join(os.path.dirname(__file__), 'icon.png')
            icon = QIcon(icon_path) if os.path.isfile(icon_path) else QIcon()

            self._action = QAction(
                icon,
                'Crear Zonas de Influencia Personalizadas',
                self.iface.mainWindow()
            )
            self._action.setToolTip(
                'Crear Zonas de Influencia Personalizadas: Puntos, Líneas y Polígonos'
            )
            self._action.triggered.connect(self._open_algorithm_dialog)
            self.iface.addPluginToMenu('Herramientas de Análisis', self._action)
            self.iface.addToolBarIcon(self._action)

    def unload(self):
        if self.iface is not None and self._action is not None:
            self.iface.removePluginMenu('Herramientas de Análisis', self._action)
            self.iface.removeToolBarIcon(self._action)
            self._action = None
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

    def _open_algorithm_dialog(self):
        """
        Abre el diálogo del algoritmo.

        Estrategia con tres niveles de compatibilidad:
          1. processing.execAlgorithmDialog()  — API pública, QGIS >= 3.x
          2. iface.openProcessingAlgorithm()   — disponible en algunas builds
          3. Construir AlgorithmDialog manualmente — fallback final
        En todos los casos, si el algoritmo no está registrado aún, se emite
        un pushWarning descriptivo en lugar de fallar silenciosamente.
        """
        # Verificar que el algoritmo está registrado
        registry = QgsApplication.processingRegistry()
        alg = registry.algorithmById(_ALGORITHM_ID)

        if alg is None:
            if self.iface:
                self.iface.messageBar().pushWarning(
                    'Crear Zonas de Influencia',
                    f'Algoritmo no encontrado ({_ALGORITHM_ID}). '
                    'Intente recargar el complemento desde '
                    'Complementos → Administrar e instalar complementos.'
                )
            return

        # Nivel 1: API pública de processing (más estable)
        try:
            import processing
            processing.execAlgorithmDialog(_ALGORITHM_ID)
            return
        except Exception:
            pass

        # Nivel 2: iface.openProcessingAlgorithm (algunas builds 3.x)
        try:
            self.iface.openProcessingAlgorithm(_ALGORITHM_ID, {})
            return
        except AttributeError:
            pass
        except Exception:
            pass

        # Nivel 3: construir AlgorithmDialog directamente
        try:
            from processing.gui.AlgorithmDialog import AlgorithmDialog
            dlg = AlgorithmDialog(
                alg.create(),
                in_place=False,
                parent=self.iface.mainWindow()
            )
            dlg.show()
        except Exception as exc:
            if self.iface:
                self.iface.messageBar().pushCritical(
                    'Crear Zonas de Influencia',
                    f'No se pudo abrir el diálogo del algoritmo: {exc}'
                )
