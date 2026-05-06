# Crear Zonas de Influencia Personalizadas

**Advanced Buffer Generator for QGIS** — 9 buffer types, dynamic field-based sizing,
ISO 19157 quality reports, overlap analysis, parallel processing and dry-run validation.

[!\[QGIS >= 3.28](https://img.shields.io/badge/QGIS-%3E%3D%203.28-green)](https://qgis.org)
[!\[License: GPL v2](https://img.shields.io/badge/License-GPL%20v2-blue)](LICENSE.txt)
[!\[Qt5/Qt6](https://img.shields.io/badge/Qt-5%20%7C%206-informational)](https://www.qt.io)

\---

## Overview

This QGIS Processing algorithm goes far beyond the standard fixed-radius buffer.
It provides a complete geoprocessing workflow for creating, auditing and post-processing
influence zones (buffers) of any shape and complexity.

\---

## Buffer Types

|#|Type|Description|
|-|-|-|
|1|Circular|Fixed or per-entity variable radius|
|2|Oval|Independent width and height axes|
|3|Rectangular|Optional corridor mode for lines|
|4|Concentric|Multiple rings; disjoint (donut) or cumulative (disc)|
|5|By Area|Iterative expansion to exact target area (ha, m², km²)|
|6|One Side|Asymmetric offset left/right or exterior/interior|
|7|Wedge|Directional sector with azimuth and aperture|
|8|Adaptive by Density|Automatic radius via KNN or Fixed Radius density|
|9|Variable Width (Points)|Variable-width corridor along an ordered point route|

\---

## Key Features

### Dynamic Parameterization

* **Numeric field**: assign radius, area or dimensions per entity from the attribute table
* **Category mapping**: JSON text → distance/area (e.g. `{"highway": 50, "path": 5}`)
* **Independent width/height fields** for Oval and Rectangular buffers
* **Variable azimuth field** for Wedge; auto-rotation by principal axis available

### Topology \& Post-Processing

* Geometry integrity: **Repair** (`makeValid`), **Omit** or **Risk** modes
* Overlap resolution: keep / assign to larger / assign to smaller polygon
* Buffer dissolve: merge connected groups into continuous coverage
* Hole removal with configurable area threshold; structural hole preservation (donut)
* Logical operations: **Union**, **Intersection**, **Difference**, **Inverse Difference**, **XOR**
* Exclusion layer: automatic clip against restricted areas
* Input/output geometry simplification (Douglas-Peucker)
* Overlap fragment layer: exclusive, double, triple… topological decomposition

### Performance \& Safety

* Multi-thread parallel processing (`ThreadPoolExecutor`, thread-safe objects)
* Batch dissolve to prevent RAM overflow on large layers
* **Dry-run validation**: audits CRS, fields, geometries and parameters — no output generated
* **Preview mode**: processes only the first feature for quick configuration testing
* Resource guard with timeout and vertex limit per feature

### Audit \& Reproducibility

* Automatic **interactive HTML report** with ISO 19157:2023 indicators:

  * Completeness (§D.1): Omission and Commission rates
  * Logical Consistency (§D.3): Topological consistency and auto-correction rate
  * Expandable ID detail per category (Repaired, Omitted, Risk, No Buffer, Fragmented)
* **JSON configuration export**: saves all 65 active parameters for full reproducibility
* 4-stage progress bar with descriptive text in QGIS

\---

## Requirements

|Component|Minimum version|
|-|-|
|QGIS|3.28 LTR|
|GEOS|3.9|
|Python|3.9|
|Qt|5 or 6 (via `qgis.PyQt` shim)|

No external Python packages required. All dependencies are included in a standard QGIS installation.

\---

## Installation

1. Download `crear\_zonas\_influencia.zip` from the [Releases](https://github.com/jfallas56-CR/complemento-qgis-areas-buffer/releases) page.
2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Select the downloaded ZIP and click **Install Plugin**.
4. The algorithm appears in: **Processing Toolbox → Herramientas de Análisis → Crear Zonas de Influencia Personalizadas**.
5. A toolbar button and menu entry are added automatically under **Plugins → Herramientas de Análisis**.

\---

## Quick Start

### Minimum configuration (Circular buffer)

1. Select your input layer (points, lines or polygons).
2. Set **Buffer type** = Circular.
3. Enter a **Distance** value in map units.
4. Run — the output layer is loaded automatically.

### Variable buffer by field

1. Set **Buffer type** = Circular (or any type).
2. Select a numeric field in **Distance/radius/area field**.
3. Leave **Distance** = 0 (the field takes priority).
4. Run.

### Dry-run validation before large batch

1. Check **Dry-run validation (validate without processing)**.
2. Run — no geometries are generated.
3. Review the HTML report and QGIS log for errors and warnings.
4. Uncheck dry-run and run again to generate the final output.

\---

## Interface Modes

|Mode|Parameters shown|Use case|
|-|-|-|
|**Compact** (default)|22 essential parameters|Daily tasks|
|**Expert** (▼ Advanced)|All 65 parameters|Full technical control|

\---

## Output Attributes

Every output feature includes:

|Field|Content|
|-|-|
|`fid`|Sequential ID|
|`tipo\_entidad`|Buffer type label|
|`area\_ha`|Area in hectares|
|`distancia\_m`|Applied distance (m)|
|`notas`|Processing notes|

\---

## Reporting

When **Generate HTML report** is active (default), a browser report opens automatically at the
end of processing. It includes:

* General information (layer, CRS, buffer type, feature count, total area)
* Performance metrics (time, area statistics, distance statistics)
* **ISO 19157:2023 quality indicators** (Completeness, Logical Consistency)
* Geometry integrity detail with expandable ID lists per category
* Parameters used (full configuration table for reproducibility)
* Overlap analysis and fragment statistics (conditional)
* Alerts and warnings log

\---

## Use Cases

|Domain|Application|
|-|-|
|Cadastre / Land management|Setback zones, easements, right-of-way|
|Environmental|Wildlife corridors, buffer zones around protected areas|
|Hydrology|Flood plain modeling with variable width along stream reaches|
|Acoustic / Air quality|Noise or emission impact zones with distance decay|
|Urban planning|Service area analysis, building footprint expansion|
|Electrical infrastructure|Safety exclusion zones along transmission lines|
|Public health|Exposure zones around facilities|

\---

## Known Limitations

* Variable Width (Points) accepts **point layers only**; geometry order in the attribute table determines the route.
* Logical operations (Union, Intersection, Difference, XOR) require **polygon input** or point aggregation (Convex Hull / Bounding Box) active.
* Overlap fragments algorithm is **O(2ⁿ)**; a safety warning is emitted when n > 30 buffers.
* Parallel processing may offer limited gains on small datasets due to thread overhead.
* `fix\_nested\_holes()` is currently inactive; scheduled for activation when GEOS ≥ 3.12 becomes the LTR minimum.

\---

## Changelog

### v1.0.0 — Initial release

* 9 buffer types including Adaptive by Density and Variable Width (Points)
* Full ISO 19157:2023 HTML reporting
* Multi-thread parallel processing
* Dry-run validation mode
* JSON configuration export
* QGIS ≥ 3.28 / Qt5 + Qt6 compatibility

\---

## License

This plugin is released under the **GNU General Public License v2 or later**.
See [LICENSE.txt](LICENSE.txt) for the full text.

\---

## Author

**Jorge Fallas** — [jfallas56@gmail.com](mailto:jfallas56@gmail.com)

Issues and feature requests: [GitHub Issues](https://github.com/jfallas56-CR/complemento-qgis-areas-buffer/issues)

