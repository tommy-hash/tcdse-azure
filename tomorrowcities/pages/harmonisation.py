"""TCDSE Harmoniser: guided, non-destructive data profiling and review."""
from io import BytesIO
import json
from pathlib import PurePosixPath
import re
import zipfile

import pandas as pd
import solara

from ..components.file_drop import FileDrop

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
        return pd.read_csv(buf, sep="\\t", nrows=MAX_ROWS)
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
            cleaned = re.sub(r"(?i)KES|KSh|[,\\s]", "", str(v)).strip()
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
            m = re.fullmatch(r"\\s*(-?\\d+(?:\\.\\d+)?)\\s*mm\\s*", str(raw), flags=re.I) if pd.notna(raw) else None
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
def AssistantPanel(filename, dataframe, suggestions, flags):
    with solara.Card():
        solara.Markdown("### ✨ Harmonisation assistant")
        if dataframe is None:
            solara.Markdown("Upload a dataset and I’ll explain its structure, suggest likely field mappings and highlight decisions that need your confirmation. I will not invent missing values or silently overwrite source data.")
        else:
            solara.Markdown(f"**Dataset:** {filename}")
            solara.Markdown(f"Detected **{len(dataframe):,} rows**, **{len(dataframe.columns)} fields**, and **{int(dataframe.isna().sum().sum())} missing cells**.")
            solara.Markdown(f"Current workspace has **{len(suggestions)} mapping suggestions** and **{len(flags)} review flags**.")
            solara.Markdown("**Suggested next action:** inspect field mappings and units, then apply conservative rules. Ambiguous values remain unresolved.")
        solara.Markdown("> This first version uses transparent local rules, not a live conversational AI service. An AI endpoint can be connected in a later stage.")


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

    def reset():
        set_filename(""); set_df(None); set_error(""); set_notice("")
        set_package_df(None); set_package_note("")
        set_suggestions(pd.DataFrame()); set_output(None)
        set_audit(pd.DataFrame()); set_flags(pd.DataFrame()); set_stage(1)

    def on_file(info):
        reset()
        try:
            name, content = str(info.get("name", "uploaded file")), info.get("data")
            if content is None:
                raise ValueError("No file content was received.")
            if _suffix(name) in {".mpk", ".mpkx"}:
                p, note = _inspect_package(content, name)
                set_filename(name); set_package_df(p); set_package_note(note)
                set_notice("Package inventory complete; no files were extracted.")
                set_stage(2); return
            data = _read_dataset(name, content)
            if not len(data.columns):
                raise ValueError("No columns were detected.")
            set_filename(name); set_df(data)
            set_notice(f"Loaded {len(data):,} rows in memory. The source file has not been changed.")
            set_stage(2)
        except Exception as exc:
            set_error(str(exc))

    def map_fields():
        if df is not None:
            set_suggestions(_mappings(df)); set_stage(3)

    def run_rules():
        if df is not None:
            result, changes, review = _harmonise(df)
            set_output(result); set_audit(changes); set_flags(review); set_stage(4)

    stages = ["Upload", "Profile", "Map fields", "Review & harmonise", "Export"]
    with solara.Column(gap="16px", style={"max-width": "1440px", "margin": "0 auto", "padding": "8px 12px 28px"}):
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
        with solara.ColumnsResponsive(12, small=12, medium=8, large=8):
            with solara.Column(gap="12px"):
                with solara.Card():
                    solara.Markdown("## 1. Add your data")
                    solara.Markdown("**Supported now:** CSV, TSV, delimited TXT, XLSX, JSON, JSON Lines and GeoJSON. **Upload limit:** 25 MB. Data stays in session memory.")
                    FileDrop(label="Drop a file here, or click to browse", on_file=on_file, lazy=False, uid="tcdse-harmoniser-upload-v2")
                    if notice: solara.Success(notice)
                    if error: solara.Error(error)
                    solara.Markdown("**Planned adapters:** SQLite table discovery; Access .mdb; read-only SQL Server/PostgreSQL; ArcGIS layer extraction from .mpk/.mpkx. These database and GIS capabilities are not yet fully enabled.")
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
                            solara.FileDownload(data=output.to_csv(index=False), filename="tcdse_harmonised_preview.csv", mime_type="text/csv")
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
                AssistantPanel(filename=filename, dataframe=df, suggestions=suggestions, flags=flags)
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
