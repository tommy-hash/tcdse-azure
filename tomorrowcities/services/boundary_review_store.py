"""Persisted, auditable human review records for boundary candidates.

The store uses append-only review events in ADLS/Blob storage. It never mutates
source geospatial objects and does not itself publish Gold outputs.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


REVIEW_STATES = {"pending_review", "approved", "rejected", "changes_requested"}
REVIEW_DECISIONS = {"approve", "reject", "request_changes"}
REQUIRED_APPROVAL_CHECKS = ("geometry_valid", "crs_known", "coverage_checked", "lineage_checked")
DEFAULT_REVIEW_PREFIX = "silver/kalobeyei/review/boundary_reviews"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_relative_path(value: str) -> str:
    path = str(value or "").replace(chr(92), "/").strip()
    if not path or path.startswith("/") or ":" in path:
        raise ValueError("Review paths must be non-empty relative object paths.")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("Review paths must not contain empty, dot, or parent segments.")
    return path


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _event_hash(event: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json_bytes(event)).hexdigest()


def _review_object_path(prefix: str, review_id: str) -> str:
    safe_prefix = _safe_relative_path(prefix.rstrip("/"))
    if not re.fullmatch(r"[0-9a-f]{32}", review_id):
        raise ValueError("review_id must be a 32-character lowercase UUID hex string.")
    return f"{safe_prefix}/{review_id}.json"


def create_boundary_review_record(
    *,
    candidate: Mapping[str, Any],
    staging: Mapping[str, Any],
    validation: Mapping[str, Any],
    requested_by: str,
    purpose: str,
    reference_sources: Sequence[str] = (),
    review_id: str | None = None,
) -> dict[str, Any]:
    """Create a new pending review record; this does not persist it."""
    requester = requested_by.strip()
    purpose_text = purpose.strip()
    if not requester:
        raise ValueError("requested_by is required.")
    if not purpose_text:
        raise ValueError("purpose is required.")
    source_path = _safe_relative_path(str(candidate.get("path") or staging.get("source_path") or ""))
    staged_hash = str(staging.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", staged_hash):
        raise ValueError("staging.sha256 must be a valid SHA-256 hex digest.")
    if staging.get("status") != "staged":
        raise ValueError("Only successfully staged candidates can enter human review.")
    if validation.get("path") and str(validation["path"]) != str(staging.get("staged_path")):
        raise ValueError("Validation evidence path must match the staged file path.")
    identifier = review_id or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("review_id must be a 32-character lowercase UUID hex string.")

    evidence = {key: validation.get(key) for key in (
        "status", "feature_count", "geometry_types", "polygonal", "geometry_valid",
        "invalid_geometry_count", "null_geometry_count", "empty_geometry_count",
        "crs", "crs_known", "bounds", "coverage_checked", "coverage_results",
        "lineage_checked", "eligible_for_human_approval", "errors", "warnings",
    )}
    record: dict[str, Any] = {
        "schema_version": 1,
        "review_id": identifier,
        "state": "pending_review",
        "created_at": _utc_now(),
        "requested_by": requester,
        "purpose": purpose_text,
        "candidate": {
            "source_path": source_path,
            "staged_path": str(staging.get("staged_path") or ""),
            "sha256": staged_hash,
            "bytes_written": staging.get("bytes_written"),
            "source_etag": staging.get("source_etag"),
            "source_last_modified": staging.get("source_last_modified"),
            "read_only_source": staging.get("read_only_source") is True,
        },
        "reference_sources": [str(source).strip() for source in reference_sources if str(source).strip()],
        "validation": evidence,
        "decision": None,
        "events": [],
        "gold_release_allowed": False,
    }
    event = {
        "event_type": "review_created",
        "at": record["created_at"],
        "actor": requester,
        "state": "pending_review",
        "candidate_sha256": staged_hash,
    }
    event["event_hash"] = _event_hash(event)
    record["events"].append(event)
    return record


def apply_boundary_review_decision(
    record: Mapping[str, Any],
    *,
    decision: str,
    reviewer: str,
    rationale: str,
    edited_boundary_path: str | None = None,
) -> dict[str, Any]:
    """Return a new record with an auditable decision; never modify input in place."""
    choice = decision.strip().lower()
    actor = reviewer.strip()
    reason = rationale.strip()
    if choice not in REVIEW_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(REVIEW_DECISIONS)}.")
    if not actor:
        raise ValueError("reviewer is required.")
    if not reason:
        raise ValueError("rationale is required.")
    if record.get("state") != "pending_review":
        raise ValueError("Only pending_review records can receive a decision.")
    if not isinstance(record.get("events"), list):
        raise ValueError("Review record has no valid event history.")

    updated = json.loads(json.dumps(record))
    validation = updated.get("validation") or {}
    if choice == "approve":
        missing_or_failed = [
            key for key in REQUIRED_APPROVAL_CHECKS if validation.get(key) is not True
        ]
        if missing_or_failed:
            raise ValueError(
                "Cannot approve boundary: required validation checks are missing or failed: "
                + ", ".join(missing_or_failed)
            )
        state = "approved"
        gold_release_allowed = True
    elif choice == "reject":
        state = "rejected"
        gold_release_allowed = False
    else:
        state = "changes_requested"
        gold_release_allowed = False

    edited_path = None
    if edited_boundary_path:
        edited_path = _safe_relative_path(edited_boundary_path)
    now = _utc_now()
    decision_record = {
        "decision": choice,
        "reviewer": actor,
        "rationale": reason,
        "decided_at": now,
        "edited_boundary_path": edited_path,
    }
    updated["state"] = state
    updated["decision"] = decision_record
    updated["gold_release_allowed"] = gold_release_allowed
    event = {
        "event_type": "review_decision",
        "at": now,
        "actor": actor,
        "decision": choice,
        "state": state,
        "rationale": reason,
        "edited_boundary_path": edited_path,
        "previous_event_hash": updated["events"][-1].get("event_hash") if updated["events"] else None,
    }
    event["event_hash"] = _event_hash(event)
    updated["events"].append(event)
    return updated


def validate_boundary_review_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a fail-closed release-gate result for a persisted review record."""
    problems: list[str] = []
    if record.get("schema_version") != 1:
        problems.append("unsupported_schema_version")
    if record.get("state") != "approved":
        problems.append("boundary_not_approved")
    if record.get("gold_release_allowed") is not True:
        problems.append("gold_release_not_allowed")
    validation = record.get("validation") or {}
    for check in REQUIRED_APPROVAL_CHECKS:
        if validation.get(check) is not True:
            problems.append(f"validation_failed_or_missing:{check}")
    decision = record.get("decision") or {}
    if decision.get("decision") != "approve" or not decision.get("reviewer") or not decision.get("rationale"):
        problems.append("approval_audit_fields_missing")
    return {
        "allowed": not problems,
        "review_id": record.get("review_id"),
        "state": record.get("state"),
        "problems": problems,
        "note": "This gate covers boundary review only; all other Gold contract and release checks still apply.",
    }


class BoundaryReviewStore:
    """Persist review records as create-only JSON blobs in ADLS/Blob storage.

    Pass an authenticated container_client for testing or reuse. When omitted,
    DefaultAzureCredential and the configured TCDSE account/filesystem are used.
    Existing records are never overwritten by this store.
    """

    def __init__(
        self,
        *,
        container_client: Any | None = None,
        account_url: str | None = None,
        filesystem: str | None = None,
        prefix: str = DEFAULT_REVIEW_PREFIX,
    ) -> None:
        import os

        self.prefix = _safe_relative_path(prefix.rstrip("/"))
        self.container_client = container_client
        if self.container_client is None:
            resolved_account_url = (account_url or os.getenv("TCDSE_ADLS_ACCOUNT_URL", "")).strip().rstrip("/")
            resolved_filesystem = (filesystem or os.getenv("TCDSE_ADLS_FILESYSTEM", "")).strip()
            if not resolved_account_url or not resolved_filesystem:
                raise RuntimeError(
                    "Set TCDSE_ADLS_ACCOUNT_URL and TCDSE_ADLS_FILESYSTEM, "
                    "or pass an authenticated container_client."
                )
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient

            if ".dfs." in resolved_account_url:
                resolved_account_url = resolved_account_url.replace(".dfs.", ".blob.")
            service = BlobServiceClient(account_url=resolved_account_url, credential=DefaultAzureCredential())
            self.container_client = service.get_container_client(resolved_filesystem)

    def save_new(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a pending review exactly once; fail if the ID already exists."""
        if record.get("state") != "pending_review" or record.get("decision") is not None:
            raise ValueError("Only undecided pending_review records can be created.")
        review_id = str(record.get("review_id") or "")
        blob_path = _review_object_path(self.prefix, review_id)
        payload = _json_bytes(record)
        blob = self.container_client.get_blob_client(blob=blob_path)
        try:
            blob.upload_blob(payload, overwrite=False, content_type="application/json")
        except TypeError:
            # Some SDK versions expect content_settings rather than content_type.
            from azure.storage.blob import ContentSettings
            blob.upload_blob(payload, overwrite=False, content_settings=ContentSettings(content_type="application/json"))
        return {"status": "saved", "review_id": review_id, "path": blob_path, "sha256": hashlib.sha256(payload).hexdigest()}

    def get(self, review_id: str) -> dict[str, Any]:
        blob_path = _review_object_path(self.prefix, review_id)
        blob = self.container_client.get_blob_client(blob=blob_path)
        payload = blob.download_blob().readall()
        record = json.loads(payload.decode("utf-8"))
        if not isinstance(record, dict) or record.get("review_id") != review_id:
            raise ValueError("Stored review record failed identity validation.")
        return record

    def append_decision(self, review_id: str, *, decision: str, reviewer: str, rationale: str,
                        edited_boundary_path: str | None = None) -> dict[str, Any]:
        """Append a decision event without overwriting the original pending record."""
        record = self.get(review_id)
        updated = apply_boundary_review_decision(
            record, decision=decision, reviewer=reviewer, rationale=rationale,
            edited_boundary_path=edited_boundary_path,
        )
        event = updated["events"][-1]
        event_path = f"{self.prefix}/{review_id}/events/{event['event_hash']}.json"
        event_blob = self.container_client.get_blob_client(blob=event_path)
        event_payload = _json_bytes(event)
        event_blob.upload_blob(event_payload, overwrite=False)
        return {"status": "decision_recorded", "review_id": review_id, "state": updated["state"],
                "event_path": event_path, "event_hash": event["event_hash"],
                "gold_release_allowed": updated["gold_release_allowed"],
                "note": "Decision event is append-only. The original pending record remains unchanged."}

    def get_events(self, review_id: str) -> list[dict[str, Any]]:
        _review_object_path(self.prefix, review_id)
        event_prefix = f"{self.prefix}/{review_id}/events/"
        events = []
        for item in self.container_client.list_blobs(name_starts_with=event_prefix):
            name = str(getattr(item, "name", "") or "")
            if not name.endswith(".json"):
                continue
            payload = self.container_client.get_blob_client(blob=name).download_blob().readall()
            events.append(json.loads(payload.decode("utf-8")))
        return sorted(events, key=lambda event: (event.get("at", ""), event.get("event_hash", "")))
