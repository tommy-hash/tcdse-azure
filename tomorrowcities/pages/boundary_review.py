"""Solara UI for discovering, staging and reviewing AOI boundary candidates.

Deploy this page only behind the application's authenticated access controls.
The reviewer name field is an audit label, not an authentication mechanism.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import solara

from ..services.boundary_discovery import (
    inspect_boundary_geometry,
    inventory_adls_boundary_candidates,
    stage_boundary_candidate,
)
from ..services.boundary_approximation import propose_approximate_boundary
from ..services.boundary_review_store import (
    BoundaryReviewStore,
    apply_boundary_review_decision,
    create_boundary_review_record,
    validate_boundary_review_record,
)


def _review_store() -> BoundaryReviewStore:
    return BoundaryReviewStore()


def _staging_root() -> str:
    return os.getenv(
        "TCDSE_BOUNDARY_STAGING_DIR",
        str(Path(tempfile.gettempdir()) / "tcdse-boundary-staging"),
    )


@solara.component
def Page():
    solara.Title("Boundary Discovery & Review")
    candidates, set_candidates = solara.use_state([])
    selected_path, set_selected_path = solara.use_state("")
    searched, set_searched = solara.use_state([])
    staged, set_staged = solara.use_state(None)
    validation, set_validation = solara.use_state(None)
    review_record, set_review_record = solara.use_state(None)
    review_id, set_review_id = solara.use_state("")
    status, set_status = solara.use_state("")
    error, set_error = solara.use_state("")
    source_paths_text, set_source_paths_text = solara.use_state("")
    requester, set_requester = solara.use_state("")
    purpose, set_purpose = solara.use_state("Confirm the intended project area of interest")
    provenance_confirmed, set_provenance_confirmed = solara.use_state(False)
    reviewer, set_reviewer = solara.use_state("")
    rationale, set_rationale = solara.use_state("")
    edited_path, set_edited_path = solara.use_state("")
    approximation_sources, set_approximation_sources = solara.use_state("")
    approximation_output, set_approximation_output = solara.use_state("")
    approximation_buffer, set_approximation_buffer = solara.use_state(250.0)
    approximation_permission, set_approximation_permission = solara.use_state(False)
    approximation_result, set_approximation_result = solara.use_state(None)

    def discover():
        set_error("")
        set_status("Searching configured ADLS prefixes…")
        try:
            result = inventory_adls_boundary_candidates()
            set_candidates([candidate.to_dict() for candidate in result.candidates])
            set_searched(list(result.searched_locations))
            set_selected_path(result.candidates[0].path if result.candidates else "")
            set_staged(None)
            set_validation(None)
            set_review_record(None)
            warning_text = " ".join(result.warnings)
            set_status(f"Discovery status: {result.status}. {len(result.candidates)} candidate(s). {warning_text}")
        except Exception as exc:
            set_error(f"{type(exc).__name__}: {exc}")
            set_status("Discovery failed. Check configuration, storage scope and managed-identity permissions.")

    def stage_and_inspect():
        set_error("")
        set_status("")
        candidate = next((item for item in candidates if item.get("path") == selected_path), None)
        if not candidate:
            set_error("Select a candidate first.")
            return
        try:
            staged_result = stage_boundary_candidate(
                candidate["path"],
                staging_root=_staging_root(),
            )
            sources = [part.strip() for part in source_paths_text.splitlines() if part.strip()]
            validation_result = inspect_boundary_geometry(
                staged_result["staged_path"],
                source_layer_paths=sources,
            )
            set_staged(staged_result)
            set_validation(validation_result)
            set_review_record(None)
            set_status("Candidate staged and inspected. It remains unapproved.")
        except Exception as exc:
            set_error(f"{type(exc).__name__}: {exc}")
            set_status("Staging or geometry inspection failed.")

    def create_review():
        set_error("")
        if not staged or not validation:
            set_error("Stage and inspect a candidate before creating a review.")
            return
        try:
            validation_for_review = dict(validation)
            validation_for_review["lineage_checked"] = bool(provenance_confirmed)
            record = create_boundary_review_record(
                candidate={"path": selected_path},
                staging=staged,
                validation=validation_for_review,
                requested_by=requester,
                purpose=purpose,
                reference_sources=source_paths_text.splitlines(),
            )
            result = _review_store().save_new(record)
            set_review_record(record)
            set_review_id(record["review_id"])
            set_status(f"Review created: {result['review_id']} (pending review).")
        except Exception as exc:
            set_error(f"{type(exc).__name__}: {exc}")

    def load_review():
        set_error("")
        try:
            store = _review_store()
            record = store.get_current_record(review_id.strip())
            events = store.get_events(review_id.strip())
            record["events"] = sorted(record.get("events", []) + events, key=lambda event: event.get("at", ""))
            set_review_record(record)
            set_status(f"Loaded review {review_id.strip()}: {record.get('state')}.")
        except Exception as exc:
            set_error(f"{type(exc).__name__}: {exc}")

    def decide(decision):
        set_error("")
        if not review_record:
            set_error("Load or create a review record first.")
            return
        if not reviewer.strip() or not rationale.strip():
            set_error("Reviewer and rationale are required.")
            return
        try:
            apply_boundary_review_decision(
                review_record,
                decision=decision,
                reviewer=reviewer,
                rationale=rationale,
                edited_boundary_path=edited_path.strip() or None,
            )
            store = _review_store()
            result = store.append_decision(
                review_record["review_id"],
                decision=decision,
                reviewer=reviewer,
                rationale=rationale,
                edited_boundary_path=edited_path.strip() or None,
            )
            updated = store.get_current_record(review_record["review_id"])
            updated["events"] = review_record.get("events", []) + store.get_events(review_record["review_id"])
            set_review_record(updated)
            set_status(
                f"Decision recorded: {result['state']}. "
                f"Boundary gate allows Gold: {result['gold_release_allowed']}."
            )
        except Exception as exc:
            set_error(f"{type(exc).__name__}: {exc}")

    with solara.Column(gap="12px", style={"max-width": "1000px", "margin": "0 auto", "padding": "16px"}):
        solara.Markdown(
            """## Boundary Discovery & Review
Search configured ADLS prefixes, inspect a candidate, and record a human decision.
Discovery is read-only; staging creates a temporary local copy. This page does not publish Gold."""
        )
        solara.Warning(
            "Access control required: deploy this page only behind authenticated application access. "
            "The reviewer field records a name but does not authenticate that person."
        )
        with solara.Card():
            solara.Markdown("### 1. Discover candidate boundaries")
            solara.Button("Search ADLS", on_click=discover, color="primary")
            if searched:
                solara.Markdown("**Searched prefixes:** " + ", ".join(searched))
            if candidates:
                solara.Markdown(f"**Candidates found:** {len(candidates)}")
                solara.Select(
                    label="Candidate boundary",
                    value=selected_path,
                    values=[item["path"] for item in candidates],
                    on_value=set_selected_path,
                )
                candidate = next((item for item in candidates if item.get("path") == selected_path), None)
                if candidate:
                    solara.Markdown(
                        f"""**Discovery evidence**

- Score: {candidate.get('score')} (ranking only)
- Format: {candidate.get('extension')}
- Reasons: {', '.join(candidate.get('reasons', []))}

Candidate is unverified until its actual geometry and lineage are reviewed."""
                    )
            elif status:
                solara.Markdown("No candidate list is currently loaded.")

        with solara.Card():
            solara.Markdown("### 2. Stage and inspect")
            solara.Markdown(
                "Optional: enter one local source-layer path per line. These files must already be available "
                "in the app environment; they are not automatically downloaded in this step."
            )
            solara.InputTextArea(
                label="Local source-layer paths (one per line)",
                value=source_paths_text,
                on_value=set_source_paths_text,
                rows=3,
            )
            solara.Button(
                "Stage and inspect selected candidate",
                on_click=stage_and_inspect,
                disabled=not bool(selected_path),
            )
            if staged:
                solara.Markdown(
                    f"""**Staging evidence**

- Source: {staged.get('source_path')}
- Temporary path: {staged.get('staged_path')}
- Bytes: {staged.get('bytes_written')}
- SHA-256: {staged.get('sha256')}"""
                )
            if validation:
                solara.Markdown(
                    f"""**Geometry validation**

- Status: {validation.get('status')}
- Geometry types: {', '.join(validation.get('geometry_types', [])) or 'none'}
- Valid geometry: {validation.get('geometry_valid')}
- CRS: {validation.get('crs')}
- Bounds: {validation.get('bounds')}
- Coverage checked: {validation.get('coverage_checked')}
- Lineage checked: {validation.get('lineage_checked')}
- Warnings: {'; '.join(validation.get('warnings', [])) or 'none'}"""
                )

        with solara.Card():
            solara.Markdown("### 3. Create a persisted review")
            solara.InputText(label="Requested by (audit label)", value=requester, on_value=set_requester)
            solara.InputText(label="Review purpose", value=purpose, on_value=set_purpose)
            solara.Checkbox(
                label="I verified the candidate's provenance against an appropriate project manifest/source record.",
                value=provenance_confirmed,
                on_value=set_provenance_confirmed,
            )
            solara.Markdown(
                "This checkbox records a reviewer attestation; it does not independently verify lineage. "
                "Do not check it unless you have inspected the evidence."
            )
            solara.Button("Create pending review record", on_click=create_review, disabled=not bool(staged and validation))
            if review_id:
                solara.Markdown(f"**Review ID:** {review_id}")

        with solara.Card():
            solara.Markdown("### 4. Review an existing record")
            solara.InputText(label="Review ID", value=review_id, on_value=set_review_id)
            solara.Button("Load review", on_click=load_review)
            if review_record:
                gate = validate_boundary_review_record(review_record)
                solara.Markdown(
                    f"""**Current state:** {review_record.get('state')}

**Boundary release gate:** {'PASS' if gate['allowed'] else 'BLOCKED'}

**Gate findings:** {', '.join(gate['problems']) or 'none'}"""
                )
                solara.InputText(label="Reviewer (audit label)", value=reviewer, on_value=set_reviewer)
                solara.InputText(label="Decision rationale", value=rationale, on_value=set_rationale)
                solara.InputText(label="Edited boundary object path (optional)", value=edited_path, on_value=set_edited_path)
                with solara.Row():
                    solara.Button("Approve boundary", on_click=lambda: decide("approve"), color="primary")
                    solara.Button("Reject", on_click=lambda: decide("reject"), color="error")
                    solara.Button("Request changes", on_click=lambda: decide("request_changes"))
                solara.Markdown(
                    "Approval is blocked unless geometry validity, CRS, coverage and lineage checks all pass. "
                    "This is only the boundary gate; other Gold checks still apply."
                )

        with solara.Card():
            solara.Markdown("### 5. Propose an approximate boundary (only if no authoritative candidate is suitable)")
            solara.Warning(
                "This creates a provisional geometry, not an official settlement or project boundary. "
                "It must be reviewed and approved before it can be considered for Gold."
            )
            solara.InputTextArea(
                label="Local reference-layer paths (one per line)",
                value=approximation_sources,
                on_value=set_approximation_sources,
                rows=4,
            )
            solara.InputText(
                label="New output path (.geojson or .gpkg)",
                value=approximation_output,
                on_value=set_approximation_output,
            )
            solara.InputFloat(
                label="Assumed buffer distance (metres)",
                value=approximation_buffer,
                on_value=set_approximation_buffer,
            )
            solara.Checkbox(
                label="I explicitly authorize creation of a provisional approximate boundary from these reference layers.",
                value=approximation_permission,
                on_value=set_approximation_permission,
            )

            def generate_approximation():
                set_error("")
                set_approximation_result(None)
                try:
                    paths = [item.strip() for item in approximation_sources.splitlines() if item.strip()]
                    result = propose_approximate_boundary(
                        paths,
                        output_path=approximation_output.strip(),
                        permission_granted=approximation_permission,
                        buffer_metres=float(approximation_buffer),
                    )
                    set_approximation_result(result)
                    set_status(
                        "Provisional boundary generated. It is not approved and cannot pass the Gold boundary gate."
                    )
                except Exception as exc:
                    set_error(f"{type(exc).__name__}: {exc}")

            solara.Button(
                "Generate provisional boundary",
                on_click=generate_approximation,
                disabled=not (approximation_permission and bool(approximation_sources.strip()) and bool(approximation_output.strip())),
                color="primary",
            )
            if approximation_result:
                solara.Markdown(
                    "**Approximation result**\
\
"
                    f"- Output: {approximation_result.get('path')}\
"
                    f"- Method: {approximation_result.get('method')}\
"
                    f"- Features used: {approximation_result.get('feature_count')}\
"
                    f"- CRS: {approximation_result.get('crs')}\
"
                    f"- Bounds: {approximation_result.get('bounds')}\
"
                    f"- Metadata: {approximation_result.get('metadata_path')}\
"
                    f"- Gold release allowed: {approximation_result.get('gold_release_allowed')}\
\
"
                    f"**Uncertainty:** {'; '.join(approximation_result.get('uncertainty_notes', []))}"
                )

        if status:
            solara.Info(status)
        if error:
            solara.Error(error)
