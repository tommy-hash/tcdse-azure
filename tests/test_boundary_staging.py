
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tomorrowcities.services.boundary_discovery import stage_boundary_candidate


class FakeDownloader:
    def __init__(self, payload, chunk_size=4):
        self.payload = payload
        self.chunk_size = chunk_size

    def chunks(self):
        for start in range(0, len(self.payload), self.chunk_size):
            yield self.payload[start:start + self.chunk_size]


class FakeBlobClient:
    def __init__(self, payload, size=None):
        self.payload = payload
        self.size = len(payload) if size is None else size

    def get_blob_properties(self):
        return SimpleNamespace(
            size=self.size,
            content_settings=SimpleNamespace(content_type="application/geopackage+sqlite3"),
            last_modified="2026-10-29T10:00:00Z",
            etag='"abc123"',
        )

    def download_blob(self):
        return FakeDownloader(self.payload)


class FakeContainer:
    def __init__(self, objects):
        self.objects = objects

    def get_blob_client(self, blob):
        if blob not in self.objects:
            raise FileNotFoundError(blob)
        return self.objects[blob]


def test_stages_candidate_streams_hash_and_leaves_source_unchanged(tmp_path):
    payload = b"test-geospatial-payload"
    client = FakeContainer({"bronze/kalobeyei/raw/aoi.geojson": FakeBlobClient(payload)})
    result = stage_boundary_candidate(
        "bronze/kalobeyei/raw/aoi.geojson",
        staging_root=str(tmp_path / "staging"),
        container_client=client,
    )
    staged = Path(result["staged_path"])
    assert staged.read_bytes() == payload
    assert result["bytes_written"] == len(payload)
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["read_only_source"] is True
    assert result["approved"] is False


def test_staging_rejects_unsafe_paths_and_extensions(tmp_path):
    client = FakeContainer({})
    with pytest.raises(ValueError):
        stage_boundary_candidate("../secret.geojson", staging_root=str(tmp_path), container_client=client)
    with pytest.raises(ValueError):
        stage_boundary_candidate("bronze/kalobeyei/aoi.exe", staging_root=str(tmp_path), container_client=client)


def test_staging_rejects_oversized_objects_before_download(tmp_path):
    client = FakeContainer({"bronze/kalobeyei/aoi.gpkg": FakeBlobClient(b"small", size=999)})
    with pytest.raises(ValueError, match="configured limit"):
        stage_boundary_candidate(
            "bronze/kalobeyei/aoi.gpkg",
            staging_root=str(tmp_path),
            container_client=client,
            max_bytes=100,
        )


def test_staging_removes_partial_file_when_stream_exceeds_limit(tmp_path):
    client = FakeContainer({"bronze/kalobeyei/aoi.geojson": FakeBlobClient(b"x" * 80)})
    with pytest.raises(ValueError, match="exceeds configured limit"):
        stage_boundary_candidate(
            "bronze/kalobeyei/aoi.geojson",
            staging_root=str(tmp_path),
            container_client=client,
            max_bytes=20,
        )
    assert list(Path(tmp_path).glob("tcdse-boundary-*")) == []


def test_staging_rejects_hash_mismatch_and_removes_file(tmp_path):
    client = FakeContainer({"bronze/kalobeyei/aoi.geojson": FakeBlobClient(b"content")})
    with pytest.raises(ValueError, match="SHA-256"):
        stage_boundary_candidate(
            "bronze/kalobeyei/aoi.geojson",
            staging_root=str(tmp_path),
            container_client=client,
            expected_sha256="0" * 64,
        )
    assert list(Path(tmp_path).glob("tcdse-boundary-*")) == []


def test_staging_reports_shapefile_sidecar_limitation(tmp_path):
    client = FakeContainer({"bronze/kalobeyei/aoi.shp": FakeBlobClient(b"shape")})
    result = stage_boundary_candidate(
        "bronze/kalobeyei/aoi.shp",
        staging_root=str(tmp_path),
        container_client=client,
    )
    assert any("without sidecars" in item for item in result["warnings"])
