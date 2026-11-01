
import json
from types import SimpleNamespace

import pytest

from tomorrowcities.services.boundary_review_store import (
    BoundaryReviewStore,
    apply_boundary_review_decision,
    create_boundary_review_record,
    validate_boundary_review_record,
)


class FakeDownload:
    def __init__(self, payload):
        self.payload = payload

    def readall(self):
        return self.payload


class FakeBlob:
    def __init__(self, container, name):
        self.container = container
        self.name = name

    def upload_blob(self, data, overwrite=False, **kwargs):
        if not overwrite and self.name in self.container.objects:
            raise FileExistsError(self.name)
        if isinstance(data, str):
            data = data.encode()
        self.container.objects[self.name] = data

    def download_blob(self):
        return FakeDownload(self.container.objects[self.name])


class FakeContainer:
    def __init__(self):
        self.objects = {}

    def get_blob_client(self, blob):
        return FakeBlob(self, blob)

    def list_blobs(self, name_starts_with):
        return [
            SimpleNamespace(name=name)
            for name in self.objects
            if name.startswith(name_starts_with)
        ]


def _review():
    return create_boundary_review_record(
        candidate={"path": "bronze/kalobeyei/raw/aoi.geojson"},
        staging={
            "status": "staged",
            "source_path": "bronze/kalobeyei/raw/aoi.geojson",
            "staged_path": "/tmp/aoi.geojson",
            "sha256": "a" * 64,
            "bytes_written": 123,
            "read_only_source": True,
            "source_etag": "etag-1",
        },
        validation={
            "path": "/tmp/aoi.geojson",
            "status": "inspected",
            "geometry_valid": True,
            "crs_known": True,
            "coverage_checked": True,
            "lineage_checked": True,
            "eligible_for_human_approval": True,
        },
        requested_by="operator@example.org",
        purpose="Verify study AOI",
        review_id="1" * 32,
    )


def test_create_review_record_requires_staged_hash_and_context():
    record = _review()
    assert record["state"] == "pending_review"
    assert record["decision"] is None
    assert record["gold_release_allowed"] is False
    assert record["events"][0]["event_type"] == "review_created"


def test_approval_fails_closed_if_any_required_check_is_missing():
    record = _review()
    record["validation"]["coverage_checked"] = False
    with pytest.raises(ValueError, match="coverage_checked"):
        apply_boundary_review_decision(
            record, decision="approve", reviewer="reviewer@example.org", rationale="Checked",
        )


def test_decisions_append_audit_event_and_gate_gold():
    record = _review()
    approved = apply_boundary_review_decision(
        record, decision="approve", reviewer="reviewer@example.org", rationale="Matches project AOI metadata.",
    )
    assert record["state"] == "pending_review"
    assert approved["state"] == "approved"
    assert approved["gold_release_allowed"] is True
    assert approved["events"][-1]["previous_event_hash"] == record["events"][-1]["event_hash"]
    assert validate_boundary_review_record(approved)["allowed"]


def test_reject_and_request_changes_do_not_allow_gold():
    record = _review()
    rejected = apply_boundary_review_decision(
        record, decision="reject", reviewer="reviewer@example.org", rationale="Wrong project extent.",
    )
    requested = apply_boundary_review_decision(
        record, decision="request_changes", reviewer="reviewer@example.org", rationale="Please correct west edge.",
    )
    assert rejected["state"] == "rejected"
    assert rejected["gold_release_allowed"] is False
    assert requested["state"] == "changes_requested"
    assert requested["gold_release_allowed"] is False


def test_store_saves_pending_review_and_appends_decision_event():
    client = FakeContainer()
    store = BoundaryReviewStore(container_client=client)
    record = _review()
    saved = store.save_new(record)
    assert saved["status"] == "saved"
    loaded = store.get(record["review_id"])
    assert loaded["review_id"] == record["review_id"]
    decision = store.append_decision(
        record["review_id"],
        decision="approve",
        reviewer="reviewer@example.org",
        rationale="Verified against project source manifest.",
    )
    assert decision["status"] == "decision_recorded"
    assert decision["gold_release_allowed"] is True
    assert len(store.get_events(record["review_id"])) == 1
    assert store.get(record["review_id"])["state"] == "pending_review"
    current = store.get_current_record(record["review_id"])
    assert current["state"] == "approved"
    assert validate_boundary_review_record(current)["allowed"]
    with pytest.raises(FileExistsError):
        store.append_decision(
            record["review_id"],
            decision="reject",
            reviewer="other-reviewer",
            rationale="Conflicting decision must not overwrite the first.",
        )


def test_store_does_not_overwrite_existing_pending_review():
    client = FakeContainer()
    store = BoundaryReviewStore(container_client=client)
    record = _review()
    store.save_new(record)
    with pytest.raises(FileExistsError):
        store.save_new(record)


def test_review_id_and_paths_are_validated():
    with pytest.raises(ValueError):
        create_boundary_review_record(
            candidate={"path": "../bad.geojson"},
            staging={"status": "staged", "sha256": "a" * 64},
            validation={},
            requested_by="operator",
            purpose="test",
        )
    gate = validate_boundary_review_record({
        "schema_version": 1,
        "review_id": "1" * 32,
        "state": "pending_review",
        "candidate": {
            "source_path": "bronze/kalobeyei/raw/aoi.geojson",
            "staged_path": "/tmp/aoi.geojson",
        },
    })
    assert gate["allowed"] is False
    assert "boundary_not_approved" in gate["problems"]
    assert "gold_release_not_allowed" in gate["problems"]
