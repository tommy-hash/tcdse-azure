from types import SimpleNamespace

import pandas as pd
import pytest

from tomorrowcities.services.spatial_exception_review import (
    SpatialExceptionReviewStore,
    apply_spatial_exception_decisions,
    create_spatial_exception,
    decide_spatial_exception,
    evaluate_spatial_exception_gate,
)


class FakeDownload:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data


class FakeBlob:
    def __init__(self, container, name):
        self.container, self.name = container, name

    def upload_blob(self, data, overwrite=False, **kwargs):
        if not overwrite and self.name in self.container.objects:
            raise FileExistsError(self.name)
        self.container.objects[self.name] = data if isinstance(data, bytes) else data.encode()

    def download_blob(self):
        return FakeDownload(self.container.objects[self.name])


class FakeContainer:
    def __init__(self):
        self.objects = {}

    def get_blob_client(self, blob):
        return FakeBlob(self, blob)

    def list_blobs(self, name_starts_with):
        return [SimpleNamespace(name=n) for n in self.objects if n.startswith(name_starts_with)]


def pending(index=325):
    return create_spatial_exception(
        dataset="Kalobeyei land use",
        source_path="silver/Kenya/Kalobeyei/landuse.shp",
        source_sha256="a" * 64,
        source_feature_key=f"row:{index}",
        source_row_index=index,
        issue="Feature lies completely outside the settlement boundary.",
        evidence={
            "geometry_type": "Polygon",
            "crs": "EPSG:32636",
            "feature_bounds": [1394647.71, 829835.3, 1394762.99, 829965.97],
            "outside_area": 7662.44,
            "sensitive_attribute": "must not be copied",
        },
        review_id=f"{index:032x}",
    )


def test_create_whitelists_metadata_and_starts_pending():
    item = pending()
    assert item["status"] == "pending"
    assert item["decision"] is None
    assert "sensitive_attribute" not in item["evidence"]


@pytest.mark.parametrize("choice,inclusion,status", [
    ("retain", True, "decided"),
    ("exclude", False, "decided"),
    ("defer", None, "deferred"),
])
def test_decisions_and_gate(choice, inclusion, status):
    item = pending()
    result = decide_spatial_exception(item, decision=choice, reviewer="reviewer", rationale="Reviewed evidence.")
    assert item["status"] == "pending"
    assert result["status"] == status
    assert result["gold_inclusion"] is inclusion
    assert evaluate_spatial_exception_gate([result])["allowed"] is (choice != "defer")


def test_missing_decision_blocks_gold():
    assert not evaluate_spatial_exception_gate([pending()])["allowed"]


def test_apply_decisions_retains_or_excludes_without_mutating_source():
    first = decide_spatial_exception(pending(1), decision="retain", reviewer="r", rationale="Valid")
    second = decide_spatial_exception(pending(2), decision="exclude", reviewer="r", rationale="Outlier")
    source = pd.DataFrame({"feature_key": ["row:1", "row:2", "row:3"], "value": [10, 20, 30]})
    result = apply_spatial_exception_decisions(source, [first, second], key_column="feature_key")
    assert result["feature_key"].tolist() == ["row:1", "row:3"]
    assert len(source) == 3


def test_unresolved_blocks_application_and_duplicate_keys_fail():
    source = pd.DataFrame({"feature_key": ["row:325"]})
    with pytest.raises(ValueError, match="unresolved"):
        apply_spatial_exception_decisions(source, [pending()], key_column="feature_key")
    first = decide_spatial_exception(pending(), decision="retain", reviewer="r", rationale="ok")
    second = decide_spatial_exception(pending(), decision="exclude", reviewer="r", rationale="no")
    with pytest.raises(ValueError, match="duplicate"):
        apply_spatial_exception_decisions(source, [first, second], key_column="feature_key")


def test_store_persists_create_only_decision_and_lists():
    container = FakeContainer()
    store = SpatialExceptionReviewStore(container_client=container)
    item = pending()
    store.create(item)
    store.decide(item["review_id"], decision="exclude", reviewer="reviewer", rationale="Outside AOI.")
    loaded = store.get(item["review_id"])
    assert loaded["decision"]["choice"] == "exclude"
    assert loaded["gold_inclusion"] is False
    assert len(store.list_records()) == 1
    with pytest.raises(FileExistsError):
        store.decide(item["review_id"], decision="retain", reviewer="other", rationale="Conflict")
