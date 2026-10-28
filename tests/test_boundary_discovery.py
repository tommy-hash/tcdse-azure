import pytest

from tomorrowcities.services.boundary_discovery import (
    discover_boundary_candidates,
    evaluate_approximation_permission,
    validate_boundary_selection,
)


def test_discovery_ranks_boundary_candidates_and_never_approves():
    result = discover_boundary_candidates(
        [
            {
                "path": "bronze/kalobeyei/raw/kalobeyei_boundary.gpkg",
                "geometry_type": "MultiPolygon",
                "crs": "EPSG:4326",
                "feature_count": 1,
            },
            {"path": "bronze/kalobeyei/raw/buildings.gpkg", "geometry_type": "Polygon"},
            {"path": "bronze/other/readme.md", "description": "notes"},
        ],
        searched_locations=["bronze/kalobeyei/raw/"],
    )
    assert result.status == "candidates_found"
    assert result.candidates[0].filename == "kalobeyei_boundary.gpkg"
    assert result.requires_human_review is True
    assert result.candidates[0].evidence_status == "unverified"


def test_discovery_skips_unsafe_and_duplicate_paths():
    result = discover_boundary_candidates([
        {"path": "../boundary.gpkg"},
        {"path": "/absolute/boundary.gpkg"},
        {"path": "bronze/boundary.gpkg", "geometry_type": "Polygon"},
        {"path": "bronze/boundary.gpkg", "geometry_type": "Polygon"},
    ])
    assert len(result.candidates) == 1
    assert any("unsafe" in warning for warning in result.warnings)
    assert any("duplicate" in warning for warning in result.warnings)


def test_discovery_returns_no_candidate_when_inventory_has_no_boundary():
    result = discover_boundary_candidates([{"path": "bronze/roads.gpkg", "geometry_type": "LineString"}])
    assert result.status == "no_candidate_found"
    assert result.candidates == ()
    assert result.requires_human_review is True


def test_validation_requires_all_checks_and_never_approves():
    candidate = {"path": "bronze/kalobeyei/raw/aoi.gpkg"}
    blocked = validate_boundary_selection(candidate, {"geometry_valid": True, "crs_known": True})
    assert blocked["status"] == "validation_blocked"
    assert not blocked["approved"]
    passed = validate_boundary_selection(candidate, {
        "geometry_valid": True,
        "crs_known": True,
        "coverage_checked": True,
        "lineage_checked": True,
    })
    assert passed["status"] == "eligible_for_human_approval"
    assert passed["eligible_for_human_approval"]
    assert not passed["approved"]


def test_approximation_requires_explicit_permission_and_purpose():
    denied = evaluate_approximation_permission(user_permission=False, purpose="study area")
    assert not denied["may_generate_approximation"]
    missing_purpose = evaluate_approximation_permission(user_permission=True, purpose=" ")
    assert not missing_purpose["may_generate_approximation"]
    allowed = evaluate_approximation_permission(
        user_permission=True,
        purpose="Provisional coverage for internal review",
        reference_sources=["buildings.gpkg"],
    )
    assert allowed["may_generate_approximation"]
    assert allowed["gold_release_allowed"] is False


def test_discovery_rejects_invalid_limits():
    with pytest.raises(ValueError):
        discover_boundary_candidates([], minimum_score=-1)
    with pytest.raises(ValueError):
        discover_boundary_candidates([], limit=0)
