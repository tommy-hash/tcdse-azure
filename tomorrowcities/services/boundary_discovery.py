"""Discover and assess candidate area-of-interest boundaries.

Storage-provider agnostic: callers supply an inventory discovered in ADLS or
another configured data lake. Ranking is triage evidence, never approval.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

BOUNDARY_EXTENSIONS = {".gpkg", ".shp", ".geojson", ".json", ".kml", ".gml", ".fgb", ".parquet"}
BOUNDARY_NAME_TERMS = {
    "aoi", "area_of_interest", "area-of-interest", "boundary", "boundaries",
    "study_area", "study-area", "project_extent", "settlement_boundary",
    "site_boundary", "kalobeyei_boundary", "kakuma_boundary",
}
BOUNDARY_CONTEXT_TERMS = {"kalobeyei", "kakuma", "turkana", "tcdse", "tomorrowscities"}
MANIFEST_EXTENSIONS = {".yaml", ".yml", ".csv", ".txt", ".md"}


@dataclass(frozen=True)
class BoundaryCandidate:
    path: str
    filename: str
    extension: str
    score: int
    reasons: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    evidence_status: str = "unverified"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["reasons"] = list(self.reasons)
        result["metadata"] = dict(self.metadata)
        return result


@dataclass(frozen=True)
class DiscoveryResult:
    status: str
    candidates: tuple[BoundaryCandidate, ...]
    searched_locations: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    requires_human_review: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "searched_locations": list(self.searched_locations),
            "warnings": list(self.warnings),
            "requires_human_review": self.requires_human_review,
        }


def _normalise_path(path: str) -> str:
    """Normalise separators and reject unsafe/non-relative inventory paths."""
    value = str(path or "").replace(chr(92), "/").strip()
    if not value or value.startswith("/") or ":" in value:
        return ""
    parts = PurePosixPath(value).parts
    if any(part in {"..", "."} for part in parts):
        return ""
    return "/".join(parts)


def _text_metadata(item: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in ("path", "name", "filename", "description", "title", "tags", "project", "dataset"):
        value = item.get(key)
        if value is not None:
            if isinstance(value, (list, tuple, set)):
                values.extend(str(entry).lower() for entry in value)
            else:
                values.append(str(value).lower())
    return " ".join(values)


def _score_candidate(item: Mapping[str, Any], path: str) -> tuple[int, list[str]]:
    """Rank candidates by evidence; a score is not proof of AOI authority."""
    text = _text_metadata({**item, "path": path})
    filename = PurePosixPath(path).name.lower()
    suffix = PurePosixPath(path).suffix.lower()
    score = 0
    reasons: list[str] = []

    if suffix in BOUNDARY_EXTENSIONS:
        score += 2
        reasons.append(f"supported geospatial format ({suffix})")
    if any(term in filename for term in BOUNDARY_NAME_TERMS):
        score += 5
        reasons.append("filename suggests a boundary or AOI")
    if any(term in text for term in BOUNDARY_CONTEXT_TERMS):
        score += 2
        reasons.append("metadata/path matches project or geographic context")
    if any(term in text for term in ("boundary", "extent", "study area", "area of interest", "aoi")):
        score += 2
        reasons.append("metadata describes boundary/extent purpose")
    if str(item.get("geometry_type", "")).lower() in {"polygon", "multipolygon"}:
        score += 3
        reasons.append("inventory reports polygon geometry")
    if item.get("crs") or item.get("crs_authority"):
        score += 1
        reasons.append("CRS metadata is available")
    if item.get("feature_count") is not None:
        reasons.append("feature-count metadata is available")
    if suffix in MANIFEST_EXTENSIONS and not any(term in text for term in BOUNDARY_NAME_TERMS):
        score = 0
        reasons.append("metadata file is not itself treated as a boundary candidate")
    return score, reasons


def discover_boundary_candidates(
    inventory: Iterable[Mapping[str, Any]],
    *,
    searched_locations: Sequence[str] = (),
    minimum_score: int = 4,
    limit: int = 20,
) -> DiscoveryResult:
    """Rank possible AOI objects from an inventory returned by a storage adapter.

    Inventory items may include path, name, description, project, dataset,
    geometry_type, crs, crs_authority, feature_count, extent, modified_at,
    size_bytes and source. This function does not access storage, inspect actual
    geometry, generate a boundary, or approve a candidate.
    """
    if minimum_score < 0:
        raise ValueError("minimum_score must be non-negative.")
    if limit < 1:
        raise ValueError("limit must be at least 1.")

    candidates: list[BoundaryCandidate] = []
    warnings: list[str] = []
    seen_paths: set[str] = set()

    for item in inventory:
        raw_path = str(item.get("path") or item.get("name") or item.get("filename") or "")
        path = _normalise_path(raw_path)
        if not path:
            warnings.append(f"Skipped unsafe or empty inventory path: {raw_path!r}.")
            continue
        if path in seen_paths:
            warnings.append(f"Skipped duplicate inventory path: {path}.")
            continue
        seen_paths.add(path)

        score, reasons = _score_candidate(item, path)
        if score < minimum_score:
            continue
        suffix = PurePosixPath(path).suffix.lower()
        candidates.append(BoundaryCandidate(
            path=path,
            filename=PurePosixPath(path).name,
            extension=suffix,
            score=score,
            reasons=tuple(reasons),
            metadata={
                key: item[key]
                for key in ("geometry_type", "crs", "crs_authority", "feature_count", "extent", "modified_at", "size_bytes", "source")
                if key in item
            },
        ))

    candidates.sort(key=lambda candidate: (-candidate.score, candidate.path.lower()))
    candidates = candidates[:limit]
    if candidates:
        status = "candidates_found"
        warnings.append(
            "Ranking is discovery evidence only. Validate geometry, CRS, coverage, "
            "lineage and intended AOI before selecting a candidate."
        )
    else:
        status = "no_candidate_found"
        warnings.append(
            "No candidate met the discovery threshold. This does not prove no boundary "
            "exists outside the searched locations or inventory."
        )
    return DiscoveryResult(
        status=status,
        candidates=tuple(candidates),
        searched_locations=tuple(searched_locations),
        warnings=tuple(warnings),
        requires_human_review=True,
    )


def validate_boundary_selection(
    candidate: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate validation evidence from a geometry-aware adapter; never approve."""
    required_checks = ("geometry_valid", "crs_known", "coverage_checked", "lineage_checked")
    missing = [key for key in required_checks if key not in validation]
    failed = [key for key in required_checks if key in validation and validation[key] is not True]
    eligible = not missing and not failed
    return {
        "candidate_path": candidate.get("path"),
        "status": "eligible_for_human_approval" if eligible else "validation_blocked",
        "eligible_for_human_approval": eligible,
        "checks": {key: validation.get(key) for key in required_checks},
        "missing_checks": missing,
        "failed_checks": failed,
        "approved": False,
        "note": "Passing technical checks does not prove this is the authoritative project AOI.",
    }


def evaluate_approximation_permission(
    *,
    user_permission: bool,
    purpose: str,
    reference_sources: Sequence[str] = (),
) -> dict[str, Any]:
    """Gate a future AI approximation workflow on explicit permission."""
    clean_purpose = purpose.strip()
    sources = [source.strip() for source in reference_sources if source.strip()]
    if not user_permission:
        return {
            "status": "permission_required",
            "may_generate_approximation": False,
            "purpose": clean_purpose,
            "reference_sources": sources,
            "message": "Ask the user for explicit permission before generating an approximate AOI.",
        }
    if not clean_purpose:
        return {
            "status": "purpose_required",
            "may_generate_approximation": False,
            "purpose": "",
            "reference_sources": sources,
            "message": "Record the intended use of the approximation before proceeding.",
        }
    return {
        "status": "permission_granted_for_proposal",
        "may_generate_approximation": True,
        "purpose": clean_purpose,
        "reference_sources": sources,
        "message": (
            "Permission allows creation of a provisional proposal only. Label it approximate; "
            "do not publish to Gold until human approval."
        ),
        "gold_release_allowed": False,
    }
