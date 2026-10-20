import pytest

from tomorrowcities.services.silver_pipeline import _classify, _safe_member


@pytest.mark.parametrize("path", ["../escape.shp", "/absolute/path.shp", "C:\\temp\\bad.shp"])
def test_safe_member_rejects_traversal_and_absolute_paths(path):
    assert _safe_member(path) is None


def test_safe_member_normalizes_package_paths():
    assert _safe_member("folder\\layer.shp") == "folder/layer.shp"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("layer.shp", "esri_shapefile_geometry"),
        ("layer.dbf", "attribute_table"),
        ("layer.prj", "coordinate_reference_system"),
        ("plan.pdf", "document"),
        ("database.accdb", "access_database"),
        ("map.mpkx", "arcgis_map_package"),
    ],
)
def test_classifies_common_kalobeyei_sources(name, expected):
    assert _classify(name) == expected


from tomorrowcities.services.silver_pipeline import _parse_ogrinfo_summary


def test_parse_ogrinfo_summary_extracts_fields_and_metadata():
    output = '''INFO: Open of "roads.shp"
Layer name: roads
Geometry: Line String
Feature Count: 802
Layer SRS WKT:
PROJCRS["WGS 84 / UTM zone 36S",
    ID["EPSG", 32736]]
road_id: Integer (9.0)
road_name: String (80.0)
length_m: Real (12.3)
'''
    parsed = _parse_ogrinfo_summary(output)
    assert parsed["feature_count"] == 802
    assert parsed["geometry_type"] == "Line String"
    assert parsed["crs_authority"] == "EPSG:32736"
    assert [field["name"] for field in parsed["fields"]] == ["road_id", "road_name", "length_m"]


def test_parse_ogrinfo_summary_does_not_treat_geometry_or_crs_as_fields():
    output = '''Geometry: Polygon
Feature Count: 1
Layer SRS WKT:
GEOGCRS["WGS 84", ID["EPSG", 4326]]
'''
    parsed = _parse_ogrinfo_summary(output)
    assert parsed["fields"] == []
    assert parsed["geometry_type"] == "Polygon"
    assert parsed["crs_authority"] == "EPSG:4326"


from tomorrowcities.services.silver_pipeline import _gold_readiness_matrix


def test_gold_readiness_matrix_keeps_candidates_unapproved():
    layers = [
        {"layer_path": "commondata/buildings.shp", "geometry_type": "Polygon", "feature_count": 10, "field_count": 4, "inspection_status": "inspected", "qa_status": "passed_initial_checks"},
        {"layer_path": "commondata/roads.shp", "geometry_type": "Line String", "feature_count": 20, "field_count": 3, "inspection_status": "inspected", "qa_status": "review_warnings"},
    ]
    matrix = _gold_readiness_matrix(layers)
    polygons = next(item for item in matrix if item["gold_artifact"] == "exclusion_polygons.geojson")
    lines = next(item for item in matrix if item["gold_artifact"] == "exclusion_lines.geojson")
    assert polygons["candidate_count"] == 1
    assert lines["candidate_count"] == 1
    assert polygons["readiness"] == "not_ready"
    assert polygons["decision"] == "human_review_required"


from tomorrowcities.services.silver_pipeline import _quality_resolution_actions


def test_quality_resolution_actions_never_auto_repairs_or_deletes():
    record = {
        "identifier_checks": [{
            "field": "Id",
            "duplicate_non_null_count": 2,
            "duplicate_values_sample": [{"value": "7", "occurrences": 3}],
            "null_count": 0,
        }],
        "geometry": {
            "invalid_count": 1,
            "invalid_feature_indices_sample": ["4"],
            "types": {"Polygon": 8, "MultiPolygon": 2},
        },
        "crs": {"present": False},
    }
    actions = _quality_resolution_actions(record)
    issues = {action["issue"] for action in actions}
    assert {"repeated_identifier", "invalid_geometry", "mixed_geometry_types", "missing_crs"} <= issues
    assert all(action["status"] == "requires_human_review" for action in actions)
    assert all("automatically" in action["recommended_action"] or "only" in action["recommended_action"] or "do not" in action["recommended_action"] for action in actions)


def test_quality_resolution_actions_empty_for_clean_record():
    assert _quality_resolution_actions({
        "identifier_checks": [],
        "geometry": {"invalid_count": 0, "types": {"Polygon": 4}},
        "crs": {"present": True},
    }) == []


from tomorrowcities.services.silver_pipeline import _parse_ogrinfo_summary, _quality_resolution_actions


def test_placeholder_id_assessment_is_exposed_by_quality_action():
    action = _quality_resolution_actions({
        "identifier_checks": [{
            "field": "Id",
            "duplicate_non_null_count": 26,
            "constant_placeholder_suspected": True,
            "distinct_non_null_value_count": 1,
            "duplicate_values_sample": [{"value": "0", "occurrences": 27}],
            "affected_feature_indices_sample": ["0", "1", "2"],
            "null_count": 0,
        }],
        "geometry": {"invalid_count": 0, "types": {"LineString": 27}},
        "crs": {"present": True},
    })[0]
    assert action["issue"] == "constant_placeholder_identifier"
    assert "do not delete records" in action["recommended_action"]
    assert "separate derived row key" in action["recommended_action"]


def test_geometry_type_family_normalization_concept():
    # Single and multi geometries can share a base family, while polygon/line is mixed family.
    def base_family(value):
        if "LineString" in value or value == "Line":
            return "Line"
        if "Polygon" in value:
            return "Polygon"
        if "Point" in value:
            return "Point"
        return value
    assert len({base_family("Polygon"), base_family("MultiPolygon")}) == 1
    assert len({base_family("LineString"), base_family("MultiLineString")}) == 1
    assert len({base_family("Polygon"), base_family("LineString")}) == 2
