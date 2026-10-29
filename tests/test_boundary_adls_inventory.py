
from types import SimpleNamespace

from tomorrowcities.services.boundary_discovery import inventory_adls_boundary_candidates


class FakeContainer:
    def __init__(self, blobs):
        self.blobs = blobs
        self.prefixes = []

    def list_blobs(self, name_starts_with):
        self.prefixes.append(name_starts_with)
        return [blob for blob in self.blobs if blob.name.startswith(name_starts_with)]


def test_adls_inventory_lists_only_configured_prefixes_and_ranks_candidates():
    client = FakeContainer([
        SimpleNamespace(
            name="bronze/kalobeyei/raw/kalobeyei_boundary.gpkg",
            metadata={"geometry_type": "MultiPolygon", "crs": "EPSG:4326"},
            last_modified="2026-10-29",
            size=1024,
        ),
        SimpleNamespace(
            name="bronze/kalobeyei/raw/buildings.gpkg",
            metadata={"geometry_type": "Polygon"},
            last_modified="2026-10-29",
            size=2048,
        ),
        SimpleNamespace(
            name="bronze/other/unrelated.gpkg",
            metadata={},
            last_modified="2026-10-29",
            size=100,
        ),
    ])
    result = inventory_adls_boundary_candidates(
        prefixes=["bronze/kalobeyei/"],
        container_client=client,
    )
    assert client.prefixes == ["bronze/kalobeyei/"]
    assert result.status == "candidates_found"
    assert result.candidates[0].filename == "kalobeyei_boundary.gpkg"
    assert result.candidates[0].metadata["crs"] == "EPSG:4326"
    assert result.requires_human_review


def test_adls_inventory_empty_result_is_not_claimed_as_proof_of_absence():
    result = inventory_adls_boundary_candidates(
        prefixes=["bronze/kalobeyei/"],
        container_client=FakeContainer([]),
    )
    assert result.status == "inventory_empty"
    assert result.candidates == ()
    assert any("Confirm prefixes" in warning for warning in result.warnings)
