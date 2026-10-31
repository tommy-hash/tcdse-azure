
import json
from pathlib import Path

import pytest

from tomorrowcities.services.boundary_approximation import propose_approximate_boundary


def test_requires_explicit_permission(tmp_path):
    with pytest.raises(PermissionError):
        propose_approximate_boundary(
            [],
            output_path=str(tmp_path / "provisional.geojson"),
            permission_granted=False,
        )


def test_creates_provisional_boundary_and_metadata_from_reference_layer(tmp_path):
    gpd = pytest.importorskip("geopandas")
    geometry = pytest.importorskip("shapely.geometry")

    source = tmp_path / "reference.geojson"
    frame = gpd.GeoDataFrame(
        {"id": [1, 2, 3, 4]},
        geometry=[
            geometry.Point(34.8, 3.6),
            geometry.Point(34.81, 3.6),
            geometry.Point(34.81, 3.61),
            geometry.Point(34.8, 3.61),
        ],
        crs="EPSG:4326",
    )
    frame.to_file(source, driver="GeoJSON")

    output = tmp_path / "provisional.geojson"
    result = propose_approximate_boundary(
        [str(source)],
        output_path=str(output),
        permission_granted=True,
        buffer_metres=100,
        minimum_features=3,
    )
    assert output.exists()
    assert Path(result["metadata_path"]).exists()
    metadata = json.loads(Path(result["metadata_path"]).read_text())
    assert metadata["authoritative"] is False
    assert metadata["human_approval_required"] is True
    assert metadata["gold_release_allowed"] is False
    assert result["status"] == "provisional_approximation"


def test_refuses_to_overwrite_existing_output(tmp_path):
    gpd = pytest.importorskip("geopandas")
    geometry = pytest.importorskip("shapely.geometry")
    source = tmp_path / "reference.geojson"
    gpd.GeoDataFrame(
        {"id": [1, 2, 3]},
        geometry=[geometry.Point(34.8, 3.6), geometry.Point(34.81, 3.6), geometry.Point(34.8, 3.61)],
        crs="EPSG:4326",
    ).to_file(source, driver="GeoJSON")
    output = tmp_path / "existing.geojson"
    output.write_text("do not overwrite")
    with pytest.raises(FileExistsError):
        propose_approximate_boundary(
            [str(source)],
            output_path=str(output),
            permission_granted=True,
        )
    assert output.read_text() == "do not overwrite"


def test_requires_enough_valid_reference_features(tmp_path):
    gpd = pytest.importorskip("geopandas")
    geometry = pytest.importorskip("shapely.geometry")
    source = tmp_path / "few.geojson"
    gpd.GeoDataFrame(
        {"id": [1, 2]},
        geometry=[geometry.Point(34.8, 3.6), geometry.Point(34.81, 3.6)],
        crs="EPSG:4326",
    ).to_file(source, driver="GeoJSON")
    with pytest.raises(ValueError, match="at least 3"):
        propose_approximate_boundary(
            [str(source)],
            output_path=str(tmp_path / "few_out.geojson"),
            permission_granted=True,
        )
