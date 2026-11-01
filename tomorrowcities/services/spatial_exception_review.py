"""Human review workflow for spatial data exceptions before Gold publication.

Review decisions are immutable, create-only ADLS/Blob objects. Source datasets
are never changed. Unresolved exceptions fail closed at Gold preparation time.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

DECISIONS = {"retain", "exclude", "defer"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} is required.")
    return result


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def create_spatial_exception(
    *,
    dataset: str,
    source_path: str,
    source_sha256: str,
    source_feature_key: str,
    source_row_index: int | None,
    issue: str,
    evidence: Mapping[str, Any],
    review_id: str | None = None,
) -> dict[str, Any]:
    """Create a pending exception record from non-sensitive metadata/evidence."""
    digest = _clean(source_sha256, "source_sha256").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("source_sha256 must be a SHA-256 hex digest.")
    if source_row_index is not None and source_row_index < 0:
        raise ValueError("source_row_index must be non-negative.")
    identifier = review_id or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("review_id must be a 32-character lowercase UUID hex string.")
    safe_evidence = {
        key: evidence[key] for key in (
            "geometry_type", "crs", "feature_bounds", "feature_area",
            "outside_area", "outside_fraction", "boundary_dataset",
            "boundary_sha256", "spatial_test",
        ) if key in evidence
    }
    return {
        "schema_version": 1,
        "review_id": identifier,
        "status": "pending",
        "created_at": _now(),
        "dataset": _clean(dataset, "dataset"),
        "source_path": _clean(source_path, "source_path"),
        "source_sha256": digest,
        "source_feature_key": _clean(source_feature_key, "source_feature_key"),
        "source_row_index": source_row_index,
        "issue": _clean(issue, "issue"),
        "evidence": safe_evidence,
        "decision": None,
        "gold_inclusion": None,
    }


def decide_spatial_exception(
    record: Mapping[str, Any], *, decision: str, reviewer: str, rationale: str
) -> dict[str, Any]:
    """Return a decided record; never mutate the pending input."""
    choice = _clean(decision, "decision").lower()
    if choice not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}.")
    if record.get("status") != "pending" or record.get("decision") is not None:
        raise ValueError("Only pending spatial exceptions can receive a decision.")
    result = json.loads(json.dumps(record))
    result["status"] = "deferred" if choice == "defer" else "decided"
    result["decision"] = {
        "choice": choice,
        "reviewer": _clean(reviewer, "reviewer"),
        "rationale": _clean(rationale, "rationale"),
        "decided_at": _now(),
    }
    result["gold_inclusion"] = True if choice == "retain" else False if choice == "exclude" else None
    return result


def evaluate_spatial_exception_gate(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Fail closed if any exception is pending/deferred or lacks a valid decision."""
    unresolved = []
    for item in records:
        decision = item.get("decision") or {}
        if (
            item.get("status") != "decided"
            or decision.get("choice") not in {"retain", "exclude"}
            or item.get("gold_inclusion") is not (decision.get("choice") == "retain")
            or not decision.get("reviewer")
            or not decision.get("rationale")
        ):
            unresolved.append(item.get("review_id"))
    return {
        "allowed": not unresolved,
        "exception_count": len(records),
        "unresolved_count": len(unresolved),
        "unresolved_review_ids": unresolved,
        "note": "This gate covers spatial exceptions only; all other Gold quality and contract gates still apply.",
    }


def apply_spatial_exception_decisions(frame: Any, records: list[Mapping[str, Any]], *, key_column: str):
    """Return a filtered copy according to recorded decisions; preserve input unchanged."""
    gate = evaluate_spatial_exception_gate(records)
    if not gate["allowed"]:
        raise ValueError("Gold preparation blocked: spatial exceptions remain unresolved.")
    if key_column not in frame.columns:
        raise ValueError(f"Feature key column not found: {key_column}")
    decisions = {}
    for item in records:
        key = str(item["source_feature_key"])
        if key in decisions:
            raise ValueError(f"Conflicting duplicate review records for feature key: {key}")
        decisions[key] = item["gold_inclusion"]
    result = frame.copy()
    mask = result[key_column].astype(str).map(lambda key: decisions.get(key, True))
    return result.loc[mask].copy()


class SpatialExceptionReviewStore:
    """Create-only review and decision snapshots in an Azure Blob container."""
    def __init__(self, *, container_client: Any, prefix: str = "silver/kalobeyei/review/spatial_exceptions"):
        self.container = container_client
        self.prefix = prefix.strip("/")

    def _blob(self, path: str):
        return self.container.get_blob_client(blob=f"{self.prefix}/{path}")

    def create(self, record: Mapping[str, Any]) -> dict[str, Any]:
        if record.get("status") != "pending" or record.get("decision") is not None:
            raise ValueError("Only pending records can be created.")
        payload = _json_bytes(record)
        path = f"{record['review_id']}/record.json"
        self._blob(path).upload_blob(payload, overwrite=False, content_type="application/json")
        return {"status": "created", "path": f"{self.prefix}/{path}", "sha256": hashlib.sha256(payload).hexdigest()}

    def get(self, review_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", review_id):
            raise ValueError("Invalid review_id.")
        payload = self._blob(f"{review_id}/record.json").download_blob().readall()
        result = json.loads(payload.decode("utf-8"))
        if result.get("review_id") != review_id:
            raise ValueError("Stored review identity mismatch.")
        try:
            decision_payload = self._blob(f"{review_id}/decision.json").download_blob().readall()
        except Exception:
            return result
        snapshot = json.loads(decision_payload.decode("utf-8"))
        if snapshot.get("review_id") != review_id:
            raise ValueError("Stored decision identity mismatch.")
        result.update({key: snapshot[key] for key in ("status", "decision", "gold_inclusion")})
        return result

    def decide(self, review_id: str, *, decision: str, reviewer: str, rationale: str) -> dict[str, Any]:
        current = self.get(review_id)
        # A persisted decision is final. Report the create-only conflict consistently;
        # the subsequent overwrite=False upload remains the race-safe backstop.
        if current.get("status") != "pending" or current.get("decision") is not None:
            raise FileExistsError(f"A decision has already been recorded for review {review_id}.")
        updated = decide_spatial_exception(current, decision=decision, reviewer=reviewer, rationale=rationale)
        snapshot = {
            "review_id": review_id,
            "status": updated["status"],
            "decision": updated["decision"],
            "gold_inclusion": updated["gold_inclusion"],
        }
        self._blob(f"{review_id}/decision.json").upload_blob(_json_bytes(snapshot), overwrite=False)
        return {"status": "recorded", **snapshot}

    def list_records(self) -> list[dict[str, Any]]:
        prefix = f"{self.prefix}/"
        records = []
        for blob in self.container.list_blobs(name_starts_with=prefix):
            name = str(getattr(blob, "name", "") or "")
            if name.endswith("/record.json"):
                review_id = name[len(prefix):-len("/record.json")]
                records.append(self.get(review_id))
        return sorted(records, key=lambda item: (item.get("created_at", ""), item.get("review_id", "")))
