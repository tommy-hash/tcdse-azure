import json
from io import BytesIO

import openpyxl
import pytest

from tomorrowcities.services.gold_contracts import (
    validate_gold_artifact,
    validate_gold_geojson,
    validate_distribution_workbook,
)


@pytest.mark.parametrize(
    ("filename", "geometry_type", "properties"),
    [
        ("exclusion_lines.geojson", "MultiLineString", {"id": 1, "setback": 5}),
        ("exclusion_points.geojson", "Point", {"id": 1, "setback": 0}),
        ("exclusion_polygons.geojson", "MultiPolygon", {"id": 1, "setback": 0, "population": 100}),
        (
            "sample_landuse_file.geojson",
            "MultiPolygon",
            {
                "zoneid": 1,
                "luf": "Low Density Residential Area",
                "population": 0,
                "avgincome": "highIncome",
                "setback": 5.0,
                "densitycap": 50.0,
            },
        ),
    ],
)
def test_geojson_gold_contract(filename, geometry_type, properties):
    geometry = {
        "type": geometry_type,
        "coordinates": (
            [[[0, 0], [1, 1]]]
            if geometry_type == "MultiLineString"
            else ([[[0, 0]]] if geometry_type == "MultiPolygon" else [36.8, -1.2])
        ),
    }
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
            "features": [{"type": "Feature", "properties": properties, "geometry": geometry}],
        }
    ).encode()
    result = validate_gold_geojson(filename, payload)
    assert result["valid"], result["errors"]
    assert result["feature_count"] == 1


def test_missing_required_property_is_reported():
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"setback": 5},
                    "geometry": {"type": "Point", "coordinates": [36.8, -1.2]},
                }
            ],
        }
    ).encode()
    result = validate_gold_geojson("exclusion_points.geojson", payload)
    assert not result["valid"]
    assert any("missing required properties" in error for error in result["errors"])


def test_distribution_workbook_required_sheets():
    workbook = openpyxl.Workbook()
    workbook.active.title = "Introduction"
    for sheet_name in [
        "Nomenclature",
        "General Inputs",
        "Tables 1-6,9,12,13",
        "Tables 7,8,11",
        "Tables 10,14",
    ]:
        workbook.create_sheet(sheet_name)
    stream = BytesIO()
    workbook.save(stream)
    result = validate_distribution_workbook(stream.getvalue())
    assert result["valid"], result["missing_required_sheets"]


def test_unknown_gold_artifact_is_rejected():
    with pytest.raises(ValueError, match="Unsupported Gold artefact"):
        validate_gold_artifact("other.geojson", b"{}")
