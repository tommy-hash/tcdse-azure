# Spatial exception human review

## Purpose
Spatial features that fail a defined spatial quality rule must not be silently
removed or included in Gold. The reusable review service records a pending
exception, accepts a reviewer decision (\`retain\`, \`exclude\`, or \`defer\`), and
fails closed while any exception is pending or deferred.

## Decisions
- **Retain**: keep the source feature in the Gold candidate, with the decision
  and rationale retained in the review record.
- **Exclude**: omit the feature from the Gold candidate. The Silver source is
  never modified.
- **Defer**: leave the exception unresolved; Gold preparation is blocked.

Every decision requires a reviewer identifier and rationale. Review records
contain only an allowlist of evidence metadata, not arbitrary source attributes.
Records and decisions are written as create-only JSON blobs under
\`silver/kalobeyei/review/spatial_exceptions/<review-id>/\`. A conflicting
second decision cannot overwrite the first. The store is designed for use with
an authenticated Azure Blob container client.

## Feature identity and provenance
Do not use a source field that is constant or non-unique (for example the
land-use layer's \`Id\` field). Supply a stable \`source_feature_key\`, the source
object path, and the SHA-256 of the source object. A row index may be recorded
as supporting evidence but is not a durable key if the source is reordered.
For the current one-off source review, \`row:325\` identifies the observed
outlier only relative to the exact source-file hash. For a production Gold
build, derive stable keys from a trusted source identifier or a documented
geometry/attribute fingerprint and preserve the source hash.

## Gold integration
Call \`evaluate_spatial_exception_gate\` before Gold generation. Call
\`apply_spatial_exception_decisions\` on a copy of the source frame using the
stable key column. It raises if any exception is pending/deferred, or if
conflicting review records exist. It does not alter the source frame.
This gate is one control only; all other Gold contracts, geometry checks,
CRS checks, domain validation and release approvals remain mandatory.

## Current Kalobeyei land-use case
The outside feature is pending review. The reported row index 325 is not a
durable identifier by itself; bind it to the SHA-256 of the exact source
shapefile component set before recording the review. Do not infer that its
coordinates prove a CRS error without checking source metadata and provenance.
