"""Consent-gated, explainable provisional AOI approximation.

This module does not claim to recover an authoritative boundary. It derives a
candidate polygon from supplied reference layers using deterministic spatial
operations. The assistant may explain the evidence, but the geometry is
reproducible and reviewable. No approximation is created without explicit consent.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


def propose_approximate_boundary(
    reference_layer_paths: Sequence[str],
    *,
    output_path: str,
    permission_granted: bool = False,
    buffer_metres: float = 250.0,
    minimum_features: int = 3,
) -> dict[str, Any]:
    """Create a provisional polygon from supplied reference features.

    Uses the union's convex hull in a locally estimated projected CRS, buffered
    by the requested margin. Inputs must be local, user-authorized reference
    datasets. This is an approximation, not a cadastral or official boundary.
    """
    if permission_granted is not True:
        raise PermissionError(
            "Explicit user permission is required before generating an approximate boundary."
        )
    if not reference_layer_paths:
        raise ValueError("At least one reference layer path is required.")
    if buffer_metres < 0:
        raise ValueError("buffer_metres must be zero or positive.")
    if minimum_features < 1:
        raise ValueError("minimum_features must be at least 1.")

    import geopandas as gpd
    from shapely.ops import unary_union

    frames = []
    input_evidence = []
    for raw_path in reference_layer_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Reference layer does not exist: {path}")
        frame = gpd.read_file(path)
        if frame.empty or frame.crs is None:
            raise ValueError(f"Reference layer is empty or has no CRS: {path}")
        geometry = frame.geometry
        usable = frame.loc[geometry.notna() & ~geometry.is_empty].copy()
        if usable.empty:
            continue
        frames.append(usable)
        input_evidence.append({
            "path": str(path),
            "feature_count": int(len(usable)),
            "crs": str(frame.crs),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })

    if not frames:
        raise ValueError("No usable geometries were found in the reference layers.")

    common_crs = frames[0].crs
    merged = gpd.GeoDataFrame(
        geometry=gpd.GeoSeries(
            [geom for frame in frames for geom in frame.to_crs(common_crs).geometry],
            crs=common_crs,
        ),
        crs=common_crs,
    )
    valid = merged.geometry[merged.geometry.is_valid]
    if valid.empty:
        raise ValueError("No valid reference geometries are available.")
    feature_count = int(len(valid))
    if feature_count < minimum_features:
        raise ValueError(
            f"Only {feature_count} valid reference features found; at least {minimum_features} are required."
        )

    projected_crs = merged.estimate_utm_crs()
    if projected_crs is None:
        raise ValueError("Could not estimate a suitable projected CRS for metric buffering.")
    projected = gpd.GeoSeries(list(valid), crs=common_crs).to_crs(projected_crs)
    source_union = unary_union(projected.tolist())
    hull = source_union.convex_hull
    if hull.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Reference features do not define a polygonal extent.")
    proposed_projected = hull.buffer(float(buffer_metres))
    if proposed_projected.is_empty or not proposed_projected.is_valid:
        raise ValueError("Approximation algorithm produced an empty or invalid polygon.")

    proposed = gpd.GeoDataFrame(
        {
            "boundary_id": ["provisional-aoi"],
            "boundary_status": ["PROVISIONAL_NOT_AUTHORITATIVE"],
            "method": ["convex_hull_plus_metric_buffer"],
            "buffer_metres": [float(buffer_metres)],
            "reference_feature_count": [feature_count],
        },
        geometry=[proposed_projected],
        crs=projected_crs,
    ).to_crs(common_crs)

    destination = Path(output_path).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing approximation: {destination}")
    if destination.suffix.lower() not in {".geojson", ".gpkg"}:
        raise ValueError("Approximation output must use .geojson or .gpkg.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    driver = "GeoJSON" if destination.suffix.lower() == ".geojson" else "GPKG"
    proposed.to_file(destination, driver=driver)

    bounds = [float(value) for value in proposed.total_bounds.tolist()]
    metadata = {
        "schema_version": 1,
        "status": "provisional_approximation",
        "authoritative": False,
        "human_approval_required": True,
        "permission_granted": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "method": "convex_hull_plus_metric_buffer",
        "buffer_metres": float(buffer_metres),
        "feature_count": feature_count,
        "input_evidence": input_evidence,
        "source_crs": str(common_crs),
        "measurement_crs": str(projected_crs),
        "output_crs": str(proposed.crs),
        "bounds": bounds,
        "uncertainty_notes": [
            "Convex hull may include areas not represented by source features.",
            "The buffer distance is an analyst-selected assumption, not a measured boundary uncertainty.",
            "Input feature coverage and completeness are unknown unless independently established.",
            "This output is not an authoritative settlement, administrative or project AOI boundary.",
        ],
        "approval": None,
        "gold_release_allowed": False,
    }
    sidecar = destination.with_suffix(destination.suffix + ".metadata.json")
    if sidecar.exists():
        destination.unlink(missing_ok=True)
        raise FileExistsError(f"Refusing to overwrite existing metadata: {sidecar}")
    sidecar.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "status": "provisional_approximation",
        "path": str(destination),
        "metadata_path": str(sidecar),
        "feature_count": feature_count,
        "bounds": bounds,
        "crs": str(proposed.crs),
        "method": metadata["method"],
        "human_approval_required": True,
        "gold_release_allowed": False,
        "uncertainty_notes": metadata["uncertainty_notes"],
        "input_evidence": input_evidence,
    }
