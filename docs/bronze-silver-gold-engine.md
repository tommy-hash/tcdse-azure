# TCDSE Bronze → Silver → Gold harmonisation engine

## Goal

The engine converts source datasets into validated Gold artefacts accepted by the
TCDSE/DSE workflow. The uploaded sample files define the initial output contract;
they are not source data and should not be used as production records.

The **Novice Urban Planner** is the user-facing planning agent. It delegates complex
harmonisation jobs to a separate Harmonisation Orchestrator and ML-assisted engine,
then receives job status, approved outputs and review exceptions.

## Initial Gold artefacts

| Output | Contract from supplied sample | Initial validation |
|---|---|---|
| `exclusion_lines.geojson` | GeoJSON FeatureCollection; line geometries; each feature has `id` and `setback` | Geometry type and required properties |
| `exclusion_points.geojson` | GeoJSON FeatureCollection; point geometries; each feature has `id` and `setback` | Geometry type and required properties |
| `exclusion_polygons.geojson` | GeoJSON FeatureCollection; polygon geometries; each feature has `id` and `setback`; sample also has `population` | Geometry type and required properties |
| `sample_landuse_file.geojson` | GeoJSON FeatureCollection; polygon geometries; properties `zoneid`, `luf`, `population`, `avgincome`, `setback`, `densitycap` | Geometry type and required properties |
| `sample_distribution_file.xlsx` | Workbook with sheets: Introduction, Nomenclature, General Inputs, Tables 1-6,9,12,13, Tables 7,8,11, Tables 10,14 | Required sheet presence |

The supplied GeoJSON examples declare CRS84 (longitude, latitude). The engine must
inspect each real input's CRS and coordinates and must not assume every future source
uses the same CRS. The samples include 4 line features, 6 point features, 2 polygon
features and 9 land-use features. These are examples of the output contract, not
production coverage or ground-truth data.

## Azure storage layout

Use the existing private ADLS Gen2 account and filesystem configured for TCDSE
(the current project notes identify storage account `tcdse2026` and filesystem
`tcdse`; verify these names against the active subscription before deployment):

- `bronze/kalobeyei/raw/` — original package, extracted source files and Access exports
- `bronze/kalobeyei/manifests/` — source inventory, hashes and import metadata
- `silver/kalobeyei/profiled/` — format inventory, schemas and data-quality reports
- `silver/kalobeyei/normalised/` — parsed layers with standardised field names and CRS metadata
- `silver/kalobeyei/review/` — unresolved mappings, geometry issues and human-review queue
- `gold/tcdse/kalobeyei/<job-id>/` — validated output files
- `gold/tcdse/kalobeyei/<job-id>/manifest.json` — outputs, source lineage, transformations, validation and review status

Do not enable public/anonymous access. Use managed identity and least-privilege RBAC
for the Container App or processing service. Never put storage keys in source code.

## Job flow

1. The Novice Urban Planner submits a job with source object paths and a target profile.
2. The orchestrator creates a job ID and records state: queued, profiling,
   harmonising, validating, awaiting_review, completed or failed.
3. Ingestion adapters parse supported inputs and write a source inventory to Silver.
4. Rules perform safe, deterministic normalisation. ML can propose field mappings,
   entity matches and spatial relationships with evidence and confidence.
5. Contract and data-quality validation runs before publishing.
6. Ambiguous or consequential changes are held for human review; raw inputs are
   immutable and every transformation is recorded.
7. Only validated/approved files are written to Gold, with a manifest and status
   returned to the Novice Urban Planner.

## Boundary discovery and approval gate

The initial boundary-discovery service is in
`tomorrowcities/services/boundary_discovery.py`. It ranks candidate paths from a
storage-adapter inventory using filename, project/geographic context, geometry type
and CRS metadata. Ranking is only a search aid: candidates remain unverified and
always require review. The service also checks whether required validation evidence
has been supplied and gates a future approximation workflow on explicit permission.

The service now includes a read-only ADLS inventory adapter using `DefaultAzureCredential`
and `TCDSE_ADLS_ACCOUNT_URL` / `TCDSE_ADLS_FILESYSTEM`, plus configurable
`TCDSE_BOUNDARY_SEARCH_PREFIXES`. It also includes `inspect_boundary_geometry()` for
local candidate files: geometry validity, polygonal type, CRS, bounds and optional
intersection checks against supplied source layers. This inspector does not download
ADLS objects; the caller must stage a candidate locally first. It also cannot establish
authoritative AOI identity by itself, so lineage remains a separate review requirement.

If discovery finds no usable boundary, the interactive assistant should ask permission
before proposing a provisional AOI. An approximate AOI must not be published to Gold
until the required human approval is recorded. Persisted review records, direct ADLS
candidate staging, UI integration and AI polygon generation remain future work.

## Current implementation boundary

The initial code registers and validates the sample Gold output contracts. It does
not yet connect to ADLS, transform Kalobeyei source layers or publish Gold files.
Before production transformation rules are written, inspect actual source attributes
and the DSE engine's field-level rules, especially the distribution workbook's cell
layouts and any constraints not represented by sheet names.

Do not fabricate population, income, building classes, vulnerability values or other
missing real-world data. Keep source files immutable and attach lineage to every
derived output.
