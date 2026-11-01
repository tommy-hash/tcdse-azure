"""Solara interface for recording and deciding spatial data exceptions."""
from __future__ import annotations

import json
import os
import solara

from ..services.spatial_exception_review import (
    SpatialExceptionReviewStore,
    create_spatial_exception,
    evaluate_spatial_exception_gate,
)


def _store() -> SpatialExceptionReviewStore:
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    account_url = os.getenv("TCDSE_ADLS_ACCOUNT_URL", "").strip().rstrip("/")
    filesystem = os.getenv("TCDSE_ADLS_FILESYSTEM", "").strip()
    if not account_url or not filesystem:
        raise RuntimeError("Configure TCDSE_ADLS_ACCOUNT_URL and TCDSE_ADLS_FILESYSTEM.")
    if ".dfs." in account_url:
        account_url = account_url.replace(".dfs.", ".blob.")
    service = BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())
    return SpatialExceptionReviewStore(container_client=service.get_container_client(filesystem))


@solara.component
def Page():
    solara.Title("Spatial Exception Review")
    records, set_records = solara.use_state([])
    selected_id, set_selected_id = solara.use_state("")
    reviewer, set_reviewer = solara.use_state("")
    rationale, set_rationale = solara.use_state("")
    dataset, set_dataset = solara.use_state("Kalobeyei land use")
    source_path, set_source_path = solara.use_state("")
    source_hash, set_source_hash = solara.use_state("")
    feature_key, set_feature_key = solara.use_state("row:325")
    row_index, set_row_index = solara.use_state("325")
    issue, set_issue = solara.use_state("Feature lies completely outside the Kalobeyei settlement boundary.")
    crs, set_crs = solara.use_state("EPSG:32636")
    bounds, set_bounds = solara.use_state("[1394647.71, 829835.30, 1394762.99, 829965.97]")
    outside_area, set_outside_area = solara.use_state("7662.44")
    status, set_status = solara.use_state("")
    error, set_error = solara.use_state("")

    def refresh():
        set_error("")
        try:
            items = _store().list_records()
            set_records(items)
            if selected_id and not any(item.get("review_id") == selected_id for item in items):
                set_selected_id("")
            set_status(f"Loaded {len(items)} spatial exception review record(s).")
        except Exception as exc:
            set_error(f"{type(exc).__name__}: {exc}")

    def create_record():
        set_error("")
        try:
            if not row_index.strip().isdigit():
                raise ValueError("Source row index must be a non-negative integer.")
            parsed_bounds = json.loads(bounds)
            if not isinstance(parsed_bounds, list) or len(parsed_bounds) != 4:
                raise ValueError("Feature bounds must be a JSON array of four numbers.")
            evidence = {
                "geometry_type": "Polygon",
                "crs": crs.strip(),
                "feature_bounds": parsed_bounds,
                "outside_area": float(outside_area),
                "spatial_test": "feature geometry does not intersect settlement boundary",
                "boundary_dataset": "Kalobeyei_Settlement_Boundary",
            }
            record = create_spatial_exception(
                dataset=dataset,
                source_path=source_path,
                source_sha256=source_hash,
                source_feature_key=feature_key,
                source_row_index=int(row_index),
                issue=issue,
                evidence=evidence,
            )
            result = _store().create(record)
            set_selected_id(record["review_id"])
            set_status(f"Created pending review {record['review_id']} at {result['path']}.")
            refresh()
        except Exception as exc:
            set_error(f"{type(exc).__name__}: {exc}")

    def decide(choice):
        set_error("")
        try:
            if not selected_id:
                raise ValueError("Select a review record first.")
            if not reviewer.strip() or not rationale.strip():
                raise ValueError("Reviewer and rationale are required.")
            result = _store().decide(
                selected_id, decision=choice, reviewer=reviewer, rationale=rationale
            )
            set_status(f"Decision recorded: {choice}. Gold inclusion: {result['gold_inclusion']}.")
            refresh()
        except Exception as exc:
            set_error(f"{type(exc).__name__}: {exc}")

    selected = next((item for item in records if item.get("review_id") == selected_id), None)
    gate = evaluate_spatial_exception_gate(records)

    with solara.Column(gap="12px", style={"max-width": "1050px", "margin": "0 auto", "padding": "16px"}):
        solara.Markdown(
            """## Spatial Exception Review

Review spatial features flagged during Silver quality assessment. Every decision
is recorded in ADLS. Silver source files are never modified by this page."""
        )
        solara.Warning(
            "Use only behind authenticated access. The reviewer field is an audit label, "
            "not proof of identity. Do not enter personal or sensitive feature attributes."
        )

        with solara.Card():
            solara.Markdown("### 1. Review queue and Gold gate")
            solara.Button("Refresh review queue", on_click=refresh, color="primary")
            if records:
                solara.Markdown(
                    f"**Records:** {len(records)} · **Unresolved:** {gate['unresolved_count']} · "
                    f"**Gold spatial-exception gate:** {'PASS' if gate['allowed'] else 'BLOCKED'}"
                )
                solara.Select(
                    label="Select exception",
                    value=selected_id,
                    values=[""] + [item["review_id"] for item in records],
                    on_value=set_selected_id,
                )
                if selected:
                    evidence = selected.get("evidence") or {}
                    decision = selected.get("decision") or {}
                    solara.Markdown(
                        f"""**Status:** {selected.get('status')}

**Dataset:** {selected.get('dataset')}

**Source object:** {selected.get('source_path')}

**Source SHA-256:** {selected.get('source_sha256')}

**Feature key:** {selected.get('source_feature_key')} (row index: {selected.get('source_row_index')})

**Issue:** {selected.get('issue')}

**Evidence:** {json.dumps(evidence, ensure_ascii=False)}

**Decision:** {decision.get('choice', 'Not yet decided')}

**Reviewer:** {decision.get('reviewer', '—')}

**Rationale:** {decision.get('rationale', '—')}"""
                    )
                    if selected.get("status") == "pending":
                        solara.InputText(label="Reviewer (audit label)", value=reviewer, on_value=set_reviewer)
                        solara.InputTextArea(label="Decision rationale", value=rationale, on_value=set_rationale, rows=3)
                        with solara.Row():
                            solara.Button("Retain in Gold", on_click=lambda: decide("retain"), color="success")
                            solara.Button("Exclude from Gold", on_click=lambda: decide("exclude"), color="error")
                            solara.Button("Defer", on_click=lambda: decide("defer"))
                    else:
                        solara.Info("This record already has a decision. Decisions are immutable; create a new review record to reconsider it.")
            else:
                solara.Markdown("No review records loaded. Refresh the queue or create the first review below.")

        with solara.Card():
            solara.Markdown("### 2. Register a spatial exception")
            solara.Markdown(
                "Enter metadata and spatial evidence only. For Shapefiles, calculate a SHA-256 "
                "manifest/hash over the complete component set (.shp, .shx, .dbf, .prj and other "
                "required sidecars), not just the .shp file. The row index is meaningful only "
                "for that exact source hash."
            )
            solara.InputText(label="Dataset", value=dataset, on_value=set_dataset)
            solara.InputText(label="Source ADLS object path", value=source_path, on_value=set_source_path)
            solara.InputText(label="Source component-set SHA-256", value=source_hash, on_value=set_source_hash)
            solara.InputText(label="Stable feature key (bound to source hash)", value=feature_key, on_value=set_feature_key)
            solara.InputText(label="Source row index", value=row_index, on_value=set_row_index)
            solara.InputTextArea(label="Issue description", value=issue, on_value=set_issue, rows=2)
            solara.InputText(label="CRS", value=crs, on_value=set_crs)
            solara.InputText(label="Feature bounds [minx, miny, maxx, maxy]", value=bounds, on_value=set_bounds)
            solara.InputText(label="Outside area (m²)", value=outside_area, on_value=set_outside_area)
            solara.Button("Create pending exception", on_click=create_record, color="primary")

        solara.Markdown(
            "**Gold integration note:** this page records decisions and reports the exception gate. "
            "The repository does not yet contain a Gold land-use transformation/release entry point; "
            "that pipeline must call evaluate_spatial_exception_gate and apply decisions before export."
        )
        if status:
            solara.Info(status)
        if error:
            solara.Error(error)
