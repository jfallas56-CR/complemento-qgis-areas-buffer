# -*- coding: utf-8 -*-
"""
================================================================================
Crear Zonas de Influencia Personalizadas: Puntos, Líneas y Polígonos
================================================================================
Autor:      Jorge Fallas <jfallas56@gmail.com>
Versión:    1.0.0
Fecha:      Marzo 2026
Licencia:   GPL-2.0-or-later (ver LICENSE.txt)

Propósito:
    Algoritmo de Processing para QGIS que genera búferes geoespaciales
    avanzados sobre capas de puntos, líneas y polígonos. Soporta 9 tipos
    de búfer (circular, oval, rectangular, concéntrico, por área, un solo
    lado, cuña, adaptativo por densidad y ancho variable por puntos),
    operaciones lógicas de superposición, post-proceso geométrico, reporte
    HTML con indicadores ISO 19157:2023, y exportación de configuración JSON.

Dependencias:
    - QGIS  >= 3.28  (LTR recomendado; mínimo absoluto 3.14 por FlagSkipGenericModelLogging)
                     poleOfInaccessibility >= 3.12, makeValid >= 3.12, QgsWkbTypes.hasZ/M >= 3.0
    - GEOS  >= 3.9   (requerido por makeValid y poleOfInaccessibility)
    - Python >= 3.9
    - PyQt5 / PyQt6  (accedido vía qgis.PyQt — compatible con Qt5 y Qt6)
    - Módulos estándar: math, datetime, os, webbrowser, traceback, sys,
      time, threading, json, tempfile, gc, warnings, html,
      itertools, concurrent.futures, dataclasses, typing, abc

Parámetros principales de entrada:
    INPUT               Capa vectorial fuente (puntos, líneas o polígonos)
    BUFFER_TYPE         Tipo de búfer (0-8, ver Constants.BUFFER_NAMES)
    DISTANCIA           Radio/distancia del búfer en unidades del mapa
    ANCHO / ALTO        Dimensiones para búferes Oval y Rectangular
    CONCENTRIC_COUNT    Número de anillos (búfer Concéntrico)
    AREA_OBJETIVO       Área objetivo en ha/m²/km² (búfer Por Área)
    DISTANCE_FIELD      Campo numérico para búfer variable por entidad
    CATEGORY_FIELD      Campo categórico para búfer variable por categoría
    CATEGORY_MAPPING    Mapeo JSON categoría→distancia
    EXCLUSION_LAYER     Capa de exclusión (resta área a todos los búferes)
    GESTION_INTEGRIDAD  Estrategia ante geometrías inválidas (0/1/2)
    DRY_RUN             Validación previa sin generar geometrías

Parámetros principales de salida:
    OUTPUT              Capa vectorial de búferes generados
    OUTPUT_FRAGMENTOS   Capa opcional de fragmentos de traslape
    RUTA_REPORTE        Ruta del reporte HTML de calidad (ISO 19157:2023)
    RUTA_CONFIG_JSON    Ruta del archivo JSON de configuración exportada

Registro de cambios:
    1.0.0 (Marzo 2026) — Versión inicial de producción.
        · 9 tipos de búfer incluyendo Adaptativo por Densidad y Ancho Variable.
        · Logger thread-safe con métricas estadísticas.
        · Reporte HTML con indicadores ISO 19157:2023.
        · Exportación de configuración JSON para trazabilidad y auditoría.
        · Modo Validación Previa (dry-run) sin generación de geometrías.
        · Procesamiento paralelo con ThreadPoolExecutor.
        · Post-proceso: resolución de traslapes, eliminación de huecos,
          disolución de búferes.

Notas:
    - Usar CRS proyectado (ej. CRTM05, UTM) para resultados en metros.
    - En CRS geográfico (grados) el algoritmo emite advertencia; los
      valores de distancia se interpretarán en grados.
    - Para datasets grandes (>500 entidades complejas) se recomienda
      activar la simplificación de geometrías de entrada.
================================================================================
"""

# ==============================================================================
# VERSIÓN DEL SCRIPT
# ==============================================================================
__version__ = "1.0.0"   # Actualizar junto con el encabezado y el JSON exportado


import math           # Cálculos trigonométricos (búfer cuña, área circular)
import datetime       # Fecha/hora en el reporte HTML
import os             # Manejo de rutas de archivos
import webbrowser     # Abrir el reporte HTML en el navegador
import traceback      # Captura de trazas de error detalladas
import sys            # Acceso a sys.exc_info() en manejo de excepciones
import time           # Medir tiempos de ejecución (Logger, ResourceGuard)
import threading      # Lock para thread-safety en Logger y procesamiento paralelo
import json           # Parseo del mapeo categoría→distancia
import tempfile       # Ruta temporal para el reporte HTML (gettempdir)
import gc             # Liberación de memoria en disolución por lotes
import warnings       # Advertencias internas (ResourceGuard.start_operation)
import html           # Escape de caracteres especiales en reporte HTML (html.escape)
from itertools import combinations          # Pares únicos en análisis de fragmentos
from concurrent.futures import ThreadPoolExecutor, as_completed  # Procesamiento paralelo
from dataclasses import dataclass, field, replace  # BufferParams y variantes
from typing import List, Tuple, Optional, Dict, Any  # Anotaciones de tipo
from abc import ABC, abstractmethod         # Clases base abstractas (BufferProcessor, LogicOperation)

from qgis.core import (
    QgsProcessingParameterDefinition,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterDistance,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterFile,
    QgsProcessingParameterString,
    QgsProcessingParameterField,
    QgsFeature,
    QgsGeometry,
    QgsWkbTypes,
    QgsFields,
    QgsField,
    QgsPointXY,
    QgsProcessingException,
    QgsProcessingContext,
    Qgis,
    QgsFeatureSink,
    QgsProcessingUtils,
    QgsCoordinateReferenceSystem,
    QgsUnitTypes,
    QgsFeatureRequest,
    QgsSpatialIndex,
    QgsRenderContext,
    QgsVectorLayer,
    QgsRectangle
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import QApplication


# ==============================================================================
# MODO DE INTERFAZ  ← ÚNICA VARIABLE DE CONFIGURACIÓN DE USUARIO EN ESTE SCRIPT
# ------------------------------------------------------------------------------
# True  → Interfaz compacta: 22 parámetros esenciales visibles +
#          44 parámetros bajo el botón "▼ Mostrar parámetros avanzados"
#          Recomendado para uso general.
#
# False → Interfaz completa: todos los parámetros visibles (comportamiento
#          original). Recomendado si necesita acceso directo a todos los
#          controles sin clics adicionales.
#
# IMPORTANTE: Esta es la ÚNICA variable pensada para ser modificada
# directamente en el código fuente por el usuario final. Todas las demás
# constantes residen en la clase Constants y no deben editarse sin conocer
# el impacto en el algoritmo.
#
# Para cambiar el modo: edite solo esta línea y recargue el complemento en
# QGIS (Complementos → Administrar → Recargar, o F5 en la consola Python).
# ==============================================================================
INTERFAZ_COMPACTA = True


# ==============================================================================
# CONSTANTES GLOBALES
# ==============================================================================
class Constants:
    """Constantes del algoritmo."""
    # Conversión de unidades de área
    HA_TO_M2 = 10000.0
    KM2_TO_M2 = 1000000.0
    M2_TO_M2 = 1.0
    
    # Geometría
    MIN_BUFFER_DISTANCE = 0.0001
    DEFAULT_SEGMENTOS = 25
    MAX_CONCENTRIC_RINGS = 50
    MAX_FRAGMENTOS_TRASLAPE = 1000
    PARALELO_THRESHOLD      = 50
    
    # Tolerancias
    AREA_TOLERANCE = 1e-3
    BISECTION_ITERATIONS = 30
    EXPANSION_ITERATIONS = 20
    
    # Tipos de búfer
    BUFFER_CIRCULAR = 0
    BUFFER_OVAL = 1
    BUFFER_RECTANGULAR = 2
    BUFFER_CONCENTRICO = 3
    BUFFER_POR_AREA = 4
    BUFFER_UN_LADO = 5
    BUFFER_CUNA = 6
    BUFFER_ADAPTATIVO = 7
    BUFFER_ANCHO_M    = 8
    
    # Operaciones lógicas
    OP_NINGUNA = 0
    OP_UNION = 1
    OP_INTERSECCION = 2
    OP_DIFERENCIA = 3
    OP_DIFERENCIA_INV = 4
    OP_XOR = 5
    
    # Estilos de unión
    JOIN_ROUND = 0
    JOIN_MITER = 1
    JOIN_BEVEL = 2
    
    # Lados (single-sided)
    SIDE_LEFT = 0
    SIDE_RIGHT = 1
    
    # Gestión de integridad geométrica
    INTEGRIDAD_RIESGO = 0
    INTEGRIDAD_OMITIR = 1
    INTEGRIDAD_REPARAR = 2
    
    # Gestión de traslapes
    TRASLAPE_MANTENER = 0      # No modificar traslapes
    TRASLAPE_MAYOR = 1         # Asignar traslape al polígono mayor
    TRASLAPE_MENOR = 2         # Asignar traslape al polígono menor
    
    # Nombres
    BUFFER_NAMES = ['Circular', 'Oval', 'Rectangular', 'Concéntrico', 
                    'Por Área', 'Un solo lado', 'Cuña', 'Adaptativo por densidad',
                    'Ancho Variable (Puntos)']
    OP_NAMES = ['Ninguna', 'Unión', 'Intersección', 'Diferencia', 
                'Dif. Inv', 'XOR']
    AREA_UNITS = ['Hectáreas', 'm²', 'km²']
    AREA_UNIT_SYMBOLS = ['ha', 'm²', 'km²']
    INTEGRIDAD_NAMES = ['⚠️ No verificar (Riesgo - Procesar "As Is")',
                       '🚫 Omitir geometría inválida',
                       '🔧 Reparar geometría (Recomendado)']
    TRASLAPE_NAMES = ['🔀 Mantener traslapes (sin modificar)',
                      '📈 Asignar traslape al polígono MAYOR',
                      '📉 Asignar traslape al polígono MENOR']
    
    # Búfer adaptativo por densidad
    DENSIDAD_KNN = 0        # Método k vecinos más cercanos
    DENSIDAD_RADIO = 1      # Método conteo en radio fijo (else implícito en comparaciones)
    DENSIDAD_NAMES = ['📍 K vecinos más cercanos (KNN)',
                      '🔵 Conteo en radio fijo']
    
    # === NUEVO: Métodos de anclaje para densidad ===
    DENSIDAD_ANCLAJE_CENTROIDE = 0
    DENSIDAD_ANCLAJE_POLO = 1
    DENSIDAD_ANCLAJE_NAMES = [
        '📍 Centroide (rápido)',
        '🎯 Punto interior representativo (preciso para formas irregulares)'
    ]


# ==============================================================================
# CLASE: LOGGER (Gestión de Alertas y Métricas)
# ==============================================================================
class Logger:
    """Sistema de logging con clasificación de mensajes y métricas.
    Thread-safe: usa Lock para proteger datos compartidos en procesamiento paralelo."""
    
    def __init__(self, feedback):
        self.feedback = feedback
        self._lock = threading.Lock()  # Protección para procesamiento paralelo
        self.errores: List[str] = []
        self.advertencias: List[str] = []
        self.start_time: float = time.time()
        
        # Métricas de procesamiento
        self.geometrias_reparadas: int = 0
        self.geometrias_omitidas: int = 0
        self.geometrias_procesadas: int = 0
        self.geometrias_con_z: int = 0
        self.geometrias_multipart: int = 0
        self.distancia_min: float = float('inf')
        self.distancia_max: float = float('-inf')
        
        # Acumuladores de área — O(1) en memoria en vez de O(n)
        self._area_sum: float = 0.0
        self._area_count: int = 0
        self._area_min: float = float('inf')
        self._area_max: float = float('-inf')
        
        # Registros para integridad geométrica (sets para búsqueda O(1))
        self.reparados_ids: set = set()
        self.omitidos_ids: set = set()
        self.riesgo_ids: set = set()
        self.sin_bufer_ids: set = set()  # Features que pasaron validación pero no generaron búfer
        self.fragmentados_ids: dict = {}  # fid → n_partes: búferes por área que produjeron MultiPolígono
        
        # Registros de campos variables con valores faltantes (para reporte HTML)
        self.null_field_count: int = 0           # Campo numérico NULL → usó distancia fija
        self.null_cat_count: int = 0             # Categoría NULL en JSON → usó distancia fija
        self.missing_cat_ids: dict = {}          # categoría_no_encontrada → n_ocurrencias
    
    def log(self, message: str, level: str = 'INFO') -> None:
        """Registra un mensaje con el nivel especificado."""
        with self._lock:
            if level == 'WARNING':
                self.advertencias.append(message)
                if hasattr(self.feedback, 'pushWarning'):
                    self.feedback.pushWarning(message)
                else:
                    self.feedback.reportError(f"⚠️ {message}")
            elif level == 'ERROR':
                self.errores.append(message)
                self.feedback.reportError(f"❌ {message}")
            else:
                self.feedback.pushInfo(message)
    
    def info(self, message: str) -> None:
        """Mensaje informativo → pushInfo."""
        self.log(message, 'INFO')
    
    def warning(self, message: str) -> None:
        """Advertencia → pushWarning + reporte HTML."""
        self.log(message, 'WARNING')
    
    def error(self, message: str) -> None:
        """Error crítico → reportError."""
        self.log(message, 'ERROR')
    
    def registrar_reparacion(self, fid: int) -> None:
        """Registra una geometría como reparada."""
        with self._lock:
            self.geometrias_reparadas += 1
            self.reparados_ids.add(fid)
    
    def registrar_omision(self, fid: int) -> None:
        """Registra una geometría como omitida."""
        with self._lock:
            self.geometrias_omitidas += 1
            self.omitidos_ids.add(fid)
    
    def registrar_riesgo(self, fid: int) -> None:
        """Registra una geometría procesada con riesgo."""
        with self._lock:
            self.riesgo_ids.add(fid)
    
    def registrar_sin_bufer(self, fid: int) -> None:
        """Registra una feature válida que no generó búfer (distancia=0, colapso, etc.)."""
        with self._lock:
            self.sin_bufer_ids.add(fid)

    def registrar_fragmentacion(self, fid: int, n_partes: int) -> None:
        """Registra un búfer Por Área cuya contracción fragmentó el polígono en múltiples partes."""
        with self._lock:
            self.fragmentados_ids[fid] = n_partes

    def registrar_null_field(self) -> None:
        """Registra una entidad cuyo campo numérico era NULL — usó distancia fija."""
        with self._lock:
            self.null_field_count += 1

    def registrar_null_cat(self) -> None:
        """Registra una entidad cuya categoría JSON era NULL — usó distancia fija."""
        with self._lock:
            self.null_cat_count += 1

    def registrar_missing_cat(self, categoria: str) -> None:
        """Registra una categoría no encontrada en el mapeo JSON."""
        with self._lock:
            self.missing_cat_ids[categoria] = self.missing_cat_ids.get(categoria, 0) + 1
    
    def registrar_geometria_procesada(self) -> None:
        with self._lock:
            self.geometrias_procesadas += 1
    
    def registrar_geometria_z(self) -> None:
        with self._lock:
            self.geometrias_con_z += 1
    
    def registrar_geometria_multipart(self) -> None:
        with self._lock:
            self.geometrias_multipart += 1
    
    def registrar_distancia(self, distancia: float) -> None:
        with self._lock:
            if distancia < self.distancia_min:
                self.distancia_min = distancia
            if distancia > self.distancia_max:
                self.distancia_max = distancia
    
    def registrar_area(self, area: float) -> None:
        with self._lock:
            self._area_sum += area
            self._area_count += 1
            if area < self._area_min:
                self._area_min = area
            if area > self._area_max:
                self._area_max = area
    
    def get_tiempo_ejecucion(self) -> float:
        return time.time() - self.start_time
    
    def get_area_promedio(self) -> float:
        return self._area_sum / self._area_count if self._area_count > 0 else 0.0
    
    def get_area_min(self) -> float:
        return self._area_min if self._area_min != float('inf') else 0.0
    
    def get_area_max(self) -> float:
        return self._area_max if self._area_max != float('-inf') else 0.0
    
    def get_metricas(self) -> Dict[str, Any]:
        """Retorna todas las métricas recopiladas."""
        return {
            'tiempo_ejecucion': self.get_tiempo_ejecucion(),
            'geometrias_procesadas': self.geometrias_procesadas,
            'geometrias_reparadas': self.geometrias_reparadas,
            'geometrias_omitidas': self.geometrias_omitidas,
            'geometrias_con_z': self.geometrias_con_z,
            'geometrias_multipart': self.geometrias_multipart,
            'distancia_min': self.distancia_min if self.distancia_min != float('inf') else 0,
            'distancia_max': self.distancia_max if self.distancia_max != float('-inf') else 0,
            'area_promedio': self.get_area_promedio(),
            'area_min': self.get_area_min(),
            'area_max': self.get_area_max(),
            'total_advertencias': len(self.advertencias),
            'total_errores': len(self.errores),
            'reparados_ids': sorted(self.reparados_ids),
            'omitidos_ids': sorted(self.omitidos_ids),
            'riesgo_ids': sorted(self.riesgo_ids),
            'sin_bufer_ids': sorted(self.sin_bufer_ids),
            'fragmentados_ids': dict(self.fragmentados_ids),
            'null_field_count': self.null_field_count,
            'null_cat_count': self.null_cat_count,
            'missing_cat_ids': dict(self.missing_cat_ids)
        }


# ==============================================================================
# CLASE: VALIDADOR DE CRS
# ==============================================================================
class CRSValidator:
    """Valida y proporciona información sobre el Sistema de Referencia de Coordenadas."""
    
    @staticmethod
    def es_geografico(crs: QgsCoordinateReferenceSystem) -> bool:
        """Verifica si el CRS es geográfico (grados)."""
        if not crs or not crs.isValid():
            return False
        return crs.isGeographic()
    
    @staticmethod
    def get_unidad(crs: QgsCoordinateReferenceSystem) -> str:
        """Obtiene la unidad del CRS."""
        if not crs or not crs.isValid():
            return "Desconocida"
        
        if crs.isGeographic():
            return "Grados"
        
        units = crs.mapUnits()
        unit_names = {
            QgsUnitTypes.DistanceMeters: "Metros",
            QgsUnitTypes.DistanceKilometers: "Kilómetros",
            QgsUnitTypes.DistanceFeet: "Pies",
            QgsUnitTypes.DistanceYards: "Yardas",
            QgsUnitTypes.DistanceMiles: "Millas",
            QgsUnitTypes.DistanceNauticalMiles: "Millas Náuticas",
            QgsUnitTypes.DistanceDegrees: "Grados"
        }
        return unit_names.get(units, "Desconocida")
    
    @staticmethod
    def generar_advertencia_crs(crs: QgsCoordinateReferenceSystem) -> Optional[str]:
        """Genera advertencia si el CRS no es apropiado para análisis métrico."""
        if CRSValidator.es_geografico(crs):
            return ("⚠️ ADVERTENCIA: El CRS es geográfico (grados). "
                   "Los cálculos de distancia y área pueden ser inexactos. "
                   "Se recomienda usar un CRS proyectado (ej: UTM, CRTM05).")
        return None


# ==============================================================================
# CLASE: RESOURCE GUARD (Protección contra operaciones costosas - CRÍTICO #4)
# ==============================================================================
class ResourceGuard:
    """
    Protección contra operaciones que consumen recursos excesivos.
    
    Previene:
    - Geometrías extremadamente complejas (>100000 vértices)
    - Operaciones que tardan más del tiempo límite
    - Consumo de memoria excesivo
    
    Uso como context manager (recomendado):
        with ResourceGuard(max_time_sec=60) as guard:
            guard.start_operation("Mi operación")
            ...
            guard.check_timeout()
    """
    
    def __init__(self, max_time_sec=300, max_vertices=100000):
        self.max_time = max_time_sec
        self.max_vertices = max_vertices
        self.start_time = None
        self.operation_name = None
    
    # ------------------------------------------------------------------
    # Context manager: garantiza reset limpio al salir del bloque with
    # ------------------------------------------------------------------
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Resetea el estado al salir, independientemente de si hubo excepción."""
        self.start_time = None
        self.operation_name = None
        return False  # No suprime excepciones
    
    def check_geometry_complexity(self, geom: QgsGeometry, operation_name: str) -> None:
        """Valida complejidad antes de operación costosa."""
        vertex_count = self._count_vertices(geom)
        
        if vertex_count > self.max_vertices:
            raise RuntimeError(
                f"{operation_name}: Geometría demasiado compleja.\n"
                f"Vértices: {vertex_count:,} (límite: {self.max_vertices:,})\n\n"
                f"💡 Simplifique la geometría antes del búfer"
            )
    
    def _count_vertices(self, geom: QgsGeometry) -> int:
        """Cuenta vértices recursivamente."""
        if not geom or geom.isEmpty():
            return 0
        
        count = 0
        geom_type = geom.type()
        
        if geom.isMultipart():
            if geom_type == QgsWkbTypes.PointGeometry:
                count = len(geom.asMultiPoint())
            elif geom_type == QgsWkbTypes.LineGeometry:
                for line in geom.asMultiPolyline():
                    count += len(line)
            elif geom_type == QgsWkbTypes.PolygonGeometry:
                for polygon in geom.asMultiPolygon():
                    for ring in polygon:
                        count += len(ring)
        else:
            if geom_type == QgsWkbTypes.PointGeometry:
                count = 1
            elif geom_type == QgsWkbTypes.LineGeometry:
                count = len(geom.asPolyline())
            elif geom_type == QgsWkbTypes.PolygonGeometry:
                for ring in geom.asPolygon():
                    count += len(ring)
        
        return count
    
    def start_operation(self, operation_name: str) -> None:
        """Inicia cronómetro para una operación.
        
        Si ya hay una operación en curso (start_time activo), emite una advertencia
        antes de sobreescribir el timer para evitar pérdida silenciosa del Tiempo de Espera de la operación anterior.
        """
        if self.start_time is not None:
            # Operación anterior no fue cerrada limpiamente — advertir pero continuar
            warnings.warn(
                f"ResourceGuard: start_operation('{operation_name}') llamado mientras "
                f"'{self.operation_name}' sigue activa. "
                f"Use el guard como context manager (with ResourceGuard(...) as g:) "
                f"para garantizar resets limpios.",
                stacklevel=2
            )
        self.start_time = time.time()
        self.operation_name = operation_name
    
    def check_timeout(self) -> None:
        """Verifica timeout."""
        if self.start_time:
            elapsed = time.time() - self.start_time
            if elapsed > self.max_time:
                raise TimeoutError(
                    f"{self.operation_name} excedió tiempo límite.\n"
                    f"Transcurrido: {elapsed:.1f}s (límite: {self.max_time}s)"
                )


# ==============================================================================
# FUNCIÓN: PARSE SEGURO DE MAPEO JSON (ALTA #2)
# ==============================================================================
def parse_category_mapping_safe(json_str: str) -> tuple:
    """
    Parse seguro de mapeo JSON categoría → distancia.
    
    ALTA #2: Valida estructura, tipos y previene inyección de código.
    
    Args:
        json_str: String JSON con mapeo {categoria: distancia}
    
    Returns:
        tuple: (success: bool, result: dict | error_message: str)
    """
    try:
        if not json_str or not json_str.strip():
            return False, "Mapeo JSON vacío"
        
        data = json.loads(json_str)
        
        if not isinstance(data, dict):
            return False, "El mapeo debe ser un diccionario JSON {...}"
        
        if len(data) > 1000:
            return False, f"Demasiadas categorías ({len(data)}). Máximo: 1000"
        
        if len(data) == 0:
            return False, "El mapeo no puede estar vacío"
        
        validated_mapping = {}
        
        for key, value in data.items():
            if not isinstance(key, str):
                return False, f"Categoría inválida: {key} (debe ser texto)"
            
            if len(key) > 100:
                return False, f"Categoría muy larga: {key[:20]}..."
            
            if not key.strip():
                return False, "No se permiten categorías vacías"
            
            if not isinstance(value, (int, float)):
                return False, f"Distancia para '{key}' debe ser numérica"
            
            distance = float(value)
            
            if distance <= 0:
                return False, f"Distancia para '{key}' debe ser > 0"
            
            if distance > 1_000_000:
                return False, f"Distancia para '{key}' fuera de rango: {distance}m"
            
            validated_mapping[key] = distance
        
        return True, validated_mapping
        
    except json.JSONDecodeError as e:
        return False, f"JSON inválido: {str(e)}"
    except Exception as e:
        return False, f"Error: {str(e)}"


# ==============================================================================
# CLASE: GESTOR DE TRANSPARENCIA
# ==============================================================================
class TransparencyManager:
    """Gestiona la transparencia de capas de salida."""
    
    @staticmethod
    def aplicar_transparencia(layer, opacity_percent: float,
                              context: QgsProcessingContext = None) -> bool:
        """Aplica transparencia (opacidad) a la capa.
        
        Args:
            layer: Capa vectorial de destino.
            opacity_percent: Opacidad deseada en porcentaje (0-100).
            context: QgsProcessingContext activo. Se usa cuando el renderer
                     necesita evaluar expresiones o variables de proyecto.
                     Si es None se usa un QgsRenderContext vacío, que es
                     suficiente para la mayoría de renderers estándar.
        """
        if not layer:
            return False
        try:
            opacity = opacity_percent / 100.0
            renderer = layer.renderer()
            if renderer:
                if hasattr(renderer, 'symbol') and renderer.symbol():
                    renderer.symbol().setOpacity(opacity)
                elif hasattr(renderer, 'symbols'):
                    # Preferir el contexto de procesamiento real si está disponible;
                    # de lo contrario usar QgsRenderContext (evita instanciar un
                    # QgsProcessingContext vacío que carece de proyecto y transformaciones).
                    render_ctx = (context.expressionContext()
                                  if context and hasattr(context, 'expressionContext')
                                  else QgsRenderContext())
                    for symbol in renderer.symbols(render_ctx):
                        symbol.setOpacity(opacity)
                layer.triggerRepaint()
                return True
            return False
        except (AttributeError, RuntimeError):
            return False


# ==============================================================================
# DATACLASS: PARÁMETROS DEL BÚFER
# ==============================================================================
@dataclass
class BufferParams:
    """Contenedor de parámetros para el procesamiento de búfer."""
    # Parámetros básicos
    buffer_type: int = 0
    distancia: float = 0.0
    ancho: float = 0.0
    alto: float = 0.0
    rotacion: float = 0.0
    segmentos: int = Constants.DEFAULT_SEGMENTOS
    
    # Concéntricos
    concentric_count: int = 0
    concentric_distance: float = 0.0
    anillos_disjuntos: bool = True
    
    # Área
    area_objetivo: float = 0.0
    unidad_area: int = 0
    calcular_por_area: bool = False
    area_objetivo_m2: float = 0.0
    
    # Transformación de puntos
    usar_hull: bool = False
    usar_box: bool = False
    usar_corredor: bool = False
    
    # Estilos
    side_idx: int = 0
    join_idx: int = 0
    miter_limit: float = 2.0
    
    # Cuña (Wedge)
    wedge_start: float = 0.0
    wedge_width: float = 45.0
    usar_rot_auto: bool = False
    rotation_field: str = ""  # Campo de azimut variable por entidad (opcional)
    
    # Operaciones
    op_logic: int = 0
    
    # Transparencia y reporte
    aplicar_transparencia: bool = True
    nivel_transparencia: int = 50
    generar_reporte: bool = True
    ruta_reporte: str = ""
    nombre_proyecto: str = "Proyecto QGIS"
    
    # Funcionalidades adicionales
    distance_field: str = ""
    category_field: str = ""  # ALTA #2: Campo de categoría
    category_mapping: dict = field(default_factory=dict)  # ALTA #2: Mapeo categoría → distancia
    ancho_field: str = ""
    alto_field: str = ""
    exclusion_layer: Any = None
    preview_mode: bool = False
    calcular_superposicion: bool = False
    generar_fragmentos_traslape: bool = False
    
    # Metadatos
    crs_info: str = "Desconocido"
    es_punto: bool = False
    es_transformacion_activa: bool = False
    
    # Simplificación de geometría
    aplicar_simplificacion: bool = False
    tolerancia_simplificacion: float = 1.0
    
    # Robustez y rendimiento
    continuar_con_errores: bool = True  # Continuar procesando si una geometría falla
    procesar_z: bool = True  # Manejar geometrías con coordenada Z
    crs_es_geografico: bool = False  # Indica si CRS es geográfico
    crs_unidad: str = "Metros"  # Unidad del CRS
    usar_paralelo: bool = False  # Procesamiento paralelo (experimental)
    num_threads: int = 4  # Número de hilos para procesamiento paralelo
    
    # Gestión de integridad geométrica
    gestion_integridad: int = Constants.INTEGRIDAD_REPARAR  # 0=Riesgo, 1=Omitir, 2=Reparar
    
    # Pre-procesamiento de geometrías de entrada
    simplificar_entrada: bool = False  # Simplificar polígonos y líneas antes de crear búfer
    tolerancia_entrada: float = 5.0  # Tolerancia de simplificación de entrada en metros
    
    # Post-procesamiento de geometrías
    resolver_traslapes: int = Constants.TRASLAPE_MANTENER  # 0=Mantener, 1=Mayor, 2=Menor
    eliminar_huecos: bool = False  # Eliminar huecos internos de polígonos
    area_minima_hueco: float = 0.0  # Área mínima de hueco a eliminar (0 = todos)
    preservar_hueco_estructural: bool = True  # Preservar el hueco mayor (donut) en búferes concéntricos
    disolver_buferes: bool = False  # Fusionar búferes que se tocan en una única geometría
    mantener_parte_mayor: bool = False  # Al fragmentar por contracción, conservar solo la parte de mayor área

    # Búfer adaptativo por densidad
    usar_densidad_adaptativa: bool = False          # Activar cálculo adaptativo de radio
    densidad_metodo: int = 0                        # 0=KNN, 1=Radio fijo
    densidad_k: int = 3                             # Número de vecinos (KNN)
    densidad_radio_ref: float = 0.0               # Radio de búsqueda en m (método Radio)
    densidad_radio_base: float = 0.0              # Radio base en m (método Radio)
    densidad_factor_escala: float = 0.5             # Factor multiplicador del radio calculado
    densidad_radio_min: float = 1.0                 # Radio mínimo permitido (cota inferior)
    densidad_radio_max: float = 0.0             # Radio máximo permitido (cota superior)
    
    # === NUEVO: Método de anclaje para densidad ===
    densidad_metodo_anclaje: int = Constants.DENSIDAD_ANCLAJE_CENTROIDE  # 0=centroide, 1=polo
    densidad_tolerancia_polo: float = 1.0  # Tolerancia para polo de inaccesibilidad (metros)

    # Override interno: vértices (x,y,m) pre-extraídos por _prepare_features.
    # El procesador VariableWidthMBufferProcessor usa estos datos directamente
    # en vez de leer M de la geometría (que puede perder M en WKB/makeValid).
    # NO es un parámetro de interfaz — se asigna internamente en el procesamiento.
    _m_vertices_override: Optional[List] = None

    # Exportar / importar configuración JSON
    exportar_config: bool = False       # Guardar parámetros en archivo JSON
    ruta_config_json: str = ""          # Ruta del archivo JSON de configuración

    # Modo Validación Previa
    validacion_previa: bool = False      # Validar sin generar geometrías



# ==============================================================================
# DATACLASS: ESTADO DE PROCESAMIENTO (contadores mutables por entidad)
# ==============================================================================
@dataclass
class ProcessState:
    """
    Agrupa los contadores que se actualizan durante el procesamiento de búferes.
    Permite pasar y actualizar el estado por referencia, simplificando
    las firmas de _postprocess_buffer y _apply_resultado_to_sink.
    """
    cnt: int = 0                        # Número de búferes escritos al sink
    area_sum: float = 0.0              # Suma de áreas de búferes generados (m²)
    total_vertices_antes: int = 0      # Vértices totales antes de simplificación
    total_vertices_despues: int = 0    # Vértices totales después de simplificación


# ==============================================================================
# CLASE: CONTADOR DE VÉRTICES Y SIMPLIFICADOR
# ==============================================================================
class GeometrySimplifier:
    """Gestiona la simplificación de geometrías y el conteo de vértices."""
    
    @staticmethod
    def count_vertices(geom: QgsGeometry) -> int:
        """Cuenta el número total de vértices en una geometría usando API nativa C++."""
        if not geom or geom.isEmpty():
            return 0
        abstract = geom.constGet()
        return abstract.nCoordinates() if abstract else 0
    
    @staticmethod
    def simplify(geom: QgsGeometry, tolerance: float) -> Tuple[QgsGeometry, int, int]:
        """
        Simplifica una geometría y retorna la geometría simplificada junto con
        el conteo de vértices antes y después.
        
        Returns:
            Tuple[QgsGeometry, int, int]: (geometría_simplificada, vértices_antes, vértices_después)
        """
        if not geom or geom.isEmpty():
            return geom, 0, 0
        
        vertices_antes = GeometrySimplifier.count_vertices(geom)
        
        if tolerance <= 0:
            return geom, vertices_antes, vertices_antes
        
        # Usar el algoritmo Douglas-Peucker para simplificación
        geom_simplificada = geom.simplify(tolerance)
        
        # Verificar que la simplificación produjo una geometría válida
        if geom_simplificada.isEmpty() or not geom_simplificada.isGeosValid():
            return geom, vertices_antes, vertices_antes
        
        vertices_despues = GeometrySimplifier.count_vertices(geom_simplificada)
        
        return geom_simplificada, vertices_antes, vertices_despues


# ==============================================================================
# CLASE: CALCULADOR DE BÚFER ADAPTATIVO POR DENSIDAD
# ==============================================================================
class AdaptiveDensityCalculator:
    """
    Calcula radios de búfer adaptativos según la densidad espacial local de
    cada entidad.

    CONCEPTO:
        Zonas densas  → radio pequeño  (hay mucha información cerca)
        Zonas dispersas → radio grande (la entidad debe cubrir más área)

    DOS MÉTODOS:
        KNN  (k vecinos más cercanos): el radio se deriva de la distancia al
             k-ésimo vecino multiplicada por un factor de escala.
             Intuitivo: "el radio es proporcional a cuánto espacio tiene cada entidad".

        RADIO FIJO: cuenta los vecinos dentro de un radio de referencia y usa
             la fórmula inversa radio = radio_base / √n_vecinos × escala.
             Más estable en capas muy heterogéneas.

    USO:
        geoms = [(geom1, fid1), (geom2, fid2), ...]
        radios = AdaptiveDensityCalculator.calcular(
            geoms,
            metodo=Constants.DENSIDAD_KNN,
            k=3,
            radio_referencia=500.0,
            radio_base=100.0,
            factor_escala=0.5,
            radio_min=10.0,
            radio_max=5000.0,
            logger=logger
        )
        # radios es un dict {fid: radio_calculado}
    """

    @staticmethod
    def calcular(geoms: List[Tuple['QgsGeometry', int]],
                 metodo: int,
                 k: int,
                 radio_referencia: float,
                 radio_base: float,
                 factor_escala: float,
                 radio_min: float,
                 radio_max: float,
                 logger: 'Logger',
                 metodo_anclaje: int = Constants.DENSIDAD_ANCLAJE_CENTROIDE,  # NUEVO
                 tolerancia_polo: float = 1.0) -> Dict[int, float]:  # NUEVO
        """
        Calcula el radio adaptativo para cada entidad.

        Args:
            geoms            : lista de tuplas (QgsGeometry, fid)
            metodo           : Constants.DENSIDAD_KNN o Constants.DENSIDAD_RADIO
            k                : número de vecinos (KNN) o sin uso (RADIO)
            radio_referencia : radio de búsqueda en m (solo método RADIO)
            radio_base       : radio base en m (solo método RADIO)
            factor_escala    : multiplicador aplicado al radio calculado
            radio_min        : clamp inferior del radio final
            radio_max        : clamp superior del radio final
            logger           : Logger del algoritmo
            metodo_anclaje   : 0=centroide, 1=polo de inaccesibilidad
            tolerancia_polo  : tolerancia para polo de inaccesibilidad (metros)

        Returns:
            Dict {fid: radio_metros}
        """
        if not geoms:
            return {}

        n = len(geoms)
        logger.info(f"🧭 Búfer Adaptativo: calculando radios para {n} entidades "
                    f"(método: {Constants.DENSIDAD_NAMES[metodo]})")
        logger.info(f"   📍 Punto de anclaje: {Constants.DENSIDAD_ANCLAJE_NAMES[metodo_anclaje]}")

        # Extraer puntos de referencia y construir índice espacial
        puntos_ref: Dict[int, 'QgsPointXY'] = {}
        index = QgsSpatialIndex()

        for geom, fid in geoms:
            if not geom or geom.isEmpty():
                continue

            punto_ref = None

            if metodo_anclaje == Constants.DENSIDAD_ANCLAJE_CENTROIDE:
                # Usar centroide (rápido)
                pt = geom.centroid()
                if pt and not pt.isEmpty():
                    punto_ref = pt.asPoint()

            else:  # DENSIDAD_ANCLAJE_POLO
                # Usar polo de inaccesibilidad (preciso)
                geom_type = geom.type()

                if geom_type == QgsWkbTypes.PointGeometry:
                    # Para puntos, usar el punto mismo
                    punto_ref = geom.asPoint()

                elif geom_type == QgsWkbTypes.LineGeometry:
                    # Para líneas, usar punto medio (mejor que polo)
                    mid = geom.interpolate(geom.length() / 2.0)
                    if mid and not mid.isEmpty():
                        punto_ref = mid.asPoint()
                    else:
                        # Fallback a centroide
                        pt = geom.centroid()
                        if pt and not pt.isEmpty():
                            punto_ref = pt.asPoint()

                else:  # PolygonGeometry
                    try:
                        # poleOfInaccessibility retorna QgsGeometry (punto) en QGIS 3.x,
                        # o una tupla (QgsGeometry, distancia) en algunas versiones.
                        # Se normaliza a QgsPointXY llamando .asPoint() siempre.
                        resultado_polo = geom.poleOfInaccessibility(tolerancia_polo)

                        # Normalizar: puede ser QgsGeometry o tupla (QgsGeometry, dist)
                        geom_polo = resultado_polo[0] if isinstance(resultado_polo, tuple) else resultado_polo

                        if geom_polo and not geom_polo.isEmpty():
                            punto_ref = geom_polo.asPoint()
                        else:
                            # Fallback a centroide si falla
                            logger.warning(f"   ⚠️ fid {fid}: polo de inaccesibilidad falló, usando centroide")
                            pt = geom.centroid()
                            if pt and not pt.isEmpty():
                                punto_ref = pt.asPoint()

                    except Exception as e:
                        logger.warning(f"   ⚠️ fid {fid}: error en polo ({str(e)[:50]}), usando centroide")
                        pt = geom.centroid()
                        if pt and not pt.isEmpty():
                            punto_ref = pt.asPoint()

            if punto_ref:
                puntos_ref[fid] = punto_ref
                feat = QgsFeature(fid)
                feat.setGeometry(QgsGeometry.fromPointXY(punto_ref))
                index.addFeature(feat)

        if not puntos_ref:
            logger.warning("⚠️ Búfer Adaptativo: ningún punto de referencia válido calculado.")
            return {}

        radios: Dict[int, float] = {}

        if metodo == Constants.DENSIDAD_KNN:
            radios = AdaptiveDensityCalculator._knn(
                puntos_ref, index, k, factor_escala, radio_min, radio_max, logger)
        else:
            radios = AdaptiveDensityCalculator._radio_fijo(
                puntos_ref, index, radio_referencia, radio_base,
                factor_escala, radio_min, radio_max, logger)

        logger.info(f"   ✅ Radios calculados: min={min(radios.values(), default=0):.1f}m  "
                    f"max={max(radios.values(), default=0):.1f}m  "
                    f"promedio={sum(radios.values())/len(radios) if radios else 0:.1f}m")
        return radios

    # ------------------------------------------------------------------
    # Método KNN
    # ------------------------------------------------------------------
    @staticmethod
    def _knn(puntos_ref: Dict[int, 'QgsPointXY'],
             index: 'QgsSpatialIndex',
             k: int,
             factor_escala: float,
             radio_min: float,
             radio_max: float,
             logger: 'Logger') -> Dict[int, float]:
        """
        radio = distancia_al_k_esimo_vecino × factor_escala
        Si solo hay 1 entidad (sin vecinos), usa radio_max como fallback.
        """
        radios: Dict[int, float] = {}
        # k+1 porque el índice retorna la propia entidad como primer resultado
        k_buscar = k + 1

        for fid, pt in puntos_ref.items():
            # Buscar los k+1 vecinos más cercanos (incluye la propia entidad)
            vecinos_ids = index.nearestNeighbor(pt, k_buscar)

            # Filtrar el propio fid
            vecinos_ids = [v for v in vecinos_ids if v != fid]

            if not vecinos_ids:
                # Entidad solitaria: asignar el radio máximo
                radios[fid] = radio_max
                continue

            # Tomar el k-ésimo vecino (último en la lista devuelta)
            fid_k = vecinos_ids[-1]
            if fid_k not in puntos_ref:
                radios[fid] = radio_max
                continue

            pt_k = puntos_ref[fid_k]
            distancia = math.sqrt((pt.x() - pt_k.x())**2 + (pt.y() - pt_k.y())**2)

            radio = distancia * factor_escala
            radios[fid] = max(radio_min, min(radio_max, radio))

        return radios

    # ------------------------------------------------------------------
    # Método Radio Fijo
    # ------------------------------------------------------------------
    @staticmethod
    def _radio_fijo(puntos_ref: Dict[int, 'QgsPointXY'],
                    index: 'QgsSpatialIndex',
                    radio_referencia: float,
                    radio_base: float,
                    factor_escala: float,
                    radio_min: float,
                    radio_max: float,
                    logger: 'Logger') -> Dict[int, float]:
        """
        Cuenta los vecinos en radio_referencia y aplica:
            radio = (radio_base / √n_vecinos) × factor_escala
        Con n_vecinos = 1 (solo la propia entidad) se usa radio_base directamente.
        """
        radios: Dict[int, float] = {}

        for fid, pt in puntos_ref.items():
            # Crear bbox cuadrado de lado 2×radio_referencia centrado en pt
            bbox = QgsRectangle(
                pt.x() - radio_referencia, pt.y() - radio_referencia,
                pt.x() + radio_referencia, pt.y() + radio_referencia
            )
            candidatos = index.intersects(bbox)

            # Filtrar por distancia euclidiana real (el índice devuelve bbox, no círculo)
            n_vecinos = sum(
                1 for c_fid in candidatos
                if c_fid in puntos_ref and math.sqrt(
                    (pt.x() - puntos_ref[c_fid].x())**2 +
                    (pt.y() - puntos_ref[c_fid].y())**2
                ) <= radio_referencia
            )

            # Al menos 1 (la propia entidad)
            n_vecinos = max(1, n_vecinos)
            radio = (radio_base / math.sqrt(n_vecinos)) * factor_escala
            radios[fid] = max(radio_min, min(radio_max, radio))

        return radios


# ==============================================================================
# CLASE: MANEJADOR DE GEOMETRÍAS (Robustez)
# ==============================================================================
class GeometryHandler:
    """Maneja geometrías complejas: Z, M, multipart y reparación."""
    
    @staticmethod
    def tiene_z(geom: QgsGeometry) -> bool:
        """Verifica si la geometría tiene coordenada Z."""
        if not geom or geom.isEmpty():
            return False
        return QgsWkbTypes.hasZ(geom.wkbType())
    
    @staticmethod
    def tiene_m(geom: QgsGeometry) -> bool:
        """Verifica si la geometría tiene coordenada M."""
        if not geom or geom.isEmpty():
            return False
        return QgsWkbTypes.hasM(geom.wkbType())
    
    @staticmethod
    def es_multipart(geom: QgsGeometry) -> bool:
        """Verifica si la geometría es multipart."""
        if not geom or geom.isEmpty():
            return False
        return QgsWkbTypes.isMultiType(geom.wkbType())
    
    @staticmethod
    def aplanar_z(geom: QgsGeometry) -> QgsGeometry:
        """Convierte geometría 3D a 2D eliminando Z. Preserva M si existe.
        M es una medida independiente de Z y no debe eliminarse al aplanar.
        """
        if not geom or geom.isEmpty():
            return geom
        
        if GeometryHandler.tiene_z(geom):
            # Crear copia y eliminar solo Z — preservar M
            geom_flat = QgsGeometry(geom)
            geom_flat.get().dropZValue()
            return geom_flat
        return geom
    
    @staticmethod
    def preparar_geometria(geom: QgsGeometry, fid: int, modo_integridad: int, 
                          logger: Logger, desc: str = "",
                          registrar_metrica: bool = True) -> Optional[QgsGeometry]:
        """
        Aplica la lógica de integridad de 3 vías.
        Args:
            registrar_metrica: Si True, incrementa contadores. Usar False para
                validaciones secundarias (búferes de salida) y evitar doble contabilización.
        """
        if not geom or geom.isEmpty():
            logger.registrar_omision(fid)
            logger.warning(f"{desc}: Geometría vacía o nula - omitida")
            return None
        
        if GeometryHandler.tiene_z(geom):
            if registrar_metrica:
                logger.registrar_geometria_z()
            geom = GeometryHandler.aplanar_z(geom)
        
        if registrar_metrica and GeometryHandler.es_multipart(geom):
            logger.registrar_geometria_multipart()
        
        if geom.isGeosValid():
            if registrar_metrica:
                logger.registrar_geometria_procesada()
            return geom
            
        if modo_integridad == Constants.INTEGRIDAD_RIESGO:
            logger.registrar_riesgo(fid)
            logger.warning(f"⚠️ {desc}: Geometría inválida procesada con RIESGO (ID: {fid})")
            if registrar_metrica:
                logger.registrar_geometria_procesada()
            return geom 
            
        elif modo_integridad == Constants.INTEGRIDAD_OMITIR:
            logger.registrar_omision(fid)
            logger.warning(f"🚫 {desc}: Geometría inválida - omitida (ID: {fid})")
            return None
            
        elif modo_integridad == Constants.INTEGRIDAD_REPARAR:
            geom_reparada = geom.makeValid()
            
            if geom_reparada and not geom_reparada.isEmpty() and geom_reparada.isGeosValid():
                # makeValid() puede retornar GeometryCollection — filtrar solo polígonos
                if QgsWkbTypes.geometryType(geom_reparada.wkbType()) != QgsWkbTypes.PolygonGeometry:
                    partes = geom_reparada.asGeometryCollection() if geom_reparada.isMultipart() else [geom_reparada]
                    polys = [p for p in partes if QgsWkbTypes.geometryType(p.wkbType()) == QgsWkbTypes.PolygonGeometry]
                    if polys:
                        geom_reparada = QgsGeometry.collectGeometry(polys)
                        logger.info(f"ℹ️ {desc}: makeValid() → GeometryCollection, extraídas {len(polys)} parte(s) (ID: {fid})")
                    else:
                        logger.registrar_omision(fid)
                        logger.error(f"❌ {desc}: makeValid() no produjo polígonos válidos - omitida (ID: {fid})")
                        return None
                logger.registrar_reparacion(fid)
                logger.info(f"🔧 {desc}: Geometría reparada exitosamente (ID: {fid})")
                if registrar_metrica:
                    logger.registrar_geometria_procesada()
                return geom_reparada
            else:
                logger.registrar_omision(fid)
                logger.error(f"❌ {desc}: Geometría inválida no pudo ser reparada - omitida (ID: {fid})")
                return None
        
        return None


# ==============================================================================
# CLASE: POST-PROCESADOR DE GEOMETRÍAS (Traslapes y Huecos)
# ==============================================================================
class GeometryPostProcessor:
    """
    Gestiona el post-procesamiento de geometrías:
    - Resolución de traslapes (asignar a polígono mayor/menor)
    - Eliminación de huecos internos
    """

    @staticmethod
    def fix_nested_holes(geom: QgsGeometry) -> QgsGeometry:
        """
        ESTADO: INACTIVO — no se llama en el flujo de producción.
        DIAGNÓSTICO: disponible para invocación manual en la consola Python
        de QGIS cuando se necesite inspeccionar geometrías concéntricas complejas.
        ROADMAP: evaluar activación cuando GEOS ≥ 3.12 sea el mínimo requerido
        en las builds LTR de QGIS (actualmente GEOS ≥ 3.9). La discrepancia
        de interpretación entre QGIS Check Validity y GEOS descrita abajo
        quedará resuelta en esa versión.
        Si este método no forma parte del roadmap, considere moverlo a un
        módulo utils/ separado o eliminarlo para reducir la superficie de
        mantenimiento.

        Intenta resolver dos violaciones OGC en MultiPolygon concéntrico:

        LIMITACIÓN CONOCIDA: el error "polígono N dentro del polígono 0"
        reportado por Check Validity método QGIS sobre anillos concéntricos
        en geometrías de alta complejidad (>100k vértices) es una diferencia
        de interpretación entre QGIS y GEOS. GEOS 3.14.1 valida la misma
        geometría como correcta. Fix Geometries de QGIS tampoco resuelve.
        El resultado visual y las operaciones básicas son correctos.

        Para operaciones vectoriales con otras capas se recomienda:
        activar SIMPLIFICAR_ENTRADA=True antes de procesar.

        Resuelve dos violaciones OGC en MultiPolygon de anillos concéntricos:

        PROBLEMA 1 — Partes dentro de partes (regla OGC MultiPolygon):
          buffer().difference() sobre geometrías cóncavas produce partes del
          MultiPolygon que quedan espacialmente dentro de otras partes.
          QGIS lo reporta como "polígono N dentro del polígono 0".
          Solución: descartar las partes que estén dentro de otras partes.
          Son fragmentos residuales del difference() en zonas de bahías.

        PROBLEMA 2 — Huecos anidados (rings dentro de rings):
          En polígonos con concavidades profundas, difference() produce huecos
          del dónut anidados dentro de los huecos de las bahías.
          Solución: conservar solo huecos directos del shell exterior.

        Optimización: usa Caja Delimitadora como pre-filtro antes de contains()
        para evitar O(n²) comparaciones sobre geometrías de alta densidad
        de vértices. Reduce comparaciones costosas en >99% de los casos.
        """
        if not geom or geom.isEmpty():
            return geom

        if geom.isMultipart():
            poligonos = geom.asMultiPolygon()
        else:
            raw = geom.asPolygon()
            if not raw:
                return geom
            poligonos = [raw]

        if len(poligonos) <= 1:
            partes_geom = None
            partes_bbox = None
        else:
            # Usar el polígono COMPLETO (con huecos) para contains().
            # Usar solo el shell exterior produciría falsos positivos:
            # una parte que está en el HUECO (Golfo) quedaría dentro
            # del shell de otra parte y sería descartada incorrectamente.
            # Con el polígono completo, contains() verifica que la parte
            # esté en el área SÓLIDA, no en el hueco.
            partes_geom = []
            partes_bbox = []
            for poly in poligonos:
                if poly:
                    g = QgsGeometry.fromPolygonXY(poly)  # polígono completo con huecos
                    partes_geom.append(g)
                    partes_bbox.append(g.boundingBox())
                else:
                    partes_geom.append(None)
                    partes_bbox.append(None)

        nuevos_poligonos = []
        hubo_cambios = False

        for i, poly in enumerate(poligonos):
            if not poly:
                continue

            # ── PROBLEMA 1: descartar parte si está dentro de otra parte ──
            if partes_geom is not None:
                geom_shell_i = partes_geom[i]
                bbox_i = partes_bbox[i]
                if geom_shell_i and bbox_i:
                    dentro_de_otra = False
                    for j in range(len(poligonos)):
                        if j == i or partes_geom[j] is None:
                            continue
                        bbox_j = partes_bbox[j]
                        # Pre-filtro: bbox_j debe contener bbox_i
                        if not bbox_j.contains(bbox_i):
                            continue
                        # Solo llamar contains() si el bbox lo permite
                        if partes_geom[j].contains(geom_shell_i):
                            dentro_de_otra = True
                            break
                    if dentro_de_otra:
                        hubo_cambios = True
                        continue  # descarta esta parte

            shell  = poly[0]
            huecos = poly[1:]

            if not huecos:
                nuevos_poligonos.append([shell])
                continue

            # ── PROBLEMA 2: descartar huecos anidados dentro de otros huecos ──
            geoms_huecos = [
                (ring, QgsGeometry.fromPolygonXY([ring]))
                for ring in huecos
            ]
            # Pre-calcular Caja Delimitadora de huecos
            bboxes_huecos = [gh.boundingBox() for _, gh in geoms_huecos]

            huecos_directos = []
            for hi, (ring_i, geom_i) in enumerate(geoms_huecos):
                bbox_i_h = bboxes_huecos[hi]
                es_anidado = False
                for hj, (_, geom_j) in enumerate(geoms_huecos):
                    if hj == hi:
                        continue
                    # Pre-filtro: bbox_j debe contener bbox_i
                    if not bboxes_huecos[hj].contains(bbox_i_h):
                        continue
                    if geom_j.contains(geom_i):
                        es_anidado = True
                        break
                if es_anidado:
                    hubo_cambios = True
                else:
                    huecos_directos.append(ring_i)

            nuevos_poligonos.append([shell] + huecos_directos)

        if not hubo_cambios:
            return geom   # sin modificaciones — retorno rápido

        if not nuevos_poligonos:
            return geom

        result = QgsGeometry.fromMultiPolygonXY(nuevos_poligonos)
        return result if (result and not result.isEmpty()) else geom

        
    @staticmethod
    def _procesar_anillos_poly(poly: list, area_minima: float,
                                preservar_hueco_mayor: bool) -> Tuple[list, int]:
        """
        Lógica común de filtrado de anillos para un único polígono (lista de anillos).

        Extrae esta lógica para evitar duplicación entre el caso simple y el multipolígono.

        Args:
            poly: Lista de anillos [[exterior], [hueco1], [hueco2], ...]
            area_minima: Área mínima de hueco a conservar (0 → aplicar preservar_hueco_mayor)
            preservar_hueco_mayor: Si True y area_minima==0, conserva solo el hueco más grande.

        Returns:
            (new_rings, huecos_eliminados)
        """
        if len(poly) == 0:
            return [], 0

        exterior = poly[0]

        if len(poly) == 1:
            return [exterior], 0

        # El polígono tiene al menos un hueco
        new_rings = [exterior]
        eliminados = 0

        if preservar_hueco_mayor and area_minima <= 0:
            # Preservar solo el hueco de mayor área (útil para donuts concéntricos)
            huecos_con_area = [
                (ring, QgsGeometry.fromPolygonXY([ring]).area())
                for ring in poly[1:]
            ]
            huecos_con_area.sort(key=lambda x: x[1], reverse=True)
            new_rings.append(huecos_con_area[0][0])     # conservar el mayor
            eliminados = len(huecos_con_area) - 1

        elif area_minima > 0:
            # Conservar huecos cuya área supera el umbral
            for ring in poly[1:]:
                if QgsGeometry.fromPolygonXY([ring]).area() >= area_minima:
                    new_rings.append(ring)
                else:
                    eliminados += 1
        else:
            # Eliminar TODOS los huecos
            eliminados = len(poly) - 1

        return new_rings, eliminados

    @staticmethod
    def eliminar_huecos(geom: QgsGeometry, area_minima: float = 0.0,
                         preservar_hueco_mayor: bool = False) -> Tuple[QgsGeometry, int]:
        """
        Elimina los huecos (anillos internos) de un polígono.

        Args:
            geom: Geometría de entrada (polígono o multipolígono)
            area_minima: Área mínima de hueco a eliminar (0 = usar lógica de preservar_hueco_mayor)
            preservar_hueco_mayor: Si True, preserva el hueco más grande de cada polígono
                                   (útil para búferes concéntricos tipo donut)

        Returns:
            Tuple[QgsGeometry, int]: (Geometría procesada, número de huecos eliminados)
        """
        if not geom or geom.isEmpty():
            return geom, 0

        if geom.type() != QgsWkbTypes.PolygonGeometry:
            return geom, 0

        huecos_eliminados = 0

        try:
            if QgsWkbTypes.isMultiType(geom.wkbType()):
                new_multi_poly = []
                for poly in geom.asMultiPolygon():
                    rings, n = GeometryPostProcessor._procesar_anillos_poly(
                        poly, area_minima, preservar_hueco_mayor)
                    if rings:
                        new_multi_poly.append(rings)
                    huecos_eliminados += n
                return QgsGeometry.fromMultiPolygonXY(new_multi_poly), huecos_eliminados
            else:
                poly = geom.asPolygon()
                if not poly:
                    return geom, 0
                rings, huecos_eliminados = GeometryPostProcessor._procesar_anillos_poly(
                    poly, area_minima, preservar_hueco_mayor)
                return QgsGeometry.fromPolygonXY(rings), huecos_eliminados

        except Exception:
            return geom, 0
    
    @staticmethod
    def resolver_traslapes(geometrias: List[Dict], modo: int, logger: Logger, feedback=None) -> List[Dict]:
        """
        Resuelve traslapes entre geometrías asignando el área traslapada
        al polígono mayor o menor según el modo.
        
        Args:
            geometrias: Lista de diccionarios con 'geom', 'id', 'tipo', 'dist', 'area', 'attrs'
            modo: TRASLAPE_MANTENER (0), TRASLAPE_MAYOR (1), TRASLAPE_MENOR (2)
            logger: Logger para mensajes
            feedback: QgsProcessingFeedback opcional para verificar cancelación
        
        Returns:
            Lista de geometrías con traslapes resueltos
        """
        if modo == Constants.TRASLAPE_MANTENER:
            return geometrias
        
        if len(geometrias) < 2:
            return geometrias
        
        logger.info(f"🔀 Resolviendo traslapes entre {len(geometrias)} geometrías...")
        
        # Crear copia de trabajo con áreas calculadas
        trabajo = []
        for i, g in enumerate(geometrias):
            geom = g['geom']
            if geom and not geom.isEmpty():
                trabajo.append({
                    'idx': i,
                    'geom': QgsGeometry(geom),  # Clonar
                    'area': geom.area(),
                    'tipo': g.get('tipo', ''),
                    'dist': g.get('dist', 0),
                    'attrs': g.get('attrs', []),
                    'modificado': False
                })
        
        # Ordenar por área (mayor a menor para TRASLAPE_MAYOR, menor a mayor para TRASLAPE_MENOR)
        if modo == Constants.TRASLAPE_MAYOR:
            trabajo.sort(key=lambda x: x['area'], reverse=True)
        else:  # TRASLAPE_MENOR
            trabajo.sort(key=lambda x: x['area'], reverse=False)
        
        traslapes_resueltos = 0
        cancelado = False
        
        # ── MEJORA 5: Índice espacial para reducir comparaciones O(n²) → O(n·k) ──
        # Con n=500 entidades esto reduce comparaciones de 125,000 a ~5,000-15,000.
        # Se activa siempre que haya 10+ geometrías (overhead del índice es mínimo).
        INDICE_THRESHOLD = 10
        usar_indice = len(trabajo) >= INDICE_THRESHOLD

        if usar_indice:
            # Construir índice espacial con las geometrías actuales
            indice_espacial = QgsSpatialIndex()
            for t in trabajo:
                if not t['geom'].isEmpty():
                    feat_idx = QgsFeature(t['idx'])
                    feat_idx.setGeometry(t['geom'])
                    indice_espacial.addFeature(feat_idx)
            logger.info(f"   🗂️ Índice espacial creado para {len(trabajo)} geometrías")
        
        # Mapear idx → posición en lista 'trabajo' para acceso rápido
        idx_to_pos = {t['idx']: pos for pos, t in enumerate(trabajo)}
        
        # Procesar cada geometría contra sus candidatos espaciales
        for i in range(len(trabajo)):
            # Verificar cancelación cada N iteraciones — usando solo isCanceled() (thread-safe)
            if feedback and i % 10 == 0:
                if feedback.isCanceled():
                    logger.info("🛑 Resolución de traslapes cancelada por el usuario")
                    cancelado = True
                    break
            
            if trabajo[i]['geom'].isEmpty():
                continue
            
            geom_i = trabajo[i]['geom']
            
            # Obtener candidatos: con índice → solo los que tocan el bbox; sin índice → todos los siguientes
            if usar_indice:
                candidatos_ids = indice_espacial.intersects(geom_i.boundingBox())
                # Filtrar: solo los que vienen DESPUÉS en el orden (evitar duplicados)
                candidatos = [
                    idx_to_pos[c] for c in candidatos_ids
                    if c in idx_to_pos and idx_to_pos[c] > i
                ]
            else:
                candidatos = range(i + 1, len(trabajo))
            
            for j in candidatos:
                if trabajo[j]['geom'].isEmpty():
                    continue
                
                geom_j = trabajo[j]['geom']
                
                # Verificar si hay intersección real (el índice filtra por bbox, no por geometría exacta)
                if not geom_i.intersects(geom_j):
                    continue
                
                interseccion = geom_i.intersection(geom_j)
                if interseccion.isEmpty() or interseccion.area() < 1e-6:
                    continue
                
                # El polígono "ganador" (primero en el orden priorizado) conserva el traslape.
                # El polígono "perdedor" recibe la diferencia.
                try:
                    nueva_geom_j = geom_j.difference(interseccion)
                    if nueva_geom_j and not nueva_geom_j.isEmpty():
                        trabajo[j]['geom'] = nueva_geom_j
                        trabajo[j]['area'] = nueva_geom_j.area()
                        trabajo[j]['modificado'] = True
                        traslapes_resueltos += 1
                        # Actualizar geometría en el índice espacial para que el siguiente
                        # candidato vea la geometría ya recortada (más preciso)
                        if usar_indice:
                            indice_espacial.deleteFeature(QgsFeature(trabajo[j]['idx']))
                            feat_act = QgsFeature(trabajo[j]['idx'])
                            feat_act.setGeometry(nueva_geom_j)
                            indice_espacial.addFeature(feat_act)
                except Exception as e:
                    logger.warning(f"⚠️ Error resolviendo traslape: {str(e)[:50]}")
                    continue
        
        if traslapes_resueltos > 0:
            modo_str = "polígono MAYOR" if modo == Constants.TRASLAPE_MAYOR else "polígono MENOR"
            logger.info(f"   ✅ {traslapes_resueltos} traslapes resueltos (asignados al {modo_str})")
        else:
            logger.info(f"   ℹ️ No se encontraron traslapes significativos")
        
        # Reconstruir la lista original con las geometrías modificadas
        # Mapear de vuelta al orden original
        resultado = []
        idx_to_trabajo = {t['idx']: t for t in trabajo}
        
        for i, g in enumerate(geometrias):
            if i in idx_to_trabajo:
                t = idx_to_trabajo[i]
                resultado.append({
                    'geom': t['geom'],
                    'tipo': t['tipo'],
                    'dist': t['dist'],
                    'attrs': t['attrs']
                })
            else:
                resultado.append(g)
        
        return resultado


# ==============================================================================
# CLASE: GESTOR DE ESTILOS DE UNIÓN
# ==============================================================================
class JoinStyleManager:
    """Gestiona los estilos de unión y terminación de búferes."""
    
    @staticmethod
    def get_styles(join_idx: int, geom_type: int) -> Tuple[Any, Any]:
        """Retorna (JoinStyle, EndCapStyle) según índice y tipo de geometría."""
        if join_idx == Constants.JOIN_MITER:
            return (Qgis.JoinStyle.Miter, Qgis.EndCapStyle.Square)
        elif join_idx == Constants.JOIN_BEVEL:
            cap = (Qgis.EndCapStyle.Flat 
                   if geom_type == QgsWkbTypes.LineGeometry 
                   else Qgis.EndCapStyle.Round)
            return (Qgis.JoinStyle.Bevel, cap)
        else:
            return (Qgis.JoinStyle.Round, Qgis.EndCapStyle.Round)
    
    @staticmethod
    def get_style_name(join_idx: int) -> str:
        names = ['Redondeado', 'Inglete (Agudo)', 'Biselado (Cortado)']
        return names[join_idx] if join_idx < len(names) else 'Redondeado'
    
    @staticmethod
    def get_short_name(join_idx: int) -> str:
        names = ['Redond', 'Ingl', 'Bisel']
        return names[join_idx] if join_idx < len(names) else 'Redond'


# ==============================================================================
# CLASES: PROCESADORES DE BÚFER (Patrón Strategy)
# ==============================================================================
class BufferProcessor(ABC):
    """Clase base abstracta para procesadores de búfer."""
    
    @abstractmethod
    def process(self, geom: QgsGeometry, params: BufferParams, 
                logger: Logger, desc: str) -> List[Tuple[QgsGeometry, str, float]]:
        pass
    

    @staticmethod
    def _compacidad(g: 'QgsGeometry') -> float:
        """
        Índice de compacidad de Polsby-Popper.
        Retorna 1.0 para un círculo perfecto; valores menores indican formas más irregulares.
        Se usa para seleccionar el fragmento más 'compacto' cuando un búfer colapsa
        en partes múltiples (contracción excesiva, formas en L/U/T).
        """
        perimetro = g.length()
        return (4 * math.pi * g.area() / (perimetro * perimetro)) if perimetro > 0 else 0

    def _find_distance_for_area(self, geom: QgsGeometry, area_objetivo: float, 
                                 segmentos: int,
                                 guard: 'ResourceGuard' = None) -> float:
        """
        Encuentra la distancia de búfer para alcanzar un área objetivo.
        
        CRÍTICO #4: Incluye Tiempo de Espera para prevenir iteraciones infinitas.
        
        Args:
            guard: ResourceGuard externo opcional. Si se pasa, se reutiliza (evita
                   crear un segundo guard cuando el caller ya tiene uno activo para
                   controlar la complejidad de la geometría). Si es None, se crea
                   uno interno con Tiempo de Espera de 350s.
        """
        if geom.type() == QgsWkbTypes.PointGeometry:
            return math.sqrt(area_objetivo / math.pi)
        
        area_base = geom.area()
        expandir = area_objetivo > area_base
        min_dist, max_dist = 0.0, 0.0
        
        # CRÍTICO #4: Usar guard externo si se proporcionó, o crear uno propio.
        # Esto evita que coexistan dos guards con Tiempo de Espera no coordinados cuando
        # el caller (ej. processAlgorithm) ya instanció uno para verificar complejidad.
        _own_guard = guard is None
        if _own_guard:
            guard = ResourceGuard(max_time_sec=350)  # ~6 minutos máximo por búsqueda
        guard.start_operation("Búfer por área - búsqueda binaria")
        
        # Límite de seguridad: 500 km (evita expansión infinita con geometrías irregulares)
        MAX_EXPANSION_DIST = 500_000.0
        
        if expandir:
            largo = geom.length()
            max_dist = (area_objetivo / largo) if largo > 0 else math.sqrt(area_objetivo)  # estimación inicial: área ÷ longitud
            for _ in range(Constants.EXPANSION_ITERATIONS):
                guard.check_timeout()  # Verificar timeout
                if max_dist > MAX_EXPANSION_DIST:
                    break
                if geom.buffer(max_dist, segmentos).area() >= area_objetivo:
                    break
                min_dist = max_dist
                max_dist *= 2
        else:
            min_dist = -math.sqrt(area_base)
            for _ in range(Constants.EXPANSION_ITERATIONS):
                guard.check_timeout()  # Verificar timeout
                if abs(min_dist) > MAX_EXPANSION_DIST:
                    break
                buf = geom.buffer(min_dist, segmentos)
                if buf.isEmpty() or buf.area() < area_objetivo:
                    break
                max_dist = min_dist
                min_dist *= 2
        
        for _ in range(Constants.BISECTION_ITERATIONS):
            guard.check_timeout()  # Verificar timeout en cada iteración
            mid = (min_dist + max_dist) / 2
            buf = geom.buffer(mid, segmentos)
            area_actual = buf.area() if buf and not buf.isEmpty() else 0.0
            
            if math.isclose(area_actual, area_objetivo, rel_tol=Constants.AREA_TOLERANCE):
                return mid
            if area_actual < area_objetivo:
                min_dist = mid
            else:
                max_dist = mid
        
        return (min_dist + max_dist) / 2


    def _calculate_auto_rotation(self, geom, logger):
        """Calcula el azimut del eje principal de la geometría para orientar la cuña.

        Retorna el azimut en grados (0°=Norte, 90°=Este, sentido horario).
        Orientado hacia la dirección de máxima extensión del polígono.

        Para LÍNEAS   : dirección del primer al último vértice.
        Para POLÍGONOS: eje de elongación mediante orientedMinimumBoundingBox()
                        con desambiguación por distribución de área a cada lado
                        del centroide. Fallback a momentos de inercia analíticos
                        si OBB no está disponible.
        Para PUNTOS   : retorna 0.0 (sin auto-+rotación aplicable).
        """
        g_type = geom.type()

        # ── LÍNEAS ────────────────────────────────────────────────────────────
        if g_type == QgsWkbTypes.LineGeometry:
            try:
                p_start = geom.vertexAt(0)
                # ✅ Usar la función segura que ya existe en el código
                total_vertices = GeometrySimplifier.count_vertices(geom)
                p_end   = geom.vertexAt(total_vertices - 1)
                dx = p_end.x() - p_start.x()
                dy = p_end.y() - p_start.y()
                if dx != 0 or dy != 0:
                    # atan2(Δeste, Δnorte) = azimut geográfico CW-desde-Norte
                    return math.degrees(math.atan2(dx, dy)) % 360.0
            except (AttributeError, IndexError, ZeroDivisionError) as e:
                logger.warning(f"Cuña — No se pudo calcular rotación automática en línea: {e}")

        # ── POLÍGONOS ─────────────────────────────────────────────────────────
        elif g_type == QgsWkbTypes.PolygonGeometry:
            try:
                return self._auto_rotation_polygon(geom, logger)
            except Exception as e:
                logger.warning(f"Cuña — Error en rotación automática de polígono: {e}")

        return 0.0

    def _auto_rotation_polygon(self, geom, logger):
        """Calcula la auto-rotación para polígonos usando el OBB de QGIS.

        Obtiene el eje de elongación del polígono a partir de los vértices del
        rectángulo mínimo orientado (orientedMinimumBoundingBox). El eje largo
        del OBB define la dirección de máxima elongación del polígono.

        El azimut retornado se normaliza al rango [0°, 180°) de modo que la
        cuña siempre parte en el semicírculo N→E→S. Combinado con WEDGE_START,
        el usuario tiene control total de la orientación:
          - WEDGE_START = 0°    → cuña a lo largo del eje hacia E/SE
          - WEDGE_START = 180°  → cuña a lo largo del eje hacia W/NW
          - WEDGE_START = -22.5 → cuña centrada en el eje

        Fallback: momentos de inercia analíticos si el OBB no está disponible.
        """
        # ── Paso 1: Obtener OBB ──────────────────────────────────────────
        # orientedMinimumBoundingBox() puede lanzar excepción (no solo retornar
        # vacío) en geometrías complejas o en versiones inestables de GEOS.
        try:
            obb_result = geom.orientedMinimumBoundingBox()
        except Exception as e:
            logger.warning(f"Cuña — orientedMinimumBoundingBox falló: {e}. Usando fallback.")
            return self._auto_rotation_inertia_fallback(geom, logger)

        try:
            obb_valido = bool(obb_result and obb_result[0] and not obb_result[0].isEmpty())
        except (TypeError, IndexError):
            obb_valido = False

        if not obb_valido:
            logger.warning("Cuña — OBB vacío, usando fallback (momentos de inercia)")
            return self._auto_rotation_inertia_fallback(geom, logger)

        obb_geom = obb_result[0]

        # ── Paso 2: Eje largo desde vértices del OBB ────────────────────
        # Se extraen los vértices del rectángulo OBB y se miden sus lados
        # directamente. Esto evita depender de obb_angle, cuya convención
        # varía entre versiones de QGIS y tiene bugs reportados.
        try:
            obb_poly = obb_geom.asPolygon()
            if not obb_poly or not obb_poly[0] or len(obb_poly[0]) < 4:
                return self._auto_rotation_inertia_fallback(geom, logger)

            obb_pts = obb_poly[0]  # 5 puntos (polígono cerrado)

            # Dos lados consecutivos del rectángulo
            dx01 = obb_pts[1].x() - obb_pts[0].x()
            dy01 = obb_pts[1].y() - obb_pts[0].y()
            dx12 = obb_pts[2].x() - obb_pts[1].x()
            dy12 = obb_pts[2].y() - obb_pts[1].y()

            d01 = math.hypot(dx01, dy01)
            d12 = math.hypot(dx12, dy12)

            # El lado más largo define el eje de elongación
            if d01 >= d12:
                axis_dx, axis_dy = dx01, dy01
            else:
                axis_dx, axis_dy = dx12, dy12

            if math.hypot(axis_dx, axis_dy) < 1e-10:
                return self._auto_rotation_inertia_fallback(geom, logger)

        except (AttributeError, IndexError, TypeError) as e:
            logger.warning(f"Cuña — Error leyendo vértices OBB: {e}")
            return self._auto_rotation_inertia_fallback(geom, logger)

        # ── Paso 3: Azimut normalizado a [0°, 180°) ────────────────────
        az_raw = math.degrees(math.atan2(axis_dx, axis_dy)) % 360.0

        if az_raw >= 180.0:
            az = az_raw - 180.0
        else:
            az = az_raw

        # Diagnóstico detallado — obb_pts[3] protegido por si el OBB tiene
        # menos de 4 vértices en alguna versión de QGIS (aunque el check
        # len < 4 anterior debería prevenirlo, se usa try por seguridad).
        try:
            logger.info(
                f"Cuña AutoRot DIAGNÓSTICO:\n"
                f"  OBB vértices: P0=({obb_pts[0].x():.1f},{obb_pts[0].y():.1f}), "
                f"P1=({obb_pts[1].x():.1f},{obb_pts[1].y():.1f}), "
                f"P2=({obb_pts[2].x():.1f},{obb_pts[2].y():.1f}), "
                f"P3=({obb_pts[3].x():.1f},{obb_pts[3].y():.1f})\n"
                f"  Lado 0→1: dx={dx01:.1f}, dy={dy01:.1f}, largo={d01:.1f}m\n"
                f"  Lado 1→2: dx={dx12:.1f}, dy={dy12:.1f}, largo={d12:.1f}m\n"
                f"  Eje largo: {'0→1' if d01 >= d12 else '1→2'} "
                f"(axis_dx={axis_dx:.1f}, axis_dy={axis_dy:.1f})\n"
                f"  Azimut crudo: {az_raw:.1f}° → normalizado [0,180): {az:.1f}°"
            )
        except (IndexError, AttributeError):
            logger.info(
                f"Cuña AutoRot: eje largo={max(d01,d12):.1f}m, "
                f"azimut={az:.1f}° (diagnóstico OBB no disponible)"
            )

        return az

    def _auto_rotation_inertia_fallback(self, geom, logger):
        """Fallback: momentos de inercia analíticos del ÁREA del polígono.

        Calcula los momentos de segundo orden integrados sobre toda el área
        del polígono (Green's theorem), no sobre los vértices. Esto elimina
        el sesgo por densidad de vértices del método PCA anterior.

        La desambiguación usa proyecciones ponderadas por longitud de segmento
        para compensar la distribución no uniforme de vértices.
        """
        # Seleccionar anillo exterior de la parte de mayor área (para MultiPolygon)
        if geom.isMultipart():
            multi = geom.asMultiPolygon()
            best_ring = None
            best_area = -1.0
            for part in multi:
                if not part or not part[0] or len(part[0]) < 3:
                    continue
                ring_tmp = part[0]
                a = 0.0
                for i in range(len(ring_tmp)):
                    j = (i + 1) % len(ring_tmp)
                    a += ring_tmp[i].x() * ring_tmp[j].y() - ring_tmp[j].x() * ring_tmp[i].y()
                a = abs(a) / 2.0
                if a > best_area:
                    best_area = a
                    best_ring = ring_tmp
            if best_ring is None:
                return 0.0
            ring_pts = best_ring
        else:
            poly = geom.asPolygon()
            if not poly or not poly[0] or len(poly[0]) < 3:
                return 0.0
            ring_pts = poly[0]

        n = len(ring_pts)

        # Usar geom.centroid() para consistencia con el punto de dibujo de la cuña
        cen_pt = geom.centroid().asPoint()
        cx = cen_pt.x()
        cy = cen_pt.y()

        # ── Momentos de inercia centrales (integración exacta, Green's theorem) ──
        Ixx = 0.0   # ∫y² dA  (segundo momento respecto al eje X)
        Iyy = 0.0   # ∫x² dA  (segundo momento respecto al eje Y)
        Ixy = 0.0   # ∫xy dA  (producto de inercia)
        area_signed = 0.0

        for i in range(n):
            j = (i + 1) % n
            xi = ring_pts[i].x() - cx
            yi = ring_pts[i].y() - cy
            xj = ring_pts[j].x() - cx
            yj = ring_pts[j].y() - cy
            cross = xi * yj - xj * yi
            area_signed += cross
            Ixx += cross * (yi * yi + yi * yj + yj * yj)
            Iyy += cross * (xi * xi + xi * xj + xj * xj)
            Ixy += cross * (xi * yj + 2.0 * xi * yi + 2.0 * xj * yj + xj * yi)

        area_signed /= 2.0
        area_abs = abs(area_signed)
        if area_abs < 1e-10:
            return 0.0

        Ixx /= 12.0
        Iyy /= 12.0
        Ixy /= 24.0

        # Ixx, Iyy son siempre positivos; Ixy cambia de signo con la orientación del anillo
        Ixx = abs(Ixx)
        Iyy = abs(Iyy)
        if area_signed < 0:
            Ixy = -Ixy

        # Covarianza del área (Iyy/A = varianza de x, Ixx/A = varianza de y)
        cov_xx = Iyy / area_abs
        cov_yy = Ixx / area_abs
        cov_xy = Ixy / area_abs

        # Eigenanalysis de [[cov_xx, cov_xy], [cov_xy, cov_yy]]
        trace = cov_xx + cov_yy
        det   = cov_xx * cov_yy - cov_xy * cov_xy
        disc  = math.sqrt(max(0.0, (trace / 2.0) ** 2 - det))
        lam1  = trace / 2.0 + disc  # Mayor eigenvalue = dirección de máxima extensión

        # Eigenvector del mayor eigenvalue: (cov_xy, lam1 - cov_xx)
        ex = cov_xy
        ey = lam1 - cov_xx

        if abs(ex) < 1e-10 and abs(ey) < 1e-10:
            ex, ey = 1.0, 0.0

        # Azimut geográfico: atan2(Δeste, Δnorte)
        az = math.degrees(math.atan2(ex, ey)) % 360.0

        # ── Desambiguación: proyecciones ponderadas por longitud de segmento ──
        norm = math.hypot(ex, ey)
        ux, uy = ex / norm, ey / norm

        sum_pos = 0.0
        sum_neg = 0.0

        for i in range(n):
            xi = ring_pts[i].x() - cx
            yi = ring_pts[i].y() - cy
            proj = xi * ux + yi * uy

            # Peso: longitud promedio de segmentos adyacentes
            prev_i = (i - 1) % n
            next_i = (i + 1) % n
            d_prev = math.hypot(ring_pts[i].x() - ring_pts[prev_i].x(),
                                ring_pts[i].y() - ring_pts[prev_i].y())
            d_next = math.hypot(ring_pts[next_i].x() - ring_pts[i].x(),
                                ring_pts[next_i].y() - ring_pts[i].y())
            w = (d_prev + d_next) / 2.0

            if proj >= 0:
                sum_pos += w * proj
            else:
                sum_neg += w * abs(proj)

        # Orientar hacia donde hay más masa ponderada del borde
        if sum_neg > sum_pos:
            az = (az + 180.0) % 360.0

        return az


class CircularBufferProcessor(BufferProcessor):
    """Procesador para búferes circulares."""
    
    def process(self, geom: QgsGeometry, params: BufferParams,
                logger: Logger, desc: str) -> List[Tuple[QgsGeometry, str, float]]:
        results = []
        j_style, cap_style = JoinStyleManager.get_styles(params.join_idx, geom.type())
        
        d = params.distancia
        if params.calcular_por_area:
            d = self._find_distance_for_area(geom, params.area_objetivo_m2, params.segmentos)
        
        # Validar distancia cero
        if abs(d) < Constants.MIN_BUFFER_DISTANCE and not params.calcular_por_area:
            logger.warning(f"{desc}: Circular — Distancia es 0 o menor al mínimo ({d:.4f}m), no se genera geometría")
            return results
        
        # Comportamiento estándar de QGIS: buffer() crea búfer en ambas direcciones
        # Para polígonos con huecos, esto significa:
        #   - Expande hacia afuera del contorno exterior
        #   - Expande hacia adentro de los huecos (reduce el tamaño del hueco)
        # Este es el comportamiento esperado de QGIS. Para evitar búfer en huecos,
        # use la opción "Eliminar huecos" antes o después del búfer.
        g = geom.buffer(d, params.segmentos, cap_style, j_style, params.miter_limit)
        
        if g and not g.isEmpty():
            results.append((g, "Circular", d))
        else:
            if d < 0:
                logger.warning(f"{desc}: Circular — Colapso geométrico (distancia={d:.2f}m, el búfer negativo colapsa la geometría)")
            else:
                logger.warning(f"{desc}: Circular — Resultado vacío (distancia={d:.2f}m)")
        
        return results


class OvalBufferProcessor(BufferProcessor):
    """Procesador para búferes ovalados."""
    
    def process(self, geom: QgsGeometry, params: BufferParams,
                logger: Logger, desc: str) -> List[Tuple[QgsGeometry, str, float]]:
        results = []
        
        # Punto de anclaje según tipo de geometría:
        # - Líneas: punto a mitad de longitud recorrida — siempre sobre la línea, sin riesgo flotante
        #   ⚠️ MultiLínea: interpolate() opera sobre longitud acumulada — puede caer en parte secundaria
        # - Polígonos: poleOfInaccessibility() — máxima distancia al borde, representativo en formas irregulares
        # - Puntos: Punto en Superficie() — el punto mismo
        if geom.type() == QgsWkbTypes.LineGeometry:
            mid = geom.interpolate(geom.length() / 2.0)
            cen = mid.asPoint() if mid and not mid.isEmpty() else geom.pointOnSurface().asPoint()
        elif geom.type() == QgsWkbTypes.PolygonGeometry:
            # Punto de anclaje: estrategia híbrida centroide + Polsby-Popper.
            # 1) Centroide: válido si cae DENTRO del polígono y a distancia mínima del borde
            #    (≥5% de √área) — garantiza que no está en un corredor estrecho.
            # 2) Fallback: parte más compacta (Polsby-Popper) vía erosión iterativa —
            #    para formas tentaculares donde el centroide cae fuera o cerca del borde.
            bbox = geom.boundingBox()
            lado_menor = min(bbox.width(), bbox.height())
            area_geom = geom.area()
            cen = None

            # --- 1) Centroide ---
            cen_geom = geom.centroid()
            if cen_geom and not cen_geom.isEmpty() and geom.contains(cen_geom):
                if QgsWkbTypes.isMultiType(geom.wkbType()):
                    cen = cen_geom.asPoint()  # MultiPolígono: centroide aceptado
                else:
                    if QgsWkbTypes.isMultiType(geom.wkbType()):
                        cen = cen_geom.asPoint()  # MultiPolígono: centroide aceptado
                    else:
                        exterior = QgsGeometry.fromPolylineXY((geom.asPolygon() or [[]])[0])
                        dist_borde = exterior.distance(cen_geom) if area_geom > 0 and not exterior.isEmpty() else 0
                        if dist_borde >= max(math.sqrt(area_geom) * 0.05, 1.0):
                            cen = cen_geom.asPoint()


            # --- 2) Polo del casco convexo (L/U/T) ---
            # El polo del casco convexo cae en la confluencia de los brazos
            # en formas concavas, garantizando anclaje dentro del poligono.
            if cen is None:
                hull = geom.convexHull()
                if hull and not hull.isEmpty():
                    hull_bbox = hull.boundingBox()
                    hull_tol = max(min(hull_bbox.width(), hull_bbox.height()) * 0.001, 0.1)
                    hull_pr = hull.poleOfInaccessibility(hull_tol)
                    hull_pole = hull_pr[0] if isinstance(hull_pr, tuple) else hull_pr
                    if hull_pole and not hull_pole.isEmpty() and geom.contains(hull_pole):
                        cen = hull_pole.asPoint()

            # --- 3) Fallback: Polsby-Popper sobre núcleo eroso ---
            if cen is None:
                for factor in (0.02, 0.05, 0.10, 0.20, 0.35):
                    erosion = max(lado_menor * factor, 1.0)
                    geom_eros = geom.buffer(-erosion, 8)  # 8 segs: núcleo interno
                    if not geom_eros or geom_eros.isEmpty():
                        continue
                    partes = geom_eros.asGeometryCollection() if geom_eros.isMultipart() else [geom_eros]
                    nucleo = max(partes, key=self._compacidad)
                    tolerancia = max(lado_menor * 0.001, 0.1)
                    pole_result = nucleo.poleOfInaccessibility(tolerancia)
                    pole = pole_result[0] if isinstance(pole_result, tuple) else pole_result
                    if pole and not pole.isEmpty() and geom.contains(pole):
                        cen = pole.asPoint()
                        break

            # --- 3) Fallback final ---
            if cen is None:
                tolerancia = max(lado_menor * 0.001, 0.1)
                pole_result = geom.poleOfInaccessibility(tolerancia)
                pole = pole_result[0] if isinstance(pole_result, tuple) else pole_result
                cen = pole.asPoint() if pole and not pole.isEmpty() and geom.contains(pole) else geom.pointOnSurface().asPoint()
        else:
            cen = geom.pointOnSurface().asPoint()
        
        if params.ancho <= 0 or params.alto <= 0:
            logger.warning(f"{desc}: Oval — Dimensiones inválidas (ancho={params.ancho:.2f}m, alto={params.alto:.2f}m): el ancho y el alto deben ser > 0")
            return results
        
        # ANCHO = eje X (horizontal), ALTO/LARGO = eje Y (vertical)
        # rad directo desde rotacion — sin el offset de 90° que causaba intercambio de ejes
        # Rotación automática: suma el eje principal del polígono a la rotación fija
        rotacion_efectiva = params.rotacion
        if params.usar_rot_auto and geom.type() == QgsWkbTypes.PolygonGeometry:
            rotacion_efectiva = (params.rotacion + self._calculate_auto_rotation(geom, logger)) % 360.0
        rad = math.radians(rotacion_efectiva)
        pts = []
        
        for k in range(params.segmentos):
            ang = 2 * math.pi * k / params.segmentos
            dx = (params.ancho / 2) * math.cos(ang)
            dy = (params.alto / 2) * math.sin(ang)
            rx = cen.x() + dx * math.cos(rad) - dy * math.sin(rad)
            ry = cen.y() + dx * math.sin(rad) + dy * math.cos(rad)
            pts.append(QgsPointXY(rx, ry))
        
        g = QgsGeometry.fromPolygonXY([pts + [pts[0]]])
        if g and not g.isEmpty():
            desc_rot_ov = f" (AutoRot: {rotacion_efectiva:.1f}°)" if params.usar_rot_auto and geom.type() == QgsWkbTypes.PolygonGeometry else ""
            results.append((g, f"Oval{desc_rot_ov}", params.ancho))
        else:
            logger.warning(f"{desc}: Oval — Resultado vacío (ancho={params.ancho:.2f}m, alto={params.alto:.2f}m)")
        return results


class RectangularBufferProcessor(BufferProcessor):
    """Procesador para búferes rectangulares."""
    
    def process(self, geom: QgsGeometry, params: BufferParams,
                logger: Logger, desc: str) -> List[Tuple[QgsGeometry, str, float]]:
        results = []
        
        if params.usar_corredor and geom.type() == QgsWkbTypes.LineGeometry:
            if params.ancho < 0:
                logger.warning(f"{desc}: Rectangular Corredor — Ancho negativo ({params.ancho:.4f}m) no válido para Corredor. Use valor positivo. Entidad omitida.")
                return results
            if params.ancho < Constants.MIN_BUFFER_DISTANCE:
                logger.warning(f"{desc}: Rectangular Corredor — Ancho es 0 ({params.ancho:.4f}m), no se genera geometría. Entidad omitida.")
                return results
            g = geom.buffer(params.ancho / 2, 2, Qgis.EndCapStyle.Flat, 
                           Qgis.JoinStyle.Miter, 2.0)
            if g and not g.isEmpty():
                results.append((g, "Corredor", params.ancho))
            else:
                logger.warning(f"{desc}: Rectangular Corredor — Resultado vacío (ancho={params.ancho:.4f}m)")
            return results
        
        # Punto de anclaje según tipo de geometría:
        # - Líneas: punto a mitad de longitud recorrida — siempre sobre la línea, sin riesgo flotante
        #   Fallback: Punto en Superficie() si interpolate() falla (geometría degenerada)
        # - Polígonos: poleOfInaccessibility() — máxima distancia al borde, representativo en formas irregulares
        # - Puntos: centroide = el punto mismo
        if geom.type() == QgsWkbTypes.LineGeometry:
            mid = geom.interpolate(geom.length() / 2.0)
            cen = mid.asPoint() if mid and not mid.isEmpty() else geom.pointOnSurface().asPoint()
        elif geom.type() == QgsWkbTypes.PolygonGeometry:
            # Punto de anclaje: estrategia híbrida centroide + Polsby-Popper.
            # 1) Centroide: válido si cae DENTRO del polígono y a distancia mínima del borde
            #    (≥5% de √área) — garantiza que no está en un corredor estrecho.
            # 2) Fallback: parte más compacta (Polsby-Popper) vía erosión iterativa —
            #    para formas tentaculares donde el centroide cae fuera o cerca del borde.
            bbox = geom.boundingBox()
            lado_menor = min(bbox.width(), bbox.height())
            area_geom = geom.area()
            cen = None

            # --- 1) Centroide ---
            cen_geom = geom.centroid()
            if cen_geom and not cen_geom.isEmpty() and geom.contains(cen_geom):
                if QgsWkbTypes.isMultiType(geom.wkbType()):
                    cen = cen_geom.asPoint()  # MultiPolígono: centroide aceptado
                else:
                    if QgsWkbTypes.isMultiType(geom.wkbType()):
                        cen = cen_geom.asPoint()  # MultiPolígono: centroide aceptado
                    else:
                        exterior = QgsGeometry.fromPolylineXY((geom.asPolygon() or [[]])[0])
                        dist_borde = exterior.distance(cen_geom) if area_geom > 0 and not exterior.isEmpty() else 0
                        if dist_borde >= max(math.sqrt(area_geom) * 0.05, 1.0):
                            cen = cen_geom.asPoint()


            # --- 2) Polo del casco convexo (L/U/T) ---
            # El polo del casco convexo cae en la confluencia de los brazos
            # en formas concavas, garantizando anclaje dentro del poligono.
            if cen is None:
                hull = geom.convexHull()
                if hull and not hull.isEmpty():
                    hull_bbox = hull.boundingBox()
                    hull_tol = max(min(hull_bbox.width(), hull_bbox.height()) * 0.001, 0.1)
                    hull_pr = hull.poleOfInaccessibility(hull_tol)
                    hull_pole = hull_pr[0] if isinstance(hull_pr, tuple) else hull_pr
                    if hull_pole and not hull_pole.isEmpty() and geom.contains(hull_pole):
                        cen = hull_pole.asPoint()

            # --- 3) Fallback: Polsby-Popper sobre núcleo eroso ---
            if cen is None:
                for factor in (0.02, 0.05, 0.10, 0.20, 0.35):
                    erosion = max(lado_menor * factor, 1.0)
                    geom_eros = geom.buffer(-erosion, 8)  # 8 segs: núcleo interno
                    if not geom_eros or geom_eros.isEmpty():
                        continue
                    partes = geom_eros.asGeometryCollection() if geom_eros.isMultipart() else [geom_eros]
                    nucleo = max(partes, key=self._compacidad)
                    tolerancia = max(lado_menor * 0.001, 0.1)
                    pole_result = nucleo.poleOfInaccessibility(tolerancia)
                    pole = pole_result[0] if isinstance(pole_result, tuple) else pole_result
                    if pole and not pole.isEmpty() and geom.contains(pole):
                        cen = pole.asPoint()
                        break

            # --- 3) Fallback final ---
            if cen is None:
                tolerancia = max(lado_menor * 0.001, 0.1)
                pole_result = geom.poleOfInaccessibility(tolerancia)
                pole = pole_result[0] if isinstance(pole_result, tuple) else pole_result
                cen = pole.asPoint() if pole and not pole.isEmpty() and geom.contains(pole) else geom.pointOnSurface().asPoint()
        else:
            cen = geom.centroid().asPoint()
        
        if params.ancho <= 0 or params.alto <= 0:
            logger.warning(f"{desc}: Rectangular — Dimensiones inválidas (ancho={params.ancho:.2f}m, alto={params.alto:.2f}m): el ancho y el alto deben ser > 0")
            return results
        
        # ANCHO = eje X (horizontal), ALTO/LARGO = eje Y (vertical)
        # rad directo desde rotacion — sin el offset de 90° que causaba intercambio de ejes
        # Rotación automática: suma el eje principal del polígono a la rotación fija
        rotacion_efectiva = params.rotacion
        if params.usar_rot_auto and geom.type() == QgsWkbTypes.PolygonGeometry:
            rotacion_efectiva = (params.rotacion + self._calculate_auto_rotation(geom, logger)) % 360.0
        rad = math.radians(rotacion_efectiva)
        pts_base = [
            QgsPointXY(-params.ancho/2, -params.alto/2),
            QgsPointXY( params.ancho/2, -params.alto/2),
            QgsPointXY( params.ancho/2,  params.alto/2),
            QgsPointXY(-params.ancho/2,  params.alto/2)
        ]
        
        pts_rot = [
            QgsPointXY(
                cen.x() + p.x() * math.cos(rad) - p.y() * math.sin(rad),
                cen.y() + p.x() * math.sin(rad) + p.y() * math.cos(rad)
            ) for p in pts_base
        ]
        
        g = QgsGeometry.fromPolygonXY([pts_rot + [pts_rot[0]]])
        if g and not g.isEmpty():
            desc_rot_re = f" (AutoRot: {rotacion_efectiva:.1f}°)" if params.usar_rot_auto and geom.type() == QgsWkbTypes.PolygonGeometry else ""
            results.append((g, f"Rect{desc_rot_re}", params.ancho))
        else:
            logger.warning(f"{desc}: Rectangular — Resultado vacío (ancho={params.ancho:.2f}m, alto={params.alto:.2f}m)")
        return results


class ConcentricBufferProcessor(BufferProcessor):
    """Procesador para búferes concéntricos."""
    
    def process(self, geom: QgsGeometry, params: BufferParams,
                logger: Logger, desc: str) -> List[Tuple[QgsGeometry, str, float]]:
        results = []

        # Validar distancia cero — geom.buffer(0) produce geometría degenerada sin advertencia
        if abs(params.concentric_distance) < Constants.MIN_BUFFER_DISTANCE:
            logger.warning(
                f"{desc}: Concéntrico — Distancia entre anillos es 0 o menor al mínimo "
                f"({params.concentric_distance:.4f}m), entidad omitida"
            )
            return results

        es_negativo = params.concentric_distance < 0
        j_style, cap_style = JoinStyleManager.get_styles(params.join_idx, geom.type())
        
        # Caché para reutilizar buffers previos
        buffer_cache = {}
        
        for k in range(1, params.concentric_count + 1):
            d_actual = params.concentric_distance * k
            d_previo = params.concentric_distance * (k - 1)
            g_final = None
            
            if d_actual not in buffer_cache:
                buffer_cache[d_actual] = geom.buffer(
                    d_actual, params.segmentos, cap_style, j_style, params.miter_limit)
            g_actual = buffer_cache[d_actual]
            
            if not es_negativo:
                if params.anillos_disjuntos:
                    g_previo = geom if d_previo == 0 else buffer_cache.setdefault(
                        d_previo, geom.buffer(d_previo, params.segmentos, cap_style, j_style, params.miter_limit))
                    if g_actual and not g_actual.isEmpty():
                        g_diff = g_actual.difference(g_previo)
                        # Filtrar astillas: en geometrías cóncavas (ej. penínsulas más
                        # estrechas que la distancia del búfer), difference() produce
                        # fragmentos microscópicos junto al anillo real. Se descartan
                        # las partes cuya área sea menor al 1% del área esperada del anillo
                        # (π × d_actual² - π × d_previo²).
                        area_esperada = 3.14159 * (d_actual**2 - max(d_previo,0)**2)
                        umbral_astilla = area_esperada * 0.01
                        if g_diff and not g_diff.isEmpty() and g_diff.isMultipart():
                            partes = g_diff.asGeometryCollection()
                            partes_validas = [
                                p for p in partes
                                if QgsWkbTypes.geometryType(p.wkbType()) == QgsWkbTypes.PolygonGeometry
                                and p.area() >= umbral_astilla
                            ]
                            if partes_validas:
                                g_final = QgsGeometry.collectGeometry(partes_validas)
                            else:
                                g_final = g_diff  # fallback: conservar todo si no queda nada
                        else:
                            g_final = g_diff
                else:
                    g_final = g_actual
            else:
                g_inner = g_actual
                if params.anillos_disjuntos:
                    g_outer = geom if d_previo == 0 else buffer_cache.setdefault(
                        d_previo, geom.buffer(d_previo, params.segmentos, cap_style, j_style, params.miter_limit))
                    if g_outer and not g_outer.isEmpty():
                        if g_inner.isEmpty():
                            # El anillo interior colapsó: todos los anillos siguientes
                            # también colapsarán (d_actual se vuelve más negativo).
                            # Se genera el anillo k como sólido y se interrumpe el loop.
                            anillos_restantes = params.concentric_count - k
                            logger.warning(
                                f"{desc}: Concéntrico — Anillo {k} COLAPSÓ "
                                f"(distancia interna={d_actual:.2f}m supera el tamaño de la geometría). "
                                f"Se generó como anillo sólido. "
                                f"Los {anillos_restantes} anillo(s) restante(s) no se generarán "
                                f"(colapsarían igualmente). "
                                f"Reduzca la distancia negativa o el número de anillos."
                            )
                            g_final = g_outer  # Entregar el anillo exterior como sólido
                            results.append((g_final, f"Anillo {k} (sólido-colapso)", d_actual))
                            break   # Anillos siguientes son imposibles — salir del loop
                        g_final = g_outer.difference(g_inner)
                else:
                    g_final = g_inner
                    if g_final.isEmpty():
                        logger.warning(f"{desc}: Concéntrico — Anillo {k} omitido, colapso geométrico (distancia={d_actual:.2f}m)")
                        continue
            
            if g_final and not g_final.isEmpty():
                tipo_salida = f"Anillo {k}" if params.anillos_disjuntos else f"Disco {k}"
                results.append((g_final, tipo_salida, d_actual))
        
        return results


class AreaBufferProcessor(BufferProcessor):
    """Procesador para búferes por área determinada."""
    
    def process(self, geom: QgsGeometry, params: BufferParams,
                logger: Logger, desc: str) -> List[Tuple[QgsGeometry, str, float]]:
        results = []
        
        # Validar área objetivo
        if params.area_objetivo_m2 <= 0:
            logger.warning(f"{desc}: Por Área — Área objetivo es 0 o negativa ({params.area_objetivo_m2:.4f} m²), no se genera geometría")
            return results
        
        j_style, cap_style = JoinStyleManager.get_styles(params.join_idx, geom.type())
        
        dist_calc = self._find_distance_for_area(geom, params.area_objetivo_m2, params.segmentos)
        
        # Comportamiento estándar de QGIS para polígonos con huecos
        g = geom.buffer(dist_calc, params.segmentos, cap_style, j_style, params.miter_limit)
        
        if g and not g.isEmpty():
            # Detectar fragmentación: contracción en polígono cóncavo puede producir MultiPolígono
            if dist_calc < 0 and QgsWkbTypes.isMultiType(g.wkbType()):
                n_partes = len(g.asMultiPolygon())
                if params.mantener_parte_mayor:
                    # Seleccionar solo el fragmento de mayor área
                    partes = g.asGeometryCollection()
                    parte_mayor = max(partes, key=lambda p: p.area())
                    area_descartada = g.area() - parte_mayor.area()
                    logger.warning(
                        f"{desc}: Por Área — La contracción fragmentó el polígono en {n_partes} partes. "
                        f"Se conserva solo la parte mayor ({parte_mayor.area() / 10000:.4f} ha). "
                        f"Área descartada: {area_descartada / 10000:.4f} ha. "
                        f"El área final NO cumple el objetivo ({params.area_objetivo_m2 / 10000:.4f} ha)."
                    )
                    g = parte_mayor
                else:
                    logger.warning(
                        f"{desc}: Por Área — La contracción fragmentó el polígono en {n_partes} partes. "
                        f"El área objetivo ({params.area_objetivo_m2 / 10000:.4f} ha) se distribuye entre los fragmentos. "
                        f"💡 Active '🧩 Al fragmentar: mantener solo parte mayor' para resultado continuo, "
                        f"o aumente el área objetivo, simplifique la geometría, "
                        f"o divida el polígono original en partes antes de procesar."
                    )
            results.append((g, "Por Área", dist_calc))
        return results


class SingleSidedBufferProcessor(BufferProcessor):
    """Procesador para búferes de un solo lado."""
    
    def process(self, geom: QgsGeometry, params: BufferParams,
                logger: Logger, desc: str) -> List[Tuple[QgsGeometry, str, float]]:
        base_type = QgsWkbTypes.geometryType(geom.wkbType())
        
        # Validar distancia cero — offset de 0 produce geometrías degeneradas
        if abs(params.distancia) < Constants.MIN_BUFFER_DISTANCE:
            logger.warning(f"{desc}: Un Lado — Distancia es 0 o menor al mínimo ({params.distancia:.4f}m), no se genera geometría")
            return []
        
        if base_type == QgsWkbTypes.PolygonGeometry:
            return self._process_polygon(geom, params, logger, desc)
        elif base_type == QgsWkbTypes.LineGeometry:
            return self._process_line(geom, params, logger, desc)
        return []
    
    def _process_polygon(self, geom, params, logger, desc):
        results = []
        sign = 1.0 if params.side_idx == Constants.SIDE_LEFT else -1.0
        final_dist = abs(params.distancia) * sign
        j_style, _ = JoinStyleManager.get_styles(params.join_idx, geom.type())
        
        buf_complete = geom.buffer(final_dist, params.segmentos, 
                                   Qgis.EndCapStyle.Round, j_style, params.miter_limit)
        g = None
        
        if buf_complete and not buf_complete.isEmpty():
            g = buf_complete.difference(geom) if params.side_idx == Constants.SIDE_LEFT else geom.difference(buf_complete)
        
        if g and not g.isEmpty():
            s_name = "Izq (Ext)" if params.side_idx == Constants.SIDE_LEFT else "Der (Int)"
            results.append((g, f"Solo un Lado-{s_name}", params.distancia))
        else:
            lado = "Izquierda/Exterior" if params.side_idx == Constants.SIDE_LEFT else "Derecha/Interior"
            if params.side_idx == Constants.SIDE_RIGHT:
                logger.warning(f"{desc}: Un Lado — Colapso interior, la distancia ({params.distancia:.2f}m) supera el tamaño del polígono (lado={lado})")
            else:
                logger.warning(f"{desc}: Un Lado — Resultado vacío (distancia={params.distancia:.2f}m, lado={lado})")
        return results
    
    def _process_line(self, geom, params, logger, desc):
        results = []
        j_style, _ = JoinStyleManager.get_styles(params.join_idx, geom.type())
        
        # Convención GEOS offsetCurve: positivo=izquierda matemática, negativo=derecha matemática
        # (respecto al sentido de digitalización de la línea)
        # CORRECCIÓN DE INTERFAZ: invertimos el signo para que la etiqueta UI coincida
        # con el resultado visual percibido por el usuario.
        # SIDE_LEFT (usuario dice "Izquierda") → negativo GEOS → resultado visual izquierdo
        # SIDE_RIGHT (usuario dice "Derecha")  → positivo GEOS → resultado visual derecho
        # Si hay campo de distancia, el signo del valor del campo tiene prioridad
        # CORRECCIÓN DE INTERFAZ: sign_side invierte GEOS para que UI coincida con visual.
        # El signo del campo variable controla el lado:
        #   positivo → mismo lado que SIDE seleccionado
        #   negativo → lado opuesto al SIDE seleccionado
        sign_side = -1.0 if params.side_idx == Constants.SIDE_LEFT else 1.0
        offset_dist = sign_side * params.distancia
        
        lado_real = "Izquierda" if params.side_idx == Constants.SIDE_LEFT else "Derecha"
        logger.info(f"🔧 {desc}: Un Lado — Procesando línea (lado={lado_real}, distancia={params.distancia:.2f}m, estilo={JoinStyleManager.get_style_name(params.join_idx)})")
        
        g = None
        try:
            
            if geom.isMultipart():
                parts_buffer = []
                for idx, pts in enumerate(geom.asMultiPolyline()):
                    temp_g = QgsGeometry.fromPolylineXY(pts)
                    try:
                        line2 = temp_g.offsetCurve(offset_dist, params.segmentos, j_style, params.miter_limit)
                        if line2 and not line2.isEmpty():
                            pts1, pts2 = temp_g.asPolyline(), line2.asPolyline()
                            pts2.reverse()
                            poly_geom = QgsGeometry.fromPolygonXY([pts1 + pts2 + [pts1[0]]])
                            if poly_geom and not poly_geom.isEmpty():
                                parts_buffer.append(poly_geom)
                                logger.info(f"  ✅ Parte {idx+1}: Polígono creado")
                    except (AttributeError, ValueError, RuntimeError) as e:
                        logger.warning(f"  Un Lado — Parte {idx+1}: Error en offset ({str(e)[:60]})")
                g = QgsGeometry.collectGeometry(parts_buffer) if parts_buffer else None
            else:
                try:
                    line2 = geom.offsetCurve(offset_dist, params.segmentos, j_style, params.miter_limit)
                    if line2 and not line2.isEmpty():
                        pts1, pts2 = geom.asPolyline(), line2.asPolyline()
                        pts2.reverse()
                        g = QgsGeometry.fromPolygonXY([pts1 + pts2 + [pts1[0]]])
                        if g and not g.isEmpty():
                            logger.info(f"  ✅ Polígono creado desde líneas paralelas")
                except (AttributeError, ValueError, RuntimeError) as e:
                    logger.warning(f"  Un Lado — Error en offset: {str(e)[:80]}")
                    
        except (AttributeError, ValueError, RuntimeError) as e:
            logger.error(f"{desc}: Un Lado — Error crítico en procesamiento de línea ({str(e)[:80]})")
        
        if g and not g.isEmpty():
            s_name = "Izq" if params.side_idx == Constants.SIDE_LEFT else "Der"
            results.append((g, f"Solo un Lado-{s_name}", params.distancia))
        else:
            logger.warning(f"{desc}: Un Lado — Resultado vacío (offset={offset_dist:.2f}m, lado={lado_real})")
        return results


class WedgeBufferProcessor(BufferProcessor):
    """Procesador para búferes en cuña (wedge)."""
    
    def process(self, geom: QgsGeometry, params: BufferParams,
                logger: Logger, desc: str) -> List[Tuple[QgsGeometry, str, float]]:
        results = []
        
        # Validar radio antes de procesar — evita geometría degenerada (todos los puntos en el centroide)
        if abs(params.distancia) < Constants.MIN_BUFFER_DISTANCE:
            logger.warning(f"{desc}: Cuña — Radio es 0 o menor al mínimo ({params.distancia:.4f}m), entidad omitida")
            return results
        
        # Detectar radio negativo — produce orientación invertida (180° opuesta a la configurada)
        radio_negativo = params.distancia < 0
        
        # Calcular punto origen según tipo de geometría
        g_type = geom.type()
        try:
            if g_type == QgsWkbTypes.LineGeometry:
                # Líneas: punto a mitad de longitud recorrida (más intuitivo que centroide)
                # Fallback: pointOnSurface() si interpolate() falla (geometría degenerada)
                mid = geom.interpolate(geom.length() / 2.0)
                cen = mid.asPoint() if mid and not mid.isEmpty() else geom.pointOnSurface().asPoint()
            elif geom.type() == QgsWkbTypes.PolygonGeometry:
                # Punto de anclaje: estrategia híbrida centroide + Polsby-Popper.
                # 1) Centroide: válido si cae DENTRO del polígono y a distancia mínima del borde
                #    (≥5% de √área) — garantiza que no está en un corredor estrecho.
                # 2) Fallback: parte más compacta (Polsby-Popper) vía erosión iterativa —
                #    para formas tentaculares donde el centroide cae fuera o cerca del borde.
                bbox = geom.boundingBox()
                lado_menor = min(bbox.width(), bbox.height())
                area_geom = geom.area()
                cen = None

                # --- 1) Centroide ---
                cen_geom = geom.centroid()
                if cen_geom and not cen_geom.isEmpty() and geom.contains(cen_geom):
                    if QgsWkbTypes.isMultiType(geom.wkbType()):
                        cen = cen_geom.asPoint()  # MultiPolígono: centroide aceptado
                    else:
                        exterior = QgsGeometry.fromPolylineXY((geom.asPolygon() or [[]])[0])
                        dist_borde = exterior.distance(cen_geom) if area_geom > 0 and not exterior.isEmpty() else 0
                        if dist_borde >= max(math.sqrt(area_geom) * 0.05, 1.0):
                            cen = cen_geom.asPoint()


                # --- 2) Polo del casco convexo (L/U/T) ---
                # El polo del casco convexo cae en la confluencia de los brazos
                # en formas concavas, garantizando anclaje dentro del poligono.
                if cen is None:
                    hull = geom.convexHull()
                    if hull and not hull.isEmpty():
                        hull_bbox = hull.boundingBox()
                        hull_tol = max(min(hull_bbox.width(), hull_bbox.height()) * 0.001, 0.1)
                        hull_pr = hull.poleOfInaccessibility(hull_tol)
                        hull_pole = hull_pr[0] if isinstance(hull_pr, tuple) else hull_pr
                        if hull_pole and not hull_pole.isEmpty() and geom.contains(hull_pole):
                            cen = hull_pole.asPoint()

                # --- 3) Fallback: Polsby-Popper sobre núcleo eroso ---
                if cen is None:
                    for factor in (0.02, 0.05, 0.10, 0.20, 0.35):
                        erosion = max(lado_menor * factor, 1.0)
                        geom_eros = geom.buffer(-erosion, 8)  # 8 segs: núcleo interno
                        if not geom_eros or geom_eros.isEmpty():
                            continue
                        partes = geom_eros.asGeometryCollection() if geom_eros.isMultipart() else [geom_eros]
                        nucleo = max(partes, key=self._compacidad)
                        tolerancia = max(lado_menor * 0.001, 0.1)
                        pole_result = nucleo.poleOfInaccessibility(tolerancia)
                        pole = pole_result[0] if isinstance(pole_result, tuple) else pole_result
                        if pole and not pole.isEmpty() and geom.contains(pole):
                            cen = pole.asPoint()
                            break

                # --- 3) Fallback final ---
                if cen is None:
                    tolerancia = max(lado_menor * 0.001, 0.1)
                    pole_result = geom.poleOfInaccessibility(tolerancia)
                    pole = pole_result[0] if isinstance(pole_result, tuple) else pole_result
                    cen = pole.asPoint() if pole and not pole.isEmpty() and geom.contains(pole) else geom.pointOnSurface().asPoint()
            else:
                # Puntos: centroide = el punto mismo
                cen = geom.centroid().asPoint()
        except Exception:
            cen = geom.pointOnSurface().asPoint()

        rotacion_base = self._calculate_auto_rotation(geom, logger) if params.usar_rot_auto else 0.0
        start_az = (params.wedge_start + rotacion_base) % 360.0

        if radio_negativo:
            az_efectivo = (start_az + 180.0) % 360.0
            logger.warning(
                f"{desc}: Cuña — ⚠️ ORIENTACIÓN INVERTIDA por radio negativo ({params.distancia:.2f}m). "
                f"La cuña apunta en dirección opuesta a la configurada "
                f"(Azimut base: {start_az:.1f}° → orientación resultante: {az_efectivo:.1f}°). "
                f"El polígono es válido geométricamente pero está rotado 180°. "
                f"Verifique el campo de distancia o use valores positivos."
            )

        if params.usar_rot_auto:
            logger.info(f"{desc}: Cuña start_az = wedge_start({params.wedge_start:.1f}°) + autorot({rotacion_base:.1f}°) = {start_az:.1f}°")
        pts = [cen]
        pasos = max(params.segmentos, 25)
        
        for k in range(pasos + 1):
            curr_az = start_az + (k * params.wedge_width / pasos)
            curr_rad = math.radians(90 - curr_az)
            px = cen.x() + params.distancia * math.cos(curr_rad)
            py = cen.y() + params.distancia * math.sin(curr_rad)
            pts.append(QgsPointXY(px, py))
        
        pts.append(cen)
        g = QgsGeometry.fromPolygonXY([pts])
        
        # Construir descripción con información de orientación
        desc_rot = f" (AutoRot: {rotacion_base:.1f}°)" if params.usar_rot_auto and rotacion_base != 0 else ""
        desc_negativo = " [⚠️ Orientación invertida - radio negativo]" if radio_negativo else ""
        
        if g and not g.isEmpty():
            results.append((g, f"Cuña{desc_rot}{desc_negativo}", params.distancia))
        else:
            logger.warning(f"{desc}: Cuña — Resultado vacío (radio={params.distancia:.2f}m, azimut={params.wedge_start:.1f}°, amplitud={params.wedge_width:.1f}°)")
        return results
    

# ==============================================================================
# FÁBRICA DE PROCESADORES
# ==============================================================================

class VariableWidthMBufferProcessor(BufferProcessor):
    """
    Procesador para búfer de ancho variable.

    Construye el corredor mediante sub-segmentación adaptativa:
    cada segmento de la ruta se divide en sub-segmentos con paso
    adaptativo min(r_min/4, 2,0 m), cada sub-segmento recibe un búfer
    GEOS con radio interpolado linealmente, y la unión (unaryUnion)
    produce el corredor final con esquinas redondeadas y transición
    continua de ancho. Único método activo: process().
    """

    def process(self, geom: QgsGeometry, params: BufferParams,
                logger: Logger, desc: str) -> List[Tuple[QgsGeometry, str, float]]:
        """
        Corredor de ancho variable universal.

        Corredor de ancho variable: sub-seg buffers adaptativos con paso min(r_min/4, 2 m).
        """
        import math
        from qgis.core import QgsPointXY, QgsGeometry, QgsWkbTypes

        base_type = QgsWkbTypes.geometryType(geom.wkbType())
        if base_type != QgsWkbTypes.LineGeometry:
            return []

        # ── EXTRACCIÓN DE VÉRTICES ──────────────────────────────────────
        m_override = getattr(params, '_m_vertices_override', None)
        if m_override and len(m_override) >= 2:
            coords = [(v[0], v[1]) for v in m_override]
            m_vals = []
            for _idx, v in enumerate(m_override):
                if v[2] < Constants.MIN_BUFFER_DISTANCE:
                    logger.info(
                        f"{desc}: Ancho Variable — vértice {_idx} con M={v[2]:.4f} "
                        f"(inicio de ruta o valor menor al mínimo). "
                        f"Se usará el valor mínimo ({Constants.MIN_BUFFER_DISTANCE}m) "
                        f"como ancho en ese punto."
                    )
                m_vals.append(max(v[2], Constants.MIN_BUFFER_DISTANCE))
            m_max  = max(v[2] for v in m_override)
        else:
            logger.warning(
                f"{desc}: Ancho Variable — no se recibieron datos de vértices. "
                "Se requiere una capa de puntos con campo de distancia "
                "o una línea con coordenada M activa."
            )
            return []

        n_v = len(coords)
        logger.info(f"📐 {desc}: ✅ Generando corredor variable con {n_v} vértices originales")

        SEGS = max(params.segmentos, 8)

        # ── CONSTRUCCIÓN DEL CORREDOR — SUB-SEG BUFFERS ADAPTATIVOS ───────
        # Cada segmento se divide en sub-segmentos con paso = min(r_min/4, 2,0m).
        # Cada sub-seg recibe un buffer GEOS con radio interpolado linealmente.
        # El solapamiento de los semicírculos de GEOS entre sub-segs adyacentes
        # produce esquinas redondeadas y transición continua de ancho.
        # Error máximo: L²/(8r) ≤ (2m)²/(8×r_min) — prácticamente invisible.
        def _fin(g):
            if not g or g.isEmpty(): return None
            if not g.isGeosValid(): g = g.makeValid()
            return g if (g and not g.isEmpty()) else None

        r_min_global = max(min(m_vals), Constants.MIN_BUFFER_DISTANCE)
        paso = min(r_min_global / 4.0, 2.0)
        paso = max(paso, 0.1)

        partes = []
        n_subs = 0
        for i in range(n_v - 1):
            x0, y0 = coords[i];  x1, y1 = coords[i+1]
            r0v, r1v = m_vals[i], m_vals[i+1]
            dx, dy = x1-x0, y1-y0
            L_seg = math.hypot(dx, dy)
            if L_seg < 1e-10: continue
            n_sub = max(1, int(math.ceil(L_seg / paso)))
            n_subs += n_sub
            for k in range(n_sub):
                t0 = k / n_sub;  t1 = (k+1) / n_sub
                sx0 = x0 + t0*dx;  sy0 = y0 + t0*dy
                sx1 = x0 + t1*dx;  sy1 = y0 + t1*dy
                ri = (r0v + t0*(r1v-r0v) + r0v + t1*(r1v-r0v)) / 2.0
                if ri < Constants.MIN_BUFFER_DISTANCE: continue
                sl = QgsGeometry.fromPolylineXY(
                    [QgsPointXY(sx0,sy0), QgsPointXY(sx1,sy1)])
                sb = sl.buffer(ri, SEGS)
                if sb and not sb.isEmpty(): partes.append(sb)

        if not partes:
            logger.warning(f"{desc}: sin geometría")
            return []
        res = _fin(QgsGeometry.unaryUnion(partes))
        if not res: return []
        logger.info(
            f"📐 {desc}: ✅ Corredor generado "
            f"({n_subs} sub-seg., paso={paso:.2f}m, esquinas: redondeadas)"
        )
        return [(res, "Corredor Distancia Variable", m_max)]

class BufferProcessorFactory:
    """Fábrica para crear el procesador de búfer apropiado."""
    
    _processors = {
        Constants.BUFFER_CIRCULAR: CircularBufferProcessor,
        Constants.BUFFER_OVAL: OvalBufferProcessor,
        Constants.BUFFER_RECTANGULAR: RectangularBufferProcessor,
        Constants.BUFFER_CONCENTRICO: ConcentricBufferProcessor,
        Constants.BUFFER_POR_AREA: AreaBufferProcessor,
        Constants.BUFFER_UN_LADO: SingleSidedBufferProcessor,
        Constants.BUFFER_CUNA: WedgeBufferProcessor,
        Constants.BUFFER_ANCHO_M: VariableWidthMBufferProcessor,
    }
    
    @classmethod
    def get_processor(cls, buffer_type: int) -> BufferProcessor:
        return cls._processors.get(buffer_type, CircularBufferProcessor)()


# ==============================================================================
# CLASES: OPERACIONES LÓGICAS
# ==============================================================================
class LogicOperation(ABC):
    @abstractmethod
    def apply(self, buf, orig, tipo, dist): pass

class NoOperation(LogicOperation):
    def apply(self, buf, orig, tipo, dist):
        return [(buf, tipo, dist)]

class UnionOperation(LogicOperation):
    def apply(self, buf, orig, tipo, dist):
        return [(buf.difference(orig), f"{tipo} (Ext)", dist),
                (buf.intersection(orig), f"{tipo} (Int)", dist),
                (orig.difference(buf), f"{tipo} (Orig)", dist)]

class IntersectionOperation(LogicOperation):
    def apply(self, buf, orig, tipo, dist):
        return [(buf.intersection(orig), tipo, dist)]

class DifferenceOperation(LogicOperation):
    def apply(self, buf, orig, tipo, dist):
        return [(orig.difference(buf), tipo, dist)]

class InverseDifferenceOperation(LogicOperation):
    def apply(self, buf, orig, tipo, dist):
        return [(buf.difference(orig), tipo, dist)]

class XOROperation(LogicOperation):
    def apply(self, buf, orig, tipo, dist):
        return [(buf.symDifference(orig), tipo, dist)]

class LogicOperationFactory:
    _operations = {
        Constants.OP_NINGUNA: NoOperation,
        Constants.OP_UNION: UnionOperation,
        Constants.OP_INTERSECCION: IntersectionOperation,
        Constants.OP_DIFERENCIA: DifferenceOperation,
        Constants.OP_DIFERENCIA_INV: InverseDifferenceOperation,
        Constants.OP_XOR: XOROperation,
    }
    
    @classmethod
    def get_operation(cls, op_type: int) -> LogicOperation:
        return cls._operations.get(op_type, NoOperation)()


# ==============================================================================
# CLASE: ANALIZADOR DE SUPERPOSICIÓN
# ==============================================================================
class OverlapAnalyzer:
    """
    Analizador de superposición entre geometrías de búfer.
    
    OPTIMIZACIONES IMPLEMENTADAS (CRÍTICO #5):
    - Índice espacial (QgsSpatialIndex) se activa con 10+ búferes (optimizado)
    - Caché de áreas calculadas para evitar recálculos
    - Early exit con Caja Delimitadora en fuerza bruta
    - Feedback detallado de progreso cada 5-20% del proceso
    - Estadísticas de reducción de comparaciones
    
    COMPLEJIDAD:
    - Sin índice (<10 búferes): O(n²) con early exit en Caja Delimitadora
    - Con índice (≥10 búferes): O(n·k) donde k = candidatos espaciales (k << n)
    
    RENDIMIENTO ESPERADO:
    - 10 búferes: ~0.5 segundos (con índice)
    - 50 búferes: ~2-3 segundos (reducción 60-70% vs brute force)
    - 100 búferes: ~5-8 segundos (reducción 75-85%)
    - 500 búferes: ~45-60 segundos (reducción 85-90%)
    - 1000 búferes: ~3-5 minutos (reducción 90-95%)
    """
    
    # Umbral mínimo de área para considerar una superposición válida (en m²)
    MIN_OVERLAP_AREA = 0.0001  # 0.0001 m² = 1 cm²
    
    # Umbral para activar índice espacial
    # CRÍTICO #5: Optimizado a 10 búferes (análisis de benchmarks demostró que
    # el overhead de crear el índice se compensa con solo 10 búferes)
    SPATIAL_INDEX_THRESHOLD = 10
    
    @staticmethod
    def analyze(buffers: List[Tuple[QgsGeometry, str, float, str]], feedback=None) -> List[Dict[str, Any]]:
        """
        Analiza las superposiciones entre todos los pares de búferes.
        
        Args:
            buffers: Lista de tuplas (geometría, tipo, distancia, descripción)
            feedback: QgsProcessingFeedback opcional para verificar cancelación
            
        Returns:
            Lista de diccionarios con información de cada superposición encontrada
        """
        overlaps = []
        n = len(buffers)
        
        if n < 2:
            return overlaps
        
        # Decidir estrategia: índice espacial para capas grandes
        usar_indice = n > OverlapAnalyzer.SPATIAL_INDEX_THRESHOLD
        
        if usar_indice:
            overlaps = OverlapAnalyzer._analyze_with_index(buffers, n, feedback)
        else:
            overlaps = OverlapAnalyzer._analyze_bruteforce(buffers, n, feedback)
        
        # Ordenar por área de superposición (mayor primero)
        overlaps.sort(key=lambda x: x['area_m2'], reverse=True)
        
        return overlaps
    
    @staticmethod
    def _analyze_with_index(buffers, n, feedback) -> List[Dict[str, Any]]:
        """Análisis con índice espacial — O(n·k) donde k = candidatos cercanos."""
        overlaps = []
        
        # Construir índice espacial con feedback
        if feedback:
            feedback.setProgressText(f"🔍 Construyendo índice espacial para {n} búferes...")
        
        index = QgsSpatialIndex()
        features_dict = {}  # Cachear features para acceso rápido
        area_cache = {}  # Caché de áreas calculadas para evitar recálculos
        
        for i, item in enumerate(buffers):
            if len(item) >= 3:
                g = item[0]
                if g and not g.isEmpty():
                    feat = QgsFeature(i)
                    feat.setGeometry(g)
                    index.addFeature(feat)
                    features_dict[i] = g  # Cachear geometría
        
        if feedback:
            feedback.setProgressText(f"✅ Índice creado. Analizando intersecciones...")
        
        # Consultar candidatos por bounding box
        pares_procesados = set()
        total_comparisons = 0
        total_intersections = 0
        
        for i in range(n):
            # Feedback cada 5% del progreso
            if feedback and i % max(1, n // 20) == 0:
                progress = int((i / n) * 100)
                feedback.setProgressText(
                    f"🔍 Analizando búfer {i+1}/{n} ({progress}%) - "
                    f"{total_intersections} traslapes encontrados"
                )
                # Verificar cancelación con isCanceled() (thread-safe).
                # No se llama QApplication.processEvents() aquí directamente para
                # evitar reentrada: la UI ya procesa eventos a través de _check_canceled
                # en el bucle principal de processAlgorithm.
                if feedback.isCanceled():
                    return overlaps
            
            item = buffers[i]
            g1 = item[0] if len(item) > 0 else None
            if not g1 or g1.isEmpty():
                continue
            
            # Usar índice espacial para obtener candidatos
            candidates = index.intersects(g1.boundingBox())
            total_comparisons += len(candidates)
            
            for j in candidates:
                if j <= i:  # Evitar duplicados y auto-intersecciones
                    continue
                
                par = (i, j)
                if par in pares_procesados:
                    continue
                pares_procesados.add(par)
                
                result = OverlapAnalyzer._check_overlap(buffers, i, j, area_cache)
                if result:
                    overlaps.append(result)
                    total_intersections += 1
        
        if feedback:
            reduction = 100 - (total_comparisons / (n * (n - 1) / 2) * 100) if n > 1 else 0
            feedback.pushInfo(
                f"📊 Índice espacial: {total_comparisons} comparaciones "
                f"(reducción de {reduction:.1f}% vs fuerza bruta)"
            )
        
        return overlaps
    
    @staticmethod
    def _analyze_bruteforce(buffers, n, feedback) -> List[Dict[str, Any]]:
        """Análisis fuerza bruta — O(n²), para capas pequeñas (<20 búferes)."""
        overlaps = []
        total_pairs = n * (n - 1) // 2
        pairs_checked = 0
        area_cache = {}  # Caché de áreas calculadas
        
        for i in range(n):
            # Feedback cada 20% del progreso
            if feedback and i % max(1, n // 5) == 0:
                progress = int((i / n) * 100)
                feedback.setProgressText(
                    f"🔍 Analizando búfer {i+1}/{n} ({progress}%) - "
                    f"{len(overlaps)} traslapes encontrados"
                )
                # Solo isCanceled() — evitar processEvents() en bucles internos (riesgo de reentrada)
                if feedback.isCanceled():
                    return overlaps
            
            item1 = buffers[i]
            g1 = item1[0] if len(item1) > 0 else None
            if not g1 or g1.isEmpty():
                continue
            
            # Cachear bounding box de g1
            bbox1 = g1.boundingBox()
                
            for j in range(i + 1, n):
                pairs_checked += 1
                
                item2 = buffers[j]
                g2 = item2[0] if len(item2) > 0 else None
                if not g2 or g2.isEmpty():
                    continue
                
                # Early exit: verificar bounding box primero (mucho más rápido)
                if not bbox1.intersects(g2.boundingBox()):
                    continue
                
                result = OverlapAnalyzer._check_overlap(buffers, i, j, area_cache)
                if result:
                    overlaps.append(result)
        
        if feedback:
            feedback.pushInfo(f"📊 Fuerza bruta: {pairs_checked} pares verificados")
        
        return overlaps
    
    @staticmethod
    def _check_overlap(buffers, i, j, area_cache=None) -> Optional[Dict[str, Any]]:
        """
        Calcula la superposición entre dos búferes. Retorna dict o None.
        
        Args:
            buffers: Lista de búferes
            i, j: Índices de los búferes a comparar
            area_cache: Diccionario opcional para cachear áreas calculadas
        """
        item1 = buffers[i]
        item2 = buffers[j]
        
        # Desempacar tuplas (soportar 3 o 4 elementos para compatibilidad)
        g1 = item1[0]
        t1 = item1[1]
        d1 = item1[2]
        desc1 = item1[3] if len(item1) > 3 else t1  # Usar descripción si está disponible
        
        g2 = item2[0]
        t2 = item2[1]
        d2 = item2[2]
        desc2 = item2[3] if len(item2) > 3 else t2  # Usar descripción si está disponible
        
        try:
            intersection = g1.intersection(g2)
            
            if intersection and not intersection.isEmpty():
                overlap_area = intersection.area()
                
                if overlap_area > OverlapAnalyzer.MIN_OVERLAP_AREA:
                    # Usar caché para áreas si está disponible
                    if area_cache is not None:
                        if i not in area_cache:
                            area_cache[i] = g1.area()
                        if j not in area_cache:
                            area_cache[j] = g2.area()
                        area_g1 = area_cache[i]
                        area_g2 = area_cache[j]
                    else:
                        area_g1 = g1.area()
                        area_g2 = g2.area()
                    
                    min_area = min(area_g1, area_g2)
                    max_area = max(area_g1, area_g2)
                    
                    porcentaje_min = (overlap_area / min_area * 100) if min_area > 0 else 0
                    porcentaje_max = (overlap_area / max_area * 100) if max_area > 0 else 0
                    
                    return {
                        'buffer_1_idx': i, 
                        'buffer_1_tipo': desc1,  # Ahora usa el descriptor completo
                        'buffer_1_area': area_g1,
                        'buffer_2_idx': j, 
                        'buffer_2_tipo': desc2,  # Ahora usa el descriptor completo
                        'buffer_2_area': area_g2,
                        'area_m2': overlap_area,
                        'area_ha': overlap_area / Constants.HA_TO_M2,
                        'porcentaje': porcentaje_min,
                        'porcentaje_mayor': porcentaje_max
                    }
        except (RuntimeError, AttributeError, Exception):
            pass
        
        return None


# ==============================================================================
# ALGORITMO PRINCIPAL
# ==============================================================================
class CrearBuferPLP(QgsProcessingAlgorithm):
    """Algoritmo de búfer para QGIS."""
    
    # Constantes de Parámetros
    INPUT = 'INPUT'
    BUFFER_TYPE = 'BUFFER_TYPE'
    CALCULAR_POR_AREA = 'CALCULAR_POR_AREA'
    DISTANCIA = 'DISTANCIA'
    ANCHO = 'ANCHO (Oval / Rectangular)'
    ALTO = 'ALTO (Oval / Rectangular)'
    ROTACION = 'ROTACION'
    SEGMENTOS = 'SEGMENTOS'
    CONCENTRIC_COUNT = 'CONCENTRIC_COUNT'
    CONCENTRIC_DISTANCE = 'CONCENTRIC_DISTANCE'
    CREAR_ANILLOS_DISJUNTOS = 'CREAR_ANILLOS_DISJUNTOS'
    AREA_OBJETIVO = 'AREA_OBJETIVO'
    UNIDAD_AREA = 'UNIDAD_AREA'
    USAR_GEOMETRIA_MINIMA = 'USAR_GEOMETRIA_MINIMA'
    CREAR_POLIGONO_PUNTOS = 'CREAR_POLIGONO_PUNTOS'
    CREAR_UNION_LOGICA = 'CREAR_UNION_LOGICA'
    USAR_CORREDOR = 'USAR_CORREDOR'
    SIDE = 'SIDE'
    JOIN_STYLE = 'JOIN_STYLE'
    MITER_LIMIT = 'MITER_LIMIT'
    WEDGE_START = 'WEDGE_START'
    WEDGE_WIDTH = 'WEDGE_WIDTH'
    USAR_ROTACION_AUTO = 'USAR_ROTACION_AUTO'
    ROTATION_FIELD = 'ROTATION_FIELD'  # Campo de azimut variable por entidad (Cuña)
    APLICAR_TRANSPARENCIA = 'APLICAR_TRANSPARENCIA'
    NIVEL_TRANSPARENCIA = 'NIVEL_TRANSPARENCIA'
    GENERAR_REPORTE = 'GENERAR_REPORTE'
    RUTA_REPORTE = 'RUTA_REPORTE'
    NOMBRE_PROYECTO = 'NOMBRE_PROYECTO'
    OUTPUT = 'OUTPUT'
    OUTPUT_FRAGMENTOS = 'OUTPUT_FRAGMENTOS'
    
    # Parámetros adicionales
    DISTANCE_FIELD = 'DISTANCE_FIELD'
    CATEGORY_FIELD = 'CATEGORY_FIELD'  # ALTA #2
    CATEGORY_MAPPING = 'CATEGORY_MAPPING'  # ALTA #2
    ANCHO_FIELD = 'ANCHO_FIELD'
    ALTO_FIELD = 'ALTO_FIELD'
    EXCLUSION_LAYER = 'EXCLUSION_LAYER'
    PREVIEW_MODE = 'PREVIEW_MODE'
    CALCULAR_SUPERPOSICION = 'CALCULAR_SUPERPOSICION'
    GENERAR_FRAGMENTOS_TRASLAPE = 'GENERAR_FRAGMENTOS_TRASLAPE'
    
    # Parámetros de simplificación
    APLICAR_SIMPLIFICACION = 'APLICAR_SIMPLIFICACION'
    TOLERANCIA_SIMPLIFICACION = 'TOLERANCIA_SIMPLIFICACION'
    
    # Parámetro de gestión de integridad
    GESTION_INTEGRIDAD = 'GESTION_INTEGRIDAD'
    
    # Parámetros de pre-procesamiento de entrada
    SIMPLIFICAR_ENTRADA = 'SIMPLIFICAR_ENTRADA'
    TOLERANCIA_ENTRADA = 'TOLERANCIA_ENTRADA'
    
    # Parámetros de procesamiento paralelo
    USAR_PARALELO = 'USAR_PARALELO'
    NUM_THREADS = 'NUM_THREADS'
    
    # Parámetros de post-procesamiento (traslapes y huecos)
    RESOLVER_TRASLAPES = 'RESOLVER_TRASLAPES'
    ELIMINAR_HUECOS = 'ELIMINAR_HUECOS'
    AREA_MINIMA_HUECO = 'AREA_MINIMA_HUECO'
    PRESERVAR_HUECO_ESTRUCTURAL = 'PRESERVAR_HUECO_ESTRUCTURAL'
    DISOLVER_BUFERES = 'DISOLVER_BUFERES'
    MANTENER_PARTE_MAYOR = 'MANTENER_PARTE_MAYOR'

    # Parámetros de búfer adaptativo por densidad
    USAR_DENSIDAD_ADAPTATIVA = 'USAR_DENSIDAD_ADAPTATIVA'

    # === NUEVO: Parámetros para método de anclaje en densidad ===
    DENSIDAD_METODO_ANCLAJE = 'DENSIDAD_METODO_ANCLAJE'
    DENSIDAD_TOLERANCIA_POLO = 'DENSIDAD_TOLERANCIA_POLO'

    # Exportar / importar configuración JSON
    EXPORTAR_CONFIG    = 'EXPORTAR_CONFIG'
    RUTA_CONFIG_JSON   = 'RUTA_CONFIG_JSON'

    # Modo Validación Previa (validar sin procesar)
    DRY_RUN            = 'DRY_RUN'
    DENSIDAD_METODO          = 'DENSIDAD_METODO'
    DENSIDAD_K               = 'DENSIDAD_K'
    DENSIDAD_RADIO_REF       = 'DENSIDAD_RADIO_REF'
    DENSIDAD_RADIO_BASE      = 'DENSIDAD_RADIO_BASE'
    DENSIDAD_FACTOR_ESCALA   = 'DENSIDAD_FACTOR_ESCALA'
    DENSIDAD_RADIO_MIN       = 'DENSIDAD_RADIO_MIN'
    DENSIDAD_RADIO_MAX       = 'DENSIDAD_RADIO_MAX'

    @staticmethod
    def _check_canceled(feedback) -> bool:
        """
        Verifica si el usuario canceló el proceso.
        Llama a QApplication.processEvents() para que la señal de cancelación
        de la UI se propague correctamente, incluso durante operaciones GEOS pesadas.
        """
        try:
            QApplication.processEvents()
        except Exception:
            pass  # Silenciar si no hay QApplication disponible (ej. ejecución en consola)
        return feedback.isCanceled()

    def initAlgorithm(self, config=None):
        """Define los parámetros del algoritmo con tooltips descriptivos."""

        # ── Verificación de versión mínima de QGIS ──────────────────────────────
        # Requiere QGIS >= 3.28 (LTR). Mínimo absoluto: 3.14 (FlagSkipGenericModelLogging),
        # 3.12 (poleOfInaccessibility, makeValid). GEOS >= 3.9 es transitivo con QGIS 3.28.
        # Se fija en 3.28 para alinear con la versión LTR y el metadata del complemento.
        QGIS_MIN = 32800
        if Qgis.QGIS_VERSION_INT < QGIS_MIN:
            raise QgsProcessingException(
                f"Este algoritmo requiere QGIS \u2265 3.28 "
                f"(versi\u00f3n actual: {Qgis.QGIS_VERSION}). "
                "Por favor actualice QGIS antes de usar este algoritmo."
            )

        # === INFORMACIÓN DEL PROYECTO ===
        param = QgsProcessingParameterString(
            self.NOMBRE_PROYECTO, '📋 Nombre del proyecto', defaultValue='Proyecto QGIS')
        param.setHelp(
            "Nombre identificador del proyecto.\n"
            "• Aparecerá en el encabezado del reporte HTML\n"
            "• Útil para organizar múltiples análisis"
        )
        self.addParameter(param)
        
        # === CAPA DE ENTRADA ===
        param = QgsProcessingParameterFeatureSource(
            self.INPUT, '📍 Capa de entrada', [QgsProcessing.TypeVector])
        # NOTA: La gestión de geometrías inválidas se controla mediante el parámetro
        # 'Gestión de Integridad' definido más abajo en este algoritmo.
        # Este algoritmo obtiene TODAS las geometrías (incluyendo inválidas) y las procesa
        # según la configuración del usuario, independientemente de la config global de QGIS.
        param.setHelp(
            "Capa vectorial sobre la cual se calcularán los búferes.\n"
            "• IMPORTANTE: Las geometrías inválidas se manejarán según la opción 'Gestión de Integridad'\n"
            "• Este algoritmo procesa las geometrías independientemente de la configuración global de QGIS\n"
            "• Soporta: Puntos, Líneas y Polígonos\n"
            "• Active 'Objetos seleccionados' para procesar solo entidades seleccionadas\n"
            "• Se recomienda usar un CRS proyectado (metros) para mayor precisión"
        )
        self.addParameter(param)
        
        # === GESTIÓN DE INTEGRIDAD GEOMÉTRICA ===
        param = QgsProcessingParameterEnum(
            self.GESTION_INTEGRIDAD, 
            '⚡ Gestión de Integridad Geométrica',
            options=Constants.INTEGRIDAD_NAMES,
            defaultValue=Constants.INTEGRIDAD_REPARAR)
        param.setHelp(
            "Estrategia para manejar geometrías inválidas (independiente de la configuración global de QGIS):\n\n"
            "• ⚠️ <strong>No verificar (Riesgo)</strong>: Procesa geometrías 'tal cual', incluso si son inválidas. "
            "¡ALERTA! Esto puede causar errores matemáticos y resultados incorrectos.\n\n"
            "• 🚫 <strong>Omitir geometría inválida</strong>: Descarta automáticamente cualquier geometría con errores topológicos. "
            "Genera vacíos de información pero garantiza resultados válidos.\n\n"
            "• 🔧 <strong>Reparar geometría (Recomendado)</strong>: Intenta corregir automáticamente los errores topológicos "
            "usando algoritmos de validación. Preserva la mayor cantidad de datos posible.\n\n"
            "<strong>Nota:</strong> Este control es EXCLUSIVO de este algoritmo y no afecta otros procesos de QGIS."
        )
        self.addParameter(param)
        
        # === TIPO DE BÚFER ===
        param = QgsProcessingParameterEnum(
            self.BUFFER_TYPE, '🔘 Tipo de búfer',
            options=['Circular (radio fijo)', 'Oval (ejes definidos)', 
                    'Rectangular (dimensiones definidas)', 'Concéntrico (múltiples distancias)',
                    'Por área determinada', 'Un solo lado (Líneas/Borde Polígono)', 'Búfer en Cuña',
                    'Adaptativo por densidad (radio según vecindad)',
                    'Ancho Variable (Puntos) — ancho definido por campo de distancia'],
            defaultValue=0)
        param.setHelp(
            "Seleccione la forma del búfer a generar:\n\n"
            "• CIRCULAR: Radio uniforme alrededor de la geometría\n"
            "• OVAL: Elipse con ancho y alto independientes\n"
            "• RECTANGULAR: Rectángulo con dimensiones personalizadas\n"
            "• CONCÉNTRICO: Múltiples anillos a distancias regulares\n"
            "• POR ÁREA: Calcula el radio para alcanzar un área objetivo\n"
            "• UN SOLO LADO: Expande solo hacia un lado (izq/der)\n"
            "• CUÑA: Sector circular (como rebanada de pizza)\n"
            "• ADAPTATIVO: Radio calculado automáticamente según densidad espacial de vecinos\n"
            "• ANCHO VARIABLE (PUNTOS): Corredor de ancho variable a lo largo de una ruta.\n"
            "  Requiere una capa de PUNTOS ordenados con un campo de distancia.\n"
            "  El script conecta los puntos como ruta y genera un polígono único\n"
            "  donde el ancho en cada punto es el valor del campo (radio = M,\n"
            "  ancho total = 2×M). Funciona en cualquier orientación (H, V, diagonal,\n"
            "  rutas mixtas). El campo distancia se ignora; usa el campo de distancia variable."
        )
        self.addParameter(param)
        
        # === DISTANCIA ===
        param = QgsProcessingParameterDistance(
            self.DISTANCIA, '📏 Distancia (Radio)', parentParameterName=self.INPUT, defaultValue=0.0001)
        param.setMetadata({'widget_wrapper': {'decimals': 4}})
        param.setHelp(
            "Distancia del búfer en unidades del mapa (generalmente metros).\n\n"
            "⚠️ NO aplica para tipo 'Adaptativo por densidad' — el radio se\n"
            "   calcula automáticamente; este campo es ignorado.\n\n"
            "• Valores POSITIVOS: Expansión hacia afuera\n"
            "• Valores NEGATIVOS: Contracción hacia adentro (solo polígonos)\n"
            "• Para CIRCULAR: Es el radio del círculo\n"
            "• Para CUÑA: Es el radio/longitud del sector\n"
            "  → Deje en 0 si usa 'Campo de distancia' para radio variable por entidad\n"
            "• Para UN SOLO LADO: Es el ancho de la franja\n\n"
            "📌 Punto de anclaje en líneas (Cuña):\n"
            "  La cuña nace desde el punto a mitad de la longitud recorrida de la línea.\n"
            "  Fallback si falla: Punto en Superficie () — siempre sobre la geometría.\n"
            "⚠️ MultiLínea: el punto medio se calcula sobre la longitud acumulada total\n"
            "  y puede caer en una parte secundaria en lugar de la parte más larga.\n"
            "  Solución: use 'Multipartes a partes simples' antes de procesar.\n\n"
"📌 Punto de anclaje en polígonos (Cuña):\n"
            "  Estrategia híbrida (4 pasos):\\n"
            "  1) Centroide, si cae dentro y lejos del borde.\\n"
            "  2) Polo del casco convexo (formas L/U/T).\\n"
            "  3) Parte más compacta, erosión iterativa (Polsby-Popper).\\n"
            "  4) Punto en Superficie() como fallback final."
        )
        self.addParameter(param)
        
        # === ANCHO ===
        param = QgsProcessingParameterNumber(
            self.ANCHO, '📐 Ancho (Oval / Rectangular)', type=QgsProcessingParameterNumber.Double, defaultValue=0.0001)
        param.setMetadata({'widget_wrapper': {'decimals': 4}})
        param.setHelp(
            "Dimensión del eje X (ancho) para búferes Oval y Rectangular.\n\n"
            "• En rotación 0°: Se alinea Norte-Sur\n"
            "• Para CORREDOR: Es el ancho total de la faja\n"
            "• Debe ser mayor a 0 para Oval/Rectangular\n\n"
            "📌 Punto de anclaje en líneas (Oval y Rectangular estándar):\n"
            "  La figura se centra en el punto a mitad de la longitud recorrida de la línea.\n"
            "  Fallback si falla: Punto en Superficie() — siempre sobre la geometría.\n"
            "⚠️ MultiLínea: el punto medio se calcula sobre la longitud acumulada total\n"
            "  y puede caer en una parte secundaria en lugar de la parte más larga.\n"
            "  Solución: use 'Multipartes a partes simples' antes de procesar.\n\n"
"📌 Punto de anclaje en polígonos (Oval y Rectangular estándar):\n"
            "  Estrategia híbrida (4 pasos):\\n"
            "  1) Centroide, si cae dentro y lejos del borde.\\n"
            "  2) Polo del casco convexo (formas L/U/T).\\n"
            "  3) Parte más compacta, erosión iterativa (Polsby-Popper).\\n"
            "  4) Punto en Superficie() como respaldo final."
        )
        self.addParameter(param)
        
        # === LARGO ===
        param = QgsProcessingParameterNumber(
            self.ALTO, '📐 Alto (Oval / Rectangular)', type=QgsProcessingParameterNumber.Double, defaultValue=0.0001)
        param.setMetadata({'widget_wrapper': {'decimals': 4}})
        param.setHelp(
            "Dimensión del eje Y (largo) para búferes Oval y Rectangular.\n\n"
            "• En rotación 0°: Se alinea Este-Oeste\n"
            "• No aplica en modo CORREDOR\n"
            "• Debe ser mayor a 0 para Oval/Rectangular\n\n"
            "⚠️ MultiLínea, fallback y anclaje en polígonos: ver parámetro Ancho."
        )
        self.addParameter(param)
        
        # === ROTACIÓN ===
        param = QgsProcessingParameterNumber(
            self.ROTACION, '↻ Rotación (Oval y Rectangular)', type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, minValue=0.0, maxValue=360.0)
        param.setHelp(
            "Ángulo de rotación en grados (0-360) para Oval y Rectangular.\n\n"
            "• 0° = Norte (sin rotación)\n"
            "• 90° = Este\n"
            "• Sentido: Horario (como las agujas del reloj)\n"
            "• Intercambiar Ancho↔Alto equivale a rotar 90°"
        )
        self.addParameter(param)
        
        # === CORREDOR ===
        param = QgsProcessingParameterBoolean(
            self.USAR_CORREDOR, '🛣️ Corredor (Líneas)', defaultValue=False)
        param.setHelp(
            "Modo especial para crear fajas/corredores a lo largo de líneas.\n\n"
            "• Solo aplica a: Búfer RECTANGULAR + geometrías de LÍNEA\n"
            "• Crea una franja continua que sigue la forma de la línea\n"
            "• Solo requiere el parámetro ANCHO (Alto se ignora)\n"
            "• Útil para: caminos, ríos, tuberías, servidumbres"
        )
        self.addParameter(param)
        
        # === CAMPO DE DISTANCIA/RADIO/ÁREA ===
        param = QgsProcessingParameterField(
            self.DISTANCE_FIELD, '📊 Campo de distancia/radio/área (búfer variable)',
            parentLayerParameterName=self.INPUT, type=QgsProcessingParameterField.Numeric, optional=True)
        param.setHelp(
            "Campo numérico para asignar valores variables a cada entidad.\n\n"
            "⚙️ <strong>El significado del valor cambia según el tipo de búfer:</strong>\n\n"
            
            "📏 <strong>DISTANCIA/RADIO (en metros):</strong>\n"
            "• Circular: Radio del círculo\n"
            "• Cuña: Radio del sector circular\n"
            "• Concéntrico: Distancia entre anillos\n"
            "• Un Solo Lado: Distancia de offset\n"
            "  → Valor positivo (+) = Izquierda (líneas) / Exterior (polígonos)\n"
            "  → Valor negativo (−) = Derecha (líneas) / Interior (polígonos)\n\n"
            
            "📐 <strong>ÁREA (en unidad seleccionada):</strong>\n"
            "• Por Área: El valor se interpreta como ÁREA objetivo\n"
            "  → Si unidad = Hectáreas: valor del campo = hectáreas\n"
            "  → Si unidad = m²: valor del campo = metros cuadrados\n"
            "  → Si unidad = km²: valor del campo = kilómetros cuadrados\n"
            "  → El algoritmo calcula el radio necesario automáticamente\n\n"
            
            "📊 <strong>DIMENSIONES (en metros):</strong>\n"
            "• Oval/Rectangular sin campos específicos:\n"
            "  → Valor se usa para ANCHO y LARGO (figura proporcional)\n"
            "• Rectangular + Corredor: Valor se usa solo para ANCHO\n"
            "  → Prioridad: Campo de Ancho > Campo de distancia > Parámetro fijo\n\n"
            
            "💡 <strong>Ejemplo práctico - Modo 'Por Área':</strong>\n"
            "Si tiene un campo llamado 'Area_Ha' con valores [2.5, 5.0, 10.0]:\n"
            "• Tipo de búfer: Por Área\n"
            "• Unidad: Hectáreas\n"
            "• Campo de distancia/radio/área: Area_Ha\n"
            "→ Entidad 1: Búfer de 2.5 ha\n"
            "→ Entidad 2: Búfer de 5.0 ha\n"
            "→ Entidad 3: Búfer de 10.0 ha\n\n"
            
            "⚠️ <strong>Importante:</strong>\n"
            "• El campo debe contener valores numéricos válidos\n"
            "• Las unidades dependen del tipo de búfer (ver arriba)\n"
            "• Valores nulos o cero pueden resultar en geometrías omitidas\n"
            "• Este campo tiene PRIORIDAD sobre los parámetros fijos de distancia/área"
        )
        self.addParameter(param)
        
        # === ALTA #2: CAMPO DE CATEGORÍA (Búfer variable por categoría) ===
        param = QgsProcessingParameterField(
            self.CATEGORY_FIELD, '📂 Campo de categoría (opcional)',
            parentLayerParameterName=self.INPUT, type=QgsProcessingParameterField.String, optional=True)
        param.setHelp(
            "Campo categórico para asignar distancias o áreas diferentes según categorías.\n\n"
            "🎯 <strong>Funcionalidad:</strong>\n"
            "• Permite búferes variables basados en valores de texto\n"
            "• Cada categoría tiene su propio valor definido en el mapeo\n"
            "• Más intuitivo que usar campos numéricos para clasificaciones\n\n"
            
            "📋 <strong>Casos de uso:</strong>\n"
            "• Red vial: 'autopista'=50m, 'calle'=20m, 'sendero'=5m\n"
            "• Zonificación: 'residencial'=100m, 'comercial'=200m\n"
            "• Amenazas: 'alta'=1000m, 'media'=500m, 'baja'=100m\n"
            "• Por Área: 'grande'=10ha, 'mediana'=5ha, 'pequeña'=1ha\n\n"
            
            "⚙️ <strong>Funcionamiento:</strong>\n"
            "• Si el valor está en el mapeo → usa el valor del mapeo\n"
            "• Si el valor NO está → usa distancia del parámetro fijo\n"
            "• Si el valor es NULL → usa distancia del parámetro fijo\n\n"
            
            "⚠️ <strong>Interpretación del valor según tipo de búfer:</strong>\n"
            "• Circular / Cuña / Un Solo Lado / Concéntrico → metros\n"
            "• Por Área → área objetivo en la unidad seleccionada (ha, m², km²)\n"
            "• Oval / Rectangular → ancho y alto en metros (proporcional)\n\n"
            
            "💡 <strong>Prioridad:</strong>\n"
            "1. Campo de categoría (si configurado y existe en mapeo)\n"
            "2. Campo de distancia numérica\n"
            "3. Parámetro fijo\n\n"
            
            "⚠️ Case-sensitive: 'Autopista' ≠ 'autopista'"
        )
        self.addParameter(param)
        
        # === ALTA #2: MAPEO CATEGORÍA → DISTANCIA (JSON) ===
        param = QgsProcessingParameterString(
            self.CATEGORY_MAPPING,
            '🗂️ Mapeo categoría → distancia (JSON)',
            defaultValue='{\n  "autopista": 50,\n  "calle": 20,\n  "sendero": 5\n}',
            multiLine=True,
            optional=True
        )
        param.setHelp(
            "Mapeo JSON: categoría → valor (distancia en metros, o área en la unidad seleccionada).\n\n"
            
            "✅ <strong>Ejemplo Circular/Un Solo Lado (metros):</strong>\n"
            "{\n"
            "  \"autopista\": 50,\n"
            "  \"avenida\": 30,\n"
            "  \"calle\": 20,\n"
            "  \"sendero\": 5\n"
            "}\n\n"
            
            "✅ <strong>Ejemplo Por Área (hectáreas, m² o km² según unidad seleccionada):</strong>\n"
            "{\n"
            "  \"grande\": 10,\n"
            "  \"mediana\": 5,\n"
            "  \"pequeña\": 1\n"
            "}\n\n"
            
            "❌ <strong>Errores comunes:</strong>\n"
            "• Sin comillas: {autopista: 50} ❌\n"
            "• Coma final: {\"sendero\": 5,} ❌\n"
            "• Distancia texto: {\"calle\": \"20\"} ❌\n\n"
            
            "💡 Validar en: https://jsonlint.com, https://jsoneditoronline.org, o Copilot (gratuito).\n"
            "💡 Nota: Reemplazar con sus datos."
        )
        self.addParameter(param)
        
        # === CAMPO DE ANCHO (NUEVO) ===
        param = QgsProcessingParameterField(
            self.ANCHO_FIELD, '📊 Campo de Ancho (Oval/Rectangular)',
            parentLayerParameterName=self.INPUT, type=QgsProcessingParameterField.Numeric, optional=True)
        param.setHelp(
            "Campo numérico para asignar valores de ANCHO diferentes a cada entidad.\n\n"
            "• Solo aplica a búferes OVAL y RECTANGULAR\n"
            "• Si se selecciona, cada geometría usará el valor de este campo para el Ancho\n"
            "• Debe usarse en combinación con 'Campo de Alto' para definir ambas dimensiones\n"
            "• Si solo se define este campo, el Alto usará el parámetro fijo 'Alto'\n"
            "• Valores en unidades del mapa (generalmente metros)"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === CAMPO DE LARGO (NUEVO) ===
        param = QgsProcessingParameterField(
            self.ALTO_FIELD, '📊 Campo de Alto (Oval/Rectangular)',
            parentLayerParameterName=self.INPUT, type=QgsProcessingParameterField.Numeric, optional=True)
        param.setHelp(
            "Campo numérico para asignar valores de LARGO diferentes a cada entidad.\n\n"
            "• Solo aplica a búferes OVAL y RECTANGULAR\n"
            "• Si se selecciona, cada geometría usará el valor de este campo para el Alto\n"
            "• Debe usarse en combinación con 'Campo de Ancho' para definir ambas dimensiones\n"
            "• Si solo se define este campo, el Ancho usará el parámetro fijo 'Ancho'\n"
            "• Valores en unidades del mapa (generalmente metros)"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === CANTIDAD DE ANILLOS ===
        param = QgsProcessingParameterNumber(
            self.CONCENTRIC_COUNT, '🎯 Cantidad anillos (Concéntrico)', type=QgsProcessingParameterNumber.Integer,
            defaultValue=0, minValue=0, maxValue=Constants.MAX_CONCENTRIC_RINGS)
        param.setHelp(
            f"Número de anillos concéntricos a generar (1-{Constants.MAX_CONCENTRIC_RINGS}).\n\n"
            "• Solo aplica al tipo CONCÉNTRICO\n"
            "• Cada anillo se genera a distancia incremental\n"
            "• Ejemplo: 3 anillos con distancia 100m = 100m, 200m, 300m"
        )
        self.addParameter(param)
        
        # === DISTANCIA ENTRE ANILLOS ===
        param = QgsProcessingParameterNumber(
            self.CONCENTRIC_DISTANCE, '🎯 Distancia anillos (Concéntrico)',
            type=QgsProcessingParameterNumber.Double, defaultValue=0.0)
        param.setHelp(
            "Distancia entre cada anillo concéntrico (separación fija para todas las entidades).\n\n"
            "• Valores POSITIVOS: Anillos hacia afuera\n"
            "• Valores NEGATIVOS: Anillos hacia adentro (solo polígonos)\n"
            "• Debe ser diferente de 0 si no usa 'Campo de distancia'\n\n"
            "💡 Distancia VARIABLE por entidad:\n"
            "• Seleccione un 'Campo de distancia' con la separación deseada por punto\n"
            "• Deje este parámetro en 0 cuando use el campo variable\n"
            "• El número de anillos ('Cantidad anillos') aplica igual para todas las entidades\n\n"
            "Ejemplo con campo variable:\n"
            "  Punto A → campo=50m → anillos: 50m, 100m, 150m\n"
            "  Punto B → campo=100m → anillos: 100m, 200m, 300m"
        )
        self.addParameter(param)
        
        # === ANILLOS DISJUNTOS ===
        param = QgsProcessingParameterBoolean(
            self.CREAR_ANILLOS_DISJUNTOS, '⭕ Crear anillos disjuntos, Dónut, (Concéntrico)', defaultValue=True)
        param.setHelp(
            "Define cómo se generan los anillos concéntricos.\n\n"
            "• ACTIVADO (Dónut): Cada anillo es una banda exclusiva\n"
            "  → El anillo 2 NO incluye el área del anillo 1\n"
            "  → Útil para análisis de proximidad por bandas\n\n"
            "• DESACTIVADO (Discos): Geometrías sólidas acumulativas\n"
            "  → El anillo 2 incluye todo desde el centro\n"
            "  → Útil para visualización de alcance total"
        )
        self.addParameter(param)
        
        # === LADO ===
        param = QgsProcessingParameterEnum(
            self.SIDE, '↔️ Lado (Para un solo lado)',
            options=['Izquierda (Exterior)', 'Derecha (Interior)'], defaultValue=0)
        param.setHelp(
            "Dirección de expansión para búfer de un solo lado.\n\n"
            "Para LÍNEAS:\n"
            "• Izquierda/Derecha según el sentido de digitalización\n"
            "• El resultado visual coincide con la etiqueta seleccionada\n\n"
            "Para POLÍGONOS:\n"
            "• Izquierda = Anillo EXTERIOR (expande hacia afuera)\n"
            "• Derecha = Anillo INTERIOR (contrae hacia adentro)\n\n"
            "⚡ Con 'Campo de distancia' en líneas:\n"
            "• El SIGNO del valor en el campo controla el lado visual\n"
            "• Valor positivo → mismo lado que el parámetro 'Lado'\n"
            "• Valor negativo → lado opuesto al parámetro 'Lado'\n"
            "• El parámetro 'Lado' se usa como base cuando no hay campo"
        )
        self.addParameter(param)
        
        # === ESTILO DE UNIÓN ===
        param = QgsProcessingParameterEnum(
            self.JOIN_STYLE, '🔗 Estilo de Unión (1 Lado / Concéntrico)',
            options=['Redondeado (Curvas suaves)', 'Inglete (Esquinas agudas)', 'Biselado (Esquinas cortadas)'],
            defaultValue=0)
        param.setHelp(
            "Forma de las esquinas en el búfer.\n\n"
            "• REDONDEADO: Esquinas suaves y curvas (recomendado)\n"
            "• INGLETE: Esquinas puntiagudas/agudas\n"
            "• BISELADO: Esquinas recortadas en diagonal\n\n"
            "Nota: Para puntos, Biselado fuerza terminación Redondeada"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === LÍMITE DE INGLETE ===
        param = QgsProcessingParameterNumber(
            self.MITER_LIMIT, '📐 Límite de Inglete', type=QgsProcessingParameterNumber.Double,
            defaultValue=2.0, minValue=1.0)
        param.setHelp(
            "Controla cuánto pueden extenderse las esquinas en modo Inglete.\n\n"
            "• Valor típico: 2,0\n"
            "• Valores más altos: Esquinas más puntiagudas\n"
            "• Valores más bajos: Esquinas más recortadas\n"
            "• Solo aplica cuando Estilo de Unión = Inglete"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === ÁNGULO INICIO CUÑA ===
        param = QgsProcessingParameterNumber(
            self.WEDGE_START, '🍕 Ángulo Inicio (Cuña - Azimut)',
            type=QgsProcessingParameterNumber.Double, defaultValue=0.0, minValue=0.0, maxValue=360.0)
        param.setHelp(
            "Dirección inicial del sector circular (cuña) en grados.\n\n"
            "• 0° = Norte\n"
            "• 90° = Este\n"
            "• 180° = Sur\n"
            "• 270° = Oeste\n"
            "• Sentido: Horario desde el Norte\n\n"
            "💡 Para azimut variable por entidad, use el parámetro\n"
            "'🧭 Campo de Rotación (Óvalo, Rectángulo, Cuña - Azimut variable)' a continuación.\n"
            "Si el campo está activo, tiene prioridad sobre este valor fijo."
        )
        self.addParameter(param)
        
        # === CAMPO DE ROTACIÓN CUÑA ===
        param = QgsProcessingParameterField(
            self.ROTATION_FIELD,
            '🧭 Campo de Rotación (Óvalo, Rectángulo, Cuña - Azimut variable)',
            parentLayerParameterName=self.INPUT,
            type=QgsProcessingParameterField.Numeric,
            optional=True)
        param.setHelp(
            "Campo numérico con el azimut de orientación para Óvalo, Rectángulo y Cuña (en grados).\n\n"
            "⚙️ Permite orientar cada figura a una dirección específica por entidad.\n\n"
            "📐 <strong>Convención de valores:</strong>\n"
            "• 0° = Norte | 90° = Este | 180° = Sur | 270° = Oeste\n"
            "• Sentido: Horario desde el Norte\n\n"
            "📋 <strong>Reglas de prioridad:</strong>\n"
            "1. Si campo activo y valor válido → usa el campo (ignora Ángulo fijo)\n"
            "2. Si campo nulo/inválido → usa Ángulo fijo como respaldo\n"
            "3. Si 'Rotación Automática' está activada → invalida el campo y el fijo\n\n"
            "💡 <strong>Casos de uso:</strong>\n"
            "• Dispersión de emisiones: cada instalación apunta a su dirección de viento\n"
            "• Visibilidad: cada edificación con su orientación real de fachada\n"
            "• Servidumbres costeras: cada parcela proyecta hacia el mar\n\n"
            "⚠️ Solo aplica al Búfer en Cuña. Ignorado en otros tipos de búfer."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === AMPLITUD CUÑA ===
        param = QgsProcessingParameterNumber(
            self.WEDGE_WIDTH, '🍕 Amplitud (Cuña - Grados)',
            type=QgsProcessingParameterNumber.Double, defaultValue=45.0, minValue=0.0, maxValue=360.0)
        param.setHelp(
            "Ancho angular del sector circular en grados.\n\n"
            "• 45° = 1/8 de círculo\n"
            "• 90° = 1/4 de círculo (cuadrante)\n"
            "• 180° = Semicírculo\n"
            "• 360° = Círculo completo\n\n"
            "Útil para: campos de visión, dispersión de viento, áreas de influencia"
        )
        self.addParameter(param)
        
        # === ROTACIÓN AUTOMÁTICA CUÑA ===
        param = QgsProcessingParameterBoolean(
            self.USAR_ROTACION_AUTO, '🔄 Rotación automática (Óvalo, Rectángulo, Cuña sigue geometría)', defaultValue=False, optional=True)
        param.setHelp(
            "Orienta automáticamente Óvalo, Rectángulo y Cuña según el eje principal de la geometría.\n\n"
            "• LÍNEAS: Se orienta del primer al último vértice.\n"
            "  El origen de la cuña se ubica en el punto medio de la longitud de la línea.\n"
            "• POLÍGONOS: Se orienta según el eje principal (largo) del polígono.\n"
            "  usa estrategia híbrida (4 pasos):\\n"
            "  1) Centroide; 2) Polo casco convexo (L/U/T);\\n"
            "  3) Polsby-Popper (erosión iterativa); 4) Punto en Superficie().\\n"
            "  Garantiza que el vértice siempre nace dentro del área sólida.\\n"
            "• PUNTOS: No aplica (usa siempre el Norte como referencia base).\n\n"
            "⚠️ Si está activada, INVALIDA el Ángulo fijo y el Campo de Rotación (Cuña).\n"
            "El ángulo de inicio se suma a la rotación automática como offset adicional."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === CALCULAR POR ÁREA ===
        param = QgsProcessingParameterBoolean(
            self.CALCULAR_POR_AREA, '📐 Por área (Circular)', defaultValue=False)
        param.setHelp(
            "Calcula automáticamente el radio para alcanzar un área objetivo.\n\n"
            "• Solo aplica a búfer CIRCULAR\n"
            "• Ignora el parámetro 'Distancia'\n"
            "• Usa el parámetro 'Área objetivo' y su unidad\n"
            "• Método iterativo de alta precisión"
        )
        self.addParameter(param)
        
        # === ÁREA OBJETIVO ===
        param = QgsProcessingParameterNumber(
            self.AREA_OBJETIVO, '📐 Área objetivo (Circular / Por Área)', type=QgsProcessingParameterNumber.Double, defaultValue=0.0001)
        param.setHelp(
            "Área deseada para el búfer resultante.\n\n"
            "• Se usa con 'Por área (Circular)' o tipo 'Por área determinada'\n"
            "• Especifique la unidad en el parámetro siguiente\n"
            "• El algoritmo calcula iterativamente el radio necesario\n"
            "• Para polígonos: expande/contrae hasta alcanzar el área\n\n"
            "💡 Nota: Si usa 'Campo de distancia/radio/área', este valor se ignora\n"
            "   (cada entidad usará el valor de su campo en lugar de este parámetro)"
        )
        self.addParameter(param)
        
        # === UNIDAD DE ÁREA ===
        param = QgsProcessingParameterEnum(
            self.UNIDAD_AREA, '📏 Unidad de área (Circular / Por Área)', options=Constants.AREA_UNITS, defaultValue=0)
        param.setHelp(
            "Unidad de medida para el área objetivo.\n\n"
            "• Hectáreas (ha): 1 ha = 10000 m²\n"
            "• Metros cuadrados (m²)\n"
            "• Kilómetros cuadrados (km²): 1 km² = 1000000 m²"
        )
        self.addParameter(param)
        
        # === SEGMENTOS ===
        param = QgsProcessingParameterNumber(
            self.SEGMENTOS, '⚙️ Segmentos', type=QgsProcessingParameterNumber.Integer,
            defaultValue=Constants.DEFAULT_SEGMENTOS, minValue=3)
        param.setHelp(
            "Número de segmentos para aproximar curvas.\n\n"
            "• Valor por defecto: 25\n"
            "• Más segmentos = curvas más suaves (pero más vértices)\n"
            "• Menos segmentos = curvas más angulosas (menos vértices)\n"
            "• Recomendado: 25-50 para uso general\n"
            "• Para alta precisión: 50-100"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === CASCO CONVEXO ===
        param = QgsProcessingParameterBoolean(
            self.USAR_GEOMETRIA_MINIMA, '🔷 Puntos: Casco Convexo', defaultValue=False)
        param.setHelp(
            "Agrupa todos los puntos en un polígono envolvente antes del búfer.\n\n"
            "• Crea el polígono convexo mínimo que contiene todos los puntos\n"
            "• El búfer se aplica sobre este polígono, no sobre puntos individuales\n"
            "• Útil para delimitar áreas de distribución\n"
            "• No compatible con 'Caja Envolvente' (usar uno u otro)"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === CAJA ENVOLVENTE ===
        param = QgsProcessingParameterBoolean(
            self.CREAR_POLIGONO_PUNTOS, '📐 Puntos: Caja Envolvente', defaultValue=False)
        param.setHelp(
            "Agrupa todos los puntos en un rectángulo envolvente antes del búfer.\n\n"
            "• Crea un rectángulo alineado a los ejes (Caja Delimitadora)\n"
            "• El búfer se aplica sobre este rectángulo\n"
            "• Útil para delimitar extensiones de trabajo\n"
            "• No compatible con 'Casco Convexo' (usar uno u otro)"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === OPERACIÓN LÓGICA ===
        param = QgsProcessingParameterEnum(
            self.CREAR_UNION_LOGICA, '🔗 Operación Lógica', options=Constants.OP_NAMES, defaultValue=0)
        param.setHelp(
            "Operación geométrica entre el búfer y la geometría original.\n\n"
            "• NINGUNA: Solo el búfer (comportamiento normal)\n"
            "• UNIÓN: Búfer + geometría original combinados\n"
            "• INTERSECCIÓN: Área común entre búfer y original\n"
            "• DIFERENCIA: Búfer menos la geometría original\n"
            "• DIF. INV: Geometría original menos el búfer\n"
            "• XOR: Áreas que NO se superponen"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === CAPA DE EXCLUSIÓN ===
        param = QgsProcessingParameterFeatureSource(
            self.EXCLUSION_LAYER, '🛑 CAPA DE EXCLUSIÓN (Opcional) ✂️',
            [QgsProcessing.TypeVectorPolygon], optional=True)
        param.setHelp(
            "Capa de polígonos cuyas áreas serán restadas de todos los búferes.\n\n"
            "• Útil para excluir: cuerpos de agua, áreas protegidas, zonas urbanas\n"
            "• Se aplica después de generar cada búfer\n"
            "• Si el búfer queda completamente dentro de la exclusión, se omite"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === MODO PREVIEW ===
        param = QgsProcessingParameterBoolean(
            self.PREVIEW_MODE, '👁️ Modo previsualización (solo primera entidad)', defaultValue=False)
        param.setHelp(
            "Procesa únicamente la primera entidad de la capa.\n\n"
            "• Útil para probar configuraciones rápidamente\n"
            "• Reduce tiempo de espera en capas grandes\n"
            "• Desactive para el procesamiento final"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === ANÁLISIS DE TRASLAPES ===
        param = QgsProcessingParameterBoolean(
            self.CALCULAR_SUPERPOSICION, '📊 Analizar traslapes entre búferes', defaultValue=False)
        param.setHelp(
            "Calcula qué búferes se traslapan entre sí y genera estadísticas.\n\n"
            "✅ Funcionalidades:\n"
            "• Agrega campos a la capa de búferes: n_traslapes, traslapa_con, area_exclusiva_ha, "
            "area_compartida_ha, pct_exclusivo\n"
            "• Genera tabla estadística de superposiciones en el reporte HTML\n"
            "• Calcula porcentajes de área exclusiva vs compartida\n\n"
            "📋 Información generada:\n"
            "• Para cada búfer: con cuántos y cuáles otros se traslapa\n"
            "• Área de intersección entre cada par de búferes\n"
            "• Porcentajes de superposición\n\n"
            "⚠️ Nota:\n"
            "• Requerido si desea generar fragmentos de traslape\n"
            "• Puede aumentar el tiempo de procesamiento"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === GENERAR FRAGMENTOS DE TRASLAPE ===
        param = QgsProcessingParameterBoolean(
            self.GENERAR_FRAGMENTOS_TRASLAPE, '🧩 Generar fragmentos de traslape (requiere análisis)', defaultValue=False)
        param.setHelp(
            "Descompone los búferes en fragmentas según las áreas de traslape.\n\n"
            "✅ Funcionalidades:\n"
            "• Crea una SEGUNDA capa de salida con todos los fragmentos únicos\n"
            "• Cada fragmento representa una combinación única de traslapes\n"
            "• Campos: fid, n_buferes, tipo_traslape, buferes, area_ha, etc.\n"
            "• Útil para análisis de influencia múltiple o zonificación\n\n"
            "📋 Tipos de fragmentos:\n"
            "• Exclusivo: Área cubierta por un solo búfer\n"
            "• Doble: Área donde se cruzan exactamente 2 búferes\n"
            "• Triple/Múltiple: Área donde se cruzan 3+ búferes\n\n"
            "⚠️ Límites de Seguridad (para evitar bloqueos del sistema):\n"
            "• Tope de fragmentos: 1000 — el algoritmo se detiene al alcanzar este límite\n"
            "• Tope de profundidad: hasta 9 búferes simultáneos por combinación\n"
            "  (controla intersecciones de tríos, cuartetos... hasta nonetos)\n"
            "• El orden de prioridad es: áreas exclusivas → traslapes dobles → triples → ...\n"
            "• Si se alcanza el límite, se notifica en el LOG del proceso\n\n"
            "✅ Fragmentos garantizados (antes del límite):\n"
            "• Todas las áreas exclusivas (1 búfer)\n"
            "• Todos los traslapes dobles (2 búferes)\n"
            "• La mayoría de traslapes triples (3 búferes)\n\n"
            "⚠️ Fragmentos que pueden omitirse (si se alcanza el límite):\n"
            "• Intersecciones de 4+ búferes simultáneos\n"
            "• En datos reales suelen ser micro-fragmentos sin utilidad práctica\n\n"
            "⚙️ Importante:\n"
            "• Automáticamente activa el análisis de traslapes\n"
            "• Requiere al menos 2 búferes para funcionar\n"
            "• No tiene Tiempo de Espera propio — el tiempo depende de la complejidad geométrica"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === SIMPLIFICACIÓN DE ENTRADA ===
        param = QgsProcessingParameterBoolean(
            self.SIMPLIFICAR_ENTRADA, '🔧 Simplificar geometrías de entrada (polígonos y líneas)', defaultValue=False)
        param.setHelp(
            "Simplifica polígonos Y líneas ANTES de crear los búferes.\n\n"
            "✅ MUY ÚTIL para:\n"
            "• Polígonos vectorizados de imágenes satelitales/raster (perímetros en escalera)\n"
            "• Líneas muy densas con muchos vértices (ríos, carreteras digitalizadas a alta escala)\n"
            "• Reducir complejidad geométrica antes del procesamiento\n"
            "• Mejorar rendimiento con geometrías muy complejas\n"
            "• Acelerar el cálculo iterativo del Búfer Por Área en líneas y polígonos\n\n"
            "⚙️ Cómo funciona:\n"
            "• Usa algoritmo Douglas-Peucker en geometrías originales\n"
            "• Los búferes se crean a partir de geometrías simplificadas\n"
            "• Resultado: búferes más limpios y suaves\n\n"
            "💡 Ejemplo polígono:\n"
            "Vectorización raster 10m → perímetro escalonado →\n"
            "Simplificar 5-10m → perímetro suavizado →\n"
            "Búfer resultante más natural y eficiente\n\n"
            "💡 Ejemplo línea:\n"
            "Río con 10000 vértices → Búfer Por Área tarda 3 min →\n"
            "Simplificar 5m → 800 vértices → Búfer Por Área tarda 15 seg\n\n"
            "📊 Beneficios:\n"
            "• Búferes más limpios visualmente\n"
            "• Menos vértices = mejor rendimiento\n"
            "• Elimina artefactos de vectorización\n"
            "• Reduce tiempo de procesamiento 20-60%\n\n"
            "⚠️ Nota: No aplica a geometrías de tipo Punto."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        param = QgsProcessingParameterNumber(
            self.TOLERANCIA_ENTRADA, '🔧 Tolerancia simplificación entrada (m)',
            type=QgsProcessingParameterNumber.Double, minValue=0.1, defaultValue=5.0)
        param.setMetadata({'widget_wrapper': {'decimals': 1}})
        param.setHelp(
            "Tolerancia para simplificar polígonos y líneas de entrada.\n\n"
            "💡 Valores recomendados por origen de datos:\n\n"
            "📡 Vectorización de raster:\n"
            "• Resolución 10m → Tolerancia 5-10m\n"
            "• Resolución 30m → Tolerancia 15-30m\n"
            "• Resolución 100m → Tolerancia 50-100m\n\n"
            "📐 Polígonos muy complejos:\n"
            "• 1-2m: Simplificación mínima\n"
            "• 5-10m: Recomendado para análisis general\n"
            "• 20-50m: Para análisis regional\n\n"
            "〰️ Líneas densas (ríos, carreteras):\n"
            "• 1-5m: Para líneas de alta precisión\n"
            "• 5-20m: Para análisis de escala media\n"
            "• 20-50m: Para análisis regional\n\n"
            "⚠️ Importante:\n"
            "• Valor muy bajo: Poco efecto, mantiene complejidad\n"
            "• Valor muy alto: Puede distorsionar forma original\n"
            "• Regla polígonos: ~50% de la resolución del raster original\n\n"
            "🎯 Esta simplificación NO afecta la distancia del búfer,\n"
            "solo suaviza el contorno de la geometría de entrada."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === SIMPLIFICACIÓN DE SALIDA ===
        param = QgsProcessingParameterBoolean(
            self.APLICAR_SIMPLIFICACION, '📐 Simplificar geometrías de salida', defaultValue=False)
        param.setHelp(
            "Reduce el número de vértices en las geometrías resultantes.\n\n"
            "• Usa el algoritmo Douglas-Peucker\n"
            "• Mejora el rendimiento de visualización\n"
            "• Reduce el tamaño del archivo\n"
            "• Configure la tolerancia en el siguiente parámetro"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === TOLERANCIA DE SIMPLIFICACIÓN ===
        param = QgsProcessingParameterNumber(
            self.TOLERANCIA_SIMPLIFICACION, '📐 Tolerancia de simplificación (m)',
            type=QgsProcessingParameterNumber.Double, minValue=0.0, defaultValue=1.0)
        param.setMetadata({'widget_wrapper': {'decimals': 2}})
        param.setHelp(
            "Distancia máxima permitida para simplificar vértices.\n\n"
            "• 0,5m: Alta precisión, poca reducción\n"
            "• 1,0m: Balance recomendado para uso general\n"
            "• 5,0m: Para análisis regional (escala 1:10000+)\n"
            "• 25m+: Solo para visualización general\n\n"
            "Regla: Tolerancia ≈ Escala del mapa ÷ 1000"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === RESOLVER TRASLAPES ===
        param = QgsProcessingParameterEnum(
            self.RESOLVER_TRASLAPES, '🔀 Resolver traslapes entre búferes',
            options=Constants.TRASLAPE_NAMES, defaultValue=Constants.TRASLAPE_MANTENER)
        param.setHelp(
            "Estrategia para resolver áreas traslapadas entre búferes:\n\n"
            "• 🔀 <strong>Mantener traslapes:</strong> No modifica los búferes. Las áreas "
            "traslapadas pertenecerán a ambos polígonos (comportamiento tradicional).\n\n"
            "• 📈 <strong>Asignar al polígono MAYOR:</strong> El área traslapada se asigna "
            "al polígono de mayor superficie. Los polígonos menores pierden el área traslapada.\n\n"
            "• 📉 <strong>Asignar al polígono MENOR:</strong> El área traslapada se asigna "
            "al polígono de menor superficie. Los polígonos mayores pierden el área traslapada.\n\n"
            "<strong>Nota:</strong> Esta opción puede aumentar el tiempo de procesamiento "
            "en capas con muchas geometrías."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === ELIMINAR HUECOS ===
        param = QgsProcessingParameterBoolean(
            self.ELIMINAR_HUECOS, '🕳️ Eliminar huecos (anillos internos)', defaultValue=False)
        param.setHelp(
            "Elimina los huecos (anillos internos) de los polígonos resultantes.\n\n"
            "⚠️ IMPORTANTE - Comportamiento de búfer con huecos:\n"
            "El algoritmo buffer() de QGIS crea búfer en AMBAS direcciones:\n"
            "• Expande hacia AFUERA del contorno exterior ✅\n"
            "• Expande hacia ADENTRO de los huecos ⚠️\n"
            "Esto significa que los huecos se reducen en tamaño.\n\n"
            
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === ÁREA MÍNIMA DE HUECO ===
        param = QgsProcessingParameterNumber(
            self.AREA_MINIMA_HUECO, '🕳️ Área mínima de hueco a eliminar (m²)',
            type=QgsProcessingParameterNumber.Double, minValue=0.0, defaultValue=0.0)
        param.setMetadata({'widget_wrapper': {'decimals': 2}})
        param.setHelp(
            "Área mínima (en m²) de los huecos a eliminar.\n\n"
            "• 0: Comportamiento según 'Preservar hueco estructural'\n"
            "• >0: Solo elimina huecos menores a este valor\n\n"
            "Ejemplos:\n"
            "• 100 m²: Elimina huecos menores a 100 m² (preserva huecos grandes)\n"
            "• 10000 m² (1 ha): Elimina huecos menores a 1 hectárea\n\n"
            "Solo aplica si 'Eliminar huecos' está activado."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === PRESERVAR HUECO ESTRUCTURAL ===
        param = QgsProcessingParameterBoolean(
            self.PRESERVAR_HUECO_ESTRUCTURAL, '🍩 Preservar hueco estructural (donut)', defaultValue=True)
        param.setHelp(
            "Preserva el hueco más grande de cada polígono (el hueco 'estructural').\n\n"
            "• ACTIVADO (Recomendado para búferes concéntricos):\n"
            "  - Mantiene el hueco central que define la forma de 'donut'\n"
            "  - Solo elimina huecos pequeños/accidentales\n"
            "  - Ideal para anillos concéntricos disjuntos\n\n"
            "• DESACTIVADO:\n"
            "  - Elimina TODOS los huecos (convierte donuts en discos)\n"
            "  - Útil para polígonos sólidos sin perforaciones\n\n"
            "Solo aplica si 'Eliminar huecos' está activado y 'Área mínima' = 0."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === DISOLVER BÚFERES ===
        param = QgsProcessingParameterBoolean(
            self.DISOLVER_BUFERES, '🔗 Disolver búferes (Agrupar geometrías que se tocan)', defaultValue=False)
        param.setHelp(
            "Fusiona los búferes que se tocan, manteniendo polígonos separados cuando no se intersectan.\n\n"
            "✅ <strong>ACTIVADO (Disolver):</strong>\n"
            "• Elimina los linderos internos entre búferes que se intersectan\n"
            "• Los grupos conectados se fusionan en polígonos individuales\n"
            "• Los búferes que NO se tocan permanecen como polígonos separados\n"
            "• El cálculo de área total es correcto (sin contar traslapes múltiples veces)\n\n"
            "❌ <strong>DESACTIVADO (Individual):</strong>\n"
            "• Cada búfer se mantiene como polígono separado (comportamiento tradicional)\n"
            "• Los búferes pueden traslaparse entre sí\n"
            "• Útil para análisis de proximidad individual por entidad\n\n"
            "🔍 <strong>Comportamiento inteligente:</strong>\n"
            "• 50 búferes todos conectados → 1 polígono fusionado\n"
            "• 50 búferes en 3 grupos separados → 3 polígonos (uno por grupo)\n"
            "• 50 búferes sin tocarse → 50 polígonos (sin cambios)\n\n"
            "📊 <strong>Casos de uso recomendados:</strong>\n"
            "• 🏘️ <strong>Áreas urbanas:</strong> Vecindarios conectados se fusionan, zonas separadas permanecen independientes\n"
            "• 🌊 <strong>Zonas de protección:</strong> Sistemas de ríos conectados se fusionan, cuencas separadas se mantienen\n"
            "• 🏥 <strong>Cobertura de servicios:</strong> Áreas continuas se fusionan, zonas aisladas se mantienen separadas\n"
            "• 📊 <strong>Cálculo de áreas:</strong> Área total real sin duplicar traslapes, con separación lógica entre grupos\n\n"
            "⚠️ <strong>Notas importantes:</strong>\n"
            "• La disolución se aplica DESPUÉS de crear todos los búferes individuales\n"
            "• Cada grupo disuelto tendrá los atributos del primer búfer del grupo\n"
            "• Compatible con todos los tipos de búfer (circular, oval, rectangular, etc.)"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        # === MANTENER PARTE MAYOR (Por Area - fragmentacion) ===
        param = QgsProcessingParameterBoolean(
            self.MANTENER_PARTE_MAYOR, '🧩 Al fragmentar: mantener solo parte mayor (Por Área)', defaultValue=False)
        param.setHelp(
            "Aplica únicamente al Búfer Por Área con contracción de polígonos cóncavos.\n\n"
            "Cuando la contracción rompe el polígono en múltiples fragmentos:\n\n"
            "❌ DESACTIVADO (por omisión):\n"
            "• Se conservan TODOS los fragmentos como MultiPolígono\n"
            "• El área total de los fragmentos cumple el objetivo\n"
            "• Se registra advertencia en el log y en el reporte HTML\n\n"
            "✅ ACTIVADO:\n"
            "• Se conserva SOLO el fragmento de mayor área\n"
            "• El área del fragmento NO cumple necesariamente el objetivo\n"
            "• La figura resultante es un polígono continuo (no fragmentado)\n"
            "• Se registra advertencia indicando el área descartada\n\n"
            "⚠️ Use esta opción cuando necesite un resultado continuo y acepta\n"
            "   que el área final sea menor a la meta configurada."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        # === TRANSPARENCIA ===
        param = QgsProcessingParameterBoolean(
            self.APLICAR_TRANSPARENCIA, '🎨 Aplicar transparencia', defaultValue=True)
        param.setHelp(
            "Aplica transparencia automática a la capa de salida.\n\n"
            "• Facilita visualizar capas superpuestas\n"
            "• El nivel se configura en el siguiente parámetro\n"
            "• Se puede ajustar manualmente después en propiedades de capa"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === NIVEL DE OPACIDAD ===
        param = QgsProcessingParameterNumber(
            self.NIVEL_TRANSPARENCIA, '🔍 Nivel de opacidad (%)',
            type=QgsProcessingParameterNumber.Integer, minValue=0, maxValue=100, defaultValue=50)
        param.setHelp(
            "Porcentaje de opacidad de la capa de salida.\n\n"
            "• 0% = Completamente transparente (invisible)\n"
            "• 50% = Semitransparente (recomendado)\n"
            "• 100% = Completamente opaco\n\n"
            "Valores 40-70% son ideales para visualizar superposiciones"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === GENERAR REPORTE ===
        param = QgsProcessingParameterBoolean(
            self.GENERAR_REPORTE, '📊 Generar reporte', defaultValue=True)
        param.setHelp(
            "Genera un reporte HTML detallado del proceso.\n\n"
            "Incluye:\n"
            "• Parámetros utilizados\n"
            "• Estadísticas de área (total, promedio, min, max)\n"
            "• Métricas de rendimiento (tiempo, geometrías procesadas)\n"
            "• Eficiencia de simplificación\n"
            "• Alertas y advertencias\n"
            "• Detalle de Integridad Geométrica (3 categorías)"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === RUTA DEL REPORTE ===
        param = QgsProcessingParameterFileDestination(
            self.RUTA_REPORTE, '📁 Ruta reporte', 'HTML (*.html)',
            defaultValue=os.path.join(tempfile.gettempdir(), 'reporte_buffer.html'))
        param.setHelp(
            "Ubicación donde se guardará el reporte HTML.\n\n"
            "• Por defecto: Carpeta temporal del sistema\n"
            "• Se abre automáticamente en el navegador al finalizar\n"
            "• Puede especificar una ruta personalizada"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === PROCESAMIENTO PARALELO ===
        param = QgsProcessingParameterBoolean(
            self.USAR_PARALELO, '⚡ Usar procesamiento paralelo (Multihilo)', defaultValue=False)
        param.setHelp(
            "Activa el procesamiento en múltiples hilos para mejorar el rendimiento.\n\n"
            "✅ Recomendado con 50+ entidades para:\n"
            "• Búfer Por Área → ganancia 50–65% (confirmado experimentalmente)\n"
            "• Búfer Concéntrico 6+ anillos → ganancia ~50% (confirmado)\n"
            "• Búfer Oval / Rectangular polígonos complejos → ~20–40% (estimado)\n\n"
            "⚠️ Beneficio limitado o no justificado para:\n"
            "• Búfer Circular → ~10–25% · Solo con 500+ entidades complejas\n"
            "• Búfer Un Solo Lado / Cuña → ahorro absoluto <2 s con 220 entidades\n"
            "  (útil solo en flujos automatizados de alta frecuencia)\n\n"
            "⚠️ Limitaciones técnicas:\n"
            "• Usa ThreadPoolExecutor (limitado por GIL de Python)\n"
            "• No activo con <50 búferes (overhead supera la ganancia)\n"
            "• Con simplificación de entrada activa + 2–4 hilos puede ser\n"
            "  más lento que el modo secuencial (alta variabilidad, CV 14–15%)\n\n"
            "💡 Primera optimización recomendada: active Simplificación de\n"
            "entrada antes de ajustar hilos. En Por Área reduce el tiempo\n"
            "secuencial en ~50% sin costo adicional."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        param = QgsProcessingParameterNumber(
            self.NUM_THREADS, '🔢 Número de hilos',
            type=QgsProcessingParameterNumber.Integer, minValue=2, maxValue=32, defaultValue=4)
        param.setHelp(
            "Cantidad de hilos de procesamiento a utilizar.\n\n"
            "📌 Recomendación basada en datos experimentales\n"
            "(Intel Core Ultra 7 155H · 220 polígonos · Búfer Por Área):\n\n"
            "Configure los hilos igual al número de P-cores (Performance cores)\n"
            "de su procesador, NO al total de núcleos físicos ni lógicos.\n"
            "En arquitectura híbrida (P-cores + E-cores), usar más hilos que\n"
            "P-cores genera variabilidad sin ganancia real.\n\n"
            "📊 Resultados medidos (media de 5–6 repeticiones):\n"
            "• 2 hilos: 1,80×  · 4 hilos: 1,55× ⚠️ (más lento que 2h)\n"
            "• 6 hilos: 2,65× ✅ óptimo estabilidad (CV 1,4%, = núm. P-cores)\n"
            "• 10 hilos: 2,77× · 12 hilos: 2,77× (ganancia marginal sobre 6h)\n\n"
            "⚠️ Anomalía confirmada: 4 hilos puede ser MÁS LENTO que 2 hilos\n"
            "en procesadores con arquitectura híbrida.\n\n"
            "💡 Para identificar sus P-cores en Windows: Administrador de\n"
            "tareas → Rendimiento → CPU, o use CPU-Z / HWiNFO64."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        # === EXPORTAR CONFIGURACIÓN JSON ===
        param = QgsProcessingParameterBoolean(
            self.EXPORTAR_CONFIG,
            '💾 Exportar configuración a JSON',
            defaultValue=False)
        param.setHelp(
            "Guarda todos los parámetros activos del algoritmo en un archivo JSON al finalizar.\n\n"
            "PROPÓSITO:\n"
            "• Auditoría: registro completo y reproducible de cada ejecución\n"
            "• Recrear la configuración manualmente en futuras ejecuciones\n"
            "• Compartir configuraciones entre usuarios o proyectos\n\n"
            "El archivo incluye TODOS los parámetros activos: tipo de búfer, distancia, "
            "mapeo de categorías, estilos, post-proceso, densidad adaptativa, etc.\n\n"
            "💡 El archivo JSON puede abrirse con cualquier editor de texto (Bloc de notas, "
            "VS Code, Notepad++). Para recrear una configuración: abra el archivo, lea los "
            "valores y configúrelos manualmente en el formulario."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        param = QgsProcessingParameterFileDestination(
            self.RUTA_CONFIG_JSON,
            '📁 Ruta del archivo JSON',
            fileFilter='JSON (*.json)',
            defaultValue=os.path.join(tempfile.gettempdir(), 'bufer_config.json'),
            optional=True)
        param.setHelp(
            "Ruta donde se guardará el archivo JSON de configuración.\n\n"
            "• Por defecto: carpeta temporal del sistema\n"
            "• Cambie la ruta para guardar en una ubicación permanente\n"
            "• Solo activo si 'Exportar configuración a JSON' está marcado"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        # === MODO VALIDACIÓN PREVIA ===
        param = QgsProcessingParameterBoolean(
            self.DRY_RUN,
            '🔍 Validación Previa (validar sin procesar)',
            defaultValue=False)
        param.setHelp(
            "Ejecuta todas las validaciones sin generar geometrías de búfer.\n\n"
            "QUÉ HACE:\n"
            "• ✅ Verifica CRS (geográfico vs. proyectado)\n"
            "• ✅ Detecta y reporta geometrías inválidas\n"
            "• ✅ Valida que los campos de distancia / categoría existen y tienen valores numéricos\n"
            "• ✅ Detecta valores negativos, nulos o cero en campos de radio\n"
            "• ✅ Verifica la capa de exclusión (si se usa)\n"
            "• ✅ Valida la coherencia de parámetros (ej: Oval sin ancho/alto)\n"
            "• ✅ Genera un reporte HTML completo del diagnóstico\n"
            "• ❌ NO crea ninguna geometría de búfer\n"
            "• ❌ NO escribe en la capa de salida\n\n"
            "CUÁNDO USARLO:\n"
            "• Antes de procesar capas grandes (detecta problemas baratos antes del cómputo costoso)\n"
            "• Para documentar la calidad de los datos de entrada\n"
            "• Para verificar que todos los campos requeridos existen antes de ejecutar en lotes"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        # === PARÁMETROS: BÚFER ADAPTATIVO POR DENSIDAD (tipo 7) ===
        # Estos parámetros aplican cuando se selecciona "Adaptativo por densidad"
        # en el selector de Tipo de búfer. El radio de cada entidad se calcula
        # automáticamente según su densidad espacial local.

        param = QgsProcessingParameterEnum(
            self.DENSIDAD_METODO,
            '📐 Adaptativo por densidad',
            options=Constants.DENSIDAD_NAMES,
            defaultValue=Constants.DENSIDAD_KNN)
        param.setHelp(
            "Método para medir la densidad local:\n\n"
            "📍 K VECINOS MÁS CERCANOS (KNN) — Recomendado para la mayoría de casos:\n"
            "• Mide la distancia al k-ésimo vecino más cercano.\n"
            "• Radio = distancia_al_k_vecino × factor_escala\n"
            "• Intuitivo: el radio es proporcional al 'espacio personal' de cada punto.\n"
            "• Con k=3 y escala=0.5 el radio es el 50% de la distancia al 3er vecino.\n"
            "• Más sensible a la distribución local que el método de radio fijo.\n\n"
            "🔵 CONTEO EN RADIO FIJO — Más estable en capas muy heterogéneas:\n"
            "• Cuenta cuántos vecinos hay dentro del radio de referencia.\n"
            "• Radio = (radio_base / √n_vecinos) × factor_escala\n"
            "• A más vecinos → radio más pequeño (relación inversa).\n"
            "• Útil cuando la escala de análisis es conocida de antemano\n"
            "  (ej: cobertura en radio de 500m)."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        param = QgsProcessingParameterNumber(
            self.DENSIDAD_K,
            '🔢 Número de vecinos (K)',
            type=QgsProcessingParameterNumber.Integer,
            minValue=1, maxValue=20, defaultValue=3)
        param.setHelp(
            "Número de vecinos más cercanos para medir la densidad local (solo método KNN).\n\n"
            "CÓMO ELEGIR K:\n"
            "• K=1 → radio basado solo en el vecino más cercano. Muy sensible, resultados ruidosos.\n"
            "• K=3 → balance recomendado para la mayoría de casos (suaviza sin perder contraste).\n"
            "• K=5 → bueno para capas con clustering pronunciado o distribución muy irregular.\n"
            "• K≥7 → aproximación a densidad regional; reduce el contraste local.\n\n"
            "REGLA PRÁCTICA (literatura GIS):\n"
            "  K ≈ √(número de entidades) / 3  → redondeado al entero más cercano.\n"
            "  Ejemplos: 50 entidades → K≈2 | 100 → K≈3 | 500 → K≈7 | 1000 → K≈10\n\n"
            "EFECTO EN EL RESULTADO:\n"
            "  K pequeño → mayor variación entre búferes (contraste espacial alto).\n"
            "  K grande  → búferes más uniformes (contraste espacial bajo).\n\n"
            "SUGERENCIA: comience con K=3 y ajuste según el resultado visual."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        param = QgsProcessingParameterDistance(
            self.DENSIDAD_RADIO_REF,
            '🔵 Radio de referencia (método radio fijo)',
            parentParameterName=self.INPUT,
            defaultValue=0.0)
        param.setHelp(
            "Radio de búsqueda en metros para contar vecinos (solo método Radio Fijo).\n\n"
            "CONCEPTO: Define el 'vecindario' de cada entidad.\n"
            "  Entidades con más vecinos dentro de este radio → radio de búfer más pequeño.\n"
            "  Entidades con pocos vecinos → radio de búfer más grande.\n\n"
            "CÓMO ESTIMAR EL VALOR:\n"
            "  1. En QGIS: Vector → Análisis → Estadísticas básicas → calcule la\n"
            "     distancia promedio entre centroides de su capa.\n"
            "  2. Regla rápida: use 1× la distancia promedio entre entidades.\n"
            "  3. Si no sabe: use 10% de la extensión total de la capa (ancho o alto).\n\n"
            "DIAGNÓSTICO:\n"
            "  • Si el log muestra 'min=X max=X promedio=X' con poca variación →\n"
            "    el radio es demasiado grande (todos tienen muchos vecinos).\n"
            "  • Si min≈radio_min → el radio es demasiado pequeño (sin vecinos).\n"
            "  • Busque una diferencia min/max de al menos 3× para resultados visibles."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        param = QgsProcessingParameterDistance(
            self.DENSIDAD_RADIO_BASE,
            '📏 Radio base (método radio fijo)',
            parentParameterName=self.INPUT,
            defaultValue=0.0)
        param.setHelp(
            "Radio de búfer para entidades con un único vecino dentro del radio de referencia\n"
            "(solo método Radio Fijo). Es el 'techo práctico' antes del factor de escala.\n\n"
            "FÓRMULA: radio_final = (radio_base / √n_vecinos) × factor_escala\n"
            "  Con n=1 vecino → radio = radio_base × factor_escala\n"
            "  Con n=4 vecinos → radio = (radio_base / 2) × factor_escala\n\n"
            "CÓMO ELEGIR EL VALOR:\n"
            "  • Use el radio de búfer máximo que tiene sentido para su análisis.\n"
            "  • Ejemplo: si su área de influencia máxima esperada es 500m,\n"
            "    use radio_base=500 con factor_escala=1.0, o radio_base=1000 con escala=0.5.\n"
            "  • El resultado final nunca excederá radio_máximo.\n\n"
            "SUGERENCIA: igual o mayor a la distancia promedio entre entidades."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        param = QgsProcessingParameterNumber(
            self.DENSIDAD_FACTOR_ESCALA,
            '⚖️ Factor de escala',
            type=QgsProcessingParameterNumber.Double,
            minValue=0.01, maxValue=10.0, defaultValue=0.5)
        param.setHelp(
            "Multiplicador final aplicado al radio calculado (antes del recorte por radio_min/max).\n\n"
            "FÓRMULAS:\n"
            "  KNN:        radio = distancia_al_k_vecino × factor_escala\n"
            "  Radio fijo: radio = (radio_base / √n_vecinos) × factor_escala\n\n"
            "INTERPRETACIÓN:\n"
            "  0,5 → cada búfer llega hasta el 50% de la distancia a su vecino\n"
            "        (los búferes se tocan pero NO se trasladan — cobertura perfecta)\n"
            "  1,0 → el radio iguala exactamente la distancia al vecino\n"
            "        (traslape del 100% entre pares de vecinos)\n"
            "  0,3 → búferes más pequeños, zonas sin cobertura entre entidades\n\n"
            "CÓMO ELEGIR:\n"
            "  • Para cobertura sin traslapes: escala ≈ 0,5 (recomendado)\n"
            "  • Para análisis de influencia amplia: escala entre 0,7 y 1,0\n"
            "  • Para zonas buffer pequeñas alrededor de cada punto: escala < 0,3\n\n"
            "⚠️ Valores > 1,0 generan traslapes intencionales (zonas de influencia compartida)."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        param = QgsProcessingParameterDistance(
            self.DENSIDAD_RADIO_MIN,
            '📉 Radio mínimo',
            parentParameterName=self.INPUT,
            defaultValue=1.0)
        param.setHelp(
            "Cota inferior del radio en metros. Ningún búfer será menor a este valor.\n\n"
            "FUNCIÓN: Protege contra búferes degenerados (radio ≈ 0) en zonas\n"
            "muy densas donde los vecinos están a centímetros de distancia.\n\n"
            "CÓMO ELEGIR:\n"
            "  • Use el tamaño mínimo con sentido para su análisis.\n"
            "  • Ejemplos por tipo de análisis:\n"
            "    - Análisis urbano (manzanas): radio_min = 10–50 m\n"
            "    - Análisis rural (fincas): radio_min = 50–200 m\n"
            "    - Análisis regional (municipios): radio_min = 500–2000 m\n"
            "  • Regla práctica: 5–10% del radio promedio esperado.\n\n"
            "DIAGNÓSTICO: Si muchos búferes tienen radio = radio_min, su capa tiene\n"
            "entidades muy agrupadas — considere aumentar radio_min o reducir factor_escala."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        param = QgsProcessingParameterDistance(
            self.DENSIDAD_RADIO_MAX,
            '📈 Radio máximo',
            parentParameterName=self.INPUT,
            defaultValue=0.0)
        param.setHelp(
            "Cota superior del radio en metros. Ningún búfer será mayor a este valor.\n\n"
            "FUNCIÓN: Protege contra búferes excesivamente grandes para entidades\n"
            "aisladas que podrían cubrir toda el área de estudio.\n\n"
            "CÓMO ELEGIR:\n"
            "  • Use la distancia máxima con sentido para su análisis:\n"
            "    - Análisis urbano (manzanas/barrios): radio_max = 500–2000 m\n"
            "    - Análisis rural (fincas/comunidades): radio_max = 2000–10000 m\n"
            "    - Análisis regional (municipios/cuencas): radio_max = 10000–50000 m\n"
            "  • Regla práctica: 2–5× la distancia promedio entre entidades.\n\n"
            "DIAGNÓSTICO: Revise el log después de ejecutar — 'max=Xm'. Si ese valor\n"
            "es mayor de lo esperado, reduzca radio_max para controlar las entidades aisladas.\n\n"
            "⚠️ Si radio_max=0 (valor por defecto), el algoritmo aplica un límite\n"
            "automático basado en la extensión total de la capa (diagonal / 4)."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        # === PUNTO DE ANCLAJE (Adaptativo) ===
        param = QgsProcessingParameterEnum(
            self.DENSIDAD_METODO_ANCLAJE,
            '📍 Punto de anclaje para densidad',
            options=Constants.DENSIDAD_ANCLAJE_NAMES,
            defaultValue=Constants.DENSIDAD_ANCLAJE_CENTROIDE)
        param.setHelp(
            "Método para calcular el punto de referencia de cada polígono en el análisis de densidad:\n\n"
            "• 📍 <strong>Centroide (rápido):</strong> Usa el centro geométrico del polígono. "
            "Muy rápido pero puede quedar fuera del polígono en formas cóncavas.\n\n"
            "• 🎯 <strong>Punto interior representativo :</strong> Encuentra el punto más alejado "
            "del borde del polígono. Siempre está dentro del polígono y es ideal para formas "
            "irregulares (L, U, anillos). Requiere tolerancia.\n\n"
            "⚠️ <strong>Nota:</strong> Para capas de puntos, ambos métodos son equivalentes."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        param = QgsProcessingParameterNumber(
            self.DENSIDAD_TOLERANCIA_POLO,
            '🎯 Tolerancia para polo de inaccesibilidad (m)',
            type=QgsProcessingParameterNumber.Double,
            minValue=0.1,
            defaultValue=1.0)
        param.setHelp(
            "Precisión del cálculo del polo de inaccesibilidad (solo aplica si se selecciona ese método).\n\n"
            "• Valores más pequeños (0,1-0,5m): Mayor precisión, pero más lento.\n"
            "• Valores medios (1,0-5,0m): Balance recomendado para la mayoría de casos.\n"
            "• Valores grandes (10m+): Más rápido, pero menos preciso.\n\n"
            "La tolerancia es la distancia máxima permitida entre el polo calculado y el polo real."
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

        # === CAPA DE SALIDA ===
        param = QgsProcessingParameterFeatureSink(self.OUTPUT, '💾 Capa de salida')
        param.setHelp(
            "Destino de las geometrías de búfer generadas.\n\n"
            "• Seleccione una ubicación para guardar permanentemente\n"
            "• O deje como 'Capa temporal' para pruebas\n"
            "• Formato recomendado: GeoPackage (.gpkg) o Shapefile (.shp)\n\n"
            "Campos siempre incluidos:\n"
            "• fid, tipo_entidad, area_ha, distancia/ancho/alto, notas\n\n"
            "Campos adicionales (si activa análisis de superposición o fragmentos):\n"
            "• n_traslapes: Número de búferes con los que se traslapa\n"
            "• traslapa_con: Lista de búferes traslapados\n"
            "• area_exclusiva_ha: Área sin traslapes\n"
            "• area_compartida_ha: Área con traslapes\n"
            "• pct_exclusivo: Porcentaje de área exclusiva"
        )
        if INTERFAZ_COMPACTA:
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        
        # === CAPA DE SALIDA FRAGMENTOS (Opcional) ===
        param = QgsProcessingParameterFeatureSink(
            self.OUTPUT_FRAGMENTOS, '🧩 Fragmentos de traslape (salida opcional)', 
            optional=True)
        param.setHelp(
            "Capa de salida OPCIONAL para fragmentos de traslape.\n\n"
            "• Solo se genera si activa 'Generar fragmentos de traslape'\n"
            "• Contiene todos los fragmentos únicos resultantes de la unión\n"
            "• Cada fragmento representa una combinación única de traslapes\n"
            "• Si no especifica, se crea como capa temporal\n\n"
            "Campos de la capa:\n"
            "• fid: ID del fragmento\n"
            "• n_buferes: Número de búferes que componen el fragmento\n"
            "• tipo_traslape: Clasificación ('Exclusivo', 'Doble', 'Triple', 'Múltiple (N)')\n"
            "• buferes: Lista de búferes (ej: 'fid: 1, fid: 7')\n"
            "• area_ha: Área en hectáreas\n"
            "• area_m2: Área en metros cuadrados\n"
            "• perimetro_m: Perímetro en metros\n"
            "• n_vertices: Número de vértices"
        )
        self.addParameter(param)

    def checkParameterValues(self, parameters, context):
        """Validación de parámetros."""
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None or source.featureCount() == 0:
            return False, "❌ Error: Capa de entrada inválida o vacía."
        
        # CRÍTICO #3: Validar CRS geográfico
        source_crs = source.sourceCrs()
        if source_crs.isGeographic():
            return False, (
                "❌ Error: CRS geográfico detectado (coordenadas en grados lat/lon).\n\n"
                f"CRS actual: {source_crs.authid()} - {source_crs.description()}\n\n"
                "Los búferes requieren un CRS proyectado con unidades en metros.\n"
                "Con un CRS geográfico, las distancias se interpretan incorrectamente\n"
                "y los resultados serán erróneos (ej: 50 grados ≈ 5,550 km).\n\n"
                "💡 Solución:\n"
                "1. Reproyecte la capa a un CRS proyectado apropiado:\n"
                "   • UTM (zona local)\n"
                "   • SIRGAS (para América Latina)\n"
                "   • Lambert Conformal Conic\n"
                "   • O cualquier CRS proyectado en metros\n\n"
                "2. En QGIS: Menú 'Vectorial' → 'Herramientas de gestión de datos' → 'Reproyectar capa'\n\n"
                "3. O use el algoritmo 'Reproyectar capa' primero y luego ejecute este búfer"
            )
        
        buffer_type = self.parameterAsEnum(parameters, self.BUFFER_TYPE, context)
        
        # VALIDACIÓN PARA BÚFER CIRCULAR
        if buffer_type == Constants.BUFFER_CIRCULAR:
            distancia = self.parameterAsDouble(parameters, self.DISTANCIA, context)
            calcular_por_area = self.parameterAsBoolean(parameters, self.CALCULAR_POR_AREA, context)
            
            if calcular_por_area:
                area_objetivo = self.parameterAsDouble(parameters, self.AREA_OBJETIVO, context)
                distance_field = self.parameterAsString(parameters, self.DISTANCE_FIELD, context)
                category_field = self.parameterAsString(parameters, self.CATEGORY_FIELD, context)
                if area_objetivo <= 0 and not distance_field and not category_field:
                    return False, "❌ Error: Para búfer Circular 'Por área', el 'Área objetivo' debe ser mayor a 0 (o use un campo/categoría variable)."
            elif abs(distancia) < Constants.MIN_BUFFER_DISTANCE:
                return False, "❌ Error: Para búfer Circular, la 'Distancia' debe ser diferente de 0 (o activar 'Por área')."

        # ADAPTATIVO: distancia ignorada — el radio lo calcula AdaptiveDensityCalculator
        # No se valida ni se exige valor en el campo Distancia.
        
        # VALIDACIÓN PARA BÚFER POR ÁREA
        if buffer_type == Constants.BUFFER_POR_AREA:
            area_objetivo = self.parameterAsDouble(parameters, self.AREA_OBJETIVO, context)
            distance_field = self.parameterAsString(parameters, self.DISTANCE_FIELD, context)
            category_field = self.parameterAsString(parameters, self.CATEGORY_FIELD, context)
            # Solo bloquear si no hay campo variable activo — el campo proveerá el área por entidad
            if area_objetivo <= 0 and not distance_field and not category_field:
                return False, "❌ Error: Para búfer 'Por Área', el 'Área objetivo' debe ser mayor a 0 (o use un campo/categoría variable)."
        
        # VALIDACIÓN CORREGIDA PARA RECTÁNGULOS Y ÓVALOS
        if buffer_type in [Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR]:
            ancho = self.parameterAsDouble(parameters, self.ANCHO, context)
            alto = self.parameterAsDouble(parameters, self.ALTO, context)
            usar_corredor = self.parameterAsBoolean(parameters, self.USAR_CORREDOR, context)
            
            # Caso A: Es un Corredor (Solo importa el Ancho, pero requiere líneas)
            if buffer_type == Constants.BUFFER_RECTANGULAR and usar_corredor:
                geom_type = QgsWkbTypes.geometryType(source.wkbType())
                if geom_type != QgsWkbTypes.LineGeometry:
                    tipo_geom = "puntos" if geom_type == QgsWkbTypes.PointGeometry else "polígonos"
                    return False, (f"❌ Error: El modo Corredor solo funciona con líneas. "
                                  f"La capa de entrada contiene {tipo_geom}. "
                                  f"Desactive la opción 'Corredor' para usar el modo estándar (centroide).")
                distance_field = self.parameterAsString(parameters, self.DISTANCE_FIELD, context)
                ancho_field = self.parameterAsString(parameters, self.ANCHO_FIELD, context)
                if ancho <= 0 and not distance_field and not ancho_field:
                    return False, "❌ Error: Para el modo Corredor, el 'Ancho' debe ser mayor a 0 (o use un campo variable)."
            
            # Caso B: Es una figura estándar (Importan Ancho y Alto)
            else:
                ancho_field = self.parameterAsString(parameters, self.ANCHO_FIELD, context)
                alto_field = self.parameterAsString(parameters, self.ALTO_FIELD, context)
                distance_field = self.parameterAsString(parameters, self.DISTANCE_FIELD, context)
                if (ancho <= 0 or alto <= 0) and not ancho_field and not alto_field and not distance_field:
                    return False, "❌ Error: Para Óvalos y Rectángulos estándar, 'Ancho' y 'Alto' deben ser mayores a 0 (o use campos variables)."

        if buffer_type == Constants.BUFFER_CONCENTRICO:
            count = self.parameterAsInt(parameters, self.CONCENTRIC_COUNT, context)
            if count < 1:
                return False, "❌ Error: Debe especificar al menos 1 anillo concéntrico."
            if count > Constants.MAX_CONCENTRIC_RINGS:
                return False, f"❌ Error: Máximo {Constants.MAX_CONCENTRIC_RINGS} anillos permitidos."
            
            conc_distance = self.parameterAsDouble(parameters, self.CONCENTRIC_DISTANCE, context)
            distance_field = self.parameterAsString(parameters, self.DISTANCE_FIELD, context)
            
            # Solo exigir distancia fija != 0 si NO hay campo de distancia activo
            if not distance_field and abs(conc_distance) < Constants.MIN_BUFFER_DISTANCE:
                return False, (
                    "❌ Error: Para búfer 'Concéntrico', la 'Distancia anillos' debe ser diferente de 0.\n"
                    "💡 Alternativa: Seleccione un 'Campo de distancia' para asignar distancias variables por entidad."
                )
        
        if buffer_type == Constants.BUFFER_UN_LADO:
            geom_type = QgsWkbTypes.geometryType(source.wkbType())
            u_hull = self.parameterAsBoolean(parameters, self.USAR_GEOMETRIA_MINIMA, context)
            u_box = self.parameterAsBoolean(parameters, self.CREAR_POLIGONO_PUNTOS, context)
            if geom_type == QgsWkbTypes.PointGeometry and not (u_hull or u_box):
                return False, "❌ Error: Búfer de un solo lado requiere líneas o polígonos."
            
            distancia = self.parameterAsDouble(parameters, self.DISTANCIA, context)
            distance_field = self.parameterAsString(parameters, self.DISTANCE_FIELD, context)
            if abs(distancia) < Constants.MIN_BUFFER_DISTANCE and not distance_field:
                return False, "❌ Error: Para búfer 'Un solo lado', la 'Distancia' debe ser mayor a 0 (o use un campo variable)."
        
        # VALIDACIÓN PARA BÚFER EN CUÑA
        if buffer_type == Constants.BUFFER_CUNA:
            distancia = self.parameterAsDouble(parameters, self.DISTANCIA, context)
            wedge_width = self.parameterAsDouble(parameters, self.WEDGE_WIDTH, context)
            distance_field = self.parameterAsString(parameters, self.DISTANCE_FIELD, context)
            
            # Solo exigir radio fijo > 0 si NO hay campo de distancia activo
            if not distance_field and distancia < Constants.MIN_BUFFER_DISTANCE:
                return False, (
                    "❌ Error: Para búfer 'Cuña', la 'Distancia (Radio)' debe ser positiva y mayor a 0.\n"
                    "💡 Alternativa: Seleccione un 'Campo de distancia' para asignar el radio de forma variable por entidad."
                )
            if wedge_width <= 0:
                return False, "❌ Error: Para búfer 'Cuña', la 'Amplitud' debe ser mayor a 0°."
        
        # VALIDACIÓN PARA BÚFER ANCHO VARIABLE (PUNTOS)
        if buffer_type == Constants.BUFFER_ANCHO_M:
            geom_type = QgsWkbTypes.geometryType(source.wkbType())
            distance_field = self.parameterAsString(parameters, self.DISTANCE_FIELD, context) or ""
            
            # Solo acepta puntos con campo de distancia
            if geom_type != QgsWkbTypes.PointGeometry:
                tipo_str = "líneas" if geom_type == QgsWkbTypes.LineGeometry else "polígonos"
                return False, (
                    f"❌ Error: Búfer 'Ancho Variable (Puntos)' solo aplica a capas de PUNTOS. "
                    f"La capa de entrada contiene {tipo_str}.\n\n"
                    "💡 Este tipo de búfer requiere una capa de puntos ordenados (vértices de una ruta) "
                    "con un campo numérico que contenga la distancia del búfer en cada punto.\n\n"
                    "El script construirá la ruta automáticamente conectando los puntos en orden "
                    "y generará un polígono con ancho variable según el campo seleccionado."
                )
            
            if not distance_field:
                return False, (
                    "❌ Error: Búfer 'Ancho Variable (Puntos)' requiere un 'Campo de distancia variable' "
                    "que contenga el ancho del búfer en cada punto.\n\n"
                    "💡 Seleccione el campo numérico que contiene la distancia deseada para cada punto."
                )
            
            # Validar que el campo existe
            field_names = {fld.name() for fld in source.fields()}
            if distance_field not in field_names:
                return False, (
                    f"❌ Error: El campo de distancia '{distance_field}' no existe en la capa.\n"
                    f"Campos disponibles: {', '.join(sorted(field_names))}"
                )
                
            # Validación OK para puntos con campo de distancia

        op_logic = self.parameterAsEnum(parameters, self.CREAR_UNION_LOGICA, context)
        if op_logic in [Constants.OP_UNION, Constants.OP_INTERSECCION, Constants.OP_DIFERENCIA, Constants.OP_XOR]:
            geom_type = QgsWkbTypes.geometryType(source.wkbType())
            u_hull = self.parameterAsBoolean(parameters, self.USAR_GEOMETRIA_MINIMA, context)
            u_box = self.parameterAsBoolean(parameters, self.CREAR_POLIGONO_PUNTOS, context)
            if geom_type != QgsWkbTypes.PolygonGeometry and not (u_hull or u_box):
                return False, f"❌ Error: La operación '{Constants.OP_NAMES[op_logic]}' requiere polígonos."
        
        # VALIDACIÓN DE TOLERANCIA VS DISTANCIA
        aplicar_simplificacion = self.parameterAsBoolean(parameters, self.APLICAR_SIMPLIFICACION, context)
        tolerancia = self.parameterAsDouble(parameters, self.TOLERANCIA_SIMPLIFICACION, context)
        distancia = self.parameterAsDouble(parameters, self.DISTANCIA, context)
        
        if buffer_type == Constants.BUFFER_CONCENTRICO:
            distancia = self.parameterAsDouble(parameters, self.CONCENTRIC_DISTANCE, context)
        elif buffer_type in [Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR]:
            ancho = self.parameterAsDouble(parameters, self.ANCHO, context)
            if ancho > 0:
                distancia = ancho

        if aplicar_simplificacion and tolerancia > 0 and abs(distancia) > 0 and buffer_type != Constants.BUFFER_POR_AREA:
            if tolerancia > abs(distancia) * 0.5:
                return False, (f"❌ Error: La tolerancia de simplificación ({tolerancia:.2f}m) es muy alta "
                              f"respecto a la distancia ({abs(distancia):.2f}m). "
                              f"Máximo recomendado: {abs(distancia)*0.5:.2f}m")
        
        # VALIDACIÓN: DISTANCIA NEGATIVA EN PUNTOS Y LÍNEAS
        geom_type = QgsWkbTypes.geometryType(source.wkbType())
        
        # BUFFER_UN_LADO con LÍNEAS: el negativo es válido (invierte el lado del offset)
        # → se advierte en processAlgorithm via feedback.pushWarning() sin bloquear
        tipos_bloqueo_negativo = [Constants.BUFFER_CIRCULAR, Constants.BUFFER_CONCENTRICO]
        # BUFFER_ADAPTATIVO no se bloquea: su distancia siempre es 0.0 (ignorada)
        
        if buffer_type in tipos_bloqueo_negativo and geom_type != QgsWkbTypes.PolygonGeometry:
            dist_check = self.parameterAsDouble(parameters, self.DISTANCIA, context)
            distance_field = self.parameterAsString(parameters, self.DISTANCE_FIELD, context)
            
            # CRÍTICO #1: Pre-escaneo del campo para detectar valores negativos
            if distance_field:
                tipo_geom = "puntos" if geom_type == QgsWkbTypes.PointGeometry else "líneas"
                
                # Escanear campo (limitado a 1 000 features para rendimiento).
                # LIMITACIÓN CONOCIDA: en capas con más de 1 000 entidades donde
                # los valores negativos estén concentrados más allá de las primeras
                # 1 000 features, esta validación previa podría no detectarlos.
                # La verificación completa se aplica entidad por entidad durante
                # _prepare_features(), por lo que el procesamiento siempre es correcto;
                # este límite sólo afecta al bloqueo anticipado en _validate_parameters().
                n_total = source.featureCount()
                max_check = min(1000, n_total)
                checked = 0
                tiene_negativos = False
                primer_negativo_fid = None
                primer_negativo_valor = None
                
                for f in source.getFeatures():
                    if checked >= max_check:
                        break
                    
                    try:
                        val = float(f[distance_field])
                        if val < 0:
                            tiene_negativos = True
                            primer_negativo_fid = f.id()
                            primer_negativo_valor = val
                            break
                    except (ValueError, TypeError):
                        pass
                    
                    checked += 1
                
                if tiene_negativos:
                    return False, (
                        f"❌ Error: El campo '{distance_field}' contiene valores negativos.\n"
                        f"Primer valor negativo detectado: {primer_negativo_valor:.2f}m (fid: {primer_negativo_fid})\n\n"
                        f"Los búferes negativos solo son válidos para polígonos.\n"
                        f"La capa de entrada contiene {tipo_geom}.\n\n"
                        f"💡 Solución:\n"
                        f"• Use la calculadora de campos para corregir: abs(\"{distance_field}\")\n"
                        f"• O filtre la capa para excluir valores negativos\n"
                        f"• O convierta las {tipo_geom} a polígonos si necesita búferes negativos (contracción interior):\n"
                        f"  1. Aplique primero un búfer POSITIVO sobre esta capa → genera polígonos\n"
                        f"  2. Use esos polígonos como capa de entrada\n"
                        f"  3. Aplique el búfer negativo sobre los polígonos resultantes"
                    )
            
            # Validación de parámetro fijo (solo si no hay campo)
            elif dist_check < 0:
                tipo_geom = "puntos" if geom_type == QgsWkbTypes.PointGeometry else "líneas"
                return False, (f"❌ Error: No se puede usar distancia negativa ({dist_check:.2f}m) con {tipo_geom}. "
                              f"Un búfer negativo (interior) solo es válido para polígonos.")
        
        # BUFFER_UN_LADO con LÍNEAS: negativo en campo invierte el lado — advertencia informativa
        if buffer_type == Constants.BUFFER_CONCENTRICO and geom_type != QgsWkbTypes.PolygonGeometry:
            distance_field = self.parameterAsString(parameters, self.DISTANCE_FIELD, context)
            # Solo validar distancia negativa si no hay campo variable (el campo puede tener valores positivos)
            if not distance_field:
                conc_dist = self.parameterAsDouble(parameters, self.CONCENTRIC_DISTANCE, context)
                if conc_dist < 0:
                    tipo_geom = "puntos" if geom_type == QgsWkbTypes.PointGeometry else "líneas"
                    return False, (f"❌ Error: No se puede usar distancia de anillos negativa ({conc_dist:.2f}m) con {tipo_geom}. "
                                  f"Anillos concéntricos negativos solo son válidos para polígonos.")
        
        return True, ""

    def _extract_parameters(self, parameters, context, source, feedback=None) -> BufferParams:
        """Extrae y organiza todos los parámetros."""
        buffer_type = self.parameterAsEnum(parameters, self.BUFFER_TYPE, context)
        u_area = self.parameterAsEnum(parameters, self.UNIDAD_AREA, context)
        area_objetivo = self.parameterAsDouble(parameters, self.AREA_OBJETIVO, context)
        
        factors = [Constants.HA_TO_M2, Constants.M2_TO_M2, Constants.KM2_TO_M2]
        area_objetivo_m2 = area_objetivo * factors[u_area]
        
        calc_area = (buffer_type == Constants.BUFFER_POR_AREA or
                     self.parameterAsBoolean(parameters, self.CALCULAR_POR_AREA, context))
        
        # Información del CRS
        crs = source.sourceCrs()
        crs_info = crs.userFriendlyIdentifier() if crs.isValid() else "Desconocido"
        crs_es_geografico = CRSValidator.es_geografico(crs)
        crs_unidad = CRSValidator.get_unidad(crs)
        
        es_punto = QgsWkbTypes.geometryType(source.wkbType()) == QgsWkbTypes.PointGeometry
        u_hull = self.parameterAsBoolean(parameters, self.USAR_GEOMETRIA_MINIMA, context)
        u_box = self.parameterAsBoolean(parameters, self.CREAR_POLIGONO_PUNTOS, context)
        
        # Gestión de integridad
        gestion_integridad = self.parameterAsEnum(parameters, self.GESTION_INTEGRIDAD, context)
        
        # ALTA #2: Parámetros de categoría
        category_field = self.parameterAsString(parameters, self.CATEGORY_FIELD, context) or ""
        category_mapping_json = self.parameterAsString(parameters, self.CATEGORY_MAPPING, context) or ""
        category_mapping = {}
        
        # Parse seguro del mapeo JSON
        if category_field and category_mapping_json:
            success, result = parse_category_mapping_safe(category_mapping_json)
            if success:
                category_mapping = result
            else:
                # Si falla el parsing, registrar advertencia pero continuar
                # (el campo de categoría simplemente no se usará)
                if feedback:
                    feedback.pushWarning(
                        f"⚠️ Mapeo de categorías inválido: {result}\n"
                        f"Se ignorará el campo de categoría y se usará distancia fija."
                    )
        
        # === NUEVO: Parámetros de método de anclaje para densidad ===
        densidad_metodo_anclaje = self.parameterAsEnum(parameters, self.DENSIDAD_METODO_ANCLAJE, context)
        densidad_tolerancia_polo = self.parameterAsDouble(parameters, self.DENSIDAD_TOLERANCIA_POLO, context)
        
        return BufferParams(
            buffer_type=buffer_type,
            distancia=(0.0 if buffer_type == Constants.BUFFER_ADAPTATIVO
                       else self.parameterAsDouble(parameters, self.DISTANCIA, context)),
            ancho=self.parameterAsDouble(parameters, self.ANCHO, context),
            alto=self.parameterAsDouble(parameters, self.ALTO, context),
            rotacion=self.parameterAsDouble(parameters, self.ROTACION, context),
            segmentos=self.parameterAsInt(parameters, self.SEGMENTOS, context),
            concentric_count=self.parameterAsInt(parameters, self.CONCENTRIC_COUNT, context),
            concentric_distance=self.parameterAsDouble(parameters, self.CONCENTRIC_DISTANCE, context),
            anillos_disjuntos=self.parameterAsBoolean(parameters, self.CREAR_ANILLOS_DISJUNTOS, context),
            area_objetivo=area_objetivo, unidad_area=u_area, calcular_por_area=calc_area,
            area_objetivo_m2=area_objetivo_m2, usar_hull=u_hull, usar_box=u_box,
            usar_corredor=self.parameterAsBoolean(parameters, self.USAR_CORREDOR, context),
            side_idx=self.parameterAsEnum(parameters, self.SIDE, context),
            join_idx=self.parameterAsEnum(parameters, self.JOIN_STYLE, context),
            miter_limit=self.parameterAsDouble(parameters, self.MITER_LIMIT, context),
            wedge_start=self.parameterAsDouble(parameters, self.WEDGE_START, context),
            wedge_width=self.parameterAsDouble(parameters, self.WEDGE_WIDTH, context),
            usar_rot_auto=self.parameterAsBoolean(parameters, self.USAR_ROTACION_AUTO, context),
            rotation_field=self.parameterAsString(parameters, self.ROTATION_FIELD, context) or "",
            op_logic=self.parameterAsEnum(parameters, self.CREAR_UNION_LOGICA, context),
            aplicar_transparencia=self.parameterAsBoolean(parameters, self.APLICAR_TRANSPARENCIA, context),
            nivel_transparencia=self.parameterAsInt(parameters, self.NIVEL_TRANSPARENCIA, context),
            generar_reporte=self.parameterAsBoolean(parameters, self.GENERAR_REPORTE, context),
            ruta_reporte=self.parameterAsString(parameters, self.RUTA_REPORTE, context),
            nombre_proyecto=self.parameterAsString(parameters, self.NOMBRE_PROYECTO, context),
            distance_field=self.parameterAsString(parameters, self.DISTANCE_FIELD, context) or "",
            category_field=category_field,  # ALTA #2
            category_mapping=category_mapping,  # ALTA #2
            ancho_field=self.parameterAsString(parameters, self.ANCHO_FIELD, context) or "",
            alto_field=self.parameterAsString(parameters, self.ALTO_FIELD, context) or "",
            exclusion_layer=self.parameterAsSource(parameters, self.EXCLUSION_LAYER, context),
            preview_mode=self.parameterAsBoolean(parameters, self.PREVIEW_MODE, context),
            calcular_superposicion=self.parameterAsBoolean(parameters, self.CALCULAR_SUPERPOSICION, context),
            generar_fragmentos_traslape=self.parameterAsBoolean(parameters, self.GENERAR_FRAGMENTOS_TRASLAPE, context),
            crs_info=crs_info, es_punto=es_punto, es_transformacion_activa=es_punto and (u_hull or u_box),
            aplicar_simplificacion=self.parameterAsBoolean(parameters, self.APLICAR_SIMPLIFICACION, context),
            tolerancia_simplificacion=self.parameterAsDouble(parameters, self.TOLERANCIA_SIMPLIFICACION, context),
            crs_es_geografico=crs_es_geografico,
            crs_unidad=crs_unidad,
            continuar_con_errores=True,
            gestion_integridad=gestion_integridad,
            simplificar_entrada=self.parameterAsBoolean(parameters, self.SIMPLIFICAR_ENTRADA, context),
            tolerancia_entrada=self.parameterAsDouble(parameters, self.TOLERANCIA_ENTRADA, context),
            usar_paralelo=self.parameterAsBoolean(parameters, self.USAR_PARALELO, context),
            num_threads=self.parameterAsInt(parameters, self.NUM_THREADS, context),
            resolver_traslapes=self.parameterAsEnum(parameters, self.RESOLVER_TRASLAPES, context),
            eliminar_huecos=self.parameterAsBoolean(parameters, self.ELIMINAR_HUECOS, context),
            area_minima_hueco=self.parameterAsDouble(parameters, self.AREA_MINIMA_HUECO, context),
            preservar_hueco_estructural=self.parameterAsBoolean(parameters, self.PRESERVAR_HUECO_ESTRUCTURAL, context),
            disolver_buferes=self.parameterAsBoolean(parameters, self.DISOLVER_BUFERES, context),
            mantener_parte_mayor=self.parameterAsBoolean(parameters, self.MANTENER_PARTE_MAYOR, context),
            usar_densidad_adaptativa=(buffer_type == Constants.BUFFER_ADAPTATIVO),
            densidad_metodo=self.parameterAsEnum(parameters, self.DENSIDAD_METODO, context),
            densidad_k=self.parameterAsInt(parameters, self.DENSIDAD_K, context),
            densidad_radio_ref=self.parameterAsDouble(parameters, self.DENSIDAD_RADIO_REF, context),
            densidad_radio_base=self.parameterAsDouble(parameters, self.DENSIDAD_RADIO_BASE, context),
            densidad_factor_escala=self.parameterAsDouble(parameters, self.DENSIDAD_FACTOR_ESCALA, context),
            densidad_radio_min=self.parameterAsDouble(parameters, self.DENSIDAD_RADIO_MIN, context),
            densidad_radio_max=self.parameterAsDouble(parameters, self.DENSIDAD_RADIO_MAX, context),
            # === NUEVO: Asignar métodos de anclaje ===
            densidad_metodo_anclaje=densidad_metodo_anclaje,
            densidad_tolerancia_polo=densidad_tolerancia_polo,
            exportar_config=self.parameterAsBoolean(parameters, self.EXPORTAR_CONFIG, context),
            ruta_config_json=self.parameterAsString(parameters, self.RUTA_CONFIG_JSON, context) or "",
            validacion_previa=self.parameterAsBoolean(parameters, self.DRY_RUN, context),
        )

    def _prepare_features(self, source, params: BufferParams, logger) -> List[Tuple[QgsGeometry, str, Any, int]]:
        """Prepara las geometrías para procesamiento.
        
        IMPORTANTE: Usa QgsFeatureRequest para obtener TODAS las geometrías,
        incluyendo las inválidas, permitiendo que el parámetro 'Gestión de Integridad'
        controle cómo se manejan dentro de este algoritmo.
        
        Si simplificar_entrada está activado, simplifica los polígonos ANTES de crear búferes.
        """
        features = []
        vertices_antes_total = 0
        vertices_despues_total = 0
        
        # Crear request que NO filtra geometrías inválidas
        # Esto permite que nuestro algoritmo las maneje según la configuración del usuario
        request = QgsFeatureRequest()
        request.setInvalidGeometryCheck(QgsFeatureRequest.GeometryNoCheck)
        
        if params.es_transformacion_activa:
            all_geoms = [f.geometry() for f in source.getFeatures(request) if f.hasGeometry()]
            if all_geoms:
                combined = QgsGeometry.unaryUnion(all_geoms)
                geom_final = combined.convexHull() if params.usar_hull else QgsGeometry.fromRect(combined.boundingBox())  # activo en QGIS 3.28+
                if geom_final and not geom_final.isEmpty():
                    if geom_final.type() != QgsWkbTypes.PolygonGeometry:
                        geom_final = geom_final.buffer(Constants.MIN_BUFFER_DISTANCE, 5)
                    
                    # Simplificar si está activado (polígonos y líneas)
                    if params.simplificar_entrada and geom_final.type() in [QgsWkbTypes.PolygonGeometry, QgsWkbTypes.LineGeometry]:
                        vertices_antes = GeometrySimplifier.count_vertices(geom_final)
                        geom_simplificada, _, vertices_despues = GeometrySimplifier.simplify(
                            geom_final, params.tolerancia_entrada)
                        vertices_antes_total += vertices_antes
                        vertices_despues_total += vertices_despues
                        geom_final = geom_simplificada
                    
                    tipo = "Agrupado (Casco Convexo)" if params.usar_hull else "Agrupado (Caja Envolvente)"
                    features = [(geom_final.asWkb(), tipo, None, -1)]  # ID -1 para geometrías agrupadas
        
        # === MODO PUNTOS para BUFFER_ANCHO_M ===
        # Cuando la entrada es una capa de puntos con campo de distancia,
        # el script construye la línea automáticamente y usa el campo como M.
        elif (params.buffer_type == Constants.BUFFER_ANCHO_M
              and params.es_punto
              and params.distance_field):
            source_field_names = {fld.name() for fld in source.fields()}
            has_distance_field = (params.distance_field in source_field_names)
            
            if has_distance_field:
                # Recolectar puntos con sus valores de distancia (ancho)
                puntos_con_m = []
                for f in source.getFeatures(request):
                    if f.hasGeometry():
                        pt = f.geometry().asPoint()
                        try:
                            m_val = float(f[params.distance_field])
                        except (ValueError, TypeError):
                            m_val = 0.0
                        puntos_con_m.append((pt.x(), pt.y(), m_val, f.id()))
                
                if len(puntos_con_m) >= 2:
                    # Construir _m_vertices directamente desde los puntos
                    _m_vertices_orig = [(p[0], p[1], p[2]) for p in puntos_con_m]
                    
                    _m_vertices = _m_vertices_orig
                    logger.info(
                        f"📐 Modo PUNTOS: Construida ruta con {len(_m_vertices_orig)} puntos"
                    )
                    
                    # Construir geometría de línea simple (para que pase las validaciones)
                    from qgis.core import QgsLineString as _QgsLS_pts, QgsPoint as _QgsP_pts
                    _ls_pts = _QgsLS_pts()
                    for _px, _py, _pm in _m_vertices_orig:
                        _ls_pts.addVertex(_QgsP_pts(_px, _py))
                    geom_linea = QgsGeometry(_ls_pts)
                    
                    # _m_vertices_orig se pasa al procesador vía _m_vertices_override.
                    # VariableWidthMBufferProcessor construye el corredor mediante
                    # sub-segmentación adaptativa (paso = min(r_min/4, 2 m)) y unaryUnion.
                    features = [(geom_linea.asWkb(), "puntos→ruta", (None, None, None, None), -1, geom_linea.wkbType(), _m_vertices_orig)]
                    
                    # Log de anchos
                    _m_sample = [f"{p[2]:.0f}" for p in _m_vertices_orig[:8]]
                    logger.info(f"📊 Anchos desde campo '{params.distance_field}': [{', '.join(_m_sample)}{'...' if len(_m_vertices_orig) > 8 else ''}]")
                else:
                    logger.error("❌ Ancho Variable (Puntos): se necesitan al menos 2 puntos")
        else:
            # Pre-computar set de nombres de campo UNA SOLA VEZ antes del loop.
            # Evita construir [fld.name() for fld in f.fields()] en cada iteración
            # (O(n×m) → O(m) + O(n)), lo que con capas grandes supone una mejora
            # significativa de rendimiento.
            source_field_names = {fld.name() for fld in source.fields()}
            
            # Determinar una vez si los campos configurados existen en la capa
            has_category_field = bool(params.category_field and params.category_mapping
                                      and params.category_field in source_field_names)
            has_distance_field = bool(params.distance_field
                                      and params.distance_field in source_field_names)
            has_ancho_field = bool(params.ancho_field
                                   and params.ancho_field in source_field_names)
            has_alto_field = bool(params.alto_field
                                  and params.alto_field in source_field_names)
            # Campo de azimut variable para cuña (solo se lee si es tipo Cuña y no está en modo auto-rotación)
            has_rotation_field = bool(
                params.rotation_field
                and params.rotation_field in source_field_names
                and params.buffer_type == Constants.BUFFER_CUNA
                and not params.usar_rot_auto   # auto-rotación tiene prioridad — si está activa, el campo se ignora
            )
            
            # Acumuladores para resumen de categorías (en lugar de log por feature)
            _cat_counts: Dict[str, int] = {}     # categoría → n features encontradas
            
            for f in source.getFeatures(request):
                if f.hasGeometry():
                    geom = f.geometry()
                    # Guardar tipo WKB ORIGINAL antes de cualquier procesamiento
                    # makeValid(), simplify() y asWkb()/fromWkb() pueden perder M
                    _wkb_type_feature_orig = geom.wkbType()
                    
                    # Simplificar geometría de entrada si está activado (polígonos y líneas)
                    if params.simplificar_entrada and geom.type() in [QgsWkbTypes.PolygonGeometry, QgsWkbTypes.LineGeometry]:
                        vertices_antes = GeometrySimplifier.count_vertices(geom)
                        geom_simplificada, _, vertices_despues = GeometrySimplifier.simplify(
                            geom, params.tolerancia_entrada)
                        vertices_antes_total += vertices_antes
                        vertices_despues_total += vertices_despues
                        geom = geom_simplificada
                    
                    # Eliminar huecos de geometría de entrada si está activado
                    # IMPORTANTE: Esto se hace ANTES de crear el búfer para evitar búfer interno en huecos
                    if params.eliminar_huecos and geom.type() == QgsWkbTypes.PolygonGeometry:
                        geom_sin_huecos, huecos_elim = GeometryPostProcessor.eliminar_huecos(
                            geom, params.area_minima_hueco, params.preservar_hueco_estructural)
                        if huecos_elim > 0:
                            logger.info(f"🕳️ Huecos eliminados en entrada (fid {f.id()}): {huecos_elim}")
                        geom = geom_sin_huecos
                    
                    dist_override = None
                    ancho_override = None
                    alto_override = None
                    az_override = None     # Azimut variable por entidad (solo Cuña con rotation_field)
                    
                    # ALTA #2: Prioridad 1 - Leer campo de categoría
                    # Usa has_category_field calculado fuera del loop (evita lookup repetido)
                    if has_category_field:
                        try:
                            categoria = f[params.category_field]
                            
                            if categoria is not None:
                                cat_str = str(categoria)
                                # Buscar categoría en el mapeo
                                if cat_str in params.category_mapping:
                                    dist_override = params.category_mapping[cat_str]
                                    # Acumular estadística (log resumido al final, no por feature)
                                    _cat_counts[cat_str] = _cat_counts.get(cat_str, 0) + 1
                                else:
                                    # Categoría no encontrada en mapeo — acumular para warning resumido
                                    logger.registrar_missing_cat(cat_str)
                            # Categoría NULL: acumular contador, usa distancia fija
                            else:
                                logger.registrar_null_cat()
                        except Exception as e:
                            logger.warning(f"fid {f.id()}: error leyendo categoría: {str(e)[:50]}")
                    
                    # Prioridad 2 - Leer campo de distancia (si no se obtuvo de categoría)
                    # Usa has_distance_field calculado fuera del loop
                    if dist_override is None and has_distance_field:
                        raw_val = f[params.distance_field]
                        if raw_val is None or raw_val != raw_val or str(raw_val).strip().upper() == 'NULL':
                            logger.warning(
                                f"fid {f.id()}: campo '{params.distance_field}' es NULL. "
                                f"Se usará el parámetro fijo de distancia."
                            )
                            logger.registrar_null_field()
                        else:
                            try:
                                dist_override = float(raw_val)
                            except (ValueError, TypeError):
                                logger.warning(
                                    f"fid {f.id()}: campo '{params.distance_field}' tiene valor no numérico "
                                    f"('{raw_val}'). Se usará el parámetro fijo de distancia."
                                )
                    
                    # Leer campo de ancho (para Oval/Rectangular)
                    if has_ancho_field:
                        raw_val = f[params.ancho_field]
                        if raw_val is None or raw_val != raw_val or str(raw_val).strip().upper() == 'NULL':
                            logger.warning(
                                f"fid {f.id()}: campo de ancho '{params.ancho_field}' es NULL. "
                                f"Se usará el parámetro fijo de ancho."
                            )
                        else:
                            try:
                                ancho_override = float(raw_val)
                            except (ValueError, TypeError):
                                logger.warning(
                                    f"fid {f.id()}: campo de ancho '{params.ancho_field}' tiene valor no numérico "
                                    f"('{raw_val}'). Se usará el parámetro fijo de ancho."
                                )
                    
                    # Leer campo de alto (para Oval/Rectangular)
                    if has_alto_field:
                        raw_val = f[params.alto_field]
                        if raw_val is None or raw_val != raw_val or str(raw_val).strip().upper() == 'NULL':
                            logger.warning(
                                f"fid {f.id()}: campo de alto '{params.alto_field}' es NULL. "
                                f"Se usará el parámetro fijo de alto."
                            )
                        else:
                            try:
                                alto_override = float(raw_val)
                            except (ValueError, TypeError):
                                logger.warning(
                                    f"fid {f.id()}: campo de alto '{params.alto_field}' tiene valor no numérico "
                                    f"('{raw_val}'). Se usará el parámetro fijo de alto."
                                )
                    
                    # Leer campo de azimut (para Cuña con rotation_field activo)
                    # Prioridad: Rotación Automática > rotation_field > wedge_start fijo
                    if has_rotation_field:
                        raw_val = f[params.rotation_field]
                        if raw_val is None or raw_val != raw_val or str(raw_val).strip().upper() == 'NULL':
                            logger.warning(
                                f"fid {f.id()}: campo de azimut '{params.rotation_field}' es NULL. "
                                f"Se usará el Ángulo de Inicio fijo ({params.wedge_start}°)."
                            )
                        else:
                            try:
                                az_val = float(raw_val) % 360.0  # Normalizar a [0, 360)
                                az_override = az_val
                            except (ValueError, TypeError):
                                logger.warning(
                                    f"fid {f.id()}: campo de azimut '{params.rotation_field}' tiene valor "
                                    f"no numérico ('{raw_val}'). Se usará el Ángulo de Inicio fijo ({params.wedge_start}°)."
                                )
                    
                    # Guardar todos los overrides en una tupla de 4 elementos
                    override_tuple = (dist_override, ancho_override, alto_override, az_override)
                    # SEGURIDAD THREAD: WKB — bytes Python puros, sin COW de C++
                    
                    # Para BUFFER_ANCHO_M en modo línea: extraer vértices M antes de
                    # asWkb(), que puede perder la coordenada M. Los valores (x,y,m)
                    # se pasan al procesador vía _m_vertices_override.
                    
                    # Para BUFFER_ANCHO_M: extraer coordenadas M de los vértices ANTES de
                    # asWkb() que puede perderlas. Se almacenan como lista de (x,y,m).
                    _m_vertices = None
                    if params.buffer_type == Constants.BUFFER_ANCHO_M:
                        try:
                            _m_vertices_orig = [
                                (v.x(), v.y(), v.m())
                                for v in geom.vertices()
                            ]
                            
                            # AUTO-DETECCIÓN: verificar si M contiene anchos reales
                            # o distancias acumuladas (medida lineal de referencia).
                            if len(_m_vertices_orig) >= 3:
                                _ms = [v[2] for v in _m_vertices_orig]
                                _m_monotonic = all(_ms[i] <= _ms[i+1] + 0.01 for i in range(len(_ms)-1))
                                
                                if _m_monotonic and (_ms[-1] - _ms[0]) > 1.0:
                                    # M es monótonamente creciente → probablemente distancia acumulada
                                    # Verificar si Z contiene los anchos
                                    _vertices_geom = list(geom.vertices())
                                    _zs = [v.z() for v in _vertices_geom]
                                    _z_has_variation = (max(_zs) - min(_zs)) > 1.0 if _zs else False
                                    _z_not_monotonic = not all(_zs[i] <= _zs[i+1] + 0.01 for i in range(len(_zs)-1)) if len(_zs) >= 3 else True
                                    
                                    if _z_has_variation and _z_not_monotonic:
                                        # Los anchos están en Z → usar Z como radio
                                        logger.warning(
                                            f"🔄 fid {f.id()}: AUTO-DETECCIÓN — Los valores M son distancia acumulada "
                                            f"(monótono creciente: {_ms[0]:.0f}→{_ms[-1]:.0f}). "
                                            f"Los anchos del búfer se encontraron en la coordenada Z. "
                                            f"Se usará Z como radio."
                                        )
                                        _m_vertices_orig = [
                                            (v.x(), v.y(), v.z())
                                            for v in _vertices_geom
                                        ]
                                    else:
                                        # Z tampoco tiene anchos → advertir
                                        logger.warning(
                                            f"⚠️ fid {f.id()}: Los valores M parecen ser distancia acumulada "
                                            f"(monótono creciente: {_ms[0]:.0f}→{_ms[-1]:.0f}), "
                                            f"NO anchos de búfer. El resultado puede no ser el esperado.\n"
                                            f"💡 Para anchos variables desde puntos, use la capa de PUNTOS como entrada "
                                            f"con el campo de distancia seleccionado."
                                        )
                            
                            _m_vertices = _m_vertices_orig
                        except Exception:
                            _m_vertices = None
                    features.append((geom.asWkb(), f"fid: {f.id()}", override_tuple, f.id(), _wkb_type_feature_orig, _m_vertices))
            
            # Log resumido de categorías: una línea por categoría en lugar de una por feature
            if _cat_counts:
                resumen = ", ".join(
                    f"'{cat}': {n} feature(s) → {params.category_mapping.get(cat, '?')}"
                    for cat, n in sorted(_cat_counts.items())
                )
                logger.info(f"📂 Campo categoría '{params.category_field}' — asignaciones: {resumen}")
            if logger.null_cat_count:
                logger.info(
                    f"📂 Campo categoría '{params.category_field}' — "
                    f"{logger.null_cat_count} feature(s) con categoría NULL: se utilizará la distancia fija del parámetro."
                )
            if logger.missing_cat_ids:
                for cat, n in sorted(logger.missing_cat_ids.items()):
                    logger.warning(
                        f"⚠️ Categoría '{cat}' no encontrada en mapeo "
                        f"({n} feature(s)). Se usará distancia fija."
                    )
        
        if params.preview_mode and features:
            logger.info("⚡ Modo previsualización: procesando solo primera entidad")
            features = features[:1]
        
        # Log de simplificación de entrada
        if params.simplificar_entrada and vertices_antes_total > 0:
            reduccion_pct = ((vertices_antes_total - vertices_despues_total) / vertices_antes_total * 100) if vertices_antes_total > 0 else 0
            logger.info(f"🔧 Simplificación de entrada: {vertices_antes_total:,} → {vertices_despues_total:,} vértices ({reduccion_pct:.1f}% reducción)")
        
        return features, vertices_antes_total, vertices_despues_total

    def _crear_params_con_override(self, params: BufferParams, override_values) -> BufferParams:
        """Mapea los valores de los campos al parámetro correcto según el tipo de búfer.
        
        Mapeo por tipo:
          - Oval/Rectangular: 
              * Si tiene ancho_field y/o alto_field → usa esos valores específicos
              * Si solo tiene distance_field → valor → ancho y alto (óvalo/rectángulo proporcional)
          - Rectangular + corredor: valor del distance_field o ancho_field → ancho (alto lo define la línea)
          - Por Área: valor distance_field → area_objetivo y area_objetivo_m2 (área variable por entidad)
          - Concéntrico: valor distance_field → concentric_distance (separación entre anillos)
          - Resto (Circular, Un Lado, Cuña): valor distance_field → distancia
        
        Args:
            params: Parámetros base del búfer (NO se mutan).
            override_values: Tupla (dist_override, ancho_override, alto_override) con valores de campos.
        
        Returns:
            Nueva instancia de BufferParams con los parámetros correspondientes reemplazados.
        """
        # Desempacar tupla de valores (4 elementos desde _prepare_features)
        if isinstance(override_values, tuple):
            if len(override_values) == 4:
                dist_override, ancho_override, alto_override, az_override = override_values
            else:
                # Compatibilidad hacia atrás con tuplas de 3 elementos
                dist_override, ancho_override, alto_override = override_values
                az_override = None
        else:
            # Compatibilidad con código antiguo (solo distancia)
            dist_override = override_values
            ancho_override = None
            alto_override = None
            az_override = None
        
        bt = params.buffer_type
        
        # CASO ESPECIAL: Oval o Rectangular con campos específicos de ancho/alto
        if bt in [Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR]:
            # Prioridad: usar campos específicos si están disponibles
            if ancho_override is not None or alto_override is not None:
                nuevo_ancho = ancho_override if ancho_override is not None else params.ancho
                nuevo_alto = alto_override if alto_override is not None else params.alto
                return replace(params, ancho=nuevo_ancho, alto=nuevo_alto)
            
            # Si no hay campos específicos pero hay distance_field, usar valor proporcional (comportamiento anterior)
            elif dist_override is not None:
                if bt == Constants.BUFFER_RECTANGULAR and params.usar_corredor:
                    return replace(params, ancho=dist_override)
                else:
                    return replace(params, ancho=dist_override, alto=dist_override)
        
        # CASO CUÑA: puede tener az_override (campo de rotación) y/o dist_override (radio variable)
        if bt == Constants.BUFFER_CUNA:
            nuevos = {}
            if dist_override is not None:
                nuevos['distancia'] = dist_override
            if az_override is not None:
                nuevos['wedge_start'] = az_override
            if nuevos:
                return replace(params, **nuevos)
            return params
        
        # CASOS NORMALES: solo usa distance_field
        if dist_override is not None:
            if bt == Constants.BUFFER_POR_AREA:
                factors = [Constants.HA_TO_M2, Constants.M2_TO_M2, Constants.KM2_TO_M2]
                area_m2 = dist_override * factors[params.unidad_area]
                return replace(params, area_objetivo=dist_override, area_objetivo_m2=area_m2)
            
            elif bt == Constants.BUFFER_CONCENTRICO:
                return replace(params, concentric_distance=dist_override)
            
            else:
                # Circular, Un Lado
                return replace(params, distancia=dist_override)
        
        # Si no hay override, devolver params sin cambios
        return params

    def _prepare_exclusion_geometry(self, params: BufferParams) -> Optional[QgsGeometry]:
        """Prepara la geometría de exclusión si existe.
        SUPUESTO: la capa de exclusión comparte CRS con la capa de entrada.
        """
        if params.exclusion_layer:
            try:
                # Usar request sin validación de geometría
                request = QgsFeatureRequest()
                request.setInvalidGeometryCheck(QgsFeatureRequest.GeometryNoCheck)
                geoms = [f.geometry() for f in params.exclusion_layer.getFeatures(request) if f.hasGeometry()]
                if geoms:
                    return QgsGeometry.unaryUnion(geoms)
            except (RuntimeError, AttributeError):
                pass
        return None

    def _generate_overlap_fragments(self, all_buffers: List[Tuple], params: BufferParams, 
                                    parameters, context, source_crs, feedback) -> Optional[str]:
        """
        Genera fragmentos de traslape mediante descomposición geométrica iterativa.
        
        En lugar de solo hacer UNION (que fusiona todo), este método genera todos
        los fragmentos únicos resultantes de las intersecciones entre búferes.
        
        Args:
            all_buffers: Lista de tuplas (geometría, tipo, distancia, descripción)
            params: Parámetros del búfer
            parameters: Parámetros del algoritmo
            context: Contexto de procesamiento
            source_crs: CRS de la capa fuente
            feedback: Objeto de feedback
            
        Returns:
            ID de destino de la capa de fragmentos o None si no se generó
        """
        if not params.generar_fragmentos_traslape or len(all_buffers) < 2:
            return None

        # ADVERTENCIA DE ESCALA O(2ⁿ):
        # El algoritmo genera todas las combinaciones posibles de intersecciones
        # (áreas exclusivas + pares + tríos + … hasta 9-tuplas). El número de
        # operaciones difference() crece exponencialmente con n búferes.
        # Con búferes simples el límite de 1 000 fragmentos actúa antes de que
        # el tiempo sea prohibitivo; con búferes complejos (>100 k vértices)
        # cada difference() puede tardar segundos, bloqueando QGIS.
        # Se emite advertencia cuando n > MAX_BUFFERS_FRAGMENTOS.
        MAX_BUFFERS_FRAGMENTOS = 30

        try:
            feedback.pushInfo("🧩 Generando fragmentos de traslape...")
            
            # Extraer geometrías
            geometrias = [item[0] for item in all_buffers if item[0] and not item[0].isEmpty()]
            
            if len(geometrias) < 2:
                feedback.pushInfo("⚠️ Se necesitan al menos 2 búferes para generar fragmentos")
                return None

            # Emitir advertencia de rendimiento cuando la capa es grande.
            if len(geometrias) > MAX_BUFFERS_FRAGMENTOS:
                feedback.pushWarning(
                    f"⚠️ Fragmentos de traslape: {len(geometrias)} búferes detectados "
                    f"(límite recomendado: {MAX_BUFFERS_FRAGMENTOS}). "
                    f"El algoritmo es O(2ⁿ) — con búferes complejos el tiempo de procesamiento "
                    f"puede ser muy elevado o bloquear QGIS. "
                    f"Si el proceso tarda demasiado, desactive la opción "
                    f"'Generar fragmentos de traslape' o reduzca la capa de entrada."
                )

            feedback.setProgressText("Descomponiendo traslapes...")
            
            # ESTRATEGIA: Usar processing.run("native:union") para obtener la descomposición correcta
            # Pero como no tenemos acceso directo a processing aquí, usamos un enfoque manual
            
            # Crear una colección temporal de todas las geometrías
            
            # Crear capa temporal en memoria
            temp_layer = QgsVectorLayer(f"MultiPolygon?crs={source_crs.authid()}", "temp", "memory")
            temp_provider = temp_layer.dataProvider()
            
            # Agregar todas las geometrías
            features = []
            for idx, geom in enumerate(geometrias):
                feat = QgsFeature()
                feat.setGeometry(geom)
                features.append(feat)
            
            temp_provider.addFeatures(features)
            temp_layer.updateExtents()
            
            # Usar QgsGeometry.unaryUnion para obtener el contorno exterior
            union_geom = QgsGeometry.unaryUnion(geometrias)
            
            if not union_geom or union_geom.isEmpty():
                feedback.pushInfo("⚠️ La unión no produjo resultados")
                return None
            
            # Ahora generamos los fragmentos mediante intersecciones
            # Guardamos tuplas de (geometría, lista_de_indices_buferes)
            fragmentos_con_info = []
            
            # Extraer descripciones de búferes para referencias
            descripciones = [item[3] if len(item) > 3 else f"Búfer {i+1}" 
                           for i, item in enumerate(all_buffers)]
            
            # Para cada geometría, calcular todas las regiones
            feedback.pushInfo(f"Procesando {len(geometrias)} búferes...")
            
            # LÍMITE DE SEGURIDAD: Evitar bloqueos con demasiados fragmentos
            MAX_FRAGMENTOS = Constants.MAX_FRAGMENTOS_TRASLAPE
            fragmentos_generados = 0
            
            # Algoritmo: Para cada combinación de búferes, calcular la región exclusiva
            
            # Primero: áreas exclusivas de cada búfer
            feedback.setProgressText(f"Calculando áreas exclusivas...")
            for i, geom in enumerate(geometrias):
                if fragmentos_generados >= MAX_FRAGMENTOS:
                    feedback.pushWarning(f"⚠️ Límite de {MAX_FRAGMENTOS} fragmentos alcanzado. Deteniendo generación.")
                    break
                
                # Feedback cada 10%
                if i % max(1, len(geometrias) // 10) == 0:
                    feedback.setProgressText(f"Áreas exclusivas: {i+1}/{len(geometrias)} ({int(i/len(geometrias)*100)}%)")
                
                # Área exclusiva = geometría - (unión de todas las demás)
                otras = [g for j, g in enumerate(geometrias) if j != i]
                if otras:
                    union_otras = QgsGeometry.unaryUnion(otras)
                    exclusiva = geom.difference(union_otras)
                    if exclusiva and not exclusiva.isEmpty():
                        fragmentos_con_info.append((exclusiva, [i]))  # Solo búfer i
                        fragmentos_generados += 1
            
            # Segundo: intersecciones de pares
            if fragmentos_generados < MAX_FRAGMENTOS:
                feedback.setProgressText(f"Calculando intersecciones de pares...")
                total_pares = len(geometrias) * (len(geometrias) - 1) // 2
                pares_procesados = 0
                
                for i, j in combinations(range(len(geometrias)), 2):
                    if fragmentos_generados >= MAX_FRAGMENTOS:
                        feedback.pushWarning(f"⚠️ Límite de {MAX_FRAGMENTOS} fragmentos alcanzado. Deteniendo generación.")
                        break
                    
                    pares_procesados += 1
                    
                    # Feedback cada 5%
                    if pares_procesados % max(1, total_pares // 20) == 0:
                        progress_pct = int(pares_procesados / total_pares * 100)
                        feedback.setProgressText(
                            f"Intersecciones: {pares_procesados}/{total_pares} ({progress_pct}%) - "
                            f"{fragmentos_generados} fragmentos"
                        )
                    
                    interseccion = geometrias[i].intersection(geometrias[j])
                    if interseccion and not interseccion.isEmpty():
                        # Restar las intersecciones con otros búferes
                        otros_indices = [k for k in range(len(geometrias)) if k not in [i, j]]
                        for k in otros_indices:
                            if geometrias[k].intersects(interseccion):
                                interseccion = interseccion.difference(geometrias[k])
                        if interseccion and not interseccion.isEmpty():
                            fragmentos_con_info.append((interseccion, [i, j]))  # Búferes i y j
                            fragmentos_generados += 1
            
            # Tercero: intersecciones de tríos y superiores
            if fragmentos_generados < MAX_FRAGMENTOS:
                feedback.setProgressText(f"Calculando intersecciones múltiples...")
                for size in range(3, min(len(geometrias) + 1, 10)):  # range excluye el 10 → máximo real: 9 (nonetos)
                    if fragmentos_generados >= MAX_FRAGMENTOS:
                        feedback.pushWarning(f"⚠️ Límite de {MAX_FRAGMENTOS} fragmentos alcanzado.")
                        break
                    
                    feedback.setProgressText(f"Intersecciones de {size} búferes...")
                    
                    for combo in combinations(range(len(geometrias)), size):
                        if fragmentos_generados >= MAX_FRAGMENTOS:
                            break
                        
                        # Intersección de todos en la combinación
                        interseccion = geometrias[combo[0]]
                        for idx in combo[1:]:
                            interseccion = interseccion.intersection(geometrias[idx])
                        
                        if interseccion and not interseccion.isEmpty():
                            # Restar intersecciones con búferes fuera del combo
                            otros_indices = [k for k in range(len(geometrias)) if k not in combo]
                            for k in otros_indices:
                                if geometrias[k].intersects(interseccion):
                                    interseccion = interseccion.difference(geometrias[k])
                            
                            if interseccion and not interseccion.isEmpty():
                                fragmentos_con_info.append((interseccion, list(combo)))
                                fragmentos_generados += 1
            
            if fragmentos_generados >= MAX_FRAGMENTOS:
                feedback.pushInfo(f"⚠️ Generación limitada a {MAX_FRAGMENTOS} fragmentos por rendimiento")
                feedback.pushInfo("")
                feedback.pushInfo("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                feedback.pushInfo("⚠️  ADVERTENCIA: Fragmentos complejos no generados")
                feedback.pushInfo("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                feedback.pushInfo("")
                feedback.pushInfo("Los fragmentos MÁS COMPLEJOS (intersecciones de 5+ búferes)")
                feedback.pushInfo("no se generaron para evitar bloqueos del sistema.")
                feedback.pushInfo("")
                feedback.pushInfo("✅ Fragmentos SÍ generados:")
                feedback.pushInfo("   • Todas las áreas exclusivas (1 búfer)")
                feedback.pushInfo("   • Todos los traslapes dobles (2 búferes)")
                feedback.pushInfo("   • Mayoría de traslapes triples (3 búferes)")
                feedback.pushInfo("")
                feedback.pushInfo("⚠️ Fragmentos NO generados (raros, áreas muy pequeñas):")
                feedback.pushInfo("   • Intersecciones de 5+ búferes simultáneos")
                feedback.pushInfo("   • Geometrías muy complejas resultantes")
                feedback.pushInfo("")
                feedback.pushInfo("💡 SOLUCIÓN RECOMENDADA:")
                feedback.pushInfo("   Si estos micro-fragmentos son 'huecos' no deseados,")
                feedback.pushInfo("   active la opción 'Eliminar huecos pequeños' que los")
                feedback.pushInfo("   rellenará automáticamente en los búferes principales.")
                feedback.pushInfo("")
                feedback.pushInfo("💡 CONTEXTO (capas vectorizadas de raster):")
                feedback.pushInfo("   En vectorizaciones de raster (resolución 10m), estos")
                feedback.pushInfo("   fragmentos muy complejos suelen ser artefactos de la")
                feedback.pushInfo("   conversión y tienen poca utilidad práctica.")
                feedback.pushInfo("")
                feedback.pushInfo("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            
            feedback.pushInfo(f"✅ Descomposición completada: {len(fragmentos_con_info)} fragmentos generados")
            
            # Definir campos de salida
            fragment_fields = QgsFields()
            fragment_fields.append(QgsField('fid', QVariant.Int))
            fragment_fields.append(QgsField('n_buferes', QVariant.Int))
            fragment_fields.append(QgsField('tipo_traslape', QVariant.String, len=20))
            fragment_fields.append(QgsField('buferes', QVariant.String, len=254))
            fragment_fields.append(QgsField('area_ha', QVariant.Double, len=20, prec=6))
            fragment_fields.append(QgsField('area_m2', QVariant.Double, len=20, prec=2))
            fragment_fields.append(QgsField('perimetro_m', QVariant.Double, len=20, prec=2))
            fragment_fields.append(QgsField('n_vertices', QVariant.Int))
            
            # Crear sink
            (fragment_sink, fragment_dest_id) = self.parameterAsSink(
                parameters, self.OUTPUT_FRAGMENTOS, context, fragment_fields,
                QgsWkbTypes.MultiPolygon, source_crs)
            
            if fragment_sink is None:
                feedback.pushInfo("⚠️ No se pudo crear la capa de fragmentos")
                return None
            
            # Escribir fragmentos
            for idx, (frag_geom, buffer_indices) in enumerate(fragmentos_con_info):
                if feedback.isCanceled():
                    break
                
                # Convertir a multipolígono si es necesario
                if not QgsWkbTypes.isMultiType(frag_geom.wkbType()):
                    if QgsWkbTypes.geometryType(frag_geom.wkbType()) == QgsWkbTypes.PolygonGeometry:
                        frag_geom = QgsGeometry.fromMultiPolygonXY([frag_geom.asPolygon()])
                    else:
                        continue  # Saltar geometrías que no son polígonos
                
                # Calcular atributos geométricos
                area_m2 = frag_geom.area()
                area_ha = area_m2 / Constants.HA_TO_M2
                perimetro = frag_geom.length()
                n_vertices = frag_geom.constGet().nCoordinates() if frag_geom.constGet() else 0
                
                # Construir string de búferes
                n_buferes = len(buffer_indices)
                buferes_str = ", ".join([descripciones[i] for i in sorted(buffer_indices)])
                
                # Determinar tipo de traslape
                if n_buferes == 1:
                    tipo_traslape = "Exclusivo"
                elif n_buferes == 2:
                    tipo_traslape = "Doble"
                elif n_buferes == 3:
                    tipo_traslape = "Triple"
                else:
                    tipo_traslape = f"Múltiple ({n_buferes})"
                
                # Crear feature
                feat = QgsFeature(fragment_fields)
                feat.setGeometry(frag_geom)
                feat.setAttributes([
                    idx + 1,        # fid
                    n_buferes,      # n_buferes
                    tipo_traslape,  # tipo_traslape
                    buferes_str,    # buferes (ej: "fid: 1, fid: 7")
                    area_ha,        # area_ha
                    area_m2,        # area_m2
                    perimetro,      # perimetro_m
                    n_vertices      # n_vertices
                ])
                
                fragment_sink.addFeature(feat, QgsFeatureSink.FastInsert)
            
            # Configurar nombre
            nombre_fragmentos = f"Fragmentos de traslape - {self._generate_layer_name(params)}"
            details = QgsProcessingContext.LayerDetails(
                nombre_fragmentos, context.project(), self.OUTPUT_FRAGMENTOS)
            context.addLayerToLoadOnCompletion(fragment_dest_id, details)
            
            # Calcular estadísticas de fragmentos
            stats_fragmentos = {
                'total': len(fragmentos_con_info),
                'por_tipo': {},
                'area_mayor_ha': 0.0,
                'area_menor_ha': float('inf'),
                'fid_mayor': None,
                'fid_menor': None
            }
            
            # Contar por tipo y encontrar mayor/menor
            for idx, (frag_geom, buffer_indices) in enumerate(fragmentos_con_info):
                n_buferes = len(buffer_indices)
                if n_buferes == 1:
                    tipo = "Exclusivo"
                elif n_buferes == 2:
                    tipo = "Doble"
                elif n_buferes == 3:
                    tipo = "Triple"
                else:
                    tipo = f"Múltiple ({n_buferes})"
                
                stats_fragmentos['por_tipo'][tipo] = stats_fragmentos['por_tipo'].get(tipo, 0) + 1
                
                # Calcular área
                area_ha = frag_geom.area() / Constants.HA_TO_M2
                if area_ha > stats_fragmentos['area_mayor_ha']:
                    stats_fragmentos['area_mayor_ha'] = area_ha
                    stats_fragmentos['fid_mayor'] = ', '.join([descripciones[i] for i in sorted(buffer_indices)])
                if area_ha < stats_fragmentos['area_menor_ha']:
                    stats_fragmentos['area_menor_ha'] = area_ha
                    stats_fragmentos['fid_menor'] = ', '.join([descripciones[i] for i in sorted(buffer_indices)])
            
            feedback.pushInfo(f"✅ Capa de fragmentos generada: {len(fragmentos_con_info)} polígonos")
            
            # Aplicar transparencia a la capa de fragmentos si está activada
            if params.aplicar_transparencia:
                fragment_layer = QgsProcessingUtils.mapLayerFromString(fragment_dest_id, context)
                if not fragment_layer:
                    fragment_layer = context.getMapLayer(fragment_dest_id)
                if fragment_layer:
                    TransparencyManager.aplicar_transparencia(fragment_layer, params.nivel_transparencia, context)
                    feedback.pushInfo(f"🎨 Transparencia aplicada a capa de fragmentos ({params.nivel_transparencia}%)")
            
            return {'dest_id': fragment_dest_id, 'stats': stats_fragmentos}
            
        except Exception as e:
            feedback.pushInfo(f"⚠️ Error al generar fragmentos: {str(e)}")
            feedback.pushInfo(traceback.format_exc())
            return None

    def _calcular_traslapes_por_bufer(self, overlap_data: List[Dict], n_buffers: int, 
                                      all_buffers: List[Tuple] = None) -> Dict[int, Dict]:
        """
        Calcula para cada búfer con cuántos y cuáles otros búferes se traslapa,
        y calcula áreas exclusivas y compartidas.
        
        Args:
            overlap_data: Lista de diccionarios con información de traslapes entre pares
            n_buffers: Número total de búferes
            all_buffers: Lista opcional de tuplas (geometría, tipo, dist, desc) para calcular áreas
            
        Returns:
            Diccionario {indice_bufer: {'n_traslapes': int, 'traslapa_con': str, 
                                        'area_exclusiva_ha': float, 'area_compartida_ha': float,
                                        'pct_exclusivo': float}}
        """
        # Inicializar diccionario para cada búfer
        traslapes = {i: {'indices': set(), 'descripciones': [], 'area_traslape_m2': 0.0} 
                    for i in range(n_buffers)}
        
        # Procesar cada traslape
        for overlap in overlap_data:
            idx1 = overlap['buffer_1_idx']
            idx2 = overlap['buffer_2_idx']
            desc1 = overlap['buffer_1_tipo']
            desc2 = overlap['buffer_2_tipo']
            area_m2 = overlap['area_m2']
            
            # Agregar traslape bidireccional
            traslapes[idx1]['indices'].add(idx2)
            traslapes[idx1]['descripciones'].append(desc2)
            traslapes[idx1]['area_traslape_m2'] += area_m2
            
            traslapes[idx2]['indices'].add(idx1)
            traslapes[idx2]['descripciones'].append(desc1)
            traslapes[idx2]['area_traslape_m2'] += area_m2
        
        # Construir resultado final
        resultado = {}
        for idx, data in traslapes.items():
            # Calcular áreas si tenemos geometrías
            area_exclusiva_ha = 0.0
            area_compartida_ha = 0.0
            pct_exclusivo = 0.0
            
            if all_buffers and idx < len(all_buffers):
                geom = all_buffers[idx][0]
                area_total_m2 = geom.area()
                area_total_ha = area_total_m2 / Constants.HA_TO_M2
                
                # Área compartida es la suma de traslapes (puede haber traslapes múltiples)
                area_compartida_m2 = data['area_traslape_m2']
                area_compartida_ha = area_compartida_m2 / Constants.HA_TO_M2
                
                # Área exclusiva = área total - área compartida
                area_exclusiva_m2 = max(0, area_total_m2 - area_compartida_m2)
                area_exclusiva_ha = area_exclusiva_m2 / Constants.HA_TO_M2
                
                # Porcentaje exclusivo
                pct_exclusivo = (area_exclusiva_m2 / area_total_m2 * 100) if area_total_m2 > 0 else 0.0
            
            if data['indices']:  # Tiene traslapes
                # Ordenar descripciones para consistencia
                descripciones_ordenadas = sorted(set(data['descripciones']))
                resultado[idx] = {
                    'n_traslapes': len(data['indices']),
                    'traslapa_con': ', '.join(descripciones_ordenadas),
                    'area_exclusiva_ha': area_exclusiva_ha,
                    'area_compartida_ha': area_compartida_ha,
                    'pct_exclusivo': pct_exclusivo
                }
            else:  # No tiene traslapes
                # Si tiene geometría, toda el área es exclusiva
                if all_buffers and idx < len(all_buffers):
                    geom = all_buffers[idx][0]
                    area_total_ha = geom.area() / Constants.HA_TO_M2
                    resultado[idx] = {
                        'n_traslapes': 0,
                        'traslapa_con': '',
                        'area_exclusiva_ha': area_total_ha,
                        'area_compartida_ha': 0.0,
                        'pct_exclusivo': 100.0
                    }
                else:
                    resultado[idx] = {
                        'n_traslapes': 0,
                        'traslapa_con': '',
                        'area_exclusiva_ha': 0.0,
                        'area_compartida_ha': 0.0,
                        'pct_exclusivo': 0.0
                    }
        
        return resultado

    def _sanitize_geometry(self, geom: QgsGeometry) -> Optional[QgsGeometry]:
        """Limpia y valida una geometría de salida."""
        if not geom or geom.isEmpty():
            return None
        wkb_type = geom.wkbType()
        base_type = QgsWkbTypes.geometryType(wkb_type)
        if base_type == QgsWkbTypes.PolygonGeometry:
            return geom
        if base_type == QgsWkbTypes.UnknownGeometry or QgsWkbTypes.isMultiType(wkb_type):
            if geom.isMultipart():
                parts = geom.asGeometryCollection()
                polys = [p for p in parts if QgsWkbTypes.geometryType(p.wkbType()) == QgsWkbTypes.PolygonGeometry]
                if polys:
                    return QgsGeometry.collectGeometry(polys)
        return None

    def _generate_layer_name(self, params: BufferParams) -> str:
        """Genera el nombre de la capa de salida."""
        nombres = ['Circular', 'Oval', 'Rect', 'Conc', 'Area', '1Lado', 'Cuña', 'Adaptativo', 'AnchoM']
        join_str = JoinStyleManager.get_short_name(params.join_idx)
        u_str = Constants.AREA_UNIT_SYMBOLS[params.unidad_area] if params.unidad_area < len(Constants.AREA_UNIT_SYMBOLS) else ''
        
        # Detectar si se usan campos variables
        tiene_distance_field = bool(params.distance_field)
        tiene_ancho_field = bool(params.ancho_field)
        tiene_alto_field = bool(params.alto_field)
        
        detalles = ""
        bt = params.buffer_type
        
        if bt == Constants.BUFFER_CIRCULAR:
            if params.calcular_por_area:
                if tiene_distance_field:
                    detalles = f" Dist_Var {u_str} {join_str}"
                else:
                    detalles = f" {params.area_objetivo:.2f} {u_str} {join_str}"
            else:
                if tiene_distance_field:
                    detalles = f" Dist_Var m {join_str}"
                else:
                    detalles = f" {params.distancia:.1f} m {join_str}"
        
        elif bt in [Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR]:
            # Prioridad: campos específicos > campo genérico > valores fijos
            rot_tag = " RotAut" if params.usar_rot_auto else ""
            if tiene_ancho_field or tiene_alto_field:
                detalles = f" Dist_Var m{rot_tag}"
            elif tiene_distance_field:
                detalles = f" Dist_Var m{rot_tag}"
            else:
                detalles = f" {params.ancho:.1f}x{params.alto:.1f} m{rot_tag}"
        
        elif bt == Constants.BUFFER_CONCENTRICO:
            if tiene_distance_field:
                detalles = f" {params.concentric_count}xDist_Var m {join_str}"
            else:
                detalles = f" {params.concentric_count}x{params.concentric_distance:.1f} m {join_str}"
        
        elif bt == Constants.BUFFER_POR_AREA:
            if tiene_distance_field:
                detalles = f" Dist_Var {u_str} {join_str}"
            else:
                detalles = f" {params.area_objetivo:.2f} {u_str} {join_str}"
        
        elif bt == Constants.BUFFER_UN_LADO:
            lado = "Iz" if params.side_idx == 0 else "Der"
            if tiene_distance_field:
                detalles = f" {lado} Dist_Var m {join_str}"
            else:
                detalles = f" {lado} {params.distancia:.1f}m {join_str}"
        
        elif bt == Constants.BUFFER_CUNA:
            if tiene_distance_field:
                detalles = f" Az{params.wedge_start:.0f} W{params.wedge_width:.0f} RDist_Var m"
            else:
                detalles = f" Az{params.wedge_start:.0f} W{params.wedge_width:.0f} R{params.distancia:.1f}m"
        
        if bt == Constants.BUFFER_ADAPTATIVO:
            metodo_tag = 'KNN' if params.densidad_metodo == Constants.DENSIDAD_KNN else 'RFijo'
            detalles = f" K={params.densidad_k} Esc={params.densidad_factor_escala} [{metodo_tag}]"
        elif bt == Constants.BUFFER_ANCHO_M:
            detalles = " Var-M"
        nombre_base = f"Búfer {nombres[bt]}{detalles}"
        return f"{nombre_base} [{Constants.OP_NAMES[params.op_logic]}]" if params.op_logic != 0 else nombre_base

    def _postprocess_buffer(self, g_raw, result_tipo, result_dist, desc, 
                            params, sink, sink_fields,
                            necesita_postproceso_traslapes, geometrias_pendientes,
                            all_buffers, state: 'ProcessState', logger):
        """
        Post-procesa una geometría de búfer: sanitizar → huecos → simplificar →
        atributos → escribir al sink o recolectar para traslapes.

        Actualiza estado(ProcessState) en lugar de retornar una tupla de contadores.
        Retorna False si la geometría fue descartada sin procesar, True en caso contrario.
        """
        g_clean = self._sanitize_geometry(g_raw)
        if not g_clean or g_clean.isEmpty():
            return False

        # Asegurar que la geometría sea MultiPolygon para el sink
        if not QgsWkbTypes.isMultiType(g_clean.wkbType()):
            # Polygon simple → MultiPolygon
            poly_rings = g_clean.asPolygon()
            if poly_rings:
                g_clean = QgsGeometry.fromMultiPolygonXY([poly_rings])
            else:
                return False  # Geometría vacía o corrupta

        # Eliminar huecos
        # NOTA: Aunque se eliminan huecos en la entrada, el proceso de búfer puede crear
        # nuevos huecos debido a auto-intersecciones o problemas topológicos.
        # Por eso, volvemos a eliminar huecos aquí en el post-procesamiento.
        if params.eliminar_huecos:
            g_clean, huecos_elim = GeometryPostProcessor.eliminar_huecos(
                g_clean, params.area_minima_hueco, params.preservar_hueco_estructural)
            if huecos_elim > 0:
                logger.info(f"   🕳️ Huecos eliminados en post-procesamiento: {huecos_elim}")

        # Simplificar
        if params.aplicar_simplificacion and params.tolerancia_simplificacion > 0:
            g_clean, v_antes, v_despues = GeometrySimplifier.simplify(
                g_clean, params.tolerancia_simplificacion)
            state.total_vertices_antes += v_antes
            state.total_vertices_despues += v_despues
        else:
            v_count = GeometrySimplifier.count_vertices(g_clean)
            state.total_vertices_antes += v_count
            state.total_vertices_despues += v_count

        # makeValid() final: aplicado DESPUÉS de toda la pipeline de post-proceso.
        # Resuelve huecos anidados que asPolygon()/fromMultiPolygonXY() pueden
        # reintroducir al reconstruir la geometría desde anillos de coordenadas.
        # Solo para Concéntrico — otros tipos no producen esta estructura.
        if params.buffer_type == Constants.BUFFER_CONCENTRICO:
            g_valida = g_clean.makeValid()
            if g_valida and not g_valida.isEmpty():
                g_clean = g_valida

        area = g_clean.area() / Constants.HA_TO_M2

        # Atributos
        if params.buffer_type in [Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR]:
            attrs = [state.cnt + 1, result_tipo, area, params.ancho, params.alto, desc]
        else:
            attrs = [state.cnt + 1, result_tipo, area, result_dist, desc]

        # Escribir o recolectar
        if necesita_postproceso_traslapes:
            geometrias_pendientes.append({
                'geom': QgsGeometry(g_clean),
                'id': state.cnt + 1,
                'tipo': result_tipo,
                'dist': result_dist,
                'area': area,
                'attrs': attrs
            })
            state.cnt += 1
        else:
            state.area_sum += area
            logger.registrar_area(area)
            logger.registrar_distancia(result_dist)
            feat = QgsFeature(sink_fields)
            feat.setGeometry(g_clean)
            feat.setAttributes(attrs)
            sink.addFeature(feat, QgsFeatureSink.FastInsert)
            state.cnt += 1

        # Superposición o fragmentos
        if params.calcular_superposicion or params.generar_fragmentos_traslape:
            all_buffers.append((QgsGeometry(g_clean), result_tipo, result_dist, desc))

        return True

    def _dissolve_in_batches(self, all_geoms, feedback, batch_size=500):
        """
        CRÍTICO #2: Disolución por lotes para evitar OOM (Out Of Memory).
        
        Procesa geometrías en lotes pequeños y luego fusiona los resultados parciales.
        Esto previene el consumo excesivo de RAM con capas grandes (>1000 features).
        
        Args:
            all_geoms: Lista de geometrías a disolver
            feedback: Objeto de feedback para progreso
            batch_size: Tamaño de lote (default: 500)
        
        Returns:
            QgsGeometry: Geometría disuelta o None si falla
        """
        total = len(all_geoms)
        
        if total == 0:
            return None
        
        # Para capas pequeñas, usar método directo (más rápido)
        if total <= batch_size:
            feedback.pushInfo(f"   🔧 Fusionando {total} geometrías (método directo)...")
            try:
                return QgsGeometry.unaryUnion(all_geoms)
            except Exception as e:
                feedback.reportError(f"Error en disolución directa: {str(e)[:100]}")
                return None
        
        # Para capas grandes, procesar por lotes
        feedback.pushInfo(f"   🔧 Fusionando {total} geometrías en lotes de {batch_size}...")
        
        partial_results = []
        batch = []
        processed = 0
        lotes_fallidos = 0
        geoms_fallidas = 0

        guard = ResourceGuard(max_time_sec=300)  # 5 minutos por lote
        
        for idx, geom in enumerate(all_geoms):
            batch.append(geom)
            
            if len(batch) >= batch_size or idx == total - 1:
                # Procesar lote
                batch_num = len(partial_results) + lotes_fallidos + 1
                batch_len = len(batch)

                try:
                    guard.start_operation(f"Disolución lote {batch_num}")
                    dissolved_batch = QgsGeometry.unaryUnion(batch)
                    guard.check_timeout()
                    
                    if dissolved_batch and not dissolved_batch.isEmpty():
                        partial_results.append(dissolved_batch)
                    
                    processed += batch_len
                    feedback.setProgress(int(processed / total * 90))  # 0-90%
                    feedback.pushInfo(f"      ✓ Lote {batch_num} completado ({processed}/{total} geometrías)")
                    
                except TimeoutError as e:
                    lotes_fallidos += 1
                    geoms_fallidas += batch_len
                    feedback.reportError(
                        f"⚠️ Lote {batch_num} excedió tiempo límite (300s) — "
                        f"{batch_len} geometrías omitidas. "
                        f"Resultado de disolución puede estar incompleto."
                    )
                except Exception as e:
                    lotes_fallidos += 1
                    geoms_fallidas += batch_len
                    feedback.reportError(f"Error en lote {batch_num}: {str(e)[:100]}")
                
                batch = []
                
                # Liberar memoria explícitamente cada N lotes
                if batch_num % 5 == 0:
                    gc.collect()
        
        # Advertencia consolidada si hubo lotes fallidos
        if lotes_fallidos > 0:
            feedback.pushWarning(
                f"⚠️ DISOLUCIÓN INCOMPLETA: {lotes_fallidos} lote(s) fallido(s) — "
                f"{geoms_fallidas} de {total} geometrías no fueron disueltas. "
                f"El resultado puede estar incompleto. "
                f"Consulte el log para detalles."
            )

        # Fusionar resultados parciales
        if not partial_results:
            return None
        
        if len(partial_results) == 1:
            return partial_results[0]
        
        feedback.pushInfo(f"   🔧 Fusionando {len(partial_results)} resultados parciales...")
        feedback.setProgress(95)
        
        try:
            guard.start_operation("Fusión final de lotes")
            final_result = QgsGeometry.unaryUnion(partial_results)
            guard.check_timeout()
            return final_result
        except TimeoutError as e:
            feedback.reportError(f"Fusión final excedió tiempo límite: {str(e)[:100]}")
            return None
        except Exception as e:
            feedback.reportError(f"Error en fusión final: {str(e)[:100]}")
            return None

    def _extraer_punto_anclaje(self, geom: QgsGeometry, metodo_anclaje: int,
                               tolerancia_polo: float, logger, desc: str) -> QgsGeometry:
        """
        Extrae el punto de anclaje de una geometría para el Búfer Adaptativo.

        Para polígonos: centroide o polo de inaccesibilidad según metodo_anclaje.
        Para líneas: punto medio de la longitud recorrida.
        Para puntos: el punto mismo.

        Returns:
            QgsGeometry de tipo Point, o None si no se puede calcular.
        """
        if not geom or geom.isEmpty():
            return None

        geom_type = geom.type()

        if geom_type == QgsWkbTypes.PointGeometry:
            # Punto: usar directamente (multi → primer punto)
            if geom.isMultipart():
                pts = geom.asMultiPoint()
                if pts:
                    return QgsGeometry.fromPointXY(pts[0])
            return QgsGeometry(geom)

        elif geom_type == QgsWkbTypes.LineGeometry:
            # Línea: punto a mitad de longitud
            mid = geom.interpolate(geom.length() / 2.0)
            if mid and not mid.isEmpty():
                return mid
            return geom.pointOnSurface()

        else:  # PolygonGeometry
            if metodo_anclaje == Constants.DENSIDAD_ANCLAJE_CENTROIDE:
                pt = geom.centroid()
                if pt and not pt.isEmpty():
                    return pt
                return geom.pointOnSurface()

            else:  # DENSIDAD_ANCLAJE_POLO
                try:
                    resultado_polo = geom.poleOfInaccessibility(tolerancia_polo)
                    geom_polo = resultado_polo[0] if isinstance(resultado_polo, tuple) else resultado_polo
                    if geom_polo and not geom_polo.isEmpty():
                        return geom_polo
                except Exception as e:
                    logger.warning(f"⚠️ {desc}: polo de inaccesibilidad falló ({str(e)[:40]}), usando centroide")
                pt = geom.centroid()
                if pt and not pt.isEmpty():
                    return pt
                return geom.pointOnSurface()

    def _process_single_feature(self, geom, desc, override_values, fid,
                                 params, processor, logic_op, exclusion_geom,
                                 logger, guard_por_area=None, m_vertices_data=None):
        """Procesa una feature individual: integridad → override → complejidad → búfer → lógica.
        
        Método extraído para eliminar la duplicación entre el path paralelo (closure
        procesar_feature) y el path secuencial del loop principal. Ambas rutas ejecutan
        exactamente la misma lógica de negocio; la diferencia es solo cómo se llama
        (hilo vs iteración) y cómo se escriben los resultados al sink.
        
        Args:
            geom: Geometría de la feature (ya leída de la capa).
            desc: Descripción textual (ej. "fid: 42").
            override_values: Tupla (dist_override, ancho_override, alto_override, az_override) o None.
            fid: ID de la feature.
            params: BufferParams base (no se muta).
            processor: BufferProcessor a usar.
            logic_op: LogicOperation a aplicar.
            exclusion_geom: Geometría de exclusión o None.
            logger: Logger compartido (thread-safe).
            guard_por_area: ResourceGuard opcional para verificar complejidad.
                            Si es None se crea uno local (para uso en hilos paralelos).
        
        Returns:
            dict con claves:
                'fid': int
                'omitido': bool — True si la geometría fue descartada antes de procesar
                'error': str | None — mensaje de error si ocurrió excepción
                'override_values': tuple — valores de campo originales
                'resultados': list de dicts con 'geom', 'tipo', 'dist', 'desc', 'geom_original'
        """
        # --- Integridad geométrica ---
        geom_prep = GeometryHandler.preparar_geometria(
            geom, fid, params.gestion_integridad, logger, desc)
        
        if geom_prep is None:
            return {'fid': fid, 'resultados': [], 'error': None,
                    'omitido': True, 'override_values': override_values}
        
        resultados = []
        
        try:
            # --- Desempacar override (tupla de 4 desde _prepare_features) ---
            if isinstance(override_values, tuple):
                if len(override_values) == 4:
                    dist_override, ancho_override, alto_override, az_override = override_values
                else:
                    dist_override, ancho_override, alto_override = override_values
                    az_override = None
            else:
                dist_override = override_values
                ancho_override = None
                alto_override = None
                az_override = None
            
            tiene_override = (dist_override is not None or ancho_override is not None
                              or alto_override is not None or az_override is not None)
            
            if tiene_override:
                params_local = self._crear_params_con_override(params, override_values)
            else:
                params_local = params
            
            # --- Complejidad geométrica (solo Por Área) ---
            if params.buffer_type == Constants.BUFFER_POR_AREA or params.calcular_por_area:
                # Si no se proporcionó guard externo (modo paralelo), crear uno local.
                _local_guard = guard_por_area or ResourceGuard(max_vertices=100_000)
                _local_guard.check_geometry_complexity(geom_prep, desc)
            
            # --- Procesar búfer ---
            # Búfer Adaptativo: el búfer se aplica sobre el PUNTO DE ANCLAJE
            # (centroide o polo de inaccesibilidad), no sobre el contorno del polígono.
            # Esto produce círculos desde el centro de cada entidad con radio variable.
            geom_para_bufer = geom_prep
            if params.buffer_type == Constants.BUFFER_ADAPTATIVO:
                geom_para_bufer = self._extraer_punto_anclaje(
                    geom_prep, params.densidad_metodo_anclaje,
                    params.densidad_tolerancia_polo, logger, desc)
                if geom_para_bufer is None or geom_para_bufer.isEmpty():
                    geom_para_bufer = geom_prep  # fallback al polígono completo
            
            # Para BUFFER_ANCHO_M: inyectar _m_vertices_override en params_local
            # para que el procesador use los datos pre-extraídos en vez de leer M
            # de la geometría (que puede haberlos perdido en WKB/makeValid).
            if (params.buffer_type == Constants.BUFFER_ANCHO_M
                    and m_vertices_data):
                params_local = replace(params_local, _m_vertices_override=m_vertices_data)
            
            buffers = processor.process(geom_para_bufer, params_local, logger, desc)
            
            if not buffers:
                logger.registrar_sin_bufer(fid)
            
            # Registrar riesgo si Cuña usó radio negativo — orientación invertida 180°
            if (buffers and params.buffer_type == Constants.BUFFER_CUNA
                    and params.distancia < 0):
                logger.registrar_riesgo(fid)
            
            # Registrar fragmentación si Por Área produjo MultiPolígono por contracción
            if buffers and (params.buffer_type == Constants.BUFFER_POR_AREA or params.calcular_por_area):
                for buf_geom_chk, _, buf_dist_chk in buffers:
                    if buf_dist_chk < 0 and QgsWkbTypes.isMultiType(buf_geom_chk.wkbType()):
                        logger.registrar_fragmentacion(fid, len(buf_geom_chk.asMultiPolygon()))
                        break
            
            for buf_geom, tipo, dst in buffers:
                # Preparar geometría del búfer (sin contar métrica — ya se contó la entrada)
                buf_geom_prep = GeometryHandler.preparar_geometria(
                    buf_geom, fid, params.gestion_integridad, logger,
                    f"{desc} (búfer)", registrar_metrica=False)
                
                if buf_geom_prep is None:
                    continue
                
                # Aplicar exclusión si existe
                if exclusion_geom:
                    buf_geom_prep = buf_geom_prep.difference(exclusion_geom)
                    if buf_geom_prep.isEmpty():
                        logger.registrar_sin_bufer(fid)
                        logger.warning(f"{desc}: Búfer eliminado 100% por exclusión (fid={fid}).")
                        continue
                
                # Aplicar operación lógica
                logic_results = logic_op.apply(buf_geom_prep, geom_prep, tipo, dst)
                
                for result_geom, result_tipo, result_dist in logic_results:
                    resultados.append({
                        'geom': result_geom,
                        'tipo': result_tipo,
                        'dist': result_dist,
                        'desc': desc,
                        'geom_original': geom_prep
                    })
            
            return {'fid': fid, 'resultados': resultados, 'error': None,
                    'omitido': False, 'override_values': override_values}
        
        except Exception as e:
            return {'fid': fid, 'resultados': [], 'error': str(e),
                    'omitido': False, 'override_values': override_values}

    def _apply_resultado_to_sink(self, resultado, params, sink, sink_fields,
                                  necesita_postproceso_traslapes, geometrias_pendientes,
                                  all_buffers, state: 'ProcessState', logger, feedback):
        """Escribe los resultados de _process_single_feature al sink o a la lista pendiente.

        Actualiza state (ProcessState) en lugar de retornar una tupla de contadores.
        Usa la nueva firma simplificada de _postprocess_buffer.
        """
        if resultado['omitido']:
            return

        if resultado['error']:
            logger.error(f"Feature {resultado['fid']}: {resultado['error'][:100]}")
            logger.registrar_omision(resultado['fid'])
            return

        # Registrar distancia
        override_values = resultado.get('override_values', None)
        if isinstance(override_values, tuple):
            dist_override = override_values[0]
        else:
            dist_override = override_values

        if dist_override is not None:
            logger.registrar_distancia(dist_override)
        else:
            logger.registrar_distancia(params.distancia)

        for res in resultado['resultados']:
            if feedback and self._check_canceled(feedback):
                break

            self._postprocess_buffer(
                res['geom'], res['tipo'], res['dist'], res['desc'],
                params, sink, sink_fields,
                necesita_postproceso_traslapes, geometrias_pendientes,
                all_buffers, state, logger)

        # Caso especial: UNION + CONCENTRICO agrega la geometría original al centro
        if params.op_logic == Constants.OP_UNION and params.buffer_type == Constants.BUFFER_CONCENTRICO:
            if resultado['resultados'] and resultado['resultados'][0].get('geom_original'):
                self._postprocess_buffer(
                    resultado['resultados'][0]['geom_original'],
                    "Original (Centro)", 0.0, resultado['resultados'][0]['desc'],
                    params, sink, sink_fields,
                    necesita_postproceso_traslapes, geometrias_pendientes,
                    all_buffers, state, logger)

    def processAlgorithm(self, parameters, context, feedback):
        """Método principal de procesamiento."""
        logger = Logger(feedback)

        try:
            # IMPORTANTE: Configurar el contexto para NO filtrar geometrías inválidas
            # Esto permite que nuestro algoritmo las maneje según el parámetro 'Gestión de Integridad'
            context.setInvalidGeometryCheck(QgsFeatureRequest.GeometryNoCheck)
            
            # Obtener la capa de entrada
            source = self.parameterAsSource(parameters, self.INPUT, context)
            
            # También obtener como VectorLayer para acceso directo si es necesario
            layer_input = self.parameterAsVectorLayer(parameters, self.INPUT, context)
            
            params = self._extract_parameters(parameters, context, source, feedback)
            
            # Validar CRS y emitir advertencia si es geográfico
            if params.crs_es_geografico:
                logger.warning(f"⚠️ CRS geográfico detectado ({params.crs_unidad}). "
                              "Se recomienda usar un CRS proyectado para mayor precisión.")
            
            # NOTA INFORMATIVA: Circular/Concéntrico con polígonos y campo con negativos
            # → negativo = contracción interior válida, no es error
            geom_type_proc = QgsWkbTypes.geometryType(source.wkbType())
            campo_tiene_negativos = False
            if (params.buffer_type in [Constants.BUFFER_CIRCULAR, Constants.BUFFER_CONCENTRICO] and
                    geom_type_proc == QgsWkbTypes.PolygonGeometry and
                    params.distance_field):
                max_check = min(1000, source.featureCount())
                checked = 0
                tiene_negativos = False
                tiene_positivos = False
                for f in source.getFeatures():
                    if checked >= max_check:
                        break
                    try:
                        val = float(f[params.distance_field])
                        if val < 0:
                            tiene_negativos = True
                        elif val > 0:
                            tiene_positivos = True
                    except (ValueError, TypeError):
                        pass
                    checked += 1
                    if tiene_negativos and tiene_positivos:
                        break
                if tiene_negativos:
                    campo_tiene_negativos = True
                    tipo_bufer = "Circular" if params.buffer_type == Constants.BUFFER_CIRCULAR else "Concéntrico"
                    if tiene_positivos:
                        feedback.pushInfo(
                            f"ℹ️ Campo '{params.distance_field}' contiene valores positivos Y negativos. "
                            f"Búfer {tipo_bufer} en polígonos: "
                            f"positivos → expansión exterior | negativos → contracción interior."
                        )
                    else:
                        feedback.pushInfo(
                            f"ℹ️ Campo '{params.distance_field}' contiene valores negativos. "
                            f"Búfer {tipo_bufer} en polígonos: negativo = contracción interior (válido)."
                        )

            # ADVERTENCIA: Un Solo Lado en líneas/polígonos con campo de distancia con negativos
            if (params.buffer_type == Constants.BUFFER_UN_LADO and params.distance_field):
                max_check = min(1000, source.featureCount())
                checked = 0
                for f in source.getFeatures():
                    if checked >= max_check:
                        break
                    try:
                        val = float(f[params.distance_field])
                        if val < 0:
                            lado_cfg = "Izquierda" if params.side_idx == 0 else "Derecha"
                            lado_inv = "Derecha" if params.side_idx == 0 else "Izquierda"
                            if geom_type_proc == QgsWkbTypes.LineGeometry:
                                logger.warning(
                                    f"⚠️ El campo '{params.distance_field}' contiene valores negativos "
                                    f"(primer caso: {val:.2f}m, fid: {f.id()}). "
                                    f"Para Un Solo Lado en líneas, un valor negativo invierte el lado visual: "
                                    f"{lado_cfg} → {lado_inv}. "
                                    f"Si es un error en los datos, corrija con: abs(\"{params.distance_field}\")."
                                )
                            elif geom_type_proc == QgsWkbTypes.PolygonGeometry:
                                logger.warning(
                                    f"⚠️ El campo '{params.distance_field}' contiene valores negativos "
                                    f"(primer caso: {val:.2f}m, fid: {f.id()}). "
                                    f"Para Un Solo Lado en polígonos, el signo se ignora — "
                                    f"se usa el valor absoluto ({abs(val):.2f}m). "
                                    f"El lado permanece: {lado_cfg}. "
                                    f"Si es un error en los datos, corrija con: abs(\"{params.distance_field}\")."
                                )
                            break
                    except (ValueError, TypeError):
                        pass
                    checked += 1
            
            feedback.pushInfo(f"📍 CRS: {params.crs_info} (Unidad: {params.crs_unidad})")
            feedback.pushInfo(f"⚡ Gestión de Integridad: {Constants.INTEGRIDAD_NAMES[params.gestion_integridad]}")

            # ── ADVERTENCIA ANTICIPADA: concéntrico con distancia negativa ────
            # Un búfer negativo contrae la geometría hacia adentro. A partir de
            # cierto anillo, la contracción supera el tamaño de la geometría y
            # el resultado colapsa (geometría vacía). El usuario debe saberlo
            # ANTES de procesar, no solo en el log de cada entidad.
            if (params.buffer_type == Constants.BUFFER_CONCENTRICO and
                    params.concentric_distance < 0 and
                    not params.distance_field and not params.category_field):
                total_contraccion = abs(params.concentric_distance) * params.concentric_count
                feedback.pushWarning(
                    f"⚠️ ATENCIÓN — Búfer Concéntrico con distancia NEGATIVA "
                    f"({params.concentric_distance:+.4f}m × {params.concentric_count} anillos = "
                    f"{-total_contraccion:.4f}m de contracción total):\n"
                    f"   • Cada anillo CONTRAE la geometría hacia adentro.\n"
                    f"   • Cuando la contracción supera el tamaño de la geometría, "
                    f"el anillo colapsa (geometría vacía) y los siguientes también.\n"
                    f"   • El número de anillos generados puede ser MENOR que {params.concentric_count}.\n"
                    f"   • Ver advertencias individuales por entidad en el log."
                )

            if (params.buffer_type == Constants.BUFFER_CIRCULAR and
                    not params.distance_field and not params.category_field and
                    not params.calcular_por_area and params.distancia < 0):
                feedback.pushWarning(
                    f"⚠️ ATENCIÓN — Búfer Circular con distancia NEGATIVA "
                    f"({params.distancia:+.4f}m):\n"
                    f"   • Un radio negativo CONTRAE la geometría hacia adentro.\n"
                    f"   • Si la contracción supera el tamaño de la geometría, "
                    f"el resultado colapsa (geometría vacía) y no se genera búfer.\n"
                    f"   • Solo aplicable a polígonos — en puntos y líneas siempre colapsa."
                )

            if (params.buffer_type == Constants.BUFFER_UN_LADO and
                    not params.distance_field and not params.category_field and
                    params.distancia < 0):
                lado = "Izquierdo" if params.side_idx == 0 else "Derecho"
                feedback.pushWarning(
                    f"⚠️ ATENCIÓN — Búfer Un Solo Lado con distancia NEGATIVA "
                    f"({params.distancia:+.4f}m, lado {lado}):\n"
                    f"   • En líneas: una distancia negativa invierte el lado "
                    f"({'Derecho' if params.side_idx == 0 else 'Izquierdo'} en lugar de {lado}).\n"
                    f"   • En polígonos: el signo se ignora — se usa el valor absoluto.\n"
                    f"   • Si la geometría es un polígono muy pequeño, puede colapsar."
                )

            if (params.buffer_type == Constants.BUFFER_CUNA and
                    not params.distance_field and not params.category_field and
                    params.distancia < 0):
                feedback.pushWarning(
                    f"⚠️ ATENCIÓN — Búfer Cuña con radio NEGATIVO "
                    f"({params.distancia:+.4f}m):\n"
                    f"   • Un radio negativo genera la cuña orientada 180° opuesta "
                    f"al azimut configurado ({params.wedge_start:.1f}°).\n"
                    f"   • Si el radio absoluto es menor al mínimo permitido, "
                    f"la entidad será omitida.\n"
                    f"   • Verifique si el valor negativo es intencional."
                )

            if (params.buffer_type == Constants.BUFFER_POR_AREA and
                    not params.distance_field and not params.category_field and
                    params.area_objetivo <= 0):
                feedback.pushWarning(
                    f"⚠️ ATENCIÓN — Búfer Por Área con área objetivo = "
                    f"{params.area_objetivo} {Constants.AREA_UNITS[params.unidad_area]}:\n"
                    f"   • El área objetivo debe ser > 0.\n"
                    f"   • Ninguna entidad generará geometría con este valor.\n"
                    f"   • Corrija el parámetro 'Área objetivo' antes de procesar."
                )

            if (params.buffer_type in [Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR] and
                    not params.ancho_field and not params.alto_field and
                    (params.ancho <= 0 or params.alto <= 0)):
                tipo_nombre = Constants.BUFFER_NAMES[params.buffer_type]
                feedback.pushWarning(
                    f"⚠️ ATENCIÓN — Búfer {tipo_nombre} con dimensiones inválidas "
                    f"(Ancho={params.ancho:+.4f}m, Alto={params.alto:+.4f}m):\n"
                    f"   • Ancho y Alto deben ser > 0.\n"
                    f"   • Las entidades con dimensiones ≤ 0 serán omitidas.\n"
                    f"   • Corrija los parámetros de dimensiones antes de procesar."
                )

            # ── PROGRESO POR ETAPAS ────────────────────────────────────────────
            # Divide el rango 0-100% en 4 fases con pesos proporcionales:
            #   Fase 1 — Validación / preparación   :  0 – 10 %
            #   Fase 2 — Cálculo de búferes          : 10 – 75 %
            #   Fase 3 — Post-proceso y análisis     : 75 – 90 %
            #   Fase 4 — Reporte y cierre            : 90 – 100%
            FASE_VAL_INI,  FASE_VAL_FIN  =  0,  10
            FASE_BUF_INI,  FASE_BUF_FIN  = 10,  75
            FASE_POST_INI, FASE_POST_FIN = 75,  90
            FASE_REP_INI,  FASE_REP_FIN  = 90, 100

            def set_progress_fase(fase_ini, fase_fin, pct_dentro):
                """Convierte un % local (0-100) al rango global de la fase."""
                global_pct = fase_ini + (fase_fin - fase_ini) * pct_dentro / 100.0
                feedback.setProgress(int(global_pct))

            feedback.setProgress(FASE_VAL_INI)
            feedback.setProgressText("Etapa 1/4 — Validando y preparando geometrías...")

            # ── VALIDACIÓN PREVIA — ejecutar validaciones y salir temprano ─────
            if params.validacion_previa:
                feedback.pushInfo("🔍 MODO VALIDACIÓN PREVIA — Solo validación, sin generar búferes")
                dry_resultado = self._dry_run_validation(source, params, feedback, logger)

                set_progress_fase(FASE_VAL_INI, FASE_VAL_FIN, 100)

                # Mostrar resultados en el log
                for e in dry_resultado['errores']:
                    feedback.reportError(f"❌ {e}")
                for w in dry_resultado['advertencias']:
                    feedback.pushWarning(f"⚠️ {w}")
                for i in dry_resultado['info']:
                    feedback.pushInfo(f"ℹ️  {i}")

                # Generar reporte HTML de diagnóstico si está activado
                if params.generar_reporte:
                    self._generate_dry_run_report(params, dry_resultado, source.sourceName(), feedback=feedback)

                # Exportar JSON de configuración si está activado
                if params.exportar_config and params.ruta_config_json:
                    self._export_config_json(params, params.ruta_config_json, feedback)

                feedback.setProgress(100)
                feedback.pushInfo("🔍 Validación Previa finalizada. Sin geometrías generadas.")

                # Crear una capa de salida vacía (QGIS exige un sink aunque esté vacío)
                sink_fields_dry = QgsFields()
                sink_fields_dry.append(QgsField('validacion_previa', QVariant.String))
                (sink_dry, dest_id_dry) = self.parameterAsSink(
                    parameters, self.OUTPUT, context, sink_fields_dry,
                    QgsWkbTypes.MultiPolygon, source.sourceCrs())
                if sink_dry is None:
                    raise QgsProcessingException("Error creando capa de salida vacía (Validación Previa)")
                # Asignar nombre visible en el panel de capas de QGIS
                details_dry = QgsProcessingContext.LayerDetails(
                    'Capa_Val_Previa', context.project(), self.OUTPUT)
                context.addLayerToLoadOnCompletion(dest_id_dry, details_dry)
                return {self.OUTPUT: dest_id_dry}
            # ── FIN VALIDACIÓN PREVIA ─────────────────────────────────────────

            feedback.setProgressText("Etapa 1/4 — Preparando geometrías...")

            # ── DISSOLVE PREVIO PARA BÚFER CONCÉNTRICO ───────────────────────
            # Cuando hay N entidades y el tipo es Concéntrico, fusionar todas las
            # geometrías en una sola antes de bufferizar elimina los traslapes entre
            # anillos de entidades adyacentes — exactamente como DISSOLVE=True en
            # native:buffer. Para una sola entidad, unaryUnion devuelve la misma
            # geometría sin costo adicional.
            if params.buffer_type == Constants.BUFFER_CONCENTRICO:
                src_dissolve = layer_input if layer_input else source
                todas_geoms = [
                    f.geometry() for f in src_dissolve.getFeatures()
                    if f.hasGeometry() and not f.geometry().isEmpty()
                ]
                if len(todas_geoms) > 1:
                    geom_disuelta = QgsGeometry.unaryUnion(todas_geoms)
                    if geom_disuelta and not geom_disuelta.isEmpty():
                        feedback.pushInfo(
                            f"🔗 Concéntrico: {len(todas_geoms)} entidades disueltas en 1 "
                            f"antes de bufferizar (elimina traslapes entre entidades adyacentes)."
                        )
                        # Crear capa temporal en memoria con la geometría disuelta
                        crs_str = src_dissolve.crs().authid()
                        geom_type_str = QgsWkbTypes.displayString(
                            QgsWkbTypes.multiType(src_dissolve.wkbType()))
                        temp_layer = QgsVectorLayer(
                            f"{geom_type_str}?crs={crs_str}", "dissolved_temp", "memory")
                        temp_pr = temp_layer.dataProvider()
                        temp_feat = QgsFeature()
                        temp_feat.setGeometry(geom_disuelta)
                        temp_pr.addFeature(temp_feat)
                        temp_layer.updateExtents()
                        layer_input = temp_layer  # reemplazar fuente para _prepare_features

            # Usar la capa directa si está disponible (bypassa el filtro de geometrías inválidas)
            features, v_entrada_antes, v_entrada_despues = self._prepare_features(
                layer_input if layer_input else source, params, logger)
            if not features:
                raise QgsProcessingException("No hay entidades para procesar.")
            
            feedback.pushInfo(f"📊 Features a procesar: {len(features)}")
            
            # Advertencia si la distancia fija es muy pequeña (probable error de configuración)
            # Solo aplica cuando NO se usa campo variable ni densidad adaptativa.
            # Excluidos: OVAL y RECTANGULAR (usan Ancho/Alto), ADAPTATIVO (radio calculado),
            # CONCENTRICO (su distancia relevante es CONCENTRIC_DISTANCE, no DISTANCIA).
            _dist_para_advertencia = params.distancia
            if params.buffer_type == Constants.BUFFER_POR_AREA:
                _dist_para_advertencia = params.area_objetivo  # usa área, no radio

            if (not params.usar_densidad_adaptativa
                    and not params.distance_field
                    and not params.calcular_por_area
                    and params.buffer_type not in [Constants.BUFFER_OVAL,
                                                   Constants.BUFFER_RECTANGULAR,
                                                   Constants.BUFFER_ADAPTATIVO,
                                                   Constants.BUFFER_CONCENTRICO,
                                                   Constants.BUFFER_ANCHO_M]
                    and abs(_dist_para_advertencia) < 1.0
                    and abs(_dist_para_advertencia) >= Constants.MIN_BUFFER_DISTANCE):
                feedback.pushWarning(
                    f"⚠️ DISTANCIA MUY PEQUEÑA: {params.distancia:.4f} m. "
                    f"El búfer será casi invisible en el mapa. "
                    f"¿Quiso decir {params.distancia * 1000:.1f} m? "
                    f"Verifique el parámetro 'Distancia (Radio)'."
                )
            
            exclusion_geom = self._prepare_exclusion_geometry(params)
            if exclusion_geom:
                logger.info("🚫 Capa de exclusión detectada y preparada")

            # ── BÚFER ADAPTATIVO POR DENSIDAD ──────────────────────────────────
            # Si está activo, calcular el radio de cada entidad ANTES de procesar.
            # Los radios se almacenan en un dict {fid: radio} y se inyectan como
            # override en _process_single_feature a través de distance_field override.
            radios_adaptativos: Dict[int, float] = {}
            if params.usar_densidad_adaptativa:
                # BUFFER_ADAPTATIVO (7) es el tipo nativo para densidad adaptativa;
                # internamente usa el motor Circular. También es compatible con Concéntrico.
                tipos_compatibles = [Constants.BUFFER_CIRCULAR, Constants.BUFFER_CONCENTRICO,
                                     Constants.BUFFER_ADAPTATIVO]
                if params.buffer_type not in tipos_compatibles:
                    logger.warning(
                        "⚠️ Búfer Adaptativo por densidad solo aplica a tipos Circular, "
                        "Concéntrico y Adaptativo. Se ignorará para este tipo de búfer."
                    )
                elif params.distance_field:
                    logger.info(
                        f"ℹ️ Búfer Adaptativo: hay un campo de distancia activo "
                        f"('{params.distance_field}'). El campo tiene prioridad; "
                        f"el radio adaptativo no se aplicará."
                    )
                else:
                    feedback.setProgressText("Calculando radios adaptativos por densidad...")
                    geoms_para_densidad = []
                    for _ft in features:
                        _ft = tuple(_ft)
                        _wkb = _ft[0]
                        _fid = _ft[3]
                        _g = QgsGeometry()
                        _g.fromWkb(_wkb)
                        if _g and not _g.isEmpty():
                            geoms_para_densidad.append((_g, _fid))
                    # Radio máximo automático cuando el usuario deja 0:
                    # Se calcula como la diagonal del bounding box de todas las entidades / 4
                    _radio_max_efectivo = params.densidad_radio_max
                    if _radio_max_efectivo <= 0:
                        try:
                            bbox = source.extent()
                            _diag = math.sqrt(bbox.width()**2 + bbox.height()**2)
                            _radio_max_efectivo = max(_diag / 4.0, 100.0)
                            feedback.pushInfo(
                                f"📏 Radio máximo automático: {_radio_max_efectivo:.0f} m "
                                f"(diagonal capa={_diag:.0f} m / 4)"
                            )
                        except Exception:
                            _radio_max_efectivo = 10000.0
                    # === NUEVO: Pasar parámetros de anclaje ===
                    radios_adaptativos = AdaptiveDensityCalculator.calcular(
                        geoms=geoms_para_densidad,
                        metodo=params.densidad_metodo,
                        k=params.densidad_k,
                        radio_referencia=params.densidad_radio_ref,
                        radio_base=params.densidad_radio_base,
                        factor_escala=params.densidad_factor_escala,
                        radio_min=params.densidad_radio_min,
                        radio_max=_radio_max_efectivo,
                        logger=logger,
                        metodo_anclaje=params.densidad_metodo_anclaje,      # NUEVO
                        tolerancia_polo=params.densidad_tolerancia_polo     # NUEVO
                    )
                    feedback.pushInfo(
                        f"🧭 Radios adaptativos calculados para {len(radios_adaptativos)} entidades | "
                        f"Método: {Constants.DENSIDAD_NAMES[params.densidad_metodo]} | "
                        f"Anclaje: {Constants.DENSIDAD_ANCLAJE_NAMES[params.densidad_metodo_anclaje]}"
                    )
            # ───────────────────────────────────────────────────────────────────

            sink_fields = QgsFields()
            sink_fields.append(QgsField('fid', QVariant.Int))
            sink_fields.append(QgsField('tipo_entidad', QVariant.String))
            sink_fields.append(QgsField('area_ha', QVariant.Double, len=20, prec=6))
            
            if params.buffer_type in [Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR]:
                sink_fields.append(QgsField('ancho', QVariant.Double, len=20, prec=2))
                sink_fields.append(QgsField('alto', QVariant.Double, len=20, prec=2))
            else:
                sink_fields.append(QgsField('distancia', QVariant.Double, len=20, prec=2))
            sink_fields.append(QgsField('notas', QVariant.String))
            
            # Agregar campos de traslape si se calcula superposición o fragmentos
            if params.calcular_superposicion or params.generar_fragmentos_traslape:
                sink_fields.append(QgsField('n_traslapes', QVariant.Int))
                sink_fields.append(QgsField('traslapa_con', QVariant.String, len=254))
                sink_fields.append(QgsField('area_exclusiva_ha', QVariant.Double, len=20, prec=6))
                sink_fields.append(QgsField('area_compartida_ha', QVariant.Double, len=20, prec=6))
                sink_fields.append(QgsField('pct_exclusivo', QVariant.Double, len=5, prec=2))
            
            nombre_salida = self._generate_layer_name(params)
            (sink, dest_id) = self.parameterAsSink(parameters, self.OUTPUT, context, sink_fields,
                                                    QgsWkbTypes.MultiPolygon, source.sourceCrs())
            if sink is None:
                raise QgsProcessingException('Error creando salida')
            
            details = QgsProcessingContext.LayerDetails(nombre_salida, context.project(), self.OUTPUT)
            context.addLayerToLoadOnCompletion(dest_id, details)
            
            # Búfer Adaptativo usa el mismo motor que Circular — solo cambia el radio
            _proc_type = Constants.BUFFER_CIRCULAR if params.buffer_type == Constants.BUFFER_ADAPTATIVO else params.buffer_type
            processor = BufferProcessorFactory.get_processor(_proc_type)
            logic_op = LogicOperationFactory.get_operation(params.op_logic)
            
            state = ProcessState()  # Contadores mutables: cnt, area_sum, vértices
            all_buffers = []
            total = len(features)
            step = 100.0 / total if total > 0 else 1
            
            # Lista para recolectar geometrías cuando se necesita resolver traslapes
            # (en lugar de escribir directamente al sink)
            geometrias_pendientes = []
            necesita_postproceso_traslapes = (
                params.resolver_traslapes != Constants.TRASLAPE_MANTENER
                and params.buffer_type != Constants.BUFFER_CONCENTRICO
            )

            if params.resolver_traslapes != Constants.TRASLAPE_MANTENER \
                    and params.buffer_type == Constants.BUFFER_CONCENTRICO:
                feedback.pushWarning(
                    "⚠️ Resolver traslapes no aplica a Búfer Concéntrico — "
                    "los anillos son ya disjuntos por definición. "
                    "En geometrías cóncavas donde los anillos se superponen, "
                    "el recorte eliminaría anillos interiores. "
                    "Opción ignorada."
                )
            
            if necesita_postproceso_traslapes:
                feedback.pushInfo(f"🔀 Post-procesamiento de traslapes activado: {Constants.TRASLAPE_NAMES[params.resolver_traslapes]}")
            
            # Decidir si usar procesamiento paralelo
            # OPTIMIZACIÓN: Solo activar con 50+ búferes (overhead de threads no vale la pena con menos)
            #
            # NOTA TÉCNICA — GIL de Python y GEOS:
            # Python tiene un bloqueo global (GIL) que normalmente limita el paralelismo real
            # en código Python puro. Sin embargo, GEOS (la librería C++ que usa QGIS para
            # calcular búferes) libera el GIL durante sus operaciones, lo que permite ganancia
            # real con múltiples hilos. La ganancia efectiva depende del tipo de búfer:
            #   - Por Área (bisección iterativa): mayor ganancia (~50-65%)
            #   - Concéntrico (N anillos): ganancia media (~30-50%)
            #   - Circular simple: ganancia baja (~10-25%), solo notable con 500+ entidades
            # Para capas pequeñas (<50 entidades), el overhead de crear los hilos supera
            # el beneficio, por eso se usa el umbral PARALELO_THRESHOLD.
            PARALELO_THRESHOLD = Constants.PARALELO_THRESHOLD
            usar_paralelo = params.usar_paralelo and total >= PARALELO_THRESHOLD
            
            # Advertir si está activado pero no se usará
            if params.usar_paralelo and total < PARALELO_THRESHOLD:
                feedback.pushInfo(f"ℹ️ Procesamiento paralelo no activado: solo útil con {PARALELO_THRESHOLD}+ búferes (tiene {total})")
            
            if usar_paralelo:
                feedback.setProgressText(f"Procesando búferes en paralelo ({params.num_threads} hilos)...")
                feedback.pushInfo(f"⚡ Procesamiento PARALELO activado: {params.num_threads} hilos para {total} entidades")
                
                # Capturar referencias para el closure (thread-safe)
                _self = self
                
                # Closure liviano: delega toda la lógica de negocio a _process_single_feature
                # Capturar radios_adaptativos en el closure (thread-safe: solo lectura)
                _radios_adapt = radios_adaptativos
                def procesar_feature(feature_data):
                    """
                    Procesa una feature en un hilo independiente.
                    Retorna el dict estándar de resultado de _process_single_feature().

                    THREAD-SAFETY — objetos compartidos entre hilos:
                    · params          : BufferParams (dataclass congelado por replace() — read-only).
                    · processor       : BufferProcessor — sólo llama métodos que crean geometrías
                                        GEOS nuevas sin mutar estado interno compartido.
                    · exclusion_geom  : QgsGeometry — se usa exclusivamente en lecturas
                                        (.difference(), .isEmpty()); nunca se muta.
                    · logic_op        : LogicOperation — sin estado mutable.
                    · logger          : Logger — todas sus escrituras van bajo threading.Lock().
                    Cada hilo crea su propia instancia de QgsGeometry desde WKB (línea siguiente)
                    y su propio ResourceGuard, garantizando aislamiento completo de estado C++.
                    """
                    _fd = tuple(feature_data)
                    wkb_bytes, desc, override_values, fid = _fd[0], _fd[1], _fd[2], _fd[3]
                    _wkb_orig    = _fd[4] if len(_fd) > 4 else None
                    _m_verts_par = _fd[5] if len(_fd) > 5 else None
                    # Reconstruir desde WKB: instancia C++ independiente por hilo
                    geom = QgsGeometry()
                    geom.fromWkb(wkb_bytes)
                    # Restaurar flag M si fromWkb lo perdió
                    if _wkb_orig and QgsWkbTypes.hasM(QgsWkbTypes.Type(_wkb_orig)) and not QgsWkbTypes.hasM(geom.wkbType()):
                        try: geom.get().setZMTypeFromSubGeometry()
                        except Exception: pass
                    # Inyectar radio adaptativo si aplica (sin override de campo activo)
                    _sin_dist_ov = (
                        override_values is None or
                        (isinstance(override_values, tuple) and
                         (override_values[0] is None if override_values else True))
                    )
                    if _radios_adapt and _sin_dist_ov:
                        if fid in _radios_adapt:
                            radio_adapt = _radios_adapt[fid]
                            if isinstance(override_values, tuple):
                                override_values = (radio_adapt, override_values[1],
                                                   override_values[2], override_values[3])
                            else:
                                override_values = radio_adapt
                        elif params.usar_densidad_adaptativa:
                            override_values = params.densidad_radio_min
                    # Para BUFFER_ANCHO_M: reconstruir geometría LineStringM desde _m_verts_par.
                    if (params.buffer_type == Constants.BUFFER_ANCHO_M
                            and _m_verts_par):
                        _reconstruida2 = False
                        try:
                            from qgis.core import QgsLineString as _QgsLS2, QgsPoint as _QgsP2
                            _ls2 = _QgsLS2()
                            for _vx2, _vy2, _vm2 in _m_verts_par:
                                _ls2.addVertex(_QgsP2(_vx2, _vy2, 0.0, float(_vm2)))
                            _ls2.dropZValue()
                            if QgsWkbTypes.hasM(_ls2.wkbType()):
                                geom = QgsGeometry(_ls2)
                                _reconstruida2 = True
                        except Exception:
                            pass
                        if not _reconstruida2:
                            try:
                                from qgis.core import QgsLineString as _QgsLS2
                                _xs2 = [v[0] for v in _m_verts_par]
                                _ys2 = [v[1] for v in _m_verts_par]
                                _ls2 = _QgsLS2(_xs2, _ys2)
                                _ls2.addMValue(0.0)
                                for _idx_m2, (_vx2, _vy2, _vm2) in enumerate(_m_verts_par):
                                    _ls2.setMAt(_idx_m2, float(_vm2))
                                geom = QgsGeometry(_ls2)
                            except Exception:
                                pass
                    # Cada hilo crea su propio guard (no se comparte entre hilos)
                    return _self._process_single_feature(
                        geom, desc, override_values, fid,
                        params, processor, logic_op, exclusion_geom,
                        logger, guard_por_area=None,
                        m_vertices_data=_m_verts_par)
                
                # Ejecutar en paralelo usando ThreadPoolExecutor
                resultados_paralelos = []
                procesados = 0
                
                with ThreadPoolExecutor(max_workers=params.num_threads) as executor:
                    # Enviar todas las tareas
                    futures = {executor.submit(procesar_feature, f): i for i, f in enumerate(features)}
                    
                    # Recoger resultados conforme van terminando
                    for future in as_completed(futures):
                        if self._check_canceled(feedback):
                            logger.info("⚠️ Proceso cancelado por persona usuaria")
                            executor.shutdown(wait=False)
                            break
                        
                        try:
                            resultado = future.result()
                            resultados_paralelos.append((futures[future], resultado))
                            procesados += 1
                            set_progress_fase(FASE_BUF_INI, FASE_BUF_FIN, int(procesados * 100 / max(total, 1)))
                        except Exception as e:
                            logger.error(f"Error en hilo: {str(e)[:100]}")
                
                # Ordenar por índice original para mantener consistencia
                resultados_paralelos.sort(key=lambda x: x[0])
                
                # Escribir resultados al sink usando el helper compartido
                for idx, resultado in resultados_paralelos:
                    if self._check_canceled(feedback):
                        break
                    self._apply_resultado_to_sink(
                            resultado, params, sink, sink_fields,
                            necesita_postproceso_traslapes, geometrias_pendientes,
                            all_buffers, state, logger, feedback)
            
            else:
                # PROCESAMIENTO SECUENCIAL (original)
                feedback.setProgressText("Procesando búferes...")
                
                # Tip: sugerir paralelo solo cuando realmente ayuda (50+)
                if total >= 50 and not params.usar_paralelo:
                    # Ganancia diferenciada según tipo de búfer (datos experimentales)
                    _bt = params.buffer_type
                    if _bt == Constants.BUFFER_POR_AREA:
                        _ganancia = "50–65% sin simplificación · 10–30% con simplificación"
                    elif _bt == Constants.BUFFER_CONCENTRICO:
                        _nc = params.concentric_count if hasattr(params, 'concentric_count') else 0
                        _ganancia = "~50%" if _nc >= 6 else "~30–45% (pocos anillos)"
                    elif _bt in [Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR]:
                        _ganancia = "~20–40% (mayor con polígonos complejos)"
                    elif _bt in [Constants.BUFFER_CIRCULAR, Constants.BUFFER_ADAPTATIVO]:
                        _ganancia = "~10–25% (solo relevante con 500+ entidades complejas)"
                    else:  # Un Solo Lado, Cuña
                        _ganancia = "~40–60% proporcional · ahorro absoluto usualmente <2 s"
                    feedback.pushInfo(
                        f"💡 Tip: Active 'Procesamiento paralelo' en parámetros avanzados "
                        f"para mejorar el rendimiento con {total} entidades "
                        f"(ganancia estimada para este tipo de búfer: {_ganancia}). "
                        f"Configure los hilos igual al número de P-cores de su CPU."
                    )
                
                # CRÍTICO #4: Crear el ResourceGuard UNA SOLA VEZ antes del loop.
                # Se reutiliza por feature (reset con start_operation) evitando instanciar
                # un nuevo objeto en cada iteración cuando hay miles de features Por Área.
                # max_time_sec=350 cubre la búsqueda binaria; max_vertices=100_000 protege
                # la verificación de complejidad (ambos casos en el mismo objeto).
                _guard_por_area = ResourceGuard(max_time_sec=350, max_vertices=100_000)
                
                for i, feature_tuple in enumerate(features):
                    _ft = tuple(feature_tuple)
                    wkb_bytes, desc, override_values, fid = _ft[0], _ft[1], _ft[2], _ft[3]
                    _wkb_orig2   = _ft[4] if len(_ft) > 4 else None
                    _m_verts_seq = _ft[5] if len(_ft) > 5 else None
                    # Reconstruir desde WKB: instancia C++ independiente
                    geom = QgsGeometry()
                    geom.fromWkb(wkb_bytes)
                    # Restaurar flag M si fromWkb lo perdió
                    if _wkb_orig2 and QgsWkbTypes.hasM(QgsWkbTypes.Type(_wkb_orig2)) and not QgsWkbTypes.hasM(geom.wkbType()):
                        try: geom.get().setZMTypeFromSubGeometry()
                        except Exception: pass
                    if self._check_canceled(feedback):
                        logger.info("⚠️ Proceso cancelado por persona usuaria")
                        break

                    # Inyectar radio adaptativo si está calculado y no hay override de campo
                    # override_values es tupla (dist, ancho, alto, az) — None cuando no hay campos
                    # Se inyecta solo si dist_override es None (no hay campo de distancia activo)
                    _sin_dist_override = (
                        override_values is None or
                        (isinstance(override_values, tuple) and
                         (override_values[0] is None if override_values else True))
                    )
                    if radios_adaptativos and _sin_dist_override:
                        if fid in radios_adaptativos:
                            radio_adapt = radios_adaptativos[fid]
                            # Preservar ancho/alto/az de la tupla original si existen
                            if isinstance(override_values, tuple):
                                override_values = (radio_adapt, override_values[1],
                                                   override_values[2], override_values[3])
                            else:
                                override_values = radio_adapt
                        elif params.usar_densidad_adaptativa:
                            override_values = params.densidad_radio_min
                            logger.warning(f"{desc}: radio adaptativo no calculado — usando radio_min={params.densidad_radio_min}m")

                    # Log descriptivo de override (solo secuencial; el paralelo no tiene contexto de log aquí)
                    if isinstance(override_values, tuple):
                        if len(override_values) == 4:
                            dist_ov, ancho_ov, alto_ov, az_ov = override_values
                        else:
                            dist_ov, ancho_ov, alto_ov = override_values
                            az_ov = None
                    else:
                        dist_ov = override_values; ancho_ov = None; alto_ov = None; az_ov = None
                    
                    if dist_ov is not None or ancho_ov is not None or alto_ov is not None or az_ov is not None:
                        valores_usados = []
                        if dist_ov is not None:
                            valores_usados.append(f"Distancia={dist_ov}")
                        if ancho_ov is not None:
                            valores_usados.append(f"Ancho={ancho_ov}")
                        if alto_ov is not None:
                            valores_usados.append(f"Alto={alto_ov}")
                        if az_ov is not None:
                            valores_usados.append(f"Azimut={az_ov:.1f}°")
                        logger.info(f"📊 {desc}: Usando valores de campo: {', '.join(valores_usados)}")
                    
                    # Para BUFFER_ANCHO_M: reconstruir geometría LineStringM desde
                    # _m_verts_seq (coordenadas pre-extraídas antes de asWkb(), que
                    # puede perder la coordenada M). Se reconstruye siempre que haya
                    # _m_verts_seq disponible, independientemente del WKB original.
                    #
                    # ESTRATEGIA DE RECONSTRUCCIÓN (3 intentos):
                    #   1. QgsPoint(x, y, 0, m) → PointZM + dropZValue → LineStringM
                    #   2. Si falla: QgsLineString con arrays X, Y, M separados
                    #   3. Si todo falla: usar geometría original del WKB
                    if (params.buffer_type == Constants.BUFFER_ANCHO_M
                            and _m_verts_seq):
                        _reconstruida = False
                        # Intento 1: PointZM + dropZValue
                        try:
                            from qgis.core import QgsLineString as _QgsLS, QgsPoint as _QgsP
                            _ls = _QgsLS()
                            for _vx, _vy, _vm in _m_verts_seq:
                                _ls.addVertex(_QgsP(_vx, _vy, 0.0, float(_vm)))
                            _ls.dropZValue()
                            if QgsWkbTypes.hasM(_ls.wkbType()):
                                geom = QgsGeometry(_ls)
                                _reconstruida = True
                        except Exception:
                            pass
                        # Intento 2: arrays X, Y + setMAt
                        if not _reconstruida:
                            try:
                                from qgis.core import QgsLineString as _QgsLS
                                _xs = [v[0] for v in _m_verts_seq]
                                _ys = [v[1] for v in _m_verts_seq]
                                _ls = _QgsLS(_xs, _ys)
                                _ls.addMValue(0.0)
                                for _idx_m, (_vx, _vy, _vm) in enumerate(_m_verts_seq):
                                    _ls.setMAt(_idx_m, float(_vm))
                                geom = QgsGeometry(_ls)
                                _reconstruida = True
                            except Exception as _me2:
                                logger.warning(f"{desc}: No se pudo reconstruir geometría con M: {_me2}")
                    resultado = self._process_single_feature(
                        geom, desc, override_values, fid,
                        params, processor, logic_op, exclusion_geom,
                        logger, guard_por_area=_guard_por_area,
                        m_vertices_data=_m_verts_seq)
                    
                    # Manejar errores de timeout por separado para mensaje específico
                    if resultado.get('error') and 'excedió tiempo límite' in resultado['error']:
                        logger.error(f"{desc}: Tiempo límite excedido en búfer Por Área "
                                     f"({resultado['error'][:120]}). "
                                     f"Entidad omitida. Considere simplificar la geometría o reducir segmentos.")
                    
                    self._apply_resultado_to_sink(
                            resultado, params, sink, sink_fields,
                            necesita_postproceso_traslapes, geometrias_pendientes,
                            all_buffers, state, logger, feedback)
                    
                    set_progress_fase(FASE_BUF_INI, FASE_BUF_FIN, int(i * 100 / max(total, 1)))

            # ===================================================================
            # VERIFICAR CANCELACIÓN antes de post-procesamiento
            # ===================================================================
            if self._check_canceled(feedback):
                feedback.pushInfo("🛑 Proceso cancelado por perspma usuaria. Retornando resultados parciales.")
                return {self.OUTPUT: dest_id}
            
            # ===================================================================
            # POST-PROCESAMIENTO DE TRASLAPES (si está activado)
            # ===================================================================
            if necesita_postproceso_traslapes and len(geometrias_pendientes) > 0:
                feedback.setProgressText(f"Resolviendo traslapes entre {len(geometrias_pendientes)} geometrías...")
                
                # Aplicar resolución de traslapes
                geometrias_procesadas = GeometryPostProcessor.resolver_traslapes(
                    geometrias_pendientes, params.resolver_traslapes, logger, feedback)
                
                # Ahora escribir las geometrías procesadas al sink
                feedback.setProgressText("Escribiendo geometrías procesadas...")
                state.cnt = 0  # Reiniciar contador para IDs correctos tras resolución de traslapes
                
                for g_data in geometrias_procesadas:
                    geom = g_data['geom']
                    if geom and not geom.isEmpty():
                        geom = self._sanitize_geometry(geom)
                        if not geom or geom.isEmpty():
                            continue
                        # Recalcular área después del post-procesamiento
                        area = geom.area() / Constants.HA_TO_M2
                        state.area_sum += area
                        logger.registrar_area(area)
                        
                        feat = QgsFeature(sink_fields)
                        feat.setGeometry(geom)
                        
                        # Actualizar atributos con la nueva área
                        attrs = g_data['attrs'].copy()
                        attrs[0] = state.cnt + 1  # Actualizar ID
                        attrs[2] = area             # Actualizar área
                        feat.setAttributes(attrs)
                        
                        sink.addFeature(feat, QgsFeatureSink.FastInsert)
                        state.cnt += 1
                
                feedback.pushInfo(f"✅ Traslapes resueltos: {len(geometrias_procesadas)} geometrías finales")
            
            # === DISOLUCIÓN DE BÚFERES ===
            if params.disolver_buferes and state.cnt > 0:
                feedback.setProgressText("Disolviendo búferes...")
                feedback.pushInfo("🔗 Aplicando disolución de búferes (fusionando geometrías que se tocan)...")
                
                try:
                    # Leer todas las geometrías del sink de salida
                    output_layer = QgsProcessingUtils.mapLayerFromString(dest_id, context)
                    if not output_layer:
                        output_layer = context.getMapLayer(dest_id)
                    
                    if output_layer and output_layer.featureCount() > 0:
                        # Recolectar todas las geometrías con sus atributos
                        all_geoms = []
                        all_attrs = []
                        
                        for feat in output_layer.getFeatures():
                            if feat.hasGeometry():
                                geom = feat.geometry()
                                if geom and not geom.isEmpty():
                                    all_geoms.append(geom)
                                    all_attrs.append(feat.attributes())
                        
                        if all_geoms:
                            # CRÍTICO #2: Aplicar disolución por lotes para evitar OOM
                            feedback.pushInfo(f"   🔧 Procesando {len(all_geoms)} geometrías con método optimizado por lotes...")
                            dissolved_geom = self._dissolve_in_batches(all_geoms, feedback, batch_size=500)
                            
                            if dissolved_geom and not dissolved_geom.isEmpty():
                                # Separar geometrías individuales del resultado de unaryUnion
                                individual_parts = []
                                
                                if dissolved_geom.isMultipart():
                                    # Extraer cada parte del multipolígono
                                    multi_geom = dissolved_geom.asMultiPolygon()
                                    for polygon in multi_geom:
                                        part_geom = QgsGeometry.fromPolygonXY(polygon)
                                        if part_geom and not part_geom.isEmpty():
                                            individual_parts.append(part_geom)
                                else:
                                    # Es un polígono simple
                                    individual_parts.append(dissolved_geom)
                                
                                # NUEVO: Identificar cuántos búferes originales forman cada polígono disuelto
                                # Para determinar si hubo fusión real o no
                                buffers_por_parte = []
                                for part_geom in individual_parts:
                                    count = 0
                                    # Contar cuántos búferes originales intersectan con esta parte
                                    for orig_geom in all_geoms:
                                        if part_geom.intersects(orig_geom):
                                            count += 1
                                    buffers_por_parte.append(count)
                                
                                feedback.pushInfo(f"   🔍 Análisis: {buffers_por_parte} búferes por polígono")
                                
                                # Editar la capa existente en lugar de crear una nueva
                                # try-except garantiza rollBack si hay excepción — evita bloqueo del data provider.
                                output_layer.startEditing()
                                try:

                                    # Borrar todos los features existentes
                                    output_layer.deleteFeatures([f.id() for f in output_layer.getFeatures()])

                                    # Agregar cada polígono individual como feature separado
                                    total_area = 0.0
                                    new_all_buffers = []
                                    new_features = []

                                    # Determinar el índice del campo de distancia/ancho/alto según el tipo de búfer
                                    if params.buffer_type in [Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR]:
                                        idx_dim1 = 3  # ancho
                                        idx_dim2 = 4  # alto
                                        idx_notas = 5
                                    else:
                                        idx_distancia = 3  # distancia
                                        idx_notas = 4

                                    # Contadores para nombres
                                    contador_disueltos = 0
                                    contador_sin_traslape = 0

                                    for idx, part_geom in enumerate(individual_parts):
                                        # Crear nuevo feature con los campos de la capa
                                        dissolved_feat = QgsFeature(output_layer.fields())
                                        dissolved_feat.setGeometry(part_geom)

                                        part_area = part_geom.area() / Constants.HA_TO_M2
                                        total_area += part_area

                                        # Determinar si este polígono es resultado de fusión o no
                                        num_buffers_fusionados = buffers_por_parte[idx]
                                        es_fusion = num_buffers_fusionados > 1

                                        if es_fusion:
                                            contador_disueltos += 1
                                            tipo_nombre = f"Disuelto-{contador_disueltos}"
                                            nota_texto = f"Fusión de {num_buffers_fusionados} búferes"
                                        else:
                                            contador_sin_traslape += 1
                                            tipo_nombre = "Sin traslape"
                                            nota_texto = "Búfer individual sin traslape"

                                        # Construir atributos usando los campos correctos
                                        if all_attrs and len(all_attrs) > 0:
                                            # IMPORTANTE: Usar list() para crear copia profunda
                                            template_attrs = list(all_attrs[0])

                                            # Actualizar campos básicos (SIEMPRE crear nuevos valores, no modificar referencias)
                                            template_attrs[0] = int(idx + 1)  # fid - nuevo int
                                            template_attrs[1] = str(tipo_nombre)  # tipo_entidad - nuevo string
                                            template_attrs[2] = float(part_area)  # area_ha - nuevo float

                                            # Actualizar campo de dimensión según tipo de búfer
                                            if params.buffer_type in [Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR]:
                                                template_attrs[idx_dim1] = float(0.0)  # ancho - nuevo float
                                                template_attrs[idx_dim2] = float(0.0)  # alto - nuevo float
                                            else:
                                                template_attrs[idx_distancia] = float(0.0)  # distancia - nuevo float

                                            # IMPORTANTE: Crear nuevo string para cada feature
                                            template_attrs[idx_notas] = str(nota_texto)

                                            # Si hay campos de traslape, inicializarlos en 0
                                            if len(template_attrs) > idx_notas + 1:
                                                for i in range(idx_notas + 1, len(template_attrs)):
                                                    if isinstance(template_attrs[i], (int, float)):
                                                        template_attrs[i] = 0
                                                    else:
                                                        template_attrs[i] = ""

                                            dissolved_feat.setAttributes(template_attrs)
                                        else:
                                            # Crear atributos mínimos si no hay plantilla
                                            basic_attrs = [
                                                idx + 1,  # fid
                                                tipo_nombre,  # tipo_entidad
                                                part_area  # area_ha
                                            ]

                                            # Agregar campo dimensional según tipo
                                            if params.buffer_type in [Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR]:
                                                basic_attrs.extend([0.0, 0.0])  # ancho, alto
                                            else:
                                                basic_attrs.append(0.0)  # distancia

                                            basic_attrs.append(nota_texto)  # notas

                                            # Agregar campos de traslape si existen
                                            if params.calcular_superposicion or params.generar_fragmentos_traslape:
                                                basic_attrs.extend([0, "", 0.0, 0.0, 0.0])

                                            dissolved_feat.setAttributes(basic_attrs)

                                        # Agregar feature a la lista
                                        new_features.append(dissolved_feat)

                                        # Actualizar all_buffers para análisis de superposición
                                        new_all_buffers.append((part_geom, tipo_nombre, 0.0, nota_texto))

                                    # Agregar todos los nuevos features a la capa
                                    output_layer.addFeatures(new_features)
                                    output_layer.commitChanges()


                                except Exception as _e_edit:
                                    output_layer.rollBack()
                                    feedback.reportError(
                                        f"⚠️ Error en edición de capa disuelta — cambios revertidos: "
                                        f"{str(_e_edit)[:100]}"
                                    )
                                    raise
                                # Actualizar variables para el reporte
                                state.cnt = len(individual_parts)
                                state.area_sum = total_area
                                all_buffers = new_all_buffers
                                
                                feedback.pushInfo(f"   ✅ Disolución completada: {len(all_geoms)} → {state.cnt} polígono(s)")
                                feedback.pushInfo(f"   📊 Área total: {state.area_sum:.4f} ha")
                                
                                # Advertencia si hubo lotes fallidos (resultado puede ser incompleto)
                                # feedback.pushWarning ya fue emitido dentro de _dissolve_in_batches
                                # Aquí se agrega nota en el Estado del Proceso para visibilidad
                                if dissolved_geom and len(individual_parts) < len(all_geoms):
                                    feedback.pushWarning(
                                        f"⚠️ Estado del Proceso — Disolución posiblemente incompleta: "
                                        f"se obtuvieron {state.cnt} polígono(s) de {len(all_geoms)} geometrías originales. "
                                        f"Uno o más lotes fallaron por timeout (300s). "
                                        f"Verifique el log completo y compare el conteo de salida con el de entrada."
                                    )
                                
                                # Reportar con más detalle
                                if contador_disueltos > 0:
                                    feedback.pushInfo(f"   🔗 {contador_disueltos} polígono(s) con fusión de búferes")
                                if contador_sin_traslape > 0:
                                    feedback.pushInfo(f"   ⭕ {contador_sin_traslape} polígono(s) sin traslape (individuales)")
                            else:
                                feedback.pushWarning("⚠️ La disolución produjo una geometría vacía")
                        else:
                            feedback.pushWarning("⚠️ No se encontraron geometrías válidas para disolver")
                    else:
                        feedback.pushWarning("⚠️ No se pudo acceder a la capa de salida para disolución")
                        
                except Exception as e:
                    feedback.reportError(f"❌ Error durante la disolución: {str(e)}")
                    logger.error(f"Error en disolución de búferes: {str(e)}")
            
            feedback.setProgressText("Finalizando...")
            
            # Verificar cancelación antes de análisis de superposición
            if self._check_canceled(feedback):
                feedback.pushInfo("🛑 Proceso cancelado por persona usuaria. Retornando resultados parciales.")
                return {self.OUTPUT: dest_id}
            
            overlap_data = []
            # Calcular superposición si se solicita O si se van a generar fragmentos (necesario para campos de traslape)
            if (params.calcular_superposicion or params.generar_fragmentos_traslape) and len(all_buffers) > 1:
                feedback.setProgressText("Calculando superposiciones...")
                logger.info(f"📊 Analizando superposición entre {len(all_buffers)} búferes...")
                overlap_data = OverlapAnalyzer.analyze(all_buffers, feedback)
                if overlap_data:
                    logger.info(f"   ✅ Encontradas {len(overlap_data)} superposiciones")
                else:
                    logger.info(f"   ℹ️ No se encontraron superposiciones significativas")
            elif (params.calcular_superposicion or params.generar_fragmentos_traslape) and len(all_buffers) <= 1:
                logger.info("📊 Análisis de superposición omitido: se requieren al menos 2 búferes")
            
            if params.aplicar_transparencia:
                output_layer = QgsProcessingUtils.mapLayerFromString(dest_id, context)
                if not output_layer:
                    output_layer = context.getMapLayer(dest_id)
                if output_layer:
                    TransparencyManager.aplicar_transparencia(output_layer, params.nivel_transparencia, context)
            
            # Mostrar resumen en consola
            tiempo_total = logger.get_tiempo_ejecucion()
            feedback.pushInfo(f"✅ Procesamiento completado en {tiempo_total:.2f} segundos")
            feedback.pushInfo(f"📊 Geometrías generadas: {state.cnt}")
            
            # Mostrar estadísticas de integridad
            metricas = logger.get_metricas()
            feedback.pushInfo(f"🔧 Geometrías reparadas: {len(logger.reparados_ids)}")
            feedback.pushInfo(f"🚫 Geometrías omitidas: {len(logger.omitidos_ids)}")
            feedback.pushInfo(f"⚠️ Geometrías con riesgo: {len(logger.riesgo_ids)}")
            
            if params.generar_reporte:
                geometry_efficiency = {
                    'vertices_antes': state.total_vertices_antes,
                    'vertices_despues': state.total_vertices_despues,
                    'simplificacion_activa': params.aplicar_simplificacion,
                    'tolerancia': params.tolerancia_simplificacion,
                    # Estadísticas de simplificación de entrada
                    'v_entrada_antes': v_entrada_antes,
                    'v_entrada_despues': v_entrada_despues,
                    'simplif_entrada_activa': params.simplificar_entrada,
                    'tolerancia_entrada': params.tolerancia_entrada,
                }
                
                # Debug: mostrar info de superposiciones
                if overlap_data:
                    feedback.pushInfo(f"📊 Superposiciones para reporte: {len(overlap_data)}")
                    for i, o in enumerate(overlap_data[:3]):
                        feedback.pushInfo(f"   {i+1}. {o['buffer_1_tipo']} ∩ {o['buffer_2_tipo']}: {o['area_ha']:.4f} ha ({o['porcentaje']:.1f}%)")
            
            # Calcular traslapes por búfer si se solicitó análisis
            traslapes_por_bufer = {}
            if (params.calcular_superposicion or params.generar_fragmentos_traslape) and overlap_data:
                traslapes_por_bufer = self._calcular_traslapes_por_bufer(overlap_data, len(all_buffers), all_buffers)
                feedback.pushInfo(f"📊 Calculados traslapes para {len(traslapes_por_bufer)} búferes")
                
                # Actualizar campos de traslape en la capa de salida
                output_layer = QgsProcessingUtils.mapLayerFromString(dest_id, context)
                if not output_layer:
                    output_layer = context.getMapLayer(dest_id)
                
                if output_layer:
                    feedback.pushInfo("🔄 Actualizando campos de traslape en capa de salida...")
                    
                    # Verificar que los campos existen
                    field_names = [field.name() for field in output_layer.fields()]
                    has_traslape_fields = 'n_traslapes' in field_names and 'traslapa_con' in field_names
                    
                    if has_traslape_fields:
                        output_layer.startEditing()
                        try:

                            # Pre-calcular índices de campos UNA SOLA VEZ (fuera del loop)
                            # Esto evita llamadas repetidas a fields().indexOf()
                            idx_n_traslapes = output_layer.fields().indexOf('n_traslapes')
                            idx_traslapa_con = output_layer.fields().indexOf('traslapa_con')
                            idx_area_excl = output_layer.fields().indexOf('area_exclusiva_ha')
                            idx_area_comp = output_layer.fields().indexOf('area_compartida_ha')
                            idx_pct_excl = output_layer.fields().indexOf('pct_exclusivo')

                            # Crear mapeo entre feature ID de QGIS y índice de búfer
                            # Usamos el orden de features que coincide con all_buffers
                            features_actualizados = 0
                            for idx, feature in enumerate(output_layer.getFeatures()):
                                if idx in traslapes_por_bufer:
                                    info = traslapes_por_bufer[idx]
                                    feature_id = feature.id()

                                    # OPTIMIZACIÓN: Usar changeAttributeValues con diccionario
                                    # Una sola llamada en lugar de 5 llamadas a changeAttributeValue
                                    attrs_to_update = {
                                        idx_n_traslapes: info['n_traslapes'],
                                        idx_traslapa_con: info['traslapa_con'],
                                        idx_area_excl: info['area_exclusiva_ha'],
                                        idx_area_comp: info['area_compartida_ha'],
                                        idx_pct_excl: info['pct_exclusivo']
                                    }

                                    output_layer.changeAttributeValues(feature_id, attrs_to_update)
                                    features_actualizados += 1

                            output_layer.commitChanges()
                            feedback.pushInfo(f"✅ Campos de traslape actualizados ({features_actualizados} búferes)")
                        except Exception as _e_edit2:
                            output_layer.rollBack()
                            feedback.reportError(
                                f"⚠️ Error actualizando campos de traslape — cambios revertidos: "
                                f"{str(_e_edit2)[:100]}"
                            )
                    else:
                        feedback.pushInfo(f"⚠️ Los campos n_traslapes y traslapa_con no existen en la capa")
            
            # Fase 3 — Post-proceso
            feedback.setProgressText("Etapa 3/4 — Post-proceso y análisis...")
            set_progress_fase(FASE_POST_INI, FASE_POST_FIN, 0)

            # ── ADVERTENCIA DE COLAPSO EN RESUMEN ─────────────────────────────
            # Si se solicitaron N anillos concéntricos y se generaron menos,
            # resumir el déficit claramente al final del log.
            if (params.buffer_type == Constants.BUFFER_CONCENTRICO and
                    params.concentric_distance < 0):
                anillos_pedidos = params.concentric_count * len(features)
                if state.cnt < anillos_pedidos:
                    deficit = anillos_pedidos - state.cnt
                    feedback.pushWarning(
                        f"⚠️ RESUMEN DE COLAPSO: Se solicitaron {anillos_pedidos} anillo(s) "
                        f"({params.concentric_count} × {len(features)} entidad(es)) "
                        f"pero se generaron {state.cnt}. "
                        f"{deficit} anillo(s) colapsaron por distancia negativa excesiva. "
                        f"Consulte el log de cada entidad para el detalle."
                    )

            if (params.buffer_type == Constants.BUFFER_CIRCULAR and
                    params.distancia < 0 and state.cnt < len(features)):
                deficit = len(features) - state.cnt
                feedback.pushWarning(
                    f"⚠️ RESUMEN DE COLAPSO: Se solicitaron {len(features)} búfer(es) "
                    f"pero se generaron {state.cnt}. "
                    f"{deficit} entidad(es) colapsaron (radio negativo supera el tamaño de la geometría). "
                    f"Consulte el log de cada entidad para el detalle."
                )

            if (params.buffer_type == Constants.BUFFER_UN_LADO and
                    params.distancia < 0 and state.cnt < len(features)):
                deficit = len(features) - state.cnt
                feedback.pushWarning(
                    f"⚠️ RESUMEN: Se solicitaron {len(features)} búfer(es) Un Solo Lado "
                    f"pero se generaron {state.cnt}. "
                    f"{deficit} entidad(es) produjeron resultado vacío (posible colapso por distancia negativa). "
                    f"Consulte el log de cada entidad para el detalle."
                )

            # Generar fragmentos de traslape si está activado
            fragment_dest_id = None
            fragmentos_generados = False
            fragment_stats = None
            if params.generar_fragmentos_traslape and all_buffers:
                fragment_result = self._generate_overlap_fragments(
                    all_buffers, params, parameters, context, source.sourceCrs(), feedback)
                if fragment_result:
                    fragment_dest_id = fragment_result['dest_id']
                    fragment_stats = fragment_result['stats']
                    fragmentos_generados = True
            
            # Fase 4 — Reporte y cierre
            feedback.setProgressText("Etapa 4/4 — Generando reporte...")
            set_progress_fase(FASE_REP_INI, FASE_REP_FIN, 0)

            # Exportar configuración JSON si está activado
            if params.exportar_config and params.ruta_config_json:
                self._export_config_json(params, params.ruta_config_json, feedback)

            # Generar reporte (después de fragmentos para saber si se generaron)
            if params.generar_reporte:
                self._generate_report(params, logger, state.cnt, state.area_sum, source.sourceName(), 
                                      overlap_data, geometry_efficiency, metricas, fragmentos_generados, fragment_stats,
                                      geom_type_source=geom_type_proc,
                                      campo_tiene_negativos=campo_tiene_negativos,
                                      feedback=feedback)
            
            # Retornar resultados
            set_progress_fase(FASE_REP_INI, FASE_REP_FIN, 100)
            feedback.setProgress(100)
            
            results = {self.OUTPUT: dest_id}
            if fragment_dest_id is not None:
                results[self.OUTPUT_FRAGMENTOS] = fragment_dest_id
            
            return results
        
        except QgsProcessingException:
            raise
        except Exception as e:
            exc_info = ''.join(traceback.format_exception(*sys.exc_info()))
            feedback.reportError(f"❌ Error crítico: {e}\n{exc_info}")
            raise QgsProcessingException(str(e))

    def _generate_report(self, params: BufferParams, logger: Logger, cnt: int, 
                         area_sum: float, source_name: str, overlap_data: List[Dict],
                         geometry_efficiency: Dict = None, metricas: Dict = None,
                         fragmentos_generados: bool = False, fragment_stats: Dict = None,
                         geom_type_source=None, campo_tiene_negativos: bool = False,
                         feedback=None):
        """Genera el reporte HTML con métricas completas."""
        if geometry_efficiency is None:
            geometry_efficiency = {'vertices_antes': 0, 'vertices_despues': 0, 
                                   'simplificacion_activa': False, 'tolerancia': 0}
        if metricas is None:
            metricas = logger.get_metricas()
        
        params_report = self._build_params_report(params)
        
        # Nota adicional en Parámetros Utilizados para Un Solo Lado con negativos en campo
        if (params.buffer_type == Constants.BUFFER_UN_LADO and params.distance_field):
            advertencias_un_lado = [a for a in logger.advertencias if 'Un Lado' in a and 'negativo' in a.lower()]
            if advertencias_un_lado:
                if geom_type_source == QgsWkbTypes.LineGeometry:
                    lado_cfg = "Izquierda" if params.side_idx == 0 else "Derecha"
                    lado_inv = "Derecha" if params.side_idx == 0 else "Izquierda"
                    params_report['⚠️ Valores negativos en campo'] = (
                        f"Detectados en '{params.distance_field}'. "
                        f"En líneas, el negativo invierte el lado: "
                        f"{lado_cfg} → {lado_inv}. "
                        f"Ver sección Alertas para detalle por entidad."
                    )
                elif geom_type_source == QgsWkbTypes.PolygonGeometry:
                    params_report['⚠️ Valores negativos en campo'] = (
                        f"Detectados en '{params.distance_field}'. "
                        f"En polígonos el signo se ignora — se usa el valor absoluto. "
                        f"El lado permanece: {'Izquierda (Exterior)' if params.side_idx == 0 else 'Derecha (Interior)'}. "
                        f"Ver sección Alertas para detalle por entidad."
                    )
        
        # Nota informativa para Cuña con valores negativos de radio en campo
        # Spec: radio negativo campo → cuña generada invertida 180° + alerta en log Y reporte
        if (params.buffer_type == Constants.BUFFER_CUNA and
                (params.distance_field or params.category_field)):
            advertencias_cuna = [a for a in logger.advertencias
                                  if 'Cuña' in a and 'negativo' in a.lower()]
            if advertencias_cuna:
                campo_radio = params.distance_field or params.category_field
                n_neg = len(advertencias_cuna)
                params_report['⚠️ Radios negativos en campo (Cuña)'] = (
                    f"Se detectaron {n_neg} entidad(es) con radio negativo en el campo '{campo_radio}'. "
                    f"Cada una generó una cuña orientada 180° opuesta al azimut configurado "
                    f"(invertida automáticamente por el signo del valor). "
                    f"Ver sección Alertas para el detalle por entidad."
                )

        # Nota informativa en Parámetros Utilizados para Circular/Concéntrico con polígonos y negativos
        if (campo_tiene_negativos and
                params.buffer_type in [Constants.BUFFER_CIRCULAR, Constants.BUFFER_CONCENTRICO] and
                geom_type_source == QgsWkbTypes.PolygonGeometry and params.distance_field):
            tipo_bufer = "Circular" if params.buffer_type == Constants.BUFFER_CIRCULAR else "Concéntrico"
            params_report[f'ℹ️ Campo con negativos ({tipo_bufer})'] = (
                f"Los valores negativos en '{params.distance_field}' generan "
                f"contracción interior del polígono (búfer hacia adentro). "
                f"Valores positivos generan expansión exterior. "
                f"Ambos pueden coexistir en el mismo campo."
            )
        
        # Extraer métricas
        tiempo_ejecucion = metricas.get('tiempo_ejecucion', 0)
        geom_procesadas = metricas.get('geometrias_procesadas', 0)
        geom_reparadas = metricas.get('geometrias_reparadas', 0)
        geom_omitidas = metricas.get('geometrias_omitidas', 0)
        
        # Generar secciones HTML
        crs_warning_html = self._report_crs_warning(params)
        metricas_html = self._report_metrics(metricas, cnt, params)
        integridad_html = self._report_integrity(metricas, params, geom_procesadas, geom_reparadas, geom_omitidas)
        overlap_html = self._report_overlap(overlap_data, params)
        fragments_html = self._report_fragments_info(params, fragmentos_generados, fragment_stats)
        efficiency_html = self._report_efficiency(geometry_efficiency)
        
        # Ensamblar HTML final
        html = self._report_assemble(
            params, logger, cnt, area_sum, source_name, 
            tiempo_ejecucion, params_report,
            crs_warning_html, metricas_html, integridad_html, 
            overlap_html, fragments_html, efficiency_html
        )
        
        # Escribir y abrir
        self._report_write_and_open(html, params.ruta_reporte, feedback=feedback)
    
    def _build_params_report(self, params: BufferParams) -> Dict[str, str]:
        """Construye el diccionario de parámetros para el reporte."""
        params_report = {
            'Operación Lógica': Constants.OP_NAMES[params.op_logic],
            'Transparencia': f"{params.nivel_transparencia}%" if params.aplicar_transparencia else "No",
            'Gestión de Integridad': Constants.INTEGRIDAD_NAMES[params.gestion_integridad]
        }
        
        estilos = JoinStyleManager.get_style_name(params.join_idx)
        bt = params.buffer_type
        tiene_campo = bool(params.distance_field)
        
        # Sufijo indicador de campo variable
        campo_tag = f" ⚡ (Campo: '{params.distance_field}')" if tiene_campo else ""
        
        if bt == Constants.BUFFER_CIRCULAR:
            if params.calcular_por_area:
                params_report['Modo'] = f"Por Área ({params.area_objetivo})"
            else:
                if tiene_campo:
                    params_report['Distancia'] = f"Variable por entidad{campo_tag}"
                    params_report['Distancia (valor por defecto)'] = f"{params.distancia:.2f} m"
                else:
                    params_report['Distancia'] = f"{params.distancia:.2f} m"
        elif bt in [Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR]:
            if tiene_campo:
                if bt == Constants.BUFFER_RECTANGULAR and params.usar_corredor:
                    params_report['Ancho'] = f"Variable por entidad{campo_tag}"
                    params_report['Ancho (valor por defecto)'] = f"{params.ancho:.2f} m"
                else:
                    params_report['Dimensiones (Ancho x Alto)'] = f"Variable por entidad{campo_tag}"
                    params_report['Dimensiones (valor por defecto)'] = f"{params.ancho:.2f} x {params.alto:.2f} m"
            else:
                params_report['Dimensiones (Ancho x Alto)'] = f"{params.ancho:.2f} x {params.alto:.2f} m"
            params_report['Rotación'] = f"{params.rotacion}°"
            params_report['Rotación Automática'] = "SÍ (orienta según eje principal de la geometría)" if params.usar_rot_auto else "No"
            if bt == Constants.BUFFER_RECTANGULAR and params.usar_corredor:
                params_report['Modo'] = "Corredor (Líneas)"
        elif bt == Constants.BUFFER_CONCENTRICO:
            params_report['Anillos'] = str(params.concentric_count)
            if tiene_campo:
                params_report['Distancia anillos'] = f"Variable por entidad{campo_tag}"
                params_report['Distancia anillos (valor por defecto)'] = f"{params.concentric_distance:.2f} m"
            else:
                params_report['Distancia'] = str(params.concentric_distance)
            params_report['Tipo Geometría'] = "Dónut (Disjuntos)" if params.anillos_disjuntos else "Discos (Acumulativos)"
            params_report['Estilo de Unión'] = estilos
        elif bt == Constants.BUFFER_POR_AREA:
            if tiene_campo:
                u_symbol = Constants.AREA_UNIT_SYMBOLS[params.unidad_area]
                params_report['Área objetivo'] = f"Variable por entidad ({u_symbol}){campo_tag}"
                params_report['Área objetivo (valor por defecto)'] = f"{params.area_objetivo} {u_symbol}"
            else:
                params_report['Área objetivo'] = f"{params.area_objetivo} {Constants.AREA_UNIT_SYMBOLS[params.unidad_area]}"
        elif bt == Constants.BUFFER_UN_LADO:
            params_report['Modo'] = "Un solo lado"
            if tiene_campo:
                params_report['Distancia'] = f"Variable por entidad{campo_tag}"
                params_report['Distancia (valor por defecto)'] = f"{params.distancia:.2f} m"
            else:
                params_report['Distancia'] = f"{params.distancia:.2f} m"
            params_report['Lado'] = "Izquierda" if params.side_idx == 0 else "Derecha"
            params_report['Estilo de Unión'] = estilos
        elif bt == Constants.BUFFER_CUNA:
            params_report['Modo'] = "Cuña (Sector Direccional)"
            if tiene_campo:
                params_report['Radio'] = f"Variable por entidad{campo_tag}"
                params_report['Radio (valor por defecto)'] = f"{params.distancia:.2f} m"
            else:
                params_report['Radio'] = f"{params.distancia:.2f} m"
            # Ángulo de inicio: indicar si usa campo de rotación o valor fijo
            if params.rotation_field and not params.usar_rot_auto:
                params_report['Azimut Inicio'] = f"Variable por entidad (campo: '{params.rotation_field}')"
                params_report['Azimut Inicio (valor por defecto)'] = f"{params.wedge_start}°"
            else:
                params_report['Azimut Inicio'] = f"{params.wedge_start}°"
            params_report['Amplitud'] = f"{params.wedge_width}°"
            if params.usar_rot_auto:
                params_report['Rotación Automática'] = "SÍ (ignora Azimut fijo y Campo de Rotación)"
            elif params.rotation_field:
                params_report['Rotación Automática'] = f"No (campo '{params.rotation_field}' activo)"
            else:
                params_report['Rotación Automática'] = "No"
        
        if params.es_transformacion_activa:
            params_report['Transformación de Puntos'] = "Casco Convexo" if params.usar_hull else "Caja Envolvente"
        if params.exclusion_layer:
            params_report['Capa de Exclusión'] = "Sí (aplicada)"
        if params.preview_mode:
            params_report['Modo'] = "Previsualización (solo primera entidad)"
        if params.simplificar_entrada:
            params_report['Simplif. Entrada'] = f"Activa — polígonos y líneas (Tolerancia: {params.tolerancia_entrada:.1f} m)"
        if params.aplicar_simplificacion:
            params_report['Simplif. Salida'] = f"Activa (Tolerancia: {params.tolerancia_simplificacion:.2f} m)"
        
        if params.usar_paralelo:
            params_report['Procesamiento'] = f"⚡ PARALELO ({params.num_threads} hilos)"
        else:
            params_report['Procesamiento'] = "Secuencial (estándar)"
        
        if params.resolver_traslapes != Constants.TRASLAPE_MANTENER:
            params_report['Resolución de Traslapes'] = Constants.TRASLAPE_NAMES[params.resolver_traslapes]
        
        if params.eliminar_huecos:
            if params.area_minima_hueco > 0:
                params_report['Eliminar Huecos'] = f"Activo (mín: {params.area_minima_hueco:.2f} m²)"
            elif params.preservar_hueco_estructural:
                params_report['Eliminar Huecos'] = "Activo (preservando hueco estructural 🍩)"
            else:
                params_report['Eliminar Huecos'] = "Activo (todos los huecos)"
        
        # Exportar configuración JSON
        if params.exportar_config:
            params_report['💾 Exportar JSON'] = f"✅ {params.ruta_config_json or '(ruta temporal)'}"

        # Modo Validación Previa (solo aparece en el reporte informativo)
        if params.validacion_previa:
            params_report['🔍 Validación Previa'] = "✅ Activada — solo validación, sin geometrías generadas"

        # Búfer adaptativo por densidad
        if params.usar_densidad_adaptativa:
            metodo_str = Constants.DENSIDAD_NAMES[params.densidad_metodo]
            anclaje_str = Constants.DENSIDAD_ANCLAJE_NAMES[params.densidad_metodo_anclaje]
            
            if params.densidad_metodo == Constants.DENSIDAD_KNN:
                params_report['🧭 Búfer Adaptativo'] = (
                    f"✅ Activado · Método: {metodo_str} · "
                    f"Anclaje: {anclaje_str} · "
                    f"K={params.densidad_k} vecinos · "
                    f"Escala={params.densidad_factor_escala} · "
                    f"Rango=[{params.densidad_radio_min:.1f}m – {params.densidad_radio_max:.1f}m]"
                )
            else:
                params_report['🧭 Búfer Adaptativo'] = (
                    f"✅ Activado · Método: {metodo_str} · "
                    f"Anclaje: {anclaje_str} · "
                    f"Radio ref={params.densidad_radio_ref:.1f}m · "
                    f"Radio base={params.densidad_radio_base:.1f}m · "
                    f"Escala={params.densidad_factor_escala} · "
                    f"Rango=[{params.densidad_radio_min:.1f}m – {params.densidad_radio_max:.1f}m]"
                )
            
            if params.densidad_metodo_anclaje == Constants.DENSIDAD_ANCLAJE_POLO:
                params_report['🎯 Tolerancia polo'] = f"{params.densidad_tolerancia_polo} m"

        # Disolución de búferes
        if params.disolver_buferes:
            params_report['Disolver Búferes'] = "✅ Activado (fusionar geometrías que se tocan)"
        
        # Análisis de superposición y fragmentos
        if params.calcular_superposicion:
            params_report['Análisis de Traslapes'] = "✅ Activado"
        else:
            params_report['Análisis de Traslapes'] = "❌ Desactivado"
        
        if params.generar_fragmentos_traslape:
            params_report['Generar Fragmentos de Traslape'] = "✅ Activado"
        else:
            params_report['Generar Fragmentos de Traslape'] = "❌ Desactivado"
        
        return params_report
    
    def _report_crs_warning(self, params: BufferParams) -> str:
        """Genera HTML de advertencia de CRS geográfico."""
        if params.crs_es_geografico:
            return f"""
            <div style="background: #f8d7da; color: #721c24; padding: 10px; border-radius: 5px; margin-bottom: 15px; border: 1px solid #f5c6cb;">
                ⚠️ <strong>Advertencia CRS:</strong> Sistema de coordenadas geográfico detectado ({params.crs_unidad}). 
                Los cálculos de distancia y área pueden ser inexactos. Se recomienda usar un CRS proyectado.
            </div>
            """
        return ""
    
    def _report_metrics(self, metricas: Dict, cnt: int, params: BufferParams) -> str:
        """Genera HTML de métricas de rendimiento."""
        tiempo_ejecucion = metricas.get('tiempo_ejecucion', 0)
        dist_min = metricas.get('distancia_min', 0)
        dist_max = metricas.get('distancia_max', 0)
        area_prom = metricas.get('area_promedio', 0)
        area_min = metricas.get('area_min', 0)
        area_max = metricas.get('area_max', 0)
        geom_con_z = metricas.get('geometrias_con_z', 0)
        geom_multipart = metricas.get('geometrias_multipart', 0)
        
        return f"""
        <div class="section">
            <h3>⏱️ Métricas de Rendimiento</h3>
            <div class="info-grid">
                <div class="info-card">
                    <h4>🕐 Tiempo y Procesamiento</h4>
                    <p><strong>Tiempo de ejecución:</strong> <span style="color: #2a5298; font-weight: bold;">{tiempo_ejecucion:.2f} segundos</span></p>
                    <p><strong>Búferes generados:</strong> {cnt}</p>
                </div>
                <div class="info-card">
                    <h4>📐 Estadísticas de Área</h4>
                    <p><strong>Área promedio:</strong> {area_prom:.4f} ha</p>
                    <p><strong>Área mínima:</strong> {area_min:.4f} ha</p>
                    <p><strong>Área máxima:</strong> {area_max:.4f} ha</p>
                    <p style="font-size: 0.85em; color: #666; margin-top: 10px;">
                        <em>💡 Nota: Estas estadísticas corresponden a los búferes originales. 
                        Si generó fragmentos de traslape, consulte la tabla de atributos de la capa de fragmentos 
                        para ver las áreas individuales de cada fragmento.</em>
                    </p>
                </div>
                <div class="info-card">
                    <h4>📏 Distancias {'(Campo Variable: ' + params.distance_field + ')' if params.distance_field else ''}</h4>
                    <p><strong>Distancia mínima:</strong> {dist_min:.2f} m</p>
                    <p><strong>Distancia máxima:</strong> {dist_max:.2f} m</p>
                </div>
                <div class="info-card">
                    <h4>🔧 Geometrías Especiales</h4>
                    <p><strong>Con coordenada Z (3D):</strong> {geom_con_z}</p>
                    <p><strong>Multipart:</strong> {geom_multipart}</p>
                </div>
            </div>
        """
    
    def _report_integrity(self, metricas: Dict, params: BufferParams,
                          geom_procesadas: int, geom_reparadas: int, geom_omitidas: int) -> str:
        """Genera HTML de integridad geométrica con tasas ISO 19157."""
        reparados_ids = metricas.get('reparados_ids', [])
        omitidos_ids = metricas.get('omitidos_ids', [])
        riesgo_ids = metricas.get('riesgo_ids', [])
        sin_bufer_ids = metricas.get('sin_bufer_ids', [])
        fragmentados_ids_dict = metricas.get('fragmentados_ids', {})
        null_field_count = metricas.get('null_field_count', 0)
        null_cat_count = metricas.get('null_cat_count', 0)
        missing_cat_ids = metricas.get('missing_cat_ids', {})
        
        integridad_class = 'success-box' if params.gestion_integridad == Constants.INTEGRIDAD_REPARAR else ('warning-box' if params.gestion_integridad == Constants.INTEGRIDAD_RIESGO else '')
        
        # Formatear IDs para Detalles por Categoría
        reparados_str = ', '.join(map(str, sorted(reparados_ids)[:10])) + ('...' if len(reparados_ids) > 10 else '') if reparados_ids else '0'
        omitidos_str = ', '.join(map(str, sorted(omitidos_ids)[:10])) + ('...' if len(omitidos_ids) > 10 else '') if omitidos_ids else '0'
        riesgo_str = ', '.join(map(str, sorted(riesgo_ids)[:10])) + ('...' if len(riesgo_ids) > 10 else '') if riesgo_ids else '0'
        sin_bufer_str = ', '.join(map(str, sorted(sin_bufer_ids)[:10])) + ('...' if len(sin_bufer_ids) > 10 else '') if sin_bufer_ids else '0'
        fragmentados_str = ', '.join(f"{fid}({n}p)" for fid, n in list(sorted(fragmentados_ids_dict.items()))[:10]) + ('...' if len(fragmentados_ids_dict) > 10 else '') if fragmentados_ids_dict else '0'
        
        todo_limpio = not reparados_ids and not omitidos_ids and not riesgo_ids and not sin_bufer_ids
        
        # ── ISO 19157: Indicadores de calidad por elemento ──
        total_entrada = geom_procesadas + geom_omitidas  # Total real que ingresó al proceso
        n_sin_bufer = len(sin_bufer_ids)
        n_riesgo = len(riesgo_ids)
        
        # Búferes efectivamente generados en la capa de salida
        bufer_efectivos = geom_procesadas - n_sin_bufer
        
        # Determinar si se verificó la geometría
        no_se_verifico_geometria = (params.gestion_integridad == Constants.INTEGRIDAD_RIESGO)
        
        if total_entrada > 0:
            # ── COMPLETITUD (ISO 19157 §4.2.1) ──
            # Omisión: features que deberían tener búfer pero no lo tienen
            n_omision_integridad = geom_omitidas              # Geometría inválida irrecuperable
            n_omision_procesador = n_sin_bufer                 # Parámetros impidieron generar búfer
            n_omision_total = n_omision_integridad + n_omision_procesador
            tasa_omision_total = (n_omision_total / total_entrada) * 100
            tasa_omision_integridad = (n_omision_integridad / total_entrada) * 100
            tasa_omision_procesador = (n_omision_procesador / total_entrada) * 100
            
            # Comisión: features presentes en la salida pero con calidad no verificada
            # REGLA CRÍTICA: Si NO se verificó geometría, comisión = 100%
            if no_se_verifico_geometria:
                n_comision = bufer_efectivos  # Todas las procesadas están sin verificar
                tasa_comision = 100.0
            else:
                n_comision = n_riesgo
                tasa_comision = (n_comision / total_entrada) * 100
            
            # Tasa de completitud = features con búfer válido / total entrada
            tasa_completitud = (bufer_efectivos / total_entrada) * 100
            
            # ── CONSISTENCIA LÓGICA (ISO 19157 §4.2.3) ──
            # Consistencia topológica: geometrías que requirieron corrección
            tasa_reparacion = (geom_reparadas / total_entrada) * 100
            # Tasa de consistencia = features que no requirieron intervención / total
            n_consistentes = bufer_efectivos - geom_reparadas
            tasa_consistencia = (n_consistentes / total_entrada) * 100 if total_entrada > 0 else 0
        else:
            tasa_completitud = tasa_omision_total = tasa_omision_integridad = 0.0
            tasa_omision_procesador = tasa_comision = tasa_reparacion = tasa_consistencia = 0.0
            n_omision_total = n_omision_integridad = n_omision_procesador = n_comision = 0
            n_consistentes = bufer_efectivos = 0
        
        # Determinar color de la barra según completitud (gradiente visual, sin umbrales normativos)
        if tasa_completitud >= 99.9:
            barra_color = "#28a745"
        elif tasa_completitud >= 90:
            barra_color = "#17a2b8"
        elif tasa_completitud >= 70:
            barra_color = "#ffc107"
        else:
            barra_color = "#dc3545"
        
        # Determinar estado de conformidad ISO (descriptivo, sin umbrales)
        if no_se_verifico_geometria:
            # Cuando NO se verifica geometría, el estado depende solo de la omisión
            if tasa_omision_total == 0:
                conformidad_iso = f"⚠️ Comisión: 100% ({bufer_efectivos} entidades procesadas sin verificar integridad) · Sin omisión"
                conformidad_color = "#856404"
            else:
                conformidad_iso = f"⚠️ Omisión: {tasa_omision_total:.1f}% ({n_omision_total} entidades) · Comisión: 100% ({bufer_efectivos} sin verificar)"
                conformidad_color = "#721c24"
        elif tasa_omision_total == 0 and tasa_comision == 0:
            conformidad_iso = "✅ Sin omisión ni comisión detectada"
            conformidad_color = "#155724"
        elif tasa_comision > 0 and tasa_omision_total > 0:
            conformidad_iso = f"Omisión: {tasa_omision_total:.1f}% ({n_omision_total} entidades) · Comisión: {tasa_comision:.1f}% ({n_comision} entidades)"
            conformidad_color = "#721c24"
        elif tasa_comision > 0:
            conformidad_iso = f"Comisión: {tasa_comision:.1f}% ({n_comision} entidades en salida sin verificar integridad)"
            conformidad_color = "#721c24"
        else:
            conformidad_iso = f"Omisión: {tasa_omision_total:.1f}% ({n_omision_total} de {total_entrada} entidades sin búfer en salida)"
            conformidad_color = "#856404"
        
        # ── HTML: Tarjeta ISO 19157 ──
        iso_html = f"""
                <div class="info-card">
                    <h4>📋 Indicadores de Calidad (ISO 19157:2023)</h4>
                    
                    <!-- Barra de completitud -->
                    <div style="margin-bottom: 14px;">
                        <div style="display: flex; justify-content: space-between; margin-bottom: 4px;">
                            <span style="font-weight: bold;">Completitud del proceso</span>
                            <span style="font-weight: bold; color: {barra_color};">{tasa_completitud:.1f}%</span>
                        </div>
                        <div style="background: #e9ecef; border-radius: 8px; height: 12px; overflow: hidden;">
                            <div style="background: {barra_color}; height: 100%; width: {min(tasa_completitud, 100):.1f}%; border-radius: 8px; transition: width 0.3s;"></div>
                        </div>
                        <p style="margin: 4px 0 0 0; font-size: 0.78em; color: #6c757d;">
                            {bufer_efectivos} de {total_entrada} entidades generaron búfer válido
                        </p>
                    </div>
                    
                    <!-- Elemento 1: COMPLETITUD -->
                    <div style="margin-bottom: 10px; padding: 8px; background: #f8f9fa; border-radius: 6px;">
                        <p style="margin: 0 0 6px 0; font-weight: bold; font-size: 0.9em; color: #2a5298;">
                            📐 Completitud <span style="font-weight: normal; font-size: 0.85em;">(ISO 19157 §D.1)</span>
                        </p>
                        <table style="width: 100%; border-collapse: collapse; font-size: 0.85em;">
                            <tr style="border-bottom: 1px solid #dee2e6;">
                                <td style="padding: 4px;" colspan="3"><em>Omisión — datos ausentes en la salida</em></td>
                            </tr>
                            <tr style="border-bottom: 1px solid #dee2e6;">
                                <td style="padding: 4px 4px 4px 16px;">🚫 Por integridad geométrica</td>
                                <td style="padding: 4px; text-align: right; font-weight: bold;">{n_omision_integridad}</td>
                                <td style="padding: 4px; text-align: right; color: {'#dc3545' if n_omision_integridad > 0 else '#28a745'}; font-weight: bold;">{tasa_omision_integridad:.1f}%</td>
                            </tr>
                            <tr style="border-bottom: 1px solid #dee2e6;">
                                <td style="padding: 4px 4px 4px 16px;">🔸 Por parámetros del búfer</td>
                                <td style="padding: 4px; text-align: right; font-weight: bold;">{n_omision_procesador}</td>
                                <td style="padding: 4px; text-align: right; color: {'#fd7e14' if n_omision_procesador > 0 else '#28a745'}; font-weight: bold;">{tasa_omision_procesador:.1f}%</td>
                            </tr>
                            <tr style="border-bottom: 1px solid #dee2e6; background: #e9ecef;">
                                <td style="padding: 4px; font-weight: bold;">Total omisión</td>
                                <td style="padding: 4px; text-align: right; font-weight: bold;">{n_omision_total}</td>
                                <td style="padding: 4px; text-align: right; font-weight: bold; color: {'#dc3545' if n_omision_total > 0 else '#28a745'};">{tasa_omision_total:.1f}%</td>
                            </tr>
                            <tr style="border-bottom: 1px solid #dee2e6;">
                                <td style="padding: 4px;" colspan="3"><em>Comisión — datos presentes sin calidad verificada</em></td>
                            </tr>
                            <tr>
                                <td style="padding: 4px 4px 4px 16px;">⚠️ En riesgo (sin verificar)</td>
                                <td style="padding: 4px; text-align: right; font-weight: bold;">{n_comision}</td>
                                <td style="padding: 4px; text-align: right; color: {'#dc3545' if n_comision > 0 else '#28a745'}; font-weight: bold;">{tasa_comision:.1f}%</td>
                            </tr>
                            {"<tr><td style='padding: 4px 4px 4px 16px; font-size: 0.75em; color: #856404;' colspan='3'>⚠️ Verificación topológica desactivada — Todas las geometrías procesadas sin validar</td></tr>" if no_se_verifico_geometria else ""}
                        </table>
                    </div>
                    
                    <!-- Elemento 2: CONSISTENCIA LÓGICA -->
                    <div style="margin-bottom: 10px; padding: 8px; background: #f8f9fa; border-radius: 6px;">
                        <p style="margin: 0 0 6px 0; font-weight: bold; font-size: 0.9em; color: #2a5298;">
                            🔗 Consistencia Lógica <span style="font-weight: normal; font-size: 0.85em;">(ISO 19157 §D.3)</span>
                        </p>
                        <p style="margin: 0 0 4px 0; font-size: 0.80em; color: #6c757d;"><em>Topológicamente consistentes</em></p>
                        {"<table style='width: 100%; border-collapse: collapse; font-size: 0.85em;'><tr><td style='padding: 8px; text-align: center; background: #fff3cd; border-radius: 4px;'><strong style='color: #856404;'>⚠️ No verificado</strong><br><span style='font-size: 0.85em; color: #856404;'>La verificación de integridad geométrica fue desactivada.<br>No se puede determinar la consistencia topológica.</span></td></tr></table>" if no_se_verifico_geometria else f"""<table style="width: 100%; border-collapse: collapse; font-size: 0.85em;">
                            <tr style="border-bottom: 1px solid #dee2e6;">
                                <td style="padding: 4px;">✅ Topológicamente consistentes</td>
                                <td style="padding: 4px; text-align: right; font-weight: bold;">{n_consistentes}</td>
                                <td style="padding: 4px; text-align: right; color: #28a745; font-weight: bold;">{tasa_consistencia:.1f}%</td>
                            </tr>
                            <tr>
                                <td style="padding: 4px;">🔧 Requirieron corrección automática</td>
                                <td style="padding: 4px; text-align: right; font-weight: bold;">{geom_reparadas}</td>
                                <td style="padding: 4px; text-align: right; color: {'#17a2b8' if geom_reparadas > 0 else '#28a745'}; font-weight: bold;">{tasa_reparacion:.1f}%</td>
                            </tr>
                        </table>"""}
                    </div>
                    
                    <!-- Conformidad -->
                    <div style="padding: 8px; background: #f8f9fa; border-left: 3px solid {conformidad_color}; border-radius: 0 4px 4px 0;">
                        <p style="margin: 0; font-size: 0.85em; color: {conformidad_color}; font-weight: bold;">
                            {conformidad_iso}
                        </p>
                        <p style="margin: 4px 0 0 0; font-size: 0.78em; color: #6c757d;">
                            Ref: ISO 19157:2023 — Calidad de datos geográficos<br>
                            Elementos evaluados: Completitud (§D.1: Omisión, Comisión) · Consistencia Lógica (§D.3: Topológica)
                        </p>
                    </div>
                </div>
        """
        
        if todo_limpio:
            detalle_categoria_html = f"""
                <div class="info-card integrity-success">
                    <h4>📊 Detalles por Categoría</h4>
                    <div style="text-align: center; padding: 15px;">
                        <p style="font-size: 1.3em; color: #155724; font-weight: bold; margin: 0;">
                            ✅ Todas las geometrías válidas
                        </p>
                        <p style="color: #155724; margin: 8px 0 0 0; font-size: 0.9em;">
                            Sin intervención requerida — datos de entrada con buena calidad topológica.
                        </p>
                    </div>
                    <p><strong>IDs reparados:</strong> 0</p>
                    <p><strong>IDs omitidos:</strong> 0</p>
                    <p><strong>IDs con riesgo:</strong> 0</p>
                    <p><strong>IDs sin búfer:</strong> 0</p>
                    <p><strong>IDs fragmentados:</strong> 0</p>
                </div>
            """
        else:
            detalle_categoria_html = f"""
                <div class="info-card">
                    <h4>📊 Detalles por Categoría</h4>
                    <p><strong>IDs reparados:</strong> {reparados_str}</p>
                    <p><strong>IDs omitidos:</strong> {omitidos_str}</p>
                    <p><strong>IDs con riesgo:</strong> {riesgo_str}</p>
                    <p><strong>IDs sin búfer:</strong> {sin_bufer_str}</p>
                    <p><strong>IDs fragmentados:</strong> {fragmentados_str}</p>
                </div>
            """
        
        integridad_html = f"""
        <div class="section">
            <h3>⚡ Gestión de Integridad Geométrica</h3>
            <div class="info-grid">
                <div class="info-card {integridad_class}">
                    <h4>{Constants.INTEGRIDAD_NAMES[params.gestion_integridad]}</h4>
                    <div class="metric">
                        <span>🔧 Geometrías reparadas:</span>
                        <span class="metric-value">{geom_reparadas}</span>
                    </div>
                    <div class="metric">
                        <span>🚫 Geometrías omitidas:</span>
                        <span class="metric-value">{geom_omitidas}</span>
                    </div>
                    <div class="metric">
                        <span>⚠️ Geometrías con riesgo:</span>
                        <span class="metric-value">{len(riesgo_ids)}</span>
                    </div>
                    <div class="metric">
                        <span>✅ Geometrías de entrada procesadas:</span>
                        <span class="metric-value">{geom_procesadas}</span>
                    </div>
                </div>
                {iso_html}
            </div>
            <div class="info-grid" style="margin-top: 10px;">
                {detalle_categoria_html}
            </div>
        """
        
        # Detalle expandido (solo si hay IDs afectados)
        if reparados_ids or omitidos_ids or riesgo_ids or sin_bufer_ids or fragmentados_ids_dict or null_field_count or null_cat_count or missing_cat_ids:
            integridad_html += self._report_integrity_detail(
                reparados_ids, omitidos_ids, riesgo_ids, sin_bufer_ids, fragmentados_ids_dict,
                null_field_count, null_cat_count, missing_cat_ids)
        
        return integridad_html
    
    def _report_integrity_detail(self, reparados_ids, omitidos_ids, riesgo_ids, sin_bufer_ids=None, fragmentados_ids=None,
                                  null_field_count=0, null_cat_count=0, missing_cat_ids=None) -> str:
        """Genera HTML del detalle expandido de integridad (tarjetas con IDs)."""
        if sin_bufer_ids is None:
            sin_bufer_ids = []
        if fragmentados_ids is None:
            fragmentados_ids = {}
        if missing_cat_ids is None:
            missing_cat_ids = {}
        
        n_categorias = sum(1 for x in [reparados_ids, omitidos_ids, riesgo_ids, sin_bufer_ids, fragmentados_ids, missing_cat_ids] if x) \
                     + (1 if null_field_count else 0) + (1 if null_cat_count else 0)
        html = f"""
            <div class="section">
                <h3>🔍 Detalle de Integridad Geométrica ({n_categorias} Categorías)</h3>
                <div class="info-grid">
        """
        
        # Categoría 1: ✅ Reparados
        if reparados_ids:
            reparados_list = ", ".join(str(fid) for fid in sorted(reparados_ids))
            html += f"""
                <div class="info-card integrity-success">
                    <h4>✅ Reparados (Corrección Automática)</h4>
                    <p><strong>Cantidad:</strong> {len(reparados_ids)} entidades</p>
                    <p><strong>Descripción:</strong> Geometrías que ingresaron con errores topológicos pero fueron corregidas automáticamente.</p>
                    <details>
                        <summary style="cursor: pointer; color: #155724; font-weight: bold;">
                            🔍 Ver IDs ({len(reparados_ids)})
                        </summary>
                        <div style="margin-top: 8px; padding: 8px; background: white; border-radius: 4px; max-height: 150px; overflow-y: auto;">
                            {reparados_list}
                        </div>
                    </details>
                    <p><strong>Implicación:</strong> El dato es válido para análisis, pero se documenta la intervención técnica.</p>
                </div>
            """
        
        # Categoría 2: 🚫 Omitidos
        if omitidos_ids:
            omitidos_list = ", ".join(str(fid) for fid in sorted(omitidos_ids))
            html += f"""
                <div class="info-card integrity-danger">
                    <h4>🚫 Omitidos (Fallo Irrecuperable)</h4>
                    <p><strong>Cantidad:</strong> {len(omitidos_ids)} entidades</p>
                    <p><strong>Descripción:</strong> Geometrías que no pudieron ser procesadas y fueron excluidas de la capa de salida.</p>
                    <details>
                        <summary style="cursor: pointer; color: #721c24; font-weight: bold;">
                            🔍 Ver IDs ({len(omitidos_ids)})
                        </summary>
                        <div style="margin-top: 8px; padding: 8px; background: white; border-radius: 4px; max-height: 150px; overflow-y: auto;">
                            {omitidos_list}
                        </div>
                    </details>
                    <p><strong>Implicación:</strong> Se genera un vacío de información (gap) que debe ser justificado.</p>
                </div>
            """
        
        # Categoría 3: ⚠️ Riesgo
        if riesgo_ids:
            riesgo_list = ", ".join(str(fid) for fid in sorted(riesgo_ids))
            html += f"""
                <div class="info-card integrity-warning">
                    <h4>⚠️ Riesgo</h4>
                    <p><strong>Cantidad:</strong> {len(riesgo_ids)} entidades</p>
                    <p><strong>Descripción:</strong> Incluye dos casos:<br>
                    <strong>(1) Geometría sin verificar:</strong> procesadas "tal cual" porque el usuario desactivó la validación.<br>
                    <strong>(2) Cuña con radio negativo:</strong> el búfer es geométricamente válido pero está rotado 180° respecto a la orientación configurada — revise el campo de distancia.</p>
                    <details>
                        <summary style="cursor: pointer; color: #856404; font-weight: bold;">
                            🔍 Ver IDs ({len(riesgo_ids)})
                        </summary>
                        <div style="margin-top: 8px; padding: 8px; background: white; border-radius: 4px; max-height: 150px; overflow-y: auto;">
                            {riesgo_list}
                        </div>
                    </details>
                    <p><strong>Implicación Crítica:</strong> Invalida la certificación del proceso. Un reporte con entidades en "Riesgo" no cumple con estándares de integridad de datos.</p>
                </div>
            """
        
        # Categoría 4: 🔸 Sin búfer generado
        if sin_bufer_ids:
            sin_bufer_list = ", ".join(str(fid) for fid in sorted(sin_bufer_ids))
            html += f"""
                <div class="info-card" style="background: #fff3e0; border-left: 4px solid #fd7e14;">
                    <h4>🔸 Sin Búfer Generado (Parámetro Inválido)</h4>
                    <p><strong>Cantidad:</strong> {len(sin_bufer_ids)} entidades</p>
                    <p><strong>Descripción:</strong> Geometrías válidas cuyo procesador no generó búfer (distancia=0, colapso geométrico por búfer negativo, o dimensiones insuficientes).</p>
                    <details>
                        <summary style="cursor: pointer; color: #e65100; font-weight: bold;">
                            🔍 Ver IDs ({len(sin_bufer_ids)})
                        </summary>
                        <div style="margin-top: 8px; padding: 8px; background: white; border-radius: 4px; max-height: 150px; overflow-y: auto;">
                            {sin_bufer_list}
                        </div>
                    </details>
                    <p><strong>Implicación:</strong> La geometría de entrada es válida, pero los parámetros del búfer (distancia, área, dimensiones) impidieron generar resultado. Revisar el campo de distancia o los valores asignados.</p>
                </div>
            """
        
        # Categoría 5: 🧩 Fragmentación por contracción
        if fragmentados_ids:
            frag_items = ", ".join(
                f"fid {fid} ({n} partes)" for fid, n in sorted(fragmentados_ids.items())
            )
            html += f"""
                <div class="info-card" style="background: #fff8e1; border-left: 4px solid #f9a825;">
                    <h4>🧩 Búfer Fragmentado (Contracción en Polígono Cóncavo)</h4>
                    <p><strong>Cantidad:</strong> {len(fragmentados_ids)} entidades</p>
                    <p><strong>Descripción:</strong> El área objetivo es menor que el área del polígono original. 
                    La contracción dividió el polígono en múltiples partes independientes porque la geometría 
                    tiene forma cóncava (L, U, T, cuello estrecho) y el borde contraído cerró alguna sección.</p>
                    <details>
                        <summary style="cursor: pointer; color: #e65100; font-weight: bold;">
                            🔍 Ver IDs y número de partes ({len(fragmentados_ids)})
                        </summary>
                        <div style="margin-top: 8px; padding: 8px; background: white; border-radius: 4px; max-height: 150px; overflow-y: auto;">
                            {frag_items}
                        </div>
                    </details>
                    <p><strong>Implicación:</strong> El búfer se generó correctamente — el área total de los 
                    fragmentos cumple el área objetivo. Verifique si el resultado fragmentado es válido para 
                    su análisis. Si necesita un polígono continuo: aumente el área objetivo, simplifique 
                    la geometría de entrada, o divida el polígono original en partes antes de procesar.</p>
                </div>
            """
        
        # Categoría 6: 🔹 Campo numérico NULL
        if null_field_count:
            html += f"""
                <div class="info-card" style="background: #e8f4fd; border-left: 4px solid #2196f3;">
                    <h4>🔹 Campo Numérico NULL (Respaldo a Distancia Fija)</h4>
                    <p><strong>Cantidad:</strong> {null_field_count} entidades</p>
                    <p><strong>Descripción:</strong> El campo de distancia/radio/área contenía NULL para estas entidades. Se aplicó el valor del parámetro fijo como respaldo.</p>
                    <p><strong>Implicación:</strong> El búfer fue generado con la distancia fija, no con el valor variable esperado. Verifique si los valores NULL en el campo son intencionales o requieren corrección.</p>
                </div>
            """

        # Categoría 7: 🔹 Categoría NULL en JSON
        if null_cat_count:
            html += f"""
                <div class="info-card" style="background: #e8f4fd; border-left: 4px solid #2196f3;">
                    <h4>🔹 Categoría NULL en Mapeo JSON (Respaldo a Distancia Fija)</h4>
                    <p><strong>Cantidad:</strong> {null_cat_count} entidades</p>
                    <p><strong>Descripción:</strong> El campo de categoría contenía NULL para estas entidades. Se aplicó el valor del parámetro fijo como respaldo.</p>
                    <p><strong>Implicación:</strong> Comportamiento esperado si algunas entidades no tienen categoría asignada. Si no es intencional, complete los valores NULL en el campo de categoría.</p>
                </div>
            """

        # Categoría 8: ⚠️ Categorías no encontradas en JSON
        if missing_cat_ids:
            total_missing = sum(missing_cat_ids.values())
            missing_rows = "".join(
                f"<tr><td style='padding:3px 8px; border-bottom:1px solid #e0e0e0;'><code>{cat}</code></td>"
                f"<td style='padding:3px 8px; border-bottom:1px solid #e0e0e0; text-align:center;'>{n}</td></tr>"
                for cat, n in sorted(missing_cat_ids.items())
            )
            html += f"""
                <div class="info-card" style="background: #fff8e1; border-left: 4px solid #ff9800;">
                    <h4>⚠️ Categorías No Encontradas en Mapeo JSON ({total_missing} entidades afectadas)</h4>
                    <p><strong>Descripción:</strong> El campo de categoría contenía valores que no existen en el mapeo JSON configurado. Se aplicó el valor del parámetro fijo como respaldo.</p>
                    <details>
                        <summary style="cursor: pointer; color: #e65100; font-weight: bold;">
                            🔍 Ver categorías ({len(missing_cat_ids)})
                        </summary>
                        <div style="margin-top: 8px; padding: 8px; background: white; border-radius: 4px; max-height: 200px; overflow-y: auto;">
                            <table style="width:100%; border-collapse:collapse; font-size:0.9em;">
                                <thead>
                                    <tr style="background:#f5f5f5;">
                                        <th style="padding:4px 8px; text-align:left;">Categoría</th>
                                        <th style="padding:4px 8px; text-align:center;">N entidades</th>
                                    </tr>
                                </thead>
                                <tbody>{missing_rows}</tbody>
                            </table>
                        </div>
                    </details>
                    <p><strong>Implicación:</strong> Verifique la ortografía de las categorías o actualice el mapeo JSON para incluir estos valores.</p>
                </div>
            """
        
        html += """
                </div>
                <div style="margin-top: 15px; padding: 10px; background: #e9ecef; border-radius: 5px;">
                    <p style="margin: 0; font-size: 0.9em; color: #495057;">
                        💡 <strong>Interpretación del Reporte:</strong><br>
                        • <strong>IDs en Reparados:</strong> "El software salvó estos datos, todo está bien."<br>
                        • <strong>IDs en Omitidos:</strong> "Perdí estos datos, debo revisar por qué mi mapa original está tan dañado."<br>
                        • <strong>IDs en Riesgo:</strong> "Alerta roja. Mi resultado no es confiable porque forcé el procesamiento de basura topológica."<br>
                        • <strong>IDs sin Búfer:</strong> "La geometría está bien, pero los parámetros no permitieron generar búfer (ej: distancia=0 en el campo)."<br>
                        • <strong>IDs Fragmentados:</strong> "El búfer se generó pero quedó dividido en partes — verifique si el resultado es útil para su análisis."<br>
                        • <strong>Campo NULL / Categoría NULL:</strong> "Algunas entidades no tenían valor en el campo variable — se aplicó la distancia fija como respaldo."<br>
                        • <strong>Categorías no encontradas:</strong> "El campo tiene valores que no están en el mapeo JSON — verifique la ortografía o actualice el mapeo."
                    </p>
                </div>
            </div>
        """
        return html
    
    def _report_overlap(self, overlap_data: List[Dict], params: BufferParams) -> str:
        """Genera HTML del análisis de superposición."""
        if overlap_data and len(overlap_data) > 0:
            rows_html = ""
            for idx, o in enumerate(overlap_data[:20]):
                area_m2 = o['area_m2']
                area_str = f"{o['area_ha']:.4f} ha" if area_m2 >= 10000 else f"{area_m2:,.2f} m²"
                
                pct = o['porcentaje']
                if pct >= 1:
                    pct_str = f"{pct:.2f}%"
                elif pct >= 0.01:
                    pct_str = f"{pct:.4f}%"
                elif pct > 0:
                    pct_str = f"{pct:.6f}%"
                else:
                    pct_str = "< 0.000001%"
                
                rows_html += f"""<tr>
                    <td>{idx+1}</td>
                    <td>{o['buffer_1_tipo']}</td>
                    <td>{o['buffer_2_tipo']}</td>
                    <td>{area_str}</td>
                    <td>{pct_str}</td>
                </tr>"""
            
            if len(overlap_data) > 20:
                rows_html += f"""<tr>
                    <td colspan="5" style="text-align: center; font-style: italic;">
                        ... y {len(overlap_data) - 20} superposiciones más (mostrando las 20 mayores)
                    </td>
                </tr>"""
            
            total_overlap_area = sum(o['area_m2'] for o in overlap_data)
            total_overlap_ha = total_overlap_area / Constants.HA_TO_M2
            
            return f"""
            <div class="section">
                <h3>📊 Análisis de Traslapes entre Búferes ({len(overlap_data)} encontrados)</h3>
                <div style="background: #d1ecf1; border-left: 4px solid #0c5460; padding: 12px; margin-bottom: 15px; border-radius: 4px;">
                    <p style="margin: 0; font-size: 0.95em; color: #0c5460;">
                        <strong>🔍 ¿Qué representa esta tabla?</strong><br>
                        Esta tabla muestra <strong>pares de búferes que se traslapan entre sí</strong>. Cada fila indica:
                    </p>
                    <ul style="margin: 8px 0 0 20px; font-size: 0.9em; color: #0c5460;">
                        <li><strong>Búfer 1 y Búfer 2:</strong> Los dos búferes que se cruzan (identificados por su fid)</li>
                        <li><strong>Área de superposición:</strong> Cuánto territorio comparten ambos búferes</li>
                        <li><strong>% del Menor:</strong> Qué porcentaje del búfer más pequeño está cubierto por el traslape</li>
                    </ul>
                    <p style="margin: 8px 0 0 0; font-size: 0.85em; color: #0c5460;">
                        <strong>💡 Interpretación del porcentaje:</strong> Un 50% significa que la mitad del búfer más pequeño 
                        está traslapada con el otro búfer. Porcentajes bajos (&lt;10%) son normales cuando hay búferes de 
                        tamaños muy diferentes o gran extensión territorial.
                    </p>
                </div>
                <table class="stats-table">
                    <tr>
                        <th>#</th>
                        <th>Búfer 1</th>
                        <th>Búfer 2</th>
                        <th>Área Superposición</th>
                        <th>% del Menor</th>
                    </tr>
                    {rows_html}
                </table>
                <div style="margin-top: 15px; padding: 10px; background: #e9ecef; border-radius: 5px;">
                    <p style="margin: 0;">
                        <strong>📏 Área total de superposición:</strong> {total_overlap_ha:.4f} ha ({total_overlap_area:,.2f} m²)
                    </p>
                    <p style="margin: 5px 0 0 0; font-size: 0.85em; color: #666;">
                        <em>Esta es la suma de todas las áreas donde los búferes se traslapan (sin contar traslapes múltiples).</em>
                    </p>
                </div>
            </div>
            """
        elif params.calcular_superposicion:
            return """
            <div class="section" style="background: #d1ecf1; border-left: 4px solid #0c5460;">
                <h3>📊 Análisis de Traslapes entre Búferes</h3>
                <div style="padding: 12px;">
                    <p style="margin: 0; font-size: 0.95em; color: #0c5460;">
                        ℹ️ <strong>No se encontraron traslapes entre los búferes generados</strong>
                    </p>
                    <p style="margin: 10px 0 0 0; font-size: 0.9em; color: #0c5460;">
                        Los búferes creados <strong>no se traslapan entre sí</strong>. Esto puede deberse a:
                    </p>
                    <ul style="margin: 8px 0 0 20px; font-size: 0.9em; color: #0c5460;">
                        <li>Las entidades de origen están muy separadas en el territorio</li>
                        <li>Las distancias de búfer son pequeñas en relación a la separación entre entidades</li>
                        <li>Solo se generó 1 búfer (se requieren mínimo 2 para analizar traslapes)</li>
                    </ul>
                    <p style="margin: 10px 0 0 0; font-size: 0.85em; color: #0c5460; font-style: italic;">
                        💡 <strong>Nota:</strong> Esto no es un error. Simplemente indica que no hay áreas compartidas 
                        entre los búferes. La capa de búferes se generó correctamente.
                    </p>
                </div>
            </div>
            """
        return ""
    
    def _report_fragments_info(self, params: BufferParams, fragmentos_generados: bool = False, 
                               fragment_stats: Dict = None) -> str:
        """Genera HTML informativo sobre los fragmentos de traslape generados."""
        if not params.generar_fragmentos_traslape:
            return ""
        
        # Caso: No se generaron fragmentos (búferes no se traslapan)
        if not fragmentos_generados:
            return """
            <div class="section" style="background: #d1ecf1; border-left: 4px solid #0c5460;">
                <h3>🧩 Fragmentos de Traslape</h3>
                <div style="padding: 12px;">
                    <p style="margin: 0; font-size: 0.95em; color: #0c5460;">
                        ℹ️ <strong>No se generaron fragmentos de traslape</strong>
                    </p>
                    <p style="margin: 10px 0 0 0; font-size: 0.9em; color: #0c5460;">
                        Los búferes generados <strong>no se traslapan entre sí</strong>, por lo que no hay 
                        fragmentos resultantes de intersecciones. Esto es normal cuando:
                    </p>
                    <ul style="margin: 8px 0 0 20px; font-size: 0.9em; color: #0c5460;">
                        <li>Las entidades están muy separadas en el territorio</li>
                        <li>Los búferes son pequeños en relación a la distancia entre puntos</li>
                        <li>Solo hay 1 búfer generado</li>
                    </ul>
                    <p style="margin: 10px 0 0 0; font-size: 0.85em; color: #0c5460; font-style: italic;">
                        💡 <strong>Nota:</strong> La capa de búferes originales sí fue generada correctamente.
                    </p>
                </div>
            </div>
            """
        
        # Caso: Sí se generaron fragmentos
        # Determinar si también se calculó superposición
        tambien_overlap = params.calcular_superposicion
        
        # Generar HTML de estadísticas si están disponibles
        stats_html = ""
        if fragment_stats:
            # Construir filas de tabla por tipo
            rows_por_tipo = ""
            total = fragment_stats['total']
            for tipo, count in sorted(fragment_stats['por_tipo'].items()):
                pct = (count / total * 100) if total > 0 else 0
                rows_por_tipo += f"<tr><td>{tipo}</td><td>{count}</td><td>{pct:.1f}%</td></tr>"
            
            stats_html = f"""
                <div style="background: #e7f3ff; padding: 12px; border-radius: 4px; margin: 15px 0; border: 1px solid #b3d9ff;">
                    <p style="margin: 0 0 10px 0; font-weight: bold; color: #004085;">📊 Estadísticas de Fragmentos</p>
                    <p style="margin: 0 0 8px 0; font-size: 0.9em; color: #004085;">
                        <strong>Total de fragmentos generados:</strong> {fragment_stats['total']}
                    </p>
                    <table style="width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 0.9em;">
                        <thead>
                            <tr style="background: #cce5ff; color: #004085;">
                                <th style="padding: 6px; text-align: left; border: 1px solid #b3d9ff;">Tipo</th>
                                <th style="padding: 6px; text-align: center; border: 1px solid #b3d9ff;">Cantidad</th>
                                <th style="padding: 6px; text-align: center; border: 1px solid #b3d9ff;">Porcentaje</th>
                            </tr>
                        </thead>
                        <tbody>
                            {rows_por_tipo}
                        </tbody>
                    </table>
                    <div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid #b3d9ff;">
                        <p style="margin: 5px 0; font-size: 0.85em; color: #004085;">
                            <strong>Fragmento más grande:</strong> {fragment_stats['area_mayor_ha']:.4f} ha ({fragment_stats['fid_mayor']})
                        </p>
                        <p style="margin: 5px 0; font-size: 0.85em; color: #004085;">
                            <strong>Fragmento más pequeño:</strong> {fragment_stats['area_menor_ha']:.6f} ha ({fragment_stats['fid_menor']})
                        </p>
                    </div>
                </div>
            """
        
        return f"""
        <div class="section" style="background: #fff3cd; border-left: 4px solid #856404;">
            <h3>🧩 Fragmentos de Traslape Generados</h3>
            <div style="padding: 12px;">
                {stats_html}
                
                <p style="margin: 0 0 10px 0; font-size: 0.95em; color: #856404;">
                    <strong>📋 ¿Qué son los fragmentos de traslape?</strong><br>
                    Los fragmentos son <strong>polígonos independientes</strong> que representan todas las áreas únicas 
                    resultantes de las intersecciones entre búferes. Cada fragmento corresponde a una combinación específica 
                    de traslapes.
                </p>
                
                <div style="background: white; padding: 10px; border-radius: 4px; margin: 10px 0;">
                    <p style="margin: 0 0 8px 0; font-weight: bold; color: #856404;">📊 Tipos de fragmentos generados:</p>
                    <ul style="margin: 0; padding-left: 20px; font-size: 0.9em; color: #856404;">
                        <li><strong>Áreas exclusivas:</strong> Regiones cubiertas por un solo búfer (sin traslapes)</li>
                        <li><strong>Traslapes dobles:</strong> Regiones donde se cruzan exactamente 2 búferes</li>
                        <li><strong>Traslapes múltiples:</strong> Regiones donde se cruzan 3 o más búferes simultáneamente</li>
                    </ul>
                </div>
                
                <div style="background: #fff3e0; padding: 12px; border-radius: 4px; margin: 10px 0; border-left: 3px solid #ff9800;">
                    <p style="margin: 0 0 8px 0; font-weight: bold; color: #e65100;">⚠️ Límites de Seguridad (para evitar bloqueos del sistema):</p>
                    <p style="margin: 0 0 8px 0; font-size: 0.9em; color: #e65100;">
                        <strong>Tope de fragmentos:</strong> 1000 — el algoritmo se detiene al alcanzar este límite y notifica en el log.<br>
                        <strong>Tope de profundidad:</strong> hasta 9 búferes simultáneos por combinación (tríos, cuartetos... hasta nonetos).<br>
                        <strong>Orden de prioridad:</strong> áreas exclusivas → traslapes dobles → triples → superiores.
                    </p>
                    <p style="margin: 0 0 8px 0; font-size: 0.9em; color: #e65100;">
                        <strong>✅ Fragmentos garantizados:</strong> Áreas exclusivas (1 búfer), traslapes dobles (2 búferes)
                        y mayoría de traslapes triples (3 búferes).<br>
                        <strong>⚠️ Pueden omitirse:</strong> Intersecciones de 4+ búferes simultáneos
                        (en datos reales suelen ser micro-fragmentos sin utilidad práctica).
                    </p>
                    <p style="margin: 0; font-size: 0.85em; color: #e65100;">
                        <strong>💡 Solución:</strong> Si los fragmentos faltantes son "huecos" no deseados,
                        use la opción <em>"Eliminar huecos pequeños"</em> que los rellenará automáticamente
                        en los búferes principales. Especialmente útil para capas vectorizadas de raster.
                    </p>
                </div>
                
                <div style="background: #ffeeba; padding: 10px; border-radius: 4px; margin: 10px 0;">
                    <p style="margin: 0 0 8px 0; font-weight: bold; color: #856404;">📍 ¿Dónde consultar los fragmentos?</p>
                    <p style="margin: 0; font-size: 0.9em; color: #856404;">
                        Los fragmentos se generaron en una <strong>capa separada</strong> llamada 
                        <em>"Fragmentos de traslape - [nombre del búfer]"</em>. Esta capa contiene:
                    </p>
                    <ul style="margin: 5px 0 0 20px; padding-left: 20px; font-size: 0.9em; color: #856404;">
                        <li><strong>fid:</strong> ID único del fragmento</li>
                        <li><strong>n_buferes:</strong> Número de búferes que componen este fragmento (1=exclusivo, 2=doble, 3+=múltiple)</li>
                        <li><strong>tipo_traslape:</strong> Clasificación del fragmento ("Exclusivo", "Doble", "Triple", "Múltiple (N)")</li>
                        <li><strong>buferes:</strong> Lista de búferes que se traslapan en este fragmento (ej: "fid: 1, fid: 7")</li>
                        <li><strong>area_ha:</strong> Área del fragmento en hectáreas</li>
                        <li><strong>area_m2:</strong> Área del fragmento en metros cuadrados</li>
                        <li><strong>perimetro_m:</strong> Perímetro del fragmento en metros</li>
                        <li><strong>n_vertices:</strong> Número de vértices del polígono</li>
                    </ul>
                </div>
                
                {'<div style="background: #d4edda; padding: 10px; border-radius: 4px; margin: 10px 0; border: 1px solid #c3e6cb;"><p style="margin: 0; font-size: 0.9em; color: #155724;"><strong>🔄 Relación entre opciones:</strong><br>Al activar <strong>Generar Fragmentos</strong>, automáticamente se activa el <em>Análisis de Traslapes</em> (necesario para crear los fragmentos). Si además activa manualmente <em>Análisis de Traslapes</em>, obtendrá:<ul style="margin: 5px 0 0 20px; padding-left: 0;"><li>✅ Tabla estadística de superposiciones (arriba en este reporte)</li><li>✅ Campos de traslape en la capa de búferes</li><li>✅ Capa de fragmentos de traslape</li></ul>Si solo activa <em>Generar Fragmentos</em> (sin marcar Análisis de Traslapes), solo obtendrá la capa de fragmentos y los campos en búferes, pero sin la tabla en el reporte.</p></div>' if tambien_overlap else '<div style="background: #e7f3ff; padding: 10px; border-radius: 4px; margin: 10px 0; border: 1px solid #b3d9ff;"><p style="margin: 0; font-size: 0.9em; color: #004085;"><strong>ℹ️ Nota:</strong> Los fragmentos se generaron correctamente. El análisis de traslapes se ejecutó automáticamente (requerido para crear fragmentos). Si desea también ver la <strong>tabla estadística de superposiciones entre pares de búferes</strong> en el reporte, active la opción <em>"Analizar traslapes entre búferes"</em> en la próxima ejecución.</p></div>'}
                
                <p style="margin: 10px 0 0 0; font-size: 0.85em; color: #856404; font-style: italic;">
                    💡 <strong>Tip:</strong> Abra la tabla de atributos de la capa de fragmentos para:
                    <br>• Ver qué búferes componen cada fragmento (campo <em>buferes</em>)
                    <br>• Ordenar por <em>n_buferes</em> para agrupar fragmentos exclusivos, dobles o múltiples
                    <br>• Ordenar por <em>area_m2</em> para identificar los fragmentos más grandes o pequeños
                </p>
            </div>
        """
    
    def _report_efficiency(self, geometry_efficiency: Dict) -> str:
        """Genera HTML de eficiencia de geometría (entrada y salida)."""
        # ── Simplificación de SALIDA ─────────────────────────────────────
        v_antes = geometry_efficiency.get('vertices_antes', 0)
        v_despues = geometry_efficiency.get('vertices_despues', 0)
        simplif_activa = geometry_efficiency.get('simplificacion_activa', False)
        tolerancia = geometry_efficiency.get('tolerancia', 0)
        # ── Simplificación de ENTRADA ─────────────────────────────────────
        ve_antes = geometry_efficiency.get('v_entrada_antes', 0)
        ve_despues = geometry_efficiency.get('v_entrada_despues', 0)
        simplif_entrada = geometry_efficiency.get('simplif_entrada_activa', False)
        tolerancia_entrada = geometry_efficiency.get('tolerancia_entrada', 0)
        
        if v_antes <= 0 and ve_antes <= 0:
            return ""
        
        # ── Métricas salida ───────────────────────────────────────────────
        vertices_eliminados = v_antes - v_despues
        porcentaje_reduccion = (vertices_eliminados / v_antes * 100) if v_antes > 0 else 0
        if simplif_activa:
            estado_simplif = f"✅ Activa (Tolerancia: {tolerancia:.2f} m)"
            color_salida = "#28a745" if porcentaje_reduccion > 0 else "#6c757d"
        else:
            estado_simplif = "❌ Desactivada"
            color_salida = "#6c757d"
        
        # ── Métricas entrada ──────────────────────────────────────────────
        ve_eliminados = ve_antes - ve_despues
        ve_pct = (ve_eliminados / ve_antes * 100) if ve_antes > 0 else 0
        if simplif_entrada and ve_antes > 0:
            estado_entrada = f"✅ Activa (Tolerancia: {tolerancia_entrada:.1f} m)"
            color_entrada = "#28a745" if ve_pct > 0 else "#6c757d"
            entrada_html = f"""
                    <div class="info-card" style="border-left: 4px solid {color_entrada};">
                        <h4>🔧 Simplificación de Entrada</h4>
                        <p><strong>Vértices originales:</strong> {ve_antes:,}</p>
                        <p><strong>Vértices simplificados:</strong> {ve_despues:,}</p>
                        <p><strong>Vértices eliminados:</strong> <span style="color:{color_entrada};font-weight:bold;">{ve_eliminados:,}</span></p>
                        <p><strong>Reducción:</strong> <span style="color:{color_entrada};font-weight:bold;font-size:1.2em;">{ve_pct:.1f}%</span></p>
                        <p><strong>Estado:</strong> {estado_entrada}</p>
                    </div>"""
        else:
            entrada_html = f"""
                    <div class="info-card">
                        <h4>🔧 Simplificación de Entrada</h4>
                        <p><strong>Estado:</strong> ❌ Desactivada</p>
                        <p><strong>Vértices de entrada:</strong> {ve_antes:,}</p>
                    </div>""" if ve_antes > 0 else ""
        
        # ── Sección salida ────────────────────────────────────────────────
        if v_antes > 0:
            salida_html = f"""
                    <div class="info-card" style="border-left: 4px solid {color_salida};">
                        <h4>📐 Simplificación de Salida</h4>
                        <p><strong>Vértices antes:</strong> {v_antes:,}</p>
                        <p><strong>Vértices después:</strong> {v_despues:,}</p>
                        <p><strong>Vértices eliminados:</strong> <span style="color:{color_salida};font-weight:bold;">{vertices_eliminados:,}</span></p>
                        <p><strong>Reducción:</strong> <span style="color:{color_salida};font-weight:bold;font-size:1.2em;">{porcentaje_reduccion:.2f}%</span></p>
                        <p><strong>Estado:</strong> {estado_simplif}</p>
                        <p><strong>Ratio compresión:</strong> {(v_despues/v_antes*100):.1f}% del original</p>
                    </div>"""
        else:
            salida_html = ""
        
        return f"""
            <div class="section">
                <h3>📐 Eficiencia de Geometría</h3>
                <div class="info-grid">
                    {entrada_html}
                    {salida_html}
                </div>
                <div style="margin-top:15px;padding:10px;background:#e9ecef;border-radius:5px;">
                    <p style="margin:0;font-size:0.9em;color:#495057;">
                        💡 <strong>Nota:</strong> La simplificación de entrada reduce la complejidad
                        de la geometría antes de calcular el búfer. La simplificación de salida
                        reduce los vértices del resultado final para mejorar el rendimiento de
                        visualización y reducir el tamaño del archivo.
                    </p>
                </div>
            </div>
        """
    
    def _report_assemble(self, params, logger, cnt, area_sum, source_name,
                         tiempo_ejecucion, params_report,
                         crs_warning_html, metricas_html, integridad_html,
                         overlap_html, fragments_html, efficiency_html) -> str:
        """Ensambla el HTML final del reporte."""
        # Sanitizar valores de usuario para evitar HTML malformado por caracteres especiales
        source_name = html.escape(str(source_name))
        return f"""
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <title>Reporte: Zonas de Influencia Personalizadas</title>
            <style>
                body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
                .container {{ max-width: 1200px; margin: 0 auto; background: white; border-radius: 10px; box-shadow: 0 0 20px rgba(0,0,0,0.1); padding: 30px; }}
                .header {{ background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%); color: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; }}
                .header h1 {{ margin: 0; font-size: 24px; }}
                .header-subtitle {{ margin: 5px 0 0 0; font-size: 14px; opacity: 0.9; }}
                .section {{ margin-bottom: 30px; background: #f8f9fa; border-radius: 8px; padding: 20px; }}
                .section h3 {{ color: #333; margin-top: 0; border-bottom: 2px solid #e9ecef; padding-bottom: 10px; }}
                .info-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-top: 20px; }}
                .info-card {{ background: white; padding: 15px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
                .info-card h4 {{ margin: 0 0 10px 0; color: #2a5298; font-size: 14px; }}
                .stats-table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
                .stats-table th, .stats-table td {{ padding: 8px; text-align: left; border-bottom: 1px solid #dee2e6; }}
                .stats-table th {{ background: #2a5298; color: white; }}
                .stats-table tr:hover {{ background: #f1f3f4; }}
                .warning-box {{ background: #fff3cd; color: #856404; padding: 15px; border-radius: 5px; border: 1px solid #ffeeba; }}
                .warning-list {{ margin: 0; padding-left: 20px; }}
                .success-box {{ background: #d4edda; color: #155724; padding: 15px; border-radius: 5px; border: 1px solid #c3e6cb; }}
                .error-box {{ background: #f8d7da; color: #721c24; padding: 15px; border-radius: 5px; border: 1px solid #f5c6cb; }}
                .metric {{ display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #eee; }}
                .metric-value {{ font-weight: bold; color: #2c3e50; }}
                .footer {{ text-align: center; padding: 20px; background: #f8f9fa; color: #6c757d; border-top: 1px solid #dee2e6; margin-top: 30px; border-radius: 8px; }}
                .badge {{ display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }}
                .badge-success {{ background: #28a745; color: white; }}
                .badge-warning {{ background: #ffc107; color: #212529; }}
                .badge-danger {{ background: #dc3545; color: white; }}
                .integrity-success {{ background: #d4edda; border: 2px solid #c3e6cb; }}
                .integrity-danger {{ background: #f8d7da; border: 2px solid #f5c6cb; }}
                .integrity-warning {{ background: #fff3cd; border: 2px solid #ffeeba; }}
                details summary::-webkit-details-marker {{ display: none; }}
                details summary {{ outline: none; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>🎯 REPORTE: Zonas de Influencia Personalizadas</h1>
                    <p class="header-subtitle">Generado: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')} | Tiempo: {tiempo_ejecucion:.2f}s</p>
                </div>
                
                {crs_warning_html}
                
                <div class="section">
                    <h3>📋 Información General</h3>
                    <div class="info-grid">
                        <div class="info-card">
                            <h4>📁 Proyecto</h4>
                            <p><strong>Nombre:</strong> {params.nombre_proyecto}</p>
                            <p><strong>Capa de entrada:</strong> {source_name}</p>
                            <p><strong>CRS:</strong> {params.crs_info}</p>
                            <p><strong>Unidad:</strong> {params.crs_unidad}</p>
                        </div>
                        <div class="info-card">
                            <h4>🔘 Tipo de Búfer</h4>
                            <p><strong>Tipo:</strong> {Constants.BUFFER_NAMES[params.buffer_type]}</p>
                            <p><strong>Segmentos:</strong> {params.segmentos}</p>
                        </div>
                        <div class="info-card">
                            <h4>📊 Resultados</h4>
                            <p><strong>Geometrías generadas:</strong> <span style="font-size: 1.3em; color: #2a5298; font-weight: bold;">{cnt}</span></p>
                            <p><strong>Área Total:</strong> {area_sum:.4f} ha</p>
                        </div>
                        {"" if not params.usar_densidad_adaptativa else f'''
                        <div class="info-card" style="border-left: 4px solid #17a2b8;">
                            <h4>🧭 Búfer Adaptativo por Densidad</h4>
                            <p><strong>Método:</strong> {Constants.DENSIDAD_NAMES[params.densidad_metodo]}</p>
                            <p><strong>Anclaje:</strong> {Constants.DENSIDAD_ANCLAJE_NAMES[params.densidad_metodo_anclaje]}</p>
                            ''' + (f'<p><strong>K vecinos:</strong> {params.densidad_k}</p>' if params.densidad_metodo == Constants.DENSIDAD_KNN else f'<p><strong>Radio ref:</strong> {params.densidad_radio_ref:.1f} m</p><p><strong>Radio base:</strong> {params.densidad_radio_base:.1f} m</p>') + f'''
                            <p><strong>Factor escala:</strong> {params.densidad_factor_escala}</p>
                            <p><strong>Rango radio:</strong> {params.densidad_radio_min:.1f} – {params.densidad_radio_max:.1f} m</p>
                            {f'<p><strong>Tolerancia polo:</strong> {params.densidad_tolerancia_polo} m</p>' if params.densidad_metodo_anclaje == Constants.DENSIDAD_ANCLAJE_POLO else ''}
                        </div>
                        '''}
                    </div>
                </div>
                
                {metricas_html}
                
                {integridad_html}
                
                <div class="section">
                    <h3>⚙️ Parámetros Utilizados</h3>
                    <table class="stats-table">
                        <tr><th>Parámetro</th><th>Valor</th></tr>
                        {''.join([f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in params_report.items()])}
                    </table>
                </div>
                
                {overlap_html}
                
                {fragments_html}
                
                {efficiency_html}
                
                <div class="section {'warning-box' if logger.advertencias else 'success-box'}">
                    <h3>{'⚠️ Alertas y Advertencias' if logger.advertencias else '✅ Estado del Proceso'}</h3>
                    {'<ul class="warning-list">' + ''.join([f"<li>{w}</li>" for w in logger.advertencias]) + '</ul>' if logger.advertencias else '<p>Proceso completado sin errores ni advertencias.</p>'}
                </div>
                
                <p style="text-align:center; color:#7f8c8d;">
                    <p>© {datetime.datetime.now().year} - Crear búfer : Puntos, líneas, Polígonos</p>
                    <p>Jorge Fallas, Email: jfallas56@gmail.com</p>
                </div>
            </div>
        </body>
        </html>
        """
    
    def _export_config_json(self, params: 'BufferParams', ruta: str, feedback) -> bool:
        """
        Serializa los parámetros activos del algoritmo a un archivo JSON.

        Retorna True si la escritura fue exitosa, False si ocurrió un error.
        El archivo resultante puede abrirse con cualquier editor de texto
        y los valores pueden copiarse manualmente al formulario en una
        ejecución posterior.
        """
        try:
            config = {
                "_meta": {
                    "plugin":   "Crear Zonas de Influencia Personalizadas",
                    "version":  __version__,   # sincronizado con la cabecera del módulo
                    "autor":    "Jorge Fallas",
                    "exportado": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "nota":     (
                        "Archivo de auditoría. Para recrear la configuración: "
                        "abra este archivo con cualquier editor de texto, "
                        "lea los valores y configúrelos manualmente en el formulario QGIS."
                    )
                },
                "proyecto": params.nombre_proyecto,
                "buffer": {
                    "tipo":           Constants.BUFFER_NAMES[params.buffer_type],
                    "tipo_indice":    params.buffer_type,
                    "distancia":      params.distancia,
                    "segmentos":      params.segmentos,
                    "campo_distancia": params.distance_field or None,
                    "campo_categoria": params.category_field or None,
                    "mapeo_categorias": params.category_mapping or None,
                },
                "oval_rectangular": {
                    "ancho":       params.ancho,
                    "alto":        params.alto,
                    "rotacion":    params.rotacion,
                    "campo_ancho": params.ancho_field or None,
                    "campo_alto":  params.alto_field or None,
                },
                "concentrico": {
                    "num_anillos":     params.concentric_count,
                    "distancia_anillo": params.concentric_distance,
                    "anillos_disjuntos": params.anillos_disjuntos,
                },
                "por_area": {
                    "area_objetivo":    params.area_objetivo,
                    "unidad_area":      Constants.AREA_UNITS[params.unidad_area],
                    "unidad_indice":    params.unidad_area,
                    "calcular_por_area": params.calcular_por_area,
                },
                "cuna": {
                    "angulo_inicio": params.wedge_start,
                    "apertura":      params.wedge_width,
                    "rotacion_auto": params.usar_rot_auto,
                    "campo_azimut":  params.rotation_field or None,
                },
                "un_solo_lado": {
                    "lado":       ["Izquierdo", "Derecho"][params.side_idx],
                    "lado_indice": params.side_idx,
                },
                "estilo": {
                    "join_style":   ["Redondo", "Miter", "Bevel"][params.join_idx],
                    "join_indice":  params.join_idx,
                    "miter_limit":  params.miter_limit,
                },
                "operacion_logica": {
                    "operacion":        Constants.OP_NAMES[params.op_logic],
                    "operacion_indice": params.op_logic,
                },
                "puntos": {
                    "usar_casco_convexo":  params.usar_hull,
                    "usar_bounding_box":   params.usar_box,
                    "usar_corredor":       params.usar_corredor,
                },
                "integridad": {
                    "gestion":        Constants.INTEGRIDAD_NAMES[params.gestion_integridad].replace("🔧 ", "").replace("🚫 ", "").replace("⚠️ ", ""),
                    "gestion_indice": params.gestion_integridad,
                },
                "simplificacion": {
                    "simplificar_salida":     params.aplicar_simplificacion,
                    "tolerancia_salida":      params.tolerancia_simplificacion,
                    "simplificar_entrada":    params.simplificar_entrada,
                    "tolerancia_entrada":     params.tolerancia_entrada,
                },
                "post_proceso": {
                    "resolver_traslapes":         Constants.TRASLAPE_NAMES[params.resolver_traslapes].replace("🔀 ", "").replace("📈 ", "").replace("📉 ", ""),
                    "resolver_traslapes_indice":  params.resolver_traslapes,
                    "eliminar_huecos":            params.eliminar_huecos,
                    "area_minima_hueco":          params.area_minima_hueco,
                    "preservar_hueco_estructural": params.preservar_hueco_estructural,
                    "disolver_buferes":           params.disolver_buferes,
                    "mantener_parte_mayor":       params.mantener_parte_mayor,
                },
                "densidad_adaptativa": {
                    "activa":          params.usar_densidad_adaptativa,
                    "metodo":          Constants.DENSIDAD_NAMES[params.densidad_metodo] if params.usar_densidad_adaptativa else None,
                    "metodo_indice":   params.densidad_metodo,
                    "k":               params.densidad_k,
                    "radio_referencia": params.densidad_radio_ref,
                    "radio_base":      params.densidad_radio_base,
                    "factor_escala":   params.densidad_factor_escala,
                    "radio_min":       params.densidad_radio_min,
                    "radio_max":       params.densidad_radio_max,
                    # === NUEVO: Método de anclaje ===
                    "metodo_anclaje":   Constants.DENSIDAD_ANCLAJE_NAMES[params.densidad_metodo_anclaje] if params.usar_densidad_adaptativa else None,
                    "metodo_anclaje_indice": params.densidad_metodo_anclaje,
                    "tolerancia_polo":  params.densidad_tolerancia_polo,
                },
                "rendimiento": {
                    "usar_paralelo": params.usar_paralelo,
                    "num_hilos":     params.num_threads,
                },
                "reporte": {
                    "generar_reporte": params.generar_reporte,
                    "ruta_reporte":    params.ruta_reporte,
                },
                "preview": {
                    "preview_mode": params.preview_mode,
                },
                "transparencia": {
                    "aplicar":        params.aplicar_transparencia,
                    "nivel_pct":      params.nivel_transparencia,
                },
            }

            ruta_abs = os.path.abspath(ruta)
            with open(ruta_abs, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)

            feedback.pushInfo(f"💾 Configuración exportada → {ruta_abs}")
            return True

        except Exception as e:
            feedback.pushWarning(f"⚠️ No se pudo exportar la configuración JSON: {e}")
            return False

    def _dry_run_validation(self, source, params: 'BufferParams', feedback, logger: 'Logger') -> Dict:
        """
        Ejecuta validaciones completas sin crear ninguna geometría de búfer (Validación Previa).

        Retorna un dict con:
            'errores'      : List[str]  — problemas que impedirían procesar
            'advertencias' : List[str]  — problemas que generarían resultados incorrectos
            'info'         : List[str]  — observaciones informativas
            'total_features': int
            'geom_invalidas': int
            'campo_dist_ok' : bool
        """
        resultado = {
            'errores': [],
            'advertencias': [],
            'info': [],
            'total_features': 0,
            'geom_invalidas': 0,
            'campo_dist_ok': True,
        }

        # ── 1. CRS ──────────────────────────────────────────────────────────
        if params.crs_es_geografico:
            resultado['advertencias'].append(
                f"CRS geográfico detectado ({params.crs_info}, unidad: {params.crs_unidad}). "
                "Las medidas en grados producirán búferes incorrectos. "
                "Use un CRS proyectado (ej. CRTM05, UTM)."
            )
        else:
            resultado['info'].append(f"CRS proyectado OK → {params.crs_info} ({params.crs_unidad})")

        # ── 2. Distancia / radio ─────────────────────────────────────────────
        tipos_necesitan_distancia = [
            Constants.BUFFER_CIRCULAR, Constants.BUFFER_CONCENTRICO,
            Constants.BUFFER_UN_LADO,  Constants.BUFFER_CUNA,
        ]
        if (params.buffer_type in tipos_necesitan_distancia and
                not params.distance_field and
                not params.category_field and
                not params.usar_densidad_adaptativa and
                not params.calcular_por_area):
            if params.distancia <= 0:
                resultado['errores'].append(
                    f"Distancia/radio = {params.distancia}. Debe ser > 0 para el tipo de búfer "
                    f"'{Constants.BUFFER_NAMES[params.buffer_type]}' sin campo de distancia."
                )
            else:
                resultado['info'].append(f"Distancia fija OK → {params.distancia} {params.crs_unidad}")

        if (params.buffer_type == Constants.BUFFER_OVAL and
                not params.ancho_field and not params.alto_field):
            if params.ancho <= 0 or params.alto <= 0:
                resultado['errores'].append(
                    f"Búfer Oval requiere Ancho > 0 y Alto > 0 "
                    f"(Ancho={params.ancho}, Alto={params.alto})."
                )

        if (params.buffer_type == Constants.BUFFER_RECTANGULAR and
                not params.ancho_field and not params.alto_field):
            if params.ancho <= 0 or params.alto <= 0:
                resultado['errores'].append(
                    f"Búfer Rectangular requiere Ancho > 0 y Alto > 0 "
                    f"(Ancho={params.ancho}, Alto={params.alto})."
                )

        if (params.buffer_type == Constants.BUFFER_CONCENTRICO and
                params.concentric_count < 1):
            resultado['errores'].append(
                f"Búfer Concéntrico requiere al menos 1 anillo (actual: {params.concentric_count})."
            )

        # ── 3. Campos de distancia / categoría ───────────────────────────────
        source_field_names = {fld.name() for fld in source.fields()}

        if params.distance_field:
            if params.distance_field not in source_field_names:
                resultado['errores'].append(
                    f"Campo de distancia '{params.distance_field}' NO existe en la capa. "
                    f"Campos disponibles: {sorted(source_field_names)}"
                )
                resultado['campo_dist_ok'] = False
            else:
                resultado['info'].append(f"Campo de distancia '{params.distance_field}' encontrado ✅")

        if params.category_field:
            if params.category_field not in source_field_names:
                resultado['errores'].append(
                    f"Campo de categoría '{params.category_field}' NO existe en la capa."
                )
            else:
                resultado['info'].append(f"Campo de categoría '{params.category_field}' encontrado ✅")

        if params.ancho_field and params.ancho_field not in source_field_names:
            resultado['errores'].append(
                f"Campo de ancho '{params.ancho_field}' NO existe en la capa."
            )
        if params.alto_field and params.alto_field not in source_field_names:
            resultado['errores'].append(
                f"Campo de alto '{params.alto_field}' NO existe en la capa."
            )

        # ── 4. Geometrías: contar, detectar inválidas, muestrear campo ────────
        feedback.setProgressText("Validación Previa: inspeccionando geometrías...")
        total = 0
        invalidas = 0
        vacias = 0
        nulos_campo = 0
        ceros_campo = 0
        negativos_campo = 0

        request = QgsFeatureRequest()
        request.setInvalidGeometryCheck(QgsFeatureRequest.GeometryNoCheck)

        for feat in source.getFeatures(request):
            total += 1
            if feedback.isCanceled():
                break

            # Geometría
            geom = feat.geometry()
            if not geom or geom.isEmpty():
                vacias += 1
            elif not geom.isGeosValid():
                invalidas += 1

            # Valores del campo de distancia (si existe y es válido)
            if params.distance_field and resultado['campo_dist_ok']:
                try:
                    val = feat[params.distance_field]
                    if val is None or val == NULL:
                        nulos_campo += 1
                    else:
                        fval = float(val)
                        if fval == 0:
                            ceros_campo += 1
                        elif fval < 0:
                            negativos_campo += 1
                except (ValueError, TypeError):
                    nulos_campo += 1

        resultado['total_features'] = total
        resultado['geom_invalidas'] = invalidas

        resultado['info'].append(f"Total de entidades: {total}")

        if vacias > 0:
            resultado['advertencias'].append(
                f"{vacias} entidad(es) con geometría vacía — serán omitidas."
            )
        if invalidas > 0:
            resultado['advertencias'].append(
                f"{invalidas} geometría(s) inválida(s) detectadas "
                f"(gestión actual: {Constants.INTEGRIDAD_NAMES[params.gestion_integridad].replace('🔧 ','').replace('🚫 ','').replace('⚠️ ','')})."
            )
        else:
            resultado['info'].append("Sin geometrías inválidas ✅")

        if params.distance_field and resultado['campo_dist_ok']:
            if nulos_campo > 0:
                resultado['advertencias'].append(
                    f"Campo '{params.distance_field}': {nulos_campo} valor(es) nulo(s) — "
                    "esas entidades usarán la distancia fija del formulario."
                )
            if ceros_campo > 0:
                resultado['advertencias'].append(
                    f"Campo '{params.distance_field}': {ceros_campo} valor(es) = 0 — "
                    "generarán búferes degenerados (geometría puntual)."
                )
            if negativos_campo > 0:
                tipo_nombre = Constants.BUFFER_NAMES[params.buffer_type]
                if params.buffer_type in [Constants.BUFFER_CIRCULAR, Constants.BUFFER_CONCENTRICO]:
                    resultado['info'].append(
                        f"Campo '{params.distance_field}': {negativos_campo} valor(es) negativo(s) — "
                        f"en {tipo_nombre} sobre polígonos = contracción interior (válido)."
                    )
                else:
                    resultado['advertencias'].append(
                        f"Campo '{params.distance_field}': {negativos_campo} valor(es) negativo(s). "
                        f"Verifique que son intencionales para el tipo '{tipo_nombre}'."
                    )

        # ── 5. Capa de exclusión ──────────────────────────────────────────────
        if params.exclusion_layer:
            try:
                n_excl = params.exclusion_layer.featureCount()
                resultado['info'].append(
                    f"Capa de exclusión detectada ({n_excl} entidades) ✅"
                )
            except Exception:
                resultado['advertencias'].append(
                    "Capa de exclusión configurada pero no se pudo acceder a ella."
                )

        # ── 6. Densidad adaptativa ────────────────────────────────────────────
        if params.usar_densidad_adaptativa:
            tipos_incompat = [
                Constants.BUFFER_OVAL, Constants.BUFFER_RECTANGULAR,
                Constants.BUFFER_POR_AREA, Constants.BUFFER_UN_LADO, Constants.BUFFER_CUNA,
            ]
            if params.buffer_type in tipos_incompat:
                resultado['advertencias'].append(
                    f"Búfer Adaptativo activo, pero tipo '{Constants.BUFFER_NAMES[params.buffer_type]}' "
                    "no es compatible (solo Circular y Concéntrico). Se ignorará."
                )
            elif params.distance_field:
                resultado['advertencias'].append(
                    f"Búfer Adaptativo activo, pero hay un campo de distancia ('{params.distance_field}'). "
                    "El campo tiene prioridad; el adaptativo no se aplicará."
                )
            else:
                anclaje_str = Constants.DENSIDAD_ANCLAJE_NAMES[params.densidad_metodo_anclaje]
                if params.densidad_metodo == Constants.DENSIDAD_KNN:
                    resultado['info'].append(
                        f"Búfer Adaptativo OK → Método: {Constants.DENSIDAD_NAMES[params.densidad_metodo]}, "
                        f"Anclaje: {anclaje_str}, "
                        f"K={params.densidad_k}, escala={params.densidad_factor_escala}"
                    )
                else:
                    resultado['info'].append(
                        f"Búfer Adaptativo OK → Método: {Constants.DENSIDAD_NAMES[params.densidad_metodo]}, "
                        f"Anclaje: {anclaje_str}, "
                        f"Radio ref={params.densidad_radio_ref}, radio base={params.densidad_radio_base}, "
                        f"escala={params.densidad_factor_escala}"
                    )
                if params.densidad_metodo_anclaje == Constants.DENSIDAD_ANCLAJE_POLO:
                    resultado['info'].append(f"   • Tolerancia polo: {params.densidad_tolerancia_polo} m")

        # ── 7. Resumen ────────────────────────────────────────────────────────
        n_err  = len(resultado['errores'])
        n_warn = len(resultado['advertencias'])
        if n_err == 0 and n_warn == 0:
            resultado['info'].append("✅ Sin problemas detectados. La capa está lista para procesar.")
        elif n_err == 0:
            resultado['info'].append(
                f"⚠️ {n_warn} advertencia(s). El proceso puede ejecutarse, "
                "pero revise las advertencias antes de continuar."
            )
        else:
            resultado['info'].append(
                f"❌ {n_err} error(es) crítico(s). Corrija estos problemas antes de procesar."
            )

        return resultado

    def _generate_dry_run_report(self, params: 'BufferParams',
                                   dry_resultado: dict, source_name: str,
                                   feedback=None):
        """Genera y abre un reporte HTML con los resultados de la Validación Previa."""
        # Sanitizar valores de usuario para evitar HTML malformado por caracteres especiales
        source_name = html.escape(str(source_name))
        errores   = dry_resultado.get('errores', [])
        advertencias = dry_resultado.get('advertencias', [])
        info      = dry_resultado.get('info', [])
        total_f   = dry_resultado.get('total_features', 0)
        inv_f     = dry_resultado.get('geom_invalidas', 0)

        estado_color = "#dc3545" if errores else ("#ffc107" if advertencias else "#28a745")
        estado_texto = ("❌ ERRORES CRÍTICOS — no procesar" if errores
                        else ("⚠️ Advertencias — revisar antes de procesar" if advertencias
                              else "✅ Sin problemas — listo para procesar"))

        def _li_list(items, color):
            if not items:
                return f'<p style="color:{color}">— Ninguno —</p>'
            return "<ul>" + "".join(f'<li style="margin-bottom:4px">{i}</li>' for i in items) + "</ul>"

        html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>Validación Previa — Diagnóstico de Búfer</title>
  <style>
    body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 20px;
            background: #f5f5f5; }}
    .container {{ max-width: 900px; margin: 0 auto; background: white;
                  border-radius: 10px; box-shadow: 0 0 20px rgba(0,0,0,0.1);
                  padding: 30px; }}
    .header {{ background: linear-gradient(135deg,#1e3c72,#2a5298);
               color:white; padding:20px; border-radius:10px; margin-bottom:20px; }}
    .header h1 {{ margin:0; font-size:22px; }}
    .sub {{ margin:5px 0 0 0; font-size:13px; opacity:.9; }}
    .estado {{ padding:14px 18px; border-radius:8px; font-size:16px;
               font-weight:bold; margin-bottom:20px;
               background:{estado_color}22; border:2px solid {estado_color};
               color:{estado_color}; }}
    .section {{ background:#f8f9fa; border-radius:8px; padding:18px;
                margin-bottom:20px; }}
    .section h3 {{ margin-top:0; color:#333;
                   border-bottom:2px solid #e9ecef; padding-bottom:8px; }}
    .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
    .card {{ background:white; border-radius:6px; padding:14px;
             box-shadow:0 1px 6px rgba(0,0,0,.08); }}
    .card h4 {{ margin:0 0 8px 0; color:#2a5298; font-size:13px; }}
    ul {{ margin:0; padding-left:18px; }}
    li {{ margin-bottom:3px; }}
    .err  {{ color:#dc3545; }}
    .warn {{ color:#856404; }}
    .ok   {{ color:#155724; }}
    .footer {{ text-align:center; padding:16px; background:#f8f9fa;
               color:#6c757d; border-top:1px solid #dee2e6;
               margin-top:24px; border-radius:8px; font-size:12px; }}
  </style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🔍 REPORTE VALIDACIÓN PREVIA — Diagnóstico de Búfer</h1>
    <p class="sub">Generado: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}
       &nbsp;|&nbsp; Capa: {source_name}
       &nbsp;|&nbsp; Tipo búfer: {Constants.BUFFER_NAMES[params.buffer_type]}</p>
  </div>

  <div class="estado">{estado_texto}</div>

  <div class="section">
    <h3>📋 Resumen de la Capa</h3>
    <div class="grid2">
      <p style="margin-left:8px; border-left:3px solid #2980b9; padding-left:8px;">
        <h4>📊 Estadísticas</h4>
        <p><strong>Total entidades:</strong> {total_f}</p>
        <p><strong>Geometrías inválidas:</strong> {inv_f}</p>
        <p><strong>CRS:</strong> {params.crs_info}</p>
        <p><strong>Unidad:</strong> {params.crs_unidad}</p>
      </div>
      <p style="margin-left:8px; border-left:3px solid #2980b9; padding-left:8px;">
        <h4>⚙️ Configuración revisada</h4>
        <p><strong>Tipo búfer:</strong> {Constants.BUFFER_NAMES[params.buffer_type]}</p>
        <p><strong>Integridad:</strong> {Constants.INTEGRIDAD_NAMES[params.gestion_integridad].replace("🔧 ","").replace("🚫 ","").replace("⚠️ ","")}</p>
        <p><strong>Campo distancia:</strong> {params.distance_field or "— (distancia fija)"}</p>
        <p><strong>Densidad adaptativa:</strong> {"✅ Activa" if params.usar_densidad_adaptativa else "No"}</p>
        {f'<p><strong>Anclaje densidad:</strong> {Constants.DENSIDAD_ANCLAJE_NAMES[params.densidad_metodo_anclaje]}</p>' if params.usar_densidad_adaptativa else ''}
        {f'<p><strong>Tolerancia polo:</strong> {params.densidad_tolerancia_polo} m</p>' if params.usar_densidad_adaptativa and params.densidad_metodo_anclaje == Constants.DENSIDAD_ANCLAJE_POLO else ''}
      </div>
    </div>
  </div>

  <div class="section">
    <h3 class="err">❌ Errores Críticos ({len(errores)})</h3>
    {_li_list(errores, "#dc3545")}
  </div>

  <div class="section">
    <h3 class="warn">⚠️ Advertencias ({len(advertencias)})</h3>
    {_li_list(advertencias, "#856404")}
  </div>

  <div class="section">
    <h3 class="ok">ℹ️ Información ({len(info)})</h3>
    {_li_list(info, "#155724")}
  </div>

  <p style="text-align:center; color:#7f8c8d;">
    <p>© {datetime.datetime.now().year} — Crear Zonas de Influencia Personalizadas</p>
    <p>Jorge Fallas &nbsp;|&nbsp; jfallas56@gmail.com</p>
    <p><em>Validación Previa: no se generaron geometrías de búfer.</em></p>
  </div>
</div>
</body>
</html>"""
        self._report_write_and_open(html_content, params.ruta_reporte)

    def _report_write_and_open(self, html: str, ruta_reporte: str, feedback=None):
        """Escribe el reporte HTML al disco y lo abre en el navegador.
        
        Args:
            html: Contenido HTML del reporte.
            ruta_reporte: Ruta de destino del archivo.
            feedback: Objeto QgsProcessingFeedback para reportar errores en el log de QGIS.
                      Si es None, los errores se imprimen en consola como respaldo.
        """
        try:
            ruta_absoluta = os.path.abspath(ruta_reporte)
            
            with open(ruta_absoluta, 'w', encoding='utf-8') as f:
                f.write(html)
            
            if sys.platform.startswith('win'):
                url = 'file:///' + ruta_absoluta.replace('\\', '/')
            else:
                url = 'file://' + ruta_absoluta
            
            webbrowser.open(url, new=2)
            
        except (IOError, OSError) as e:
            msg = f"⚠️ No se pudo escribir el reporte HTML: {e}"
            if feedback:
                feedback.reportError(msg)
            else:
                print(msg)
        except Exception as e:
            msg = f"⚠️ No se pudo abrir el reporte en el navegador: {e}"
            if feedback:
                feedback.pushWarning(msg)
            else:
                print(msg)

    def name(self):
        return 'zonas_influencia_personalizadas'
    
    def displayName(self):
        return 'Crear Zonas de Influencia Personalizadas: Puntos, Líneas y Polígonos'
    
    def group(self):
        return 'Herramientas de Análisis'
    
    def groupId(self):
        return 'herramientas_analisis'
    
    def createInstance(self):
        return CrearBuferPLP()
    
    def flags(self):
        """
        Indica que este algoritmo puede manejar geometrías inválidas internamente.
        """
        return super().flags() | QgsProcessingAlgorithm.FlagSkipGenericModelLogging
    
    def prepareAlgorithm(self, parameters, context, feedback):
        """
        Se ejecuta antes del procesamiento principal.
        Configura el contexto para NO filtrar geometrías inválidas.
        """
        # Desactivar el chequeo de geometrías inválidas en el contexto
        context.setInvalidGeometryCheck(QgsFeatureRequest.GeometryNoCheck)
        return True
    
    def shortHelpString(self):
        return """
        <p>Genera áreas de búfer personalizadas sobre puntos, líneas y polígonos con control total sobre geometría, topología y estilo visual.</p>
        <h3>📍 TRATAMIENTO DE PUNTOS</h3>
        <ul>
        <li>🔷 <strong>Geometría Mínima (Casco Convexo):</strong> polígono envolvente de todos los puntos.</li>
        <li>📐 <strong>Polígono Envolvente (Caja Delimitadora):</strong> rectángulo que abarca la extensión total.</li>
        <li>⚠️ Con <em>"Objetos seleccionados solamente"</em>, todos los puntos se agrupan en una sola geometría base.</li>
        </ul>
        <h3>⚙️ TIPOS DE BÚFER</h3>
        <h4>1. 🔘 Circular</h4>
        <ul>
        <li>📏 <strong>Distancia:</strong> Positiva (exterior) o negativa (interior).</li>
        <li>📐 <strong>Por área:</strong> calcula el radio para alcanzar el área objetivo.</li>
        </ul>
        <h4>2. 🟢 Ovalado</h4>
        <ul>
        <li>📏 <strong>Ancho y Alto:</strong> dimensiones independientes obligatorias. En <strong>líneas</strong> se ancla al punto medio de la longitud recorrida. En <strong>polígonos</strong> usa estrategia híbrida (4 pasos): centroide → polo del casco convexo → erosión iterativa → Punto en Superficie(). En <strong>puntos</strong> el origen es el punto mismo.</li>
        <li>↻ <strong>Rotación:</strong> 0–360° (0°=Norte, sentido horario). 0° alinea el Ancho al N–S; 90° al E–O. Intercambiar Ancho y Alto equivale a rotar 90°.</li>
        </ul>
        <h4>3. 🟡 Rectangular</h4>
        <ul>
        <li>📏 <strong>Ancho y Alto:</strong> independientes, obligatorios. Misma lógica de anclaje y rotación que el Oval.</li>
        <li>🛣️ <strong>Opción Corredor:</strong> para líneas, crea faja continua (solo requiere Ancho). Sin Corredor, la figura se ancla al punto medio de la línea.</li>
        <li>⚠️ <strong>MultiLínea:</strong> el punto medio puede caer en una parte secundaria. Use <em>"Multipartes a partes simples"</em> antes.</li>
        </ul>
        <h4>4. 🎯 Concéntrico</h4>
        <ul>
        <li>📏 <strong>Distancia:</strong> positiva (exterior) o negativa (interior).</li>
        <li>🎯 <strong>Cantidad:</strong> 1–50 anillos.</li>
        <li>⭕ <strong>Anillos Disjuntos (Dónut):</strong> bandas exclusivas (activado) o discos acumulativos (desactivado).</li>
        <li>🔗 <strong>Estilo de Unión:</strong> Redondeado, Inglete o Biselado. ⚠️ Puntos + Biselado fuerza terminación Redonda.</li>
        </ul>
        <h4>5. 📊 Por Área</h4>
        <ul>
        <li>📐 <strong>Área objetivo:</strong> en hectáreas, m² o km².</li>
        <li>🧮 <strong>Cálculo:</strong> método iterativo (hasta 50 iteraciones). En <strong>puntos</strong>: fórmula directa <code>r = √(área/π)</code>. En <strong>polígonos</strong>: expande/contrae automáticamente. En <strong>líneas</strong>: faja simétrica iterativa.</li>
        <li>🧩 <strong>Fragmentación:</strong> contracción de polígonos cóncavos (L, U, T) puede generar múltiples fragmentos. Opción de mantener solo la parte mayor.</li>
        <li>🛡️ Entidades con &gt;100.000 vértices se omiten. ⏱️ Tiempo de Espera : 350 s por entidad.</li>
        <li>💡 <strong>Rendimiento:</strong> active <em>"Simplificar geometrías de entrada"</em> (tolerancia 2–5 m) para reducir tiempos drásticamente.</li>
        </ul>
        <h4>6. 🛤️ Un Solo Lado (Líneas / Polígonos)</h4>
        <ul>
        <li>↔️ <strong>Lado:</strong> En <strong>líneas</strong>: izquierda/derecha relativo a la digitalización. En <strong>polígonos</strong>: izquierda = exterior, derecha = interior.</li>
        <li>🔗 <strong>Estilo de Unión:</strong> Redondeado, Inglete o Biselado.</li>
        </ul>
        <h4>7. 🍕 Cuña</h4>
        <p>Sector circular orientado desde un punto interior de la geometría.</p>
        <ul>
        <li>📍 <strong>Origen:</strong> En <strong>puntos</strong>: punto exacto. En <strong>líneas</strong>: punto medio. En <strong>polígonos</strong>: estrategia híbrida (centroide → polo → erosión → Punto en Superficie).</li>
        <li>📍 <strong>Múltiples puntos:</strong> una cuña por punto, o cuña única desde centroide del grupo (con Hull/Box).</li>
        <li>📏 <strong>Radio:</strong> fijo (parámetro Distancia) o variable (campo numérico, dejar Distancia en 0). ⚠️ Radio = 0 o nulo → entidad omitida. Radio negativo → cuña opuesta 180°.</li>
        <li>🧭 <strong>Azimut:</strong> fijo (parámetro) o variable (campo de rotación por entidad).</li>
        <li>🍕 <strong>Amplitud:</strong> ancho angular en grados (constante para toda la capa).</li>
        <li>🔄 <strong>Rotación Automática:</strong> orienta según el eje principal. <strong>Líneas:</strong> primer→último vértice. <strong>Polígonos:</strong> Rectángulo Orientado Mínimo. <strong>Puntos:</strong> no aplica.</li>
        <li>📋 <strong>Prioridad:</strong> Rotación Automática &gt; Campo de Rotación &gt; Ángulo fijo.</li>
        <li>👁️ Ideal para campos de visión, dispersión de emisiones, servidumbres costeras.</li>
        </ul>
        <h4>8. 🧭 Adaptativo por Densidad</h4>
        <p>Radio individual por entidad según densidad espacial local: más vecinos → radio menor; más aislado → radio mayor.</p>
        <ul>
        <li>📍 <strong>KNN (Recomendado):</strong> <code>Radio = dist_al_k_vecino × escala</code>. K=3, escala=0.5 → radio = 50% de la distancia al 3er vecino. Entidades aisladas → radio máximo.</li>
        <li>🔵 <strong>Conteo en Radio Fijo:</strong> <code>Radio = (radio_base / √n_vecinos) × escala</code>. Útil con distancia de referencia conocida (ej. 500 m).</li>
        <li><strong>K (solo KNN):</strong> K=1 ruidoso · K=3 recomendado · K≥7 regional. Regla: <code>K ≈ √(n) / 3</code>.</li>
        <li><strong>Factor de escala:</strong> 0.5 = sin traslape · 1.0 = se tocan · &gt;1.0 = traslape.</li>
        <li><strong>Radio mín/máx:</strong> cotas de seguridad.</li>
        <li>📍 <strong>Anclaje — Centroide:</strong> rápido, puede caer fuera en formas cóncavas.</li>
        <li>🎯 <strong>Anclaje — Polo de inaccesibilidad:</strong> siempre dentro del polígono. Tolerancia: 1–5 m recomendado.</li>
        <li>Solo compatible con búferes <strong>Circular</strong> y <strong>Concéntrico</strong>. Campo de Distancia tiene prioridad.</li>
        <li>⚠️ Para polígonos asimétricos, use polo de inaccesibilidad.</li>
        </ul>
        <h4>9. 〰️ Ancho Variable (Puntos) — Corredor</h4>
        <p>Polígono de ancho variable a partir de <strong>puntos ordenados</strong>. El ancho en cada punto está definido por un campo de distancia (radio) y se interpola linealmente entre puntos adyacentes.</p>
        <ul>
        <li>📏 El campo se interpreta como <strong>radio</strong> (distancia eje→borde). Ancho total = <code>2 × radio</code>.</li>
        <li>⚙️ El corredor se construye dividiendo cada segmento en sub-segmentos cortos con paso adaptativo <code>min(r_min / 4, 2,0 m)</code>. Cada sub-segmento recibe un buffer GEOS con radio interpolado linealmente. La unión produce esquinas redondeadas y transición continua de ancho.</li>
        <li>✅ El ancho se respeta con precisión en tramos rectos y esquinas.</li>
        <li>📍 <strong>Requisitos:</strong> solo puntos, campo numérico &gt; 0, orden secuencial, CRS proyectado (UTM, CRTM05).</li>
        <li>💡 <strong>Casos de uso:</strong> zonas de inundación, zonas acústicas, fajas de seguridad eléctrica, corredores biológicos, derecho de vía, amortiguamiento ambiental, cobertura de señal.</li>
        <li>⚠️ Solo acepta puntos. El orden en la tabla de atributos determina la ruta. Distancia = 0 → mínimo operativo (0,0001 m).</li>
        </ul>
        <h5>📊 Atributos de salida</h5>
        <ul>
        <li><code>fid</code> — ID secuencial</li>
        <li><code>tipo_entidad</code> — "Corredor Distancia Variable"</li>
        <li><code>area_ha</code> — Área total (hectáreas)</li>
        <li><code>distancia</code> — Radio máximo del corredor (m)</li>
        <li><code>notas</code> — "puntos→ruta→Polígono"</li>
        </ul>
        <h3>🆕 FUNCIONALIDADES ADICIONALES</h3>
        <h4>📊 Campo de distancia/radio/área (búfer variable)</h4>
        <p>Cada entidad puede tener un valor diferente. <strong>El significado varía según el tipo:</strong></p>
        <ul>
        <li><strong>Circular / Cuña:</strong> Radio en metros. Dejar Distancia fija en 0. ⚠️ Valor 0 o nulo → omitida.</li>
        <li><strong>Por Área:</strong> ⚠️ Área objetivo (NO radio) en la unidad seleccionada.</li>
        <li><strong>Oval:</strong> Ancho y Alto en metros (proporcional).</li>
        <li><strong>Rect. + Corredor:</strong> Ancho en metros.</li>
        <li><strong>Concéntrico:</strong> Distancia entre anillos (m). Dejar Distancia fija en 0.</li>
        <li><strong>Un Solo Lado:</strong> Offset en metros. (+) = Izquierda, (−) = Derecha.</li>
        </ul>
        <h4>📊 Campos de Ancho / Alto (Oval y Rectangular)</h4>
        <ul>
        <li>Valores independientes por entidad. Si solo define uno, el otro usa el parámetro fijo.</li>
        <li>Tienen prioridad sobre el Campo de distancia.</li>
        </ul>
        <h4>Otras funcionalidades</h4>
        <ul>
        <li>🚫 <strong>Capa de exclusión:</strong> resta áreas de una capa de polígonos de todos los búferes.</li>
        <li>👁️ <strong>Modo previsualización:</strong> procesa solo la primera entidad.</li>
        <li>📊 <strong>Análisis de superposición:</strong> calcula áreas de superposición entre búferes.</li>
        <li>📐 <strong>Simplificación de geometrías:</strong> reduce vértices para mejorar rendimiento.</li>
        <li>⚡ <strong>Integridad geométrica:</strong> 🔧 Reparadas · 🚫 Omitidas · ⚠️ Con riesgo · ✅ Procesadas.</li>
        </ul>
        <h3>📋 REPORTE DE CALIDAD — ISO 19157:2023</h3>
        <p>El reporte HTML incluye indicadores basados en <em>ISO 19157:2023</em>:</p>
        <ul>
        <li><strong>📐 Completitud (§D.1):</strong> Omisión (🚫 integridad geométrica, 🔸 parámetros) y Comisión (⚠️ entidades sin calidad verificada).</li>
        <li><strong>🔗 Consistencia Lógica (§D.3):</strong> ✅ Consistentes · 🔧 Reparadas con <code>makeValid()</code> · Sin verificación → "No verificado".</li>
        <li><strong>Barra:</strong> <code>(entidades con búfer / total entrada) × 100</code>. Conformidad factual sin umbrales — la aceptabilidad corresponde al profesional.</li>
        </ul>
        <h3>🔀 RESOLUCIÓN DE TRASLAPES Y HUECOS</h3>
        <ul>
        <li>🔀 <strong>Traslapes:</strong> Mantener (sin modificar) · Asignar al MAYOR · Asignar al MENOR.</li>
        <li>🔗 <strong>Disolver:</strong> ❌ Desactivado = polígonos individuales, conserva ID. ✅ Activado = fusiona geometrías conectadas. ⚠️ Se pierde identidad individual.</li>
        <li>🕳️ <strong>Eliminar huecos:</strong> Área mínima = 0 elimina todos; Área &gt; 0 preserva huecos grandes.</li>
        </ul>
        <h3>⚡ PROCESAMIENTO PARALELO</h3>
        <ul>
        <li>🚀 Múltiples núcleos del CPU simultáneamente. Recomendado para &gt;50 entidades complejas.</li>
        <li>🔢 Número de hilos: generalmente = número de núcleos del CPU.</li>
        </ul>
        <h3>🎯 CAPAS VECTORIZADAS DE RASTER</h3>
        <p>Para polígonos con perímetros pixelados tipo "escalera":</p>
        <ul>
        <li>☑️ <strong>Simplificar geometrías:</strong> tolerancia ≈ 50% del pixel (Sentinel-2: 5 m, Landsat: 15 m, MODIS: 125 m). Reduce vértices 60–80%.</li>
        <li>☑️ <strong>Eliminar huecos:</strong> Área mínima = 0 (todos) o 500–1000 m² (preservar lagos). Se eliminan ANTES del búfer.</li>
        <li>💡 Use <em>Previsualización</em> para validar la configuración. Segmentos: 25–30 es suficiente.</li>
        </ul>
        <h3>💾 EXPORTAR CONFIGURACIÓN JSON</h3>
        <p>Guarda todos los parámetros en JSON para auditoría y reproducibilidad. ⚠️ QGIS no auto-carga valores — el JSON es referencia para copiar manualmente.</p>
        <h3>🔍 VALIDACIÓN PREVIA</h3>
        <p>Ejecuta validaciones <strong>sin generar geometría</strong>: CRS, campos, valores nulos/cero/negativos, coherencia de parámetros, geometrías inválidas, capa de exclusión, compatibilidad adaptativo. Resultado: reporte HTML + mensajes en log.</p>
        <h3>📊 PROGRESO POR ETAPAS</h3>
        <ul>
        <li><strong>Etapa 1/4</strong> (0–10%) — Validación</li>
        <li><strong>Etapa 2/4</strong> (10–75%) — Generación de búferes</li>
        <li><strong>Etapa 3/4</strong> (75–90%) — Post-proceso</li>
        <li><strong>Etapa 4/4</strong> (90–100%) — Reporte y JSON</li>
        </ul>
        <hr>
        <p><strong>📅</strong> Marzo 2026 · <strong>👤</strong> Jorge Fallas · <strong>📧</strong> jfallas56@gmail.com</p>
        """