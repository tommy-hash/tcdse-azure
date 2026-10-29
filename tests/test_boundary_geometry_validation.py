
import pytest


def test_inspect_boundary_geometry_reports_valid_polygon_and_requires_lineage_review(tmp_path):
    gpd = pytest.importorskip("geopandas")
    shapely_geometry = pytest.importorskip("shapely.geometry")
    from tomorrowcities.services.boundary_discovery import inspect_boundary_geometry

    boundary_path = tmp_path / "aoi.geojson"
    frame = gpd.GeoDataFrame(
        {"boundary_id": ["aoi-1"]},
        geometry=[shapely_geometry.box(34.8, 3.6, 34.9, 3.7)],
        crs="EPSG:4326",
    )
    frame.to_file(boundary_path, driver="GeoJSON")
    result = inspect_boundary_geometry(str(boundary_path))
    assert result["status"] == "inspected"
    assert result["geometry_valid"]
    assert result["crs_known"]
    assert result["geometry_types"] == ["Polygon"]
    assert result["coverage_checked"] is False
    assert result["lineage_checked"] is False
    assert result["approved"] is False


def test_inspect_boundary_geometry_reports_missing_file(tmp_path):
    from tomorrowcities.services.boundary_discovery import inspect_boundary_geometry

    with pytest.raises(FileNotFoundError):
        inspect_boundary_geometry(str(tmp_path / "missing.gpkg"))
