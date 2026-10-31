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

The service is intentionally storage-provider agnostic. It does not yet scan ADLS,
open and validate geospatial files, generate approximate polygons, or write review
decisions to a persistent queue. The next integration work is to add an ADLS inventory
adapter using managed identity, a geometry validation adapter, and persisted review
records. If discovery finds no usable boundary, the interactive assistant should ask
permission before proposing a provisional AOI; an approximate AOI must not be published
to Gold until the required human approval is recorded.

## Current implementation boundary

The initial code registers and validates the sample Gold output contracts. It does
not yet connect to ADLS, transform Kalobeyei source layers or publish Gold files.
Before production transformation rules are written, inspect actual source attributes
and the DSE engine's field-level rules, especially the distribution workbook's cell
layouts and any constraints not represented by sheet names.

Do not fabricate population, income, building classes, vulnerability values or other
missing real-world data. Keep source files immutable and attach lineage to every
derived output.


## Consent-gated provisional boundary approximation

tomorrowcities/services/boundary_approximation.py provides a deterministic provisional-AOI method. It requires permission_granted=True, one or more local reference layers with a known CRS, and at least three valid geometries by default. It projects the references to an estimated UTM CRS, computes their union's convex hull, applies an explicitly configured metric buffer, and writes a new GeoJSON or GeoPackage plus a metadata sidecar.

The output is always marked PROVISIONAL_NOT_AUTHORITATIVE; metadata records input paths, hashes, feature counts, CRS, method, buffer distance, bounds and uncertainty notes. Existing outputs are not overwritten. The operation does not approve the boundary or permit Gold release.

The boundary review page exposes this operation only after the user checks an explicit permission control. Reference paths must already be available locally in the application environment. This increment is an AI-assisted workflow foundation, not a generative-AI polygon model: the geometry itself is generated deterministically so that it is reproducible and auditable. An AI explanation/ranking layer can be added later, but must not invent authoritative boundaries or bypass human review.

Before production use, validate reference completeness and the chosen buffer with domain experts, protect the page with authenticated access, and run tests in the deployment environment. A convex hull can include areas unsupported by source features and must not be treated as an administrative or settlement boundary.
