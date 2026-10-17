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
