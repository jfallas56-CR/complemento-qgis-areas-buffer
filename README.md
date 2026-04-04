# 🗺️ Complementos QGIS
### Jorge Fallas · [@jfallas56-CR](https://github.com/jfallas56-CR)

Colección de complementos de geoprocesamiento avanzado para QGIS, orientados al análisis
espacial profesional, la calidad de datos geográficos y la reproducibilidad metodológica.

---

## 📦 Complementos disponibles

### 1. 🎯 Crear Zonas de Influencia Personalizadas
**Generador de búferes avanzado — Puntos, Líneas y Polígonos**

Herramienta integral para la generación de áreas de influencia con control geométrico avanzado,
auditoría de calidad bajo el estándar ISO 19157 y exportación de configuración para
trazabilidad y reproducibilidad del análisis.

| Versión | QGIS mínimo | Qt | Licencia |
|---------|------------|-----|----------|
| 1.0.0 | ≥ 3.28 LTR | 5 / 6 | GPL-2.0-or-later |

**9 tipos de búfer:**
`Circular` · `Oval` · `Rectangular` · `Concéntrico` · `Por Área` ·
`Un Solo Lado` · `Cuña` · `Adaptativo por Densidad` · `Ancho Variable (Puntos)`

**Características principales:**
- Dimensiones dinámicas por campo numérico o mapeo JSON por categoría
- Reporte HTML automático con indicadores ISO 19157:2023 (Completitud y Consistencia Lógica)
- Procesamiento paralelo multihilo para capas masivas
- Validación previa (dry-run): audita datos sin generar geometrías
- Operaciones lógicas de superposición: Unión, Intersección, Diferencia, XOR
- Exportación de configuración JSON para reproducibilidad científica

📥 **[Descargar](../../releases/latest)** &nbsp;·&nbsp;
📖 **[Documentación](crear_zonas_influencia/README.md)** &nbsp;·&nbsp;
🐛 **[Reportar problema](../../issues)**

---

## 🚀 Instalación

Todos los complementos de este repositorio se instalan de la misma forma:

1. Descargar el archivo `.zip` desde la sección **[Releases](../../releases)**.
2. En QGIS: **Complementos → Administrar e instalar complementos → Instalar desde ZIP**.
3. Seleccionar el archivo descargado y hacer clic en **Instalar complemento**.

> ⚠️ Requiere QGIS ≥ 3.28. Sin dependencias Python externas.

---

## 🗂️ Estructura del repositorio

```
Complementos-QGIS/
│
├── crear_zonas_influencia/     # Búferes avanzados con auditoría ISO 19157
│   ├── __init__.py
│   ├── plugin.py
│   ├── provider.py
│   ├── crear_bufer.py
│   ├── metadata.txt
│   ├── icon.png
│   ├── LICENSE.txt
│   └── README.md
│
└── README.md                   # Este archivo
```

Cada complemento reside en su propia carpeta con documentación, licencia e ícono propios.

---

## 📋 Requisitos generales

| Componente | Versión mínima |
|---|---|
| QGIS | 3.28 LTR |
| Python | 3.9 |
| GEOS | 3.9 |
| Qt | 5 o 6 (compatible vía `qgis.PyQt`) |

Todos los complementos funcionan con una instalación estándar de QGIS
sin dependencias externas adicionales.

---

## 📄 Licencia

Todos los complementos de este repositorio se publican bajo la
**GNU General Public License v2 or later (GPL-2.0-or-later)**.
Consulte el archivo `LICENSE.txt` dentro de cada complemento para el texto completo.

---

## 👤 Autor

**Jorge Fallas**
📧 [jfallas56@gmail.com](mailto:jfallas56@gmail.com)
🐛 Problemas y sugerencias: [Abrir un issue](../../issues)
