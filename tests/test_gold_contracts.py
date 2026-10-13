import json
from pathlib import Path

import pytest

from tomorrowcities.services.gold_contracts import (
    validate_gold_artifact,
    validate_gold_geojson,
    validate_distribution_workbook,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "name",
    [
        "exclusion_lines.geojson",
        "exclusion_points.geojson",
        "exclusion_polygons.geojson",
        "sample_landuse_file.geojson",
    ],
)
def test_sample_geojsons_match_initial_gold_contract(name):
    payload = (ROOT / name).read_bytes()
    result = validate_gold_geojson(name, payload)
    assert result["valid"], result["errors"]
    assert result["feature_count"] > 0


def test_distribution_workbook_contains_expected_sheets():
    payload = (ROOT / "sample_distribution_file.xlsx").read_bytes()
    result = validate_distribution_workbook(payload)
    assert result["valid"], result["missing_required_sheets"]


def test_rejects_unregistered_artifact():
    with pytest.raises(ValueError, match="Unsupported Gold artefact"):
        validate_gold_artifact("unregistered.geojson", b"{}")


def test_geojson_required_property_is_reported():
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
    assert any("missing required properties ['id']" in error for error in result["errors"])
