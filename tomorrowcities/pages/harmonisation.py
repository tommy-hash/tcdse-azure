"""TCDSE Harmoniser: guided, non-destructive data profiling and review."""
from io import BytesIO
import json
from pathlib import PurePosixPath
import re
import zipfile

import pandas as pd
import solara

from ..components.file_drop import FileDropMultiple
from ..services.bronze_upload import list_raw_batches, upload_raw_batch, verify_raw_batch
from ..services.silver_pipeline import run_silver_inventory, run_silver_gis_inspection, run_silver_gis_quality_assessment

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ROWS = 100_000


def _suffix(filename):
    name = PurePosixPath(str(filename).replace("\\", "/")).name.lower()
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def _read_json(content, line_delimited=False):
    text = content.decode("utf-8-sig")
    if line_delimited:
        rows = []
        for number, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON Lines record on line {number}.") from exc
        if not rows:
            raise ValueError("No JSON records found.")
        return pd.json_normalize(rows, sep=".")
    obj = json.loads(text)
    if isinstance(obj, list):
        return pd.json_normalize(obj, sep=".")
    if isinstance(obj, dict) and obj.get("type") == "FeatureCollection":
        rows = []
        for feature in obj.get("features", []):
            row = dict(feature.get("properties") or {})
            geom = feature.get("geometry")
            row["__geometry_type"] = geom.get("type") if isinstance(geom, dict) else None
            row["__geometry_json"] = json.dumps(geom) if geom else None
            rows.append(row)
        return pd.DataFrame(rows)
    if isinstance(obj, dict) and isinstance(obj.get("data"), list):
        return pd.json_normalize(obj["data"], sep=".")
    if isinstance(obj, dict):
        return pd.json_normalize(obj, sep=".")
    raise ValueError("JSON root must be an object or array.")


def _inspect_package(content, filename):
    try:
        with zipfile.ZipFile(BytesIO(content)) as z:
            items = [x for x in z.infolist() if not x.is_dir()]
            if len(items) > 20000 or sum(x.file_size for x in items) > 500 * 1024 * 1024:
                raise ValueError("Package exceeds the safe inspection limits.")
            inventory = pd.DataFrame([{
                "package_entry": x.filename, "size_bytes": x.file_size,
                "file_type": PurePosixPath(x.filename).suffix.lower() or "(none)"
            } for x in items[:500]])
            return inventory, (
                f"{filename}: inspected {len(items):,} package entries. "
                "This is an archive inventory only; GIS layers have not been extracted or harmonised."
            )
    except zipfile.BadZipFile as exc:
        raise ValueError("This file is not a readable ZIP-based package. Some legacy MPK files require ArcGIS tooling.") from exc


def _read_dataset(filename, content):
    if not content:
        raise ValueError("The uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("File exceeds the 25 MB upload limit.")
    ext = _suffix(filename)
    buf = BytesIO(content)
    if ext == ".csv":
        return pd.read_csv(buf, nrows=MAX_ROWS)
    if ext == ".tsv":
        return pd.read_csv(buf, sep="\t", nrows=MAX_ROWS)
    if ext == ".txt":
        try:
            return pd.read_csv(buf, sep=None, engine="python", nrows=MAX_ROWS)
        except Exception as exc:
            raise ValueError("Delimiter could not be detected. Try CSV or TSV.") from exc
    if ext in {".xlsx", ".xls"}:
        try:
            return pd.read_excel(buf, nrows=MAX_ROWS)
        except ImportError as exc:
            raise ValueError("The Excel reader required for this file is not installed.") from exc
        except Exception as exc:
            raise ValueError("Excel file could not be read; check that it is not password-protected.") from exc
    if ext in {".json", ".jsonl", ".ndjson", ".geojson"}:
        try:
            return _read_json(content, line_delimited=ext in {".jsonl", ".ndjson"})
        except UnicodeDecodeError as exc:
            raise ValueError("File is not UTF-8 encoded.") from exc
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON syntax.") from exc
    raise ValueError("This file needs a different ingestion adapter. Currently supported: CSV, TSV, TXT, XLSX, JSON, JSONL, NDJSON and GeoJSON.")


def _profile(df):
    return pd.DataFrame([{
        "source_column": str(c), "inferred_type": str(df.iloc[:, i].dtype),
        "missing_count": int(df.iloc[:, i].isna().sum()),
        "missing_percent": round(float(df.iloc[:, i].isna().mean() * 100), 2),
        "distinct_values": int(df.iloc[:, i].nunique(dropna=True))
    } for i, c in enumerate(df.columns)])


def _mappings(df):
    rows = []
    for col in df.columns:
        key = re.sub(r"[^a-z0-9]+", "_", str(col).strip().lower()).strip("_")
        mapping = {
            "county": ("geography.county_name", "Validate against an authoritative geography list."),
            "county_name": ("geography.county_name", "Validate aliases against an authoritative geography list."),
            "sub_county": ("geography.sub_county_name", "Confirm administrative level."),
            "ward": ("geography.ward_name", "Confirm administrative level."),
            "hh_size": ("socioeconomic.household_size", "Positive integer expected; invalid values need review."),
            "household_size": ("socioeconomic.household_size", "Positive integer expected; invalid values need review."),
            "event": ("hazard.event_type", "Use a documented controlled vocabulary."),
            "event_type": ("hazard.event_type", "Use a documented controlled vocabulary."),
            "rainfall": ("climate.precipitation", "Confirm units and accumulation period."),
            "rainfall_mm": ("climate.precipitation", "Confirm units and accumulation period."),
            "female_headed": ("socioeconomic.household_head_gender", "Confirm category definitions."),
        }.get(key)
        if not mapping and "income" in key:
            mapping = ("socioeconomic.income_amount", "Confirm currency, period and statistic.")
        if mapping:
            rows.append({"source_field": str(col), "suggested_target": mapping[0],
                         "basis": "Field-name heuristic", "review_note": mapping[1],
                         "sample_values": ", ".join(df[col].dropna().astype(str).head(3).tolist())})
    return pd.DataFrame(rows, columns=["source_field", "suggested_target", "basis", "review_note", "sample_values"])


def _harmonise(df):
    out = df.copy(deep=True)
    audit, flags = [], []
    norm = {re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_"): c for c in df.columns}
    county = next((norm[k] for k in ("county", "county_name") if k in norm), None)
    if county:
        aliases = {"makueni county": "Makueni", "kitui cty": "Kitui",
                   "garissa county": "Garissa", "taita taveta": "Taita-Taveta"}
        values = []
        for idx, raw in df[county].items():
            new = aliases.get(str(raw).strip().lower()) if pd.notna(raw) else None
            values.append(new if new else raw)
            if new and str(raw) != new:
                audit.append({"row": idx, "field": str(county), "original": raw, "proposed": new,
                              "rule": "Exact demo alias", "status": "Applied"})
        out["harmonised_county"] = values
    size = next((norm[k] for k in ("hh_size", "household_size") if k in norm), None)
    if size:
        parsed = pd.to_numeric(df[size], errors="coerce")
        out["harmonised_household_size"] = parsed.astype("Int64")
        for idx, raw in df[size].items():
            if pd.notna(raw) and pd.isna(parsed.loc[idx]):
                flags.append({"row": idx, "field": str(size), "value": raw, "issue": "Invalid numeric value; human review required"})
    income = next((norm[k] for k in norm if "income" in k), None)
    if income:
        def amount(v):
            if pd.isna(v) or not str(v).strip():
                return pd.NA
            cleaned = re.sub(r"(?i)KES|KSh|[,\s]", "", str(v)).strip()
            try:
                return float(cleaned)
            except ValueError:
                return pd.NA
        out["harmonised_income_amount"] = pd.to_numeric(df[income].map(amount), errors="coerce")
        for idx, raw in df[income].items():
            if pd.notna(raw) and str(raw).strip() and pd.isna(out.loc[idx, "harmonised_income_amount"]):
                flags.append({"row": idx, "field": str(income), "value": raw, "issue": "Income parsing failed; human review required"})
    rainfall = next((norm[k] for k in norm if "rainfall" in k or "precipitation" in k), None)
    if rainfall:
        vals = []
        for idx, raw in df[rainfall].items():
            m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*mm\s*", str(raw), flags=re.I) if pd.notna(raw) else None
            vals.append(float(m.group(1)) if m else pd.NA)
            if pd.notna(raw) and str(raw).strip() and not m:
                flags.append({"row": idx, "field": str(rainfall), "value": raw, "issue": "Rainfall unit ambiguous; expected explicit mm"})
        out["harmonised_rainfall_mm"] = pd.to_numeric(pd.Series(vals, index=df.index), errors="coerce")
    event = next((norm[k] for k in ("event", "event_type", "hazard", "hazard_type") if k in norm), None)
    if event:
        aliases = {"drought": "Drought", "flooding": "Flood", "flash-flood": "Flash flood"}
        out["harmonised_event_type"] = df[event].map(lambda v: aliases.get(str(v).strip().lower(), v) if pd.notna(v) else v)
    female = next((norm[k] for k in norm if "female_headed" in k), None)
    if female:
        aliases = {"y": "Yes", "yes": "Yes", "n": "No", "no": "No"}
        out["harmonised_female_headed"] = df[female].map(lambda v: aliases.get(str(v).strip().lower(), v) if pd.notna(v) else v)
    return out, pd.DataFrame(audit), pd.DataFrame(flags, columns=["row", "field", "value", "issue"])


@solara.component
def AssistantPanel(datasets, suggestions, flags):
    with solara.Card():
        solara.Markdown("### ✨ Harmonisation assistant")
        if not datasets:
            solara.Markdown("Upload one or more datasets. I’ll summarise each file separately, show aggregate counts, suggest likely field mappings and highlight decisions that need your confirmation. I will not invent missing values or silently overwrite source data.")
        else:
            total_rows = sum(len(item["df"]) for item in datasets)
            total_missing = sum(int(item["df"].isna().sum().sum()) for item in datasets)
            solara.Markdown(f"**Workspace:** {len(datasets)} dataset(s)")
            solara.Markdown(f"Across all uploaded files: **{total_rows:,} rows**, **{total_missing:,} missing cells**.")
            solara.Markdown("**Per-file summary:**")
            for item in datasets:
                frame = item["df"]
                solara.Markdown(f"- **{item['name']}** — {len(frame):,} rows, {len(frame.columns)} fields, {int(frame.isna().sum().sum()):,} missing cells")
            solara.Markdown(f"Current workspace has **{len(suggestions)} mapping suggestions** and **{len(flags)} review flags**.")
            solara.Markdown("**Suggested next action:** inspect field mappings and units, then apply conservative rules. Ambiguous values remain unresolved.")
        solara.Markdown("> This first version uses transparent local rules, not a live conversational AI service.")

@solara.component
def Page():
    filename, set_filename = solara.use_state("")
    df, set_df = solara.use_state(None)
    error, set_error = solara.use_state("")
    notice, set_notice = solara.use_state("")
    package_df, set_package_df = solara.use_state(None)
    package_note, set_package_note = solara.use_state("")
    suggestions, set_suggestions = solara.use_state(pd.DataFrame())
    output, set_output = solara.use_state(None)
    audit, set_audit = solara.use_state(pd.DataFrame())
    flags, set_flags = solara.use_state(pd.DataFrame())
    stage, set_stage = solara.use_state(1)
    datasets, set_datasets = solara.use_state([])
    results, set_results = solara.use_state([])
    combined_output, set_combined_output = solara.use_state(None)
    assistant_question, set_assistant_question = solara.use_state("")
    assistant_answer, set_assistant_answer = solara.use_state("Upload files and ask about their profile, missing values, mapping suggestions or review flags.")
    bronze_notice, set_bronze_notice = solara.use_state("")
    bronze_error, set_bronze_error = solara.use_state("")
    bronze_batches, set_bronze_batches = solara.use_state([])
    checksum_results, set_checksum_results = solara.use_state([])
    silver_report, set_silver_report = solara.use_state(None)
    silver_error, set_silver_error = solara.use_state("")
    gis_catalogue, set_gis_catalogue = solara.use_state(None)
    gis_error, set_gis_error = solara.use_state("")
    gis_quality_report, set_gis_quality_report = solara.use_state(None)
    gis_quality_error, set_gis_quality_error = solara.use_state("")

    def refresh_bronze():
        try:
            set_bronze_batches(list_raw_batches())
            set_bronze_error("")
            set_bronze_notice("Import records refreshed from Azure Bronze.")
        except Exception as exc:
            set_bronze_error(f"Could not read Bronze import records: {exc}")

    def upload_bronze_files(infos):
        try:
            if not infos:
                raise ValueError("Choose at least one raw file.")
            files = []
            total = 0
            for info in infos:
                name, content = str(info.get("name", "uploaded file")), info.get("data")
                if content is None:
                    raise ValueError(f"No file content was received for {name}.")
                total += len(content)
                if total > 100 * 1024 * 1024:
                    raise ValueError("The combined raw upload exceeds 100 MB. Upload smaller batches.")
                files.append({"name": name, "data": content})
            result = upload_raw_batch(files, country="Kenya", project="Kalobeyei Settlement")
        except Exception as exc:
            set_bronze_error(f"Bronze upload failed: {exc}")
            set_bronze_notice("")
            return

        # Keep a successful upload distinct from a failed catalogue refresh.
        set_bronze_notice(
            f"Uploaded {len(result['files'])} original file(s). Batch: {result['batch_id']}. "
            f"Manifest: {result['manifest_path']}"
        )
        set_bronze_error("")
        set_checksum_results([])
        try:
            set_bronze_batches(list_raw_batches())
        except Exception as exc:
            set_bronze_error(f"Upload succeeded, but refreshing import records failed: {exc}")

    def run_silver_batch(manifest_path):
        try:
            report = run_silver_inventory(manifest_path)
            set_silver_report(report)
            set_silver_error("")
        except Exception as exc:
            set_silver_error(f"Silver processing failed: {exc}")
    def run_gis_inspection(inventory_path):
        try:
            catalogue = run_silver_gis_inspection(inventory_path)
            set_gis_catalogue(catalogue)
            set_gis_error("")
        except Exception as exc:
            set_gis_error(f"Silver GIS inspection failed: {exc}")

    def run_gis_quality(catalogue_path):
        try:
            report = run_silver_gis_quality_assessment(catalogue_path)
            set_gis_quality_report(report)
            set_gis_quality_error("")
        except Exception as exc:
            set_gis_quality_error(f"Silver GIS quality assessment failed: {exc}")

    def verify_bronze_batch(manifest_path):
        try:
            results = verify_raw_batch(manifest_path)
            set_checksum_results(results)
            failures = sum(1 for item in results if item.get("status") != "PASS")
            set_bronze_notice(f"Checksum verification finished: {len(results) - failures} passed; {failures} failed or errored.")
            set_bronze_error("")
        except Exception as exc:
            set_bronze_error(f"Checksum verification failed: {exc}")
            set_checksum_results([])

    def reset():
        set_filename(""); set_df(None); set_error(""); set_notice("")
        set_package_df(None); set_package_note("")
        set_suggestions(pd.DataFrame()); set_output(None)
        set_audit(pd.DataFrame()); set_flags(pd.DataFrame()); set_stage(1)
        set_datasets([]); set_results([]); set_combined_output(None)
        set_assistant_question("")
        set_assistant_answer("Upload files and ask about their profile, missing values, mapping suggestions or review flags.")

    def on_files(infos):
        reset()
        loaded, errors, packages = [], [], []
        total_bytes = 0
        for info in infos:
            name, content = str(info.get("name", "uploaded file")), info.get("data")
            try:
                if content is None:
                    raise ValueError("No file content was received.")
                total_bytes += len(content)
                if total_bytes > 100 * 1024 * 1024:
                    raise ValueError("The combined upload exceeds 100 MB. Upload smaller batches.")
                if _suffix(name) in {".mpk", ".mpkx"}:
                    p, note = _inspect_package(content, name)
                    packages.append({"name": name, "inventory": p, "note": note})
                    continue
                data = _read_dataset(name, content)
                if not len(data.columns):
                    raise ValueError("No columns were detected.")
                loaded.append({"name": name, "df": data})
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        set_datasets(loaded)
        set_package_df(packages[0]["inventory"] if packages else None)
        set_package_note("\\n".join(x["note"] for x in packages))
        if loaded:
            set_filename(loaded[0]["name"] if len(loaded) == 1 else f"{len(loaded)} datasets")
            set_df(loaded[0]["df"])
            set_notice(f"Loaded {len(loaded)} dataset(s) and {len(packages)} package(s). Source files remain unchanged.")
            set_stage(2)
        if errors:
            set_error("\\n".join(errors))
        if not loaded and not packages and not errors:
            set_error("No files were received.")

    def map_fields():
        if datasets:
            rows = []
            for item in datasets:
                mapped = _mappings(item["df"])
                if not mapped.empty:
                    mapped.insert(0, "source_file", item["name"])
                    rows.extend(mapped.to_dict("records"))
            set_suggestions(pd.DataFrame(rows, columns=["source_file", "source_field", "suggested_target", "basis", "review_note", "sample_values"]))
            set_stage(3)

    def run_rules():
        if datasets:
            processed = []
            all_audit, all_flags = [], []
            for item in datasets:
                result, changes, review = _harmonise(item["df"])
                result.insert(0, "__source_file", item["name"])
                if not changes.empty:
                    changes.insert(0, "source_file", item["name"])
                    all_audit.extend(changes.to_dict("records"))
                if not review.empty:
                    review.insert(0, "source_file", item["name"])
                    all_flags.extend(review.to_dict("records"))
                processed.append({"name": item["name"], "df": result})
            set_results(processed)
            set_output(processed[0]["df"] if processed else None)
            set_audit(pd.DataFrame(all_audit))
            set_flags(pd.DataFrame(all_flags))
            set_stage(4)

    def combine_compatible():
        if not results:
            set_error("Run the safe-rule review before combining files.")
            return
        first_cols = list(results[0]["df"].columns)
        first_schema = {str(c).strip().lower() for c in first_cols}
        incompatible = [x["name"] for x in results[1:] if {str(c).strip().lower() for c in x["df"].columns} != first_schema]
        if incompatible:
            set_error("Cannot append these files safely because their column sets differ: " + ", ".join(incompatible) + ". Review mappings and align schemas first.")
            return
        set_combined_output(pd.concat([x["df"] for x in results], ignore_index=True, sort=False))
        set_error("")
        set_notice(f"Combined {len(results)} files by appending rows. The __source_file column preserves provenance; this is not a relational join.")

    def ask_assistant():
        q = assistant_question.strip().lower()
        if not q:
            set_assistant_answer("Enter a question about the uploaded data first.")
            return
        if not datasets:
            set_assistant_answer("Upload one or more datasets first. I can then summarise the profiles and rule-based review flags.")
        elif any(term in q for term in ("missing", "null", "empty")):
            total = sum(int(x["df"].isna().sum().sum()) for x in datasets)
            set_assistant_answer(f"Across {len(datasets)} uploaded dataset(s), {total:,} cells are missing according to the current parser. This count does not determine why values are missing or whether imputation is appropriate.")
        elif any(term in q for term in ("flag", "review", "issue", "problem", "error")):
            set_assistant_answer(f"The current rule pass has {len(flags)} review flag(s). Run ‘Apply safe rules & show review flags’ to refresh them. Review flags are evidence for inspection, not automatic corrections.")
        elif any(term in q for term in ("map", "field", "column", "schema")):
            set_assistant_answer(f"There are {len(suggestions)} field-mapping suggestion(s). They are based on field-name heuristics and need confirmation of meaning, units, geography and time period.")
        elif any(term in q for term in ("combine", "merge", "join")):
            set_assistant_answer("The current controlled combine operation appends rows only when processed files have matching column sets. It preserves a __source_file provenance column. Relational joins by household ID, geography or time are not enabled yet because keys and cardinality must be validated first.")
        else:
            set_assistant_answer(f"I can currently answer profile questions for {len(datasets)} dataset(s), report missing-cell counts, explain rule-based review flags, summarise mapping suggestions and describe safe row-appending. This panel is a local prototype, not yet connected to a live AI model.")

    stages = ["Upload", "Profile", "Map fields", "Review & harmonise", "Export"]
    with solara.Column(gap="16px", style={"width": "100%", "max-width": "none", "min-width": "0", "margin": "0", "padding": "8px 20px 28px", "box-sizing": "border-box"}):
        with solara.Row(style={"align-items": "center", "justify-content": "space-between", "flex-wrap": "wrap"}):
            with solara.Column(gap="2px"):
                solara.Markdown("# TCDSE Harmoniser")
                solara.Markdown("A guided workspace for socio-economic, climate, infrastructure and GIS data.")
            solara.Button("New dataset", icon_name="mdi-refresh", on_click=reset, outlined=True)
        with solara.Row(style={"gap": "6px", "flex-wrap": "wrap"}):
            for i, label in enumerate(stages, 1):
                mark = "✓ " if stage > i else ("● " if stage == i else "")
                solara.Button(f"{mark}{i}. {label}", text=True, outlined=(stage == i),
                              disabled=(i > 2 and df is None), on_click=lambda n=i: set_stage(n),
                              style={"border-radius": "22px", "text-transform": "none"})
        with solara.ColumnsResponsive(12, small=12, medium=12, large=12):
            with solara.Column(gap="12px"):
                with solara.Card():
                    solara.Markdown("## Bronze raw-file import")
                    solara.Markdown("Import the original Kalobeyei source files here, even when they cannot yet be profiled. Files are stored unchanged in Azure Data Lake Storage; this step does not extract, harmonise or train models.")
                    solara.Markdown("**Destination:** Kenya / Kalobeyei Settlement · **Limit:** 100 MB combined per batch")
                    FileDropMultiple(label="Drop raw Kalobeyei files here, or click to browse", on_file=upload_bronze_files, lazy=False, uid="tcdse-bronze-raw-upload-v1")
                    with solara.Row(style={"gap": "8px", "flex-wrap": "wrap"}):
                        solara.Button("Refresh import records", on_click=refresh_bronze, color="primary")
                    if bronze_notice:
                        solara.Success(bronze_notice)
                    if bronze_error:
                        solara.Error(bronze_error)
                    if bronze_batches:
                        solara.Markdown("### Import records")
                        solara.DataFrame(pd.DataFrame([{
                            "batch_id": b.get("batch_id", ""),
                            "created_utc": b.get("created_utc", ""),
                            "country": b.get("country", ""),
                            "project": b.get("project", ""),
                            "files": b.get("file_count", 0),
                            "total_bytes": b.get("total_bytes", 0),
                            "status": b.get("status", ""),
                            "manifest_path": b.get("manifest_path", ""),
                        } for b in bronze_batches]))
                        for batch in bronze_batches:
                            with solara.Details(f"Batch {batch.get('batch_id', 'unknown')} — {batch.get('file_count', 0)} file(s)", expand=False):
                                if batch.get("files"):
                                    solara.DataFrame(pd.DataFrame(batch["files"]))
                                solara.Button("Verify SHA-256 checksums", on_click=lambda path=batch.get("manifest_path", ""): verify_bronze_batch(path), color="primary")
                                solara.Button("Run Bronze to Silver inventory", on_click=lambda path=batch.get("manifest_path", ""): run_silver_batch(path), color="primary", disabled=batch.get("status") == "purged")
                    if silver_error:
                        solara.Error(silver_error)
                    if silver_report:
                        solara.Markdown("### Silver inventory result")
                        solara.Markdown(f"**Status:** {silver_report.get('processing_status')} · **Source files:** {silver_report.get('source_file_count')} · **Extracted archive members:** {silver_report.get('extracted_member_count')}")
                        solara.Markdown(f"**Silver report:** `{silver_report.get('report_path')}`")
                        solara.Markdown("**Candidate shapefile layers:** " + (", ".join(silver_report.get("candidate_shapefile_layers", [])) or "None detected"))
                        solara.DataFrame(pd.DataFrame([{"source": f.get("source_filename", ""), "type": f.get("source_type", ""), "status": f.get("processing_status", ""), "extracted_members": len(f.get("extracted_members", [])), "warnings": " | ".join(f.get("warnings", []))} for f in silver_report.get("files", [])]))
                        solara.Button("Inspect Silver GIS layers", on_click=lambda path=silver_report.get("report_path", ""): run_gis_inspection(path), color="primary", disabled=not silver_report.get("report_path"))
                        solara.Markdown("Gold outputs are not generated at this stage; mappings and domain rules must be validated first.")
                    if gis_error:
                        solara.Error(gis_error)
                    if gis_catalogue:
                        solara.Markdown("### Silver GIS layer inspection")
                        solara.Markdown(f"**Inspected:** {gis_catalogue.get('inspected_layer_count')} / {gis_catalogue.get('candidate_layer_count')} · **Failed:** {gis_catalogue.get('failed_layer_count')} · **Elapsed:** {gis_catalogue.get('elapsed_seconds')} s")
                        solara.Markdown(f"\`**Catalogue:** {gis_catalogue.get('report_path')}\`")
                        solara.DataFrame(pd.DataFrame([{"layer": item.get("layer_path"), "status": item.get("inspection_status"), "features": item.get("feature_count"), "geometry": item.get("geometry_type"), "CRS": item.get("crs_authority") or item.get("crs_name"), "fields": item.get("field_count"), "initial suitability": (item.get("initial_suitability") or {}).get("category"), "warnings": " | ".join(item.get("warnings", []))} for item in gis_catalogue.get("layers", [])]))
                        with solara.Details("Inspect field definitions", expand=False):
                            for item in gis_catalogue.get("layers", []):
                                solara.Markdown(f"**{item.get('layer_path')}**")
                                if item.get("fields"):
                                    solara.DataFrame(pd.DataFrame(item["fields"]))
                                else:
                                    solara.Warning("No field definitions returned; treat this layer as not ready for analytical use.")
                        solara.Button("Run GIS quality assessment", on_click=lambda path=gis_catalogue.get("report_path", ""): run_gis_quality(path), color="primary", disabled=not gis_catalogue.get("report_path"))
                    if gis_quality_error:
                        solara.Error(gis_quality_error)
                    if gis_quality_report:
                        solara.Markdown("### Silver GIS quality assessment")
                        solara.Markdown(f"**Layers:** {gis_quality_report.get('layer_count')} · **Blocked:** {gis_quality_report.get('blocked_layer_count')} · **With warnings:** {gis_quality_report.get('layers_with_warnings')} · **Elapsed:** {gis_quality_report.get('elapsed_seconds')} s")
                        solara.Markdown("Quality report: " + str(gis_quality_report.get("report_path")))
                        solara.DataFrame(pd.DataFrame([{"layer": item.get("layer_path"), "QA status": item.get("qa_status"), "features": item.get("feature_count_actual"), "CRS": item.get("crs", {}).get("authority") or item.get("crs", {}).get("value"), "null/empty geometry": (item.get("geometry", {}).get("null_count", 0) + item.get("geometry", {}).get("empty_count", 0)), "invalid geometry": item.get("geometry", {}).get("invalid_count"), "fields": len(item.get("fields", [])), "blockers": " | ".join(item.get("blockers", [])), "warnings": " | ".join(item.get("warnings", []))} for item in gis_quality_report.get("layers", [])]))
                        solara.Markdown("### Gold readiness — candidate screening only")
                        solara.DataFrame(pd.DataFrame([{"Gold artifact": item.get("gold_artifact"), "candidate layers": item.get("candidate_count"), "readiness": item.get("readiness"), "decision": item.get("decision"), "reason": item.get("reason")} for item in gis_quality_report.get("gold_readiness", [])]))
                        with solara.Details("Field completeness and sample values", expand=False):
                            for item in gis_quality_report.get("layers", []):
                                solara.Markdown(f"**{item.get('layer_path')}** — {item.get('qa_status')}")
                                if item.get("fields"):
                                    solara.DataFrame(pd.DataFrame(item["fields"]))
                                if item.get("sample_records"):
                                    solara.DataFrame(pd.DataFrame(item["sample_records"]))
                        solara.Warning("Gold generation remains disabled until CRS, field meaning, geometry quality and output rules are reviewed.")

                    if checksum_results:
                        solara.Markdown("### Checksum verification results")
                        solara.DataFrame(pd.DataFrame(checksum_results))
                        if all(item.get("status") == "PASS" for item in checksum_results):
                            solara.Success("All files in the selected batch match their recorded SHA-256 checksums and byte sizes.")
                        else:
                            solara.Warning("One or more files could not be verified. Review the status and details above.")
                with solara.Card():
                    solara.Markdown("## 1. Add your data")
                    solara.Markdown("**Supported now:** CSV, TSV, delimited TXT, XLSX, JSON, JSON Lines and GeoJSON. Up to 25 MB per file and 100 MB combined per upload batch. Files are profiled separately first.")
                    FileDropMultiple(label="Drop multiple files here, or click to browse", on_file=on_files, lazy=False, uid="tcdse-harmoniser-upload-v3")
                    if notice: solara.Success(notice)
                    if error: solara.Error(error)
                    solara.Markdown("**Planned adapters:** SQLite table discovery; Access .mdb; read-only SQL Server/PostgreSQL; ArcGIS layer extraction from .mpk/.mpkx. These database and GIS capabilities are not yet fully enabled.")
                if datasets:
                    with solara.Card():
                        solara.Markdown("## 2. Uploaded datasets")
                        solara.Markdown("Each file remains a separate dataset unless you explicitly combine compatible schemas.")
                        solara.DataFrame(pd.DataFrame([{"file": item["name"], "rows": len(item["df"]), "columns": len(item["df"].columns), "missing_cells": int(item["df"].isna().sum().sum()), "duplicate_column_names": int(item["df"].columns.duplicated().sum())} for item in datasets]))
                        for item in datasets:
                            with solara.Details(f"Profile: {item['name']}", expand=False):
                                solara.DataFrame(_profile(item["df"]))
                                solara.DataFrame(item["df"].head(10))
                    # The legacy single-file profile uses df, while multi-file
                    # uploads are stored in datasets. Guard against transient
                    # state updates where datasets is populated before df.
                    if df is not None:
                        with solara.Card():
                            solara.Markdown("## 2. Source profile")
                            with solara.Row(style={"gap": "8px", "flex-wrap": "wrap"}):
                                for label, value in [("Rows", f"{len(df):,}"), ("Columns", str(len(df.columns))),
                                                     ("Missing cells", f"{int(df.isna().sum().sum()):,}"),
                                                     ("Duplicate column names", str(int(df.columns.duplicated().sum())))]:
                                    with solara.Card():
                                        solara.Markdown(f"**{label}**")
                                        solara.Markdown(f"### {value}")
                            solara.DataFrame(_profile(df))
                            with solara.Details("Preview source rows", expand=False):
                                solara.DataFrame(df.head(20))
                    with solara.Card():
                        solara.Markdown("## 3. Map source fields")
                        solara.Markdown("Suggestions are based on field names and sample values. Confirm meaning, units, geography and time period before applying mappings.")
                        solara.Button("Suggest field mappings", icon_name="mdi-graph", on_click=map_fields, color="primary")
                        if not suggestions.empty:
                            solara.DataFrame(suggestions)
                    if output is not None:
                        with solara.Card():
                            solara.Markdown("## 4. Review & harmonise")
                            solara.Markdown("Original columns are retained. Proposed values are added in new columns; uncertain values are flagged.")
                            solara.DataFrame(output.head(100))
                            solara.Markdown(f"**Audit entries:** {len(audit)} · **Review flags:** {len(flags)}")
                            if not audit.empty:
                                solara.Markdown("### Transformation audit")
                                solara.DataFrame(audit)
                            if not flags.empty:
                                solara.Markdown("### Needs human review")
                                solara.DataFrame(flags)
                            else:
                                solara.Success("No values flagged by these rules. This does not guarantee semantic correctness.")
                            with solara.Card():
                                solara.Markdown("## 5. Export results")
                                solara.Markdown("Download the processed CSV. For multi-file uploads, each dataset has its own download; combining is optional and only appends rows when column sets match.")
                                solara.FileDownload(data=output.to_csv(index=False), filename="tcdse_harmonised_preview.csv", mime_type="text/csv")
                                solara.Markdown("**Download processed file:** use the download control above.")
                                if len(results) > 1:
                                    solara.Markdown("### Per-file exports")
                                    for item in results:
                                        solara.Markdown(f"**{item['name']}** — {len(item['df']):,} rows")
                                        solara.FileDownload(data=item["df"].to_csv(index=False), filename=f"harmonised_{PurePosixPath(item['name']).stem}.csv", mime_type="text/csv")
                                    solara.Button("Combine files by appending rows (matching schemas only)", on_click=combine_compatible, color="primary", block=True)
                                    if combined_output is not None:
                                        solara.Markdown(f"Combined output: **{len(combined_output):,} rows** across {len(results)} files.")
                                        solara.FileDownload(data=combined_output.to_csv(index=False), filename="tcdse_combined_harmonised.csv", mime_type="text/csv")
                if package_df is not None:
                    with solara.Card():
                        solara.Markdown("## ArcGIS package inspection")
                        solara.Info(package_note)
                        solara.DataFrame(package_df)
                        solara.Markdown("Container inventory only: this does not confirm that GIS layers were extracted, geometries validated or attributes harmonised.")
                if df is None and package_df is None:
                    with solara.Card():
                        solara.Markdown("## Your workflow")
                        solara.Markdown("1. Upload data.\\n2. Review the profile.\\n3. Generate mapping suggestions.\\n4. Apply conservative rules.\\n5. Review flags and export.")
            with solara.Column(gap="12px"):
                AssistantPanel(datasets=datasets, suggestions=suggestions, flags=flags)
                with solara.Card():
                    solara.Markdown("### Ask the Assistant")
                    solara.Markdown("Ask about missing values, mapping suggestions, review flags or combining datasets. This first interface uses local rule-based answers; live AI integration is a separate, upcoming step.")
                    solara.InputText(label="Your question", value=assistant_question, on_value=set_assistant_question)
                    solara.Button("Ask", on_click=ask_assistant, color="primary", block=True)
                    solara.Markdown(assistant_answer)
                with solara.Card():
                    solara.Markdown("### Evidence-constrained safeguards")
                    solara.Markdown("- Preserve original values.\\n- Never fill missing observations automatically.\\n- Flag ambiguous units and invalid values.\\n- Record transformation rules.\\n- Require human review where meaning is uncertain.")
                if df is not None:
                    with solara.Card():
                        solara.Markdown("### Recommended action")
                        solara.Markdown("Generate field mappings, review the suggestions, then run the conservative rule pass.")
                        solara.Button("Apply safe rules & show review flags", icon_name="mdi-check-decagram", on_click=run_rules, color="primary", block=True)
                with solara.Card():
                    solara.Markdown("### Implementation roadmap")
                    solara.Markdown("**Next:** SQLite table discovery and full ArcGIS layer extraction.\\n\\n**After validation:** Access .mdb and read-only SQL connections.\\n\\n**Assistant:** this panel currently gives local, transparent guidance; a live AI endpoint is a separate integration.")
