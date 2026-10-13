"""Initial Gold-output contracts for the TCDSE harmonisation engine.

This module validates the shapes of the sample Gold artefacts supplied for the
project. It deliberately does not infer missing domain values or silently
coerce source fields into target fields.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

import pandas as pd

GOLD_GEOJSON_CONTRACTS = {
    "exclusion_lines.geojson": {
        "geometry_types": {"LineString", "MultiLineString"},
        "required_properties": {"id", "setback"},
    },
    "exclusion_points.geojson": {
        "geometry_types": {"Point", "MultiPoint"},
        "required_properties": {"id", "setback"},
    },
    "exclusion_polygons.geojson": {
        "geometry_types": {"Polygon", "MultiPolygon"},
        "required_properties": {"id", "setback"},
        "optional_properties": {"population"},
    },
    "sample_landuse_file.geojson": {
        "geometry_types": {"Polygon", "MultiPolygon"},
        "required_properties": {
            "zoneid", "luf", "population", "avgincome", "setback", "densitycap"
        },
    },
}

DISTRIBUTION_WORKBOOK = "sample_distribution_file.xlsx"
DISTRIBUTION_REQUIRED_SHEETS = {
    "Introduction",
    "Nomenclature",
    "General Inputs",
    "Tables 1-6,9,12,13",
    "Tables 7,8,11",
    "Tables 10,14",
}


def _feature_collection(payload: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("GeoJSON must be valid UTF-8 JSON.") from exc
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        raise ValueError("Expected a GeoJSON FeatureCollection.")
    if not isinstance(obj.get("features"), list):
        raise ValueError("GeoJSON FeatureCollection must contain a features array.")
    return obj


def validate_gold_geojson(filename: str, payload: bytes) -> dict[str, Any]:
    """Validate a sample Gold GeoJSON contract without modifying the input."""
    name = PurePosixPath(filename).name
    contract = GOLD_GEOJSON_CONTRACTS.get(name)
    if contract is None:
        raise ValueError(f"No registered Gold GeoJSON contract for {name!r}.")

    collection = _feature_collection(payload)
    errors: list[str] = []
    geometry_types: set[str] = set()
    property_keys: set[str] = set()
    feature_count = len(collection["features"])

    for index, feature in enumerate(collection["features"]):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            errors.append(f"Feature {index}: expected type Feature.")
            continue
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            errors.append(f"Feature {index}: properties must be an object.")
            properties = {}
        property_keys.update(str(key) for key in properties)
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            errors.append(f"Feature {index}: geometry is missing or invalid.")
            continue
        geometry_type = geometry.get("type")
        if isinstance(geometry_type, str):
            geometry_types.add(geometry_type)
            if geometry_type not in contract["geometry_types"]:
                errors.append(
                    f"Feature {index}: geometry type {geometry_type!r} is not allowed "
                    f"for {name}."
                )
        else:
            errors.append(f"Feature {index}: geometry type is missing.")

        missing = contract["required_properties"] - set(properties)
        if missing:
            errors.append(
                f"Feature {index}: missing required properties {sorted(missing)}."
            )

    return {
        "filename": name,
        "valid": not errors,
        "feature_count": feature_count,
        "geometry_types": sorted(geometry_types),
        "property_keys": sorted(property_keys),
        "errors": errors,
        "source_crs": (collection.get("crs") or {}).get("properties", {}).get("name"),
        "note": "Validation only; no field mapping or geometry transformation was applied.",
    }


def validate_distribution_workbook(payload: bytes) -> dict[str, Any]:
    """Validate workbook sheet presence while preserving its original table layout."""
    from io import BytesIO

    try:
        workbook = pd.ExcelFile(BytesIO(payload))
    except Exception as exc:
        raise ValueError("Distribution workbook could not be opened as an Excel file.") from exc

    actual = set(workbook.sheet_names)
    missing = sorted(DISTRIBUTION_REQUIRED_SHEETS - actual)
    return {
        "filename": DISTRIBUTION_WORKBOOK,
        "valid": not missing,
        "sheet_names": workbook.sheet_names,
        "missing_required_sheets": missing,
        "note": (
            "Sheet-presence validation only. Cell-level distribution rules must be "
            "implemented from the DSE specification before values are transformed."
        ),
    }


def validate_gold_artifact(filename: str, payload: bytes) -> dict[str, Any]:
    """Route a supplied sample Gold artifact to its registered validator."""
    name = PurePosixPath(filename).name
    if name in GOLD_GEOJSON_CONTRACTS:
        return validate_gold_geojson(name, payload)
    if name.lower() == DISTRIBUTION_WORKBOOK:
        return validate_distribution_workbook(payload)
    raise ValueError(f"Unsupported Gold artefact: {name!r}.")
