"""First-pass Bronze-to-Silver preparation for Kalobeyei raw batches.

Preserves Bronze objects. Extracts safe archive members from MPK/ZIP-like files
and writes a machine-readable inventory to Silver. It does not fabricate Gold data.
"""
import hashlib
import json
import os
import posixpath
import re
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath


def _storage():
    account_url = os.getenv("TCDSE_ADLS_ACCOUNT_URL", "").strip().rstrip("/")
    container_name = os.getenv("TCDSE_ADLS_FILESYSTEM", "").strip()
    if not account_url or not container_name:
        raise RuntimeError("Set TCDSE_ADLS_ACCOUNT_URL and TCDSE_ADLS_FILESYSTEM.")
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    if ".dfs." in account_url:
        account_url = account_url.replace(".dfs.", ".blob.")
    client = BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())
    return client, client.get_container_client(container_name)


def _safe_member(name):
    normal = name.replace("\\", "/")
    if normal.startswith("/") or re.match(r"^[A-Za-z]:", normal):
        return None
    parts = [p for p in normal.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return "/".join(parts) if parts else None


def _classify(name):
    ext = PurePosixPath(name).suffix.lower()
    return {
        ".pdf": "document",
        ".mpk": "arcgis_map_package",
        ".mpkx": "arcgis_map_package",
        ".mdb": "access_database",
        ".accdb": "access_database",
        ".shp": "esri_shapefile_geometry",
        ".shx": "esri_shapefile_index",
        ".dbf": "attribute_table",
        ".prj": "coordinate_reference_system",
        ".cpg": "encoding_metadata",
        ".geojson": "geojson",
        ".json": "json",
        ".xlsx": "excel_workbook",
        ".xls": "excel_workbook",
        ".csv": "tabular_data",
        ".tif": "raster",
        ".tiff": "raster",
        ".lyr": "arcgis_layer_definition",
        ".lyrx": "arcgis_layer_definition",
        ".gdb": "file_geodatabase_component",
    }.get(ext, "other")



def _extract_with_7z(payload, source_name, silver_base, container, report):
    """Extract an MPK/ZIP package with 7-Zip as the primary extraction engine."""
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="tcdse-mpk-") as work:
        root = Path(work)
        # Keep the original extension for diagnostics; 7-Zip detects the real format by signature.
        suffix = PurePosixPath(source_name).suffix or ".mpk"
        archive_path = root / f"package{suffix}"
        output_dir = root / "contents"
        output_dir.mkdir()
        archive_path.write_bytes(payload)
        # 7z signature: 37 7A BC AF 27 1C. Do not assume an MPK is a ZIP file.
        signature = payload[:6]
        if signature == bytes.fromhex("377ABCAF271C"):
            report["archive_format"] = "7z"
        elif any(payload.startswith(bytes.fromhex(sig)) for sig in ("504B0304", "504B0506", "504B0708")):
            report["archive_format"] = "zip"
        else:
            report["archive_format"] = "unknown"
        seven_zip = shutil.which("7z") or shutil.which("7zz")
        if not seven_zip:
            raise RuntimeError("No 7-Zip executable found (checked 7z and 7zz).")
        try:
            result = subprocess.run(
                [seven_zip, "x", "-y", "-bd", f"-o{output_dir}", str(archive_path)],
                capture_output=True, text=True, timeout=300, check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("7-Zip (7z) is not installed in this container image.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("7-Zip extraction exceeded the 300-second safety limit.") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "No diagnostic output").strip()[-1200:]
            raise RuntimeError(f"7-Zip extraction failed: {detail}")

        members, total_bytes = [], 0
        source_stem = PurePosixPath(source_name).stem
        for current, _, filenames in os.walk(output_dir):
            for filename in filenames:
                local_path = Path(current) / filename
                relative = local_path.relative_to(output_dir).as_posix()
                safe = _safe_member(relative)
                if not safe:
                    report["warnings"].append(f"Skipped unsafe extracted path: {relative}")
                    continue
                if local_path.is_symlink() or not local_path.resolve().is_relative_to(output_dir.resolve()):
                    report["warnings"].append(f"Skipped symlink or path outside extraction directory: {relative}")
                    continue
                size = local_path.stat().st_size
                if size > 250 * 1024 * 1024:
                    report["warnings"].append(f"Skipped member over 250 MB: {safe}")
                    continue
                total_bytes += size
                if total_bytes > 1024 * 1024 * 1024 or len(members) >= 20000:
                    raise RuntimeError("Extraction exceeded the 1 GB or 20,000-member safety limit.")
                target = f"{silver_base}/extracted/{source_stem}/{safe}"
                with local_path.open("rb") as stream:
                    data = stream.read()
                container.get_blob_client(target).upload_blob(data, overwrite=True)
                members.append({
                    "member": safe, "silver_path": target, "size_bytes": size,
                    "sha256": hashlib.sha256(data).hexdigest(), "type": _classify(safe),
                })
        report["extracted_members"].extend(members)
        return len(members)

def run_silver_inventory(manifest_path):
    """Create a Silver inventory and safely extract archive members from one batch."""
    started = time.perf_counter()
    client, container = _storage()
    try:
        manifest = json.loads(container.get_blob_client(manifest_path).download_blob().readall().decode("utf-8"))
        if manifest.get("processing_status") == "purged":
            raise ValueError("This Bronze batch has been purged and cannot be processed.")
        batch_id = manifest.get("batch_id")
        if not batch_id:
            raise ValueError("Bronze manifest has no batch_id.")
        safe_batch = re.sub(r"[^A-Za-z0-9_-]", "_", batch_id)
        silver_base = f"silver/Kenya/Kalobeyei_Settlement/{safe_batch}"
        file_reports = []
        extracted_count = 0
        for source in manifest.get("files", []):
            blob_path = source.get("path", "")
            payload = container.get_blob_client(blob_path).download_blob().readall()
            actual_hash = hashlib.sha256(payload).hexdigest()
            if actual_hash != source.get("sha256") or len(payload) != int(source.get("size_bytes", -1)):
                raise ValueError(f"Bronze integrity check failed for {source.get('original_filename', blob_path)}; Silver processing stopped.")
            name = source.get("original_filename") or PurePosixPath(blob_path).name
            report = {
                "source_filename": name, "source_path": blob_path, "source_sha256": actual_hash,
                "size_bytes": len(payload), "source_type": _classify(name),
                "processing_status": "inventoried", "extracted_members": [], "warnings": [],
            }
            ext = PurePosixPath(name).suffix.lower()
            if ext in {".mpk", ".mpkx", ".zip"}:
                try:
                    extracted = _extract_with_7z(payload, name, silver_base, container, report)
                    report["processing_status"] = "extracted" if extracted else "inventoried"
                    if not extracted:
                        report["warnings"].append("7-Zip completed but no files were extracted.")
                except Exception as extraction_error:
                    report["processing_status"] = "inventory_only"
                    report["warnings"].append(f"7-Zip extraction failed: {extraction_error}")
            elif ext in {".mdb", ".accdb"}:
                report["processing_status"] = "inventory_only"
                report["warnings"].append("Access database identified; table/relationship extraction requires a compatible read-only Access driver or mdbtools runtime.")
            elif ext == ".pdf":
                report["processing_status"] = "inventory_only"
                report["warnings"].append("PDF preserved in Bronze; text extraction is not yet enabled in this first-pass pipeline.")
            file_reports.append(report)

        all_members = [m for r in file_reports for m in r["extracted_members"]]
        candidate_layers = sorted({str(PurePosixPath(m["member"]).with_suffix("")) for m in all_members if PurePosixPath(m["member"]).suffix.lower() == ".shp"})
        report_doc = {
            "pipeline": "bronze-to-silver-inventory-v1",
            "batch_id": batch_id, "source_manifest": manifest_path,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "silver_base": silver_base, "processing_status": "completed_with_warnings" if any(r["warnings"] for r in file_reports) else "completed",
            "source_file_count": len(file_reports), "extracted_member_count": extracted_count,
            "candidate_shapefile_layers": candidate_layers,
            "files": file_reports,
            "gold_readiness": {
                "expected_geojson": ["exclusion_lines.geojson", "exclusion_points.geojson", "exclusion_polygons.geojson", "sample_landuse_file.geojson"],
                "expected_workbook": "sample_distribution_file.xlsx",
                "status": "not_generated",
                "reason": "Gold artifacts require verified source layers, attribute mappings, CRS and domain rules. This pipeline only inventories/extracts source evidence.",
            },
        }
        report_path = f"{silver_base}/silver_inventory.json"
        container.get_blob_client(report_path).upload_blob(json.dumps(report_doc, indent=2).encode("utf-8"), overwrite=True)
        return report_doc | {"report_path": report_path}
    finally:
        client.close()




def _parse_ogrinfo_summary(output):
    """Parse common GDAL/OGR text summary output without mistaking CRS lines for fields."""
    feature_match = re.search(r"^\s*Feature Count:\s*(\d+)\s*$", output, re.MULTILINE)
    geometry_match = re.search(r"^\s*Geometry:\s*(.+?)\s*$", output, re.MULTILINE)
    epsg_match = re.search(r'ID\["EPSG",\s*(\d+)\]', output)
    srs_match = re.search(r"^\s*(?:Layer SRS WKT|Layer SRS):\s*\n\s*([^\n]+)", output, re.MULTILINE)
    fields = []
    field_types = r"(?:Integer64|Integer|Real|String|DateTime|Date|Time|Binary|IntegerList|RealList|StringList)"
    for line in output.splitlines():
        match = re.match(
            rf"^\s*([^:\n]+):\s+({field_types})\b(?:\s+\(([^)]*)\))?\s*$",
            line,
        )
        if match:
            fields.append({
                "name": match.group(1).strip(),
                "type": match.group(2).strip(),
                "width": match.group(3),
            })
    return {
        "feature_count": int(feature_match.group(1)) if feature_match else None,
        "geometry_type": geometry_match.group(1).strip() if geometry_match else None,
        "crs_authority": f"EPSG:{epsg_match.group(1)}" if epsg_match else None,
        "crs_name": srs_match.group(1).strip() if srs_match else None,
        "fields": fields,
    }



def _initial_layer_suitability(layer_path, geometry_type, field_count):
    """Transparent name/geometry-based triage hint, not an approval decision."""
    name = PurePosixPath(layer_path).stem.lower()
    if "building" in name:
        category, reason = "building_exposure_candidate", "Name suggests building footprints; confirm geometry, attributes, date and completeness."
    elif any(token in name for token in ("waterway", "lagga", "drain")):
        category, reason = "hydrology_context_candidate", "Name suggests drainage or waterways; confirm hydrological meaning and CRS."
    elif any(token in name for token in ("road", "path")):
        category, reason = "access_network_candidate", "Name suggests transport infrastructure; confirm road class, access status and topology."
    elif any(token in name for token in ("health", "school", "shop", "place", "tank", "electricity", "power")):
        category, reason = "critical_facility_or_infrastructure_candidate", "Name suggests a facility or infrastructure asset; validate attributes and operational status."
    elif any(token in name for token in ("land_use", "land_uses", "agricultural", "boundary")):
        category, reason = "land_use_or_extent_candidate", "Name suggests land use or settlement extent; verify class definitions and geometry."
    else:
        category, reason = "context_candidate", "Layer name alone does not establish its analytical role; inspect fields and sample records."
    if not geometry_type:
        reason += " Geometry type is unknown."
    if field_count == 0:
        reason += " No attribute fields were parsed; do not use analytically until corrected."
    return {"category": category, "reason": reason, "approval": "not_approved"}


def run_silver_gis_inspection(inventory_path):
    """Inspect extracted shapefiles with read-only GDAL/OGR and persist a Silver catalogue."""
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    started = time.perf_counter()
    client, container = _storage()
    try:
        inventory = json.loads(
            container.get_blob_client(inventory_path).download_blob().readall().decode("utf-8")
        )
        all_members = [
            member
            for file_report in inventory.get("files", [])
            for member in file_report.get("extracted_members", [])
        ]
        shp_members = [
            member for member in all_members
            if PurePosixPath(member.get("member", "")).suffix.lower() == ".shp"
        ]
        ogrinfo = shutil.which("ogrinfo")
        catalogue = []
        if not ogrinfo:
            raise RuntimeError("GDAL/OGR command 'ogrinfo' is not installed in this container.")

        with tempfile.TemporaryDirectory(prefix="tcdse-gis-inspection-") as work:
            root = Path(work)
            for shp in shp_members:
                member_path = PurePosixPath(shp["member"])
                parent = member_path.parent
                stem = member_path.stem
                # A shapefile is a set of companion files. Download same-folder, same-stem
                # members together so OGR can read attributes, projection and geometry.
                companions = [
                    item for item in all_members
                    if PurePosixPath(item.get("member", "")).parent == parent
                    and PurePosixPath(item.get("member", "")).stem.lower() == stem.lower()
                ]
                local_dir = root / str(parent)
                local_dir.mkdir(parents=True, exist_ok=True)
                for item in companions:
                    safe = _safe_member(item.get("member", ""))
                    if not safe:
                        continue
                    local_path = root / safe
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    data = container.get_blob_client(item["silver_path"]).download_blob().readall()
                    local_path.write_bytes(data)

                local_shp = root / shp["member"]
                record = {
                    "layer_path": shp["member"],
                    "silver_path": shp.get("silver_path"),
                    "source_filename": next(
                        (fr.get("source_filename") for fr in inventory.get("files", [])
                         if any(m.get("member") == shp["member"] for m in fr.get("extracted_members", []))),
                        None,
                    ),
                    "inspection_status": "failed",
                    "feature_count": None,
                    "geometry_type": None,
                    "crs_name": None,
                    "crs_authority": None,
                    "fields": [],
                    "field_count": 0,
                    "warnings": [],
                }
                try:
                    result = subprocess.run(
                        [ogrinfo, "-ro", "-so", "-al", str(local_shp)],
                        capture_output=True, text=True, timeout=120, check=False,
                    )
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout or "ogrinfo returned a non-zero exit code").strip()
                        record["warnings"].append(detail[-1500:])
                    else:
                        parsed = _parse_ogrinfo_summary(result.stdout)
                        record["feature_count"] = parsed["feature_count"]
                        record["geometry_type"] = parsed["geometry_type"]
                        record["crs_authority"] = parsed["crs_authority"]
                        record["crs_name"] = parsed["crs_name"]
                        record["fields"] = parsed["fields"]
                        record["field_count"] = len(record["fields"])
                        if record["field_count"] == 0:
                            record["warnings"].append("OGR inspection succeeded but no attribute fields were parsed; inspect raw ogrinfo output.")
                        record["inspection_status"] = "inspected"
                except subprocess.TimeoutExpired:
                    record["warnings"].append("OGR inspection exceeded the 120-second timeout.")
                except (OSError, KeyError, TypeError) as exc:
                    record["warnings"].append(f"Could not parse OGR inspection output: {exc}")
                if record["inspection_status"] == "inspected":
                    if record.get("feature_count") == 0:
                        record["warnings"].append("Layer contains zero features.")
                    if not record.get("crs_authority") and not record.get("crs_name"):
                        record["warnings"].append("CRS was not identified in the OGR summary; verify the .prj file and coordinate reference system.")
                    if not record.get("geometry_type"):
                        record["warnings"].append("Geometry type was not identified in the OGR summary.")
                    record["initial_suitability"] = _initial_layer_suitability(record["layer_path"], record.get("geometry_type"), record.get("field_count", 0))
                else:
                    record["initial_suitability"] = {"category": "needs_review", "reason": "Layer metadata could not be fully inspected.", "approval": "not_approved"}
                catalogue.append(record)

        inspected = sum(1 for item in catalogue if item["inspection_status"] == "inspected")
        result_doc = {
            "pipeline": "silver-gis-inspection-v1",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_inventory": inventory_path,
            "silver_base": inventory.get("silver_base"),
            "candidate_layer_count": len(shp_members),
            "inspected_layer_count": inspected,
            "failed_layer_count": len(catalogue) - inspected,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "layers": catalogue,
            "limitations": [
                "Read-only metadata inspection only; no geometry repair or reprojection was performed.",
                "Feature counts and CRS are reported only when GDAL/OGR returns them.",
                "This catalogue does not establish semantic suitability for a Gold output; attributes and domain meaning still require review.",
            ],
        }
        report_path = f"{inventory.get('silver_base', 'silver')}/silver_gis_catalogue.json"
        container.get_blob_client(report_path).upload_blob(
            json.dumps(result_doc, indent=2).encode("utf-8"), overwrite=True
        )
        return result_doc | {"report_path": report_path}
    finally:
        client.close()


def _gold_readiness_matrix(layers):
    """Map source candidates to Gold contracts without silently approving transformations."""
    rules = [
        ("exclusion_lines.geojson", ["road", "path", "waterway", "lagga", "electricity_line", "transmission_line", "distribution_line"], "Line features may be candidates; exclusion buffers/rules and units must be explicitly defined."),
        ("exclusion_points.geojson", ["health", "shop", "place", "tank", "power_plant", "solar"], "Point/asset candidates only; confirm geometry and define whether/how each asset creates an exclusion."),
        ("exclusion_polygons.geojson", ["building", "boundary", "land_use", "land_uses", "agricultural", "agriculture"], "Polygon candidates only; confirm geometry, land-use codes and exclusion logic."),
        ("sample_landuse_file.geojson", ["land_uses_and_their_current_status", "land_use", "agricultural", "main_agricultural_areas"], "Likely land-use input; preserve source classes and verify category mappings before export."),
        ("sample_distribution_file.xlsx", [], "Not derivable from the GIS layers alone; requires a defined tabular distribution schema and validated source indicators."),
    ]
    result = []
    for target, tokens, note in rules:
        candidates = []
        if tokens:
            for layer in layers:
                name = PurePosixPath(layer.get("layer_path", "")).stem.lower()
                if any(token in name for token in tokens):
                    candidates.append({
                        "layer_path": layer.get("layer_path"),
                        "geometry_type": layer.get("geometry_type"),
                        "feature_count": layer.get("feature_count"),
                        "crs": layer.get("crs_authority") or layer.get("crs_name"),
                        "inspection_status": layer.get("inspection_status"),
                        "field_count": layer.get("field_count", 0),
                        "qa_status": layer.get("qa_status", "not_assessed"),
                    })
        result.append({
            "gold_artifact": target,
            "candidate_layers": candidates,
            "candidate_count": len(candidates),
            "readiness": "not_ready",
            "decision": "human_review_required",
            "reason": note if candidates else (note if not tokens else "No candidate layers matched the current name-based screen; inspect catalogue manually."),
        })
    return result



def _quality_resolution_actions(record):
    """Provide non-destructive next actions based on observed QA evidence."""
    actions = []
    for check in record.get("identifier_checks", []):
        if check.get("duplicate_non_null_count", 0):
            actions.append({
                "issue": "repeated_identifier",
                "field": check.get("field"),
                "evidence": check.get("duplicate_values_sample", []),
                "recommended_action": "Inspect affected records and source schema; determine whether the field is a feature key, parent/join key, or non-unique label. Do not delete or renumber records automatically.",
                "status": "requires_human_review",
            })
        if check.get("null_count", 0):
            actions.append({
                "issue": "null_identifier",
                "field": check.get("field"),
                "evidence": {"null_count": check.get("null_count")},
                "recommended_action": "Confirm whether the identifier is required. Preserve nulls until a documented source-based rule exists.",
                "status": "requires_human_review",
            })
    geometry = record.get("geometry", {})
    if geometry.get("invalid_count", 0):
        actions.append({
            "issue": "invalid_geometry",
            "evidence": {"count": geometry.get("invalid_count"), "feature_indices_sample": geometry.get("invalid_feature_indices_sample", [])},
            "recommended_action": "Inspect invalid features and compare with source GIS. Repair only in a separate derived copy after review; preserve original geometry and record method.",
            "status": "requires_human_review",
        })
    if len(geometry.get("types", {})) > 1:
        actions.append({
            "issue": "mixed_geometry_types",
            "evidence": geometry.get("types", {}),
            "recommended_action": "Review the intended output contract. Separate by geometry type or normalize only when the target schema explicitly permits it.",
            "status": "requires_human_review",
        })
    crs = record.get("crs", {})
    if not crs.get("present"):
        actions.append({
            "issue": "missing_crs",
            "evidence": crs,
            "recommended_action": "Find authoritative CRS metadata from the source package/provider; do not guess or reproject until confirmed.",
            "status": "requires_human_review",
        })
    return actions


def run_silver_gis_quality_assessment(catalogue_path):
    """Profile extracted shapefiles and produce a conservative Gold-readiness report."""
    import tempfile
    from pathlib import Path

    started = time.perf_counter()
    client, container = _storage()
    try:
        catalogue = json.loads(
            container.get_blob_client(catalogue_path).download_blob().readall().decode("utf-8")
        )
        inventory_path = catalogue.get("source_inventory")
        if not inventory_path:
            raise ValueError("GIS catalogue does not reference its source inventory.")
        inventory = json.loads(
            container.get_blob_client(inventory_path).download_blob().readall().decode("utf-8")
        )
        all_members = [
            member for file_report in inventory.get("files", [])
            for member in file_report.get("extracted_members", [])
        ]
        import geopandas as gpd
        import pandas as pd

        qa_layers = []
        with tempfile.TemporaryDirectory(prefix="tcdse-gis-qa-") as work:
            root = Path(work)
            for layer in catalogue.get("layers", []):
                shp_name = layer.get("layer_path", "")
                if layer.get("inspection_status") != "inspected":
                    qa_layers.append({
                        "layer_path": shp_name, "qa_status": "blocked",
                        "blockers": ["Layer metadata inspection failed."],
                    })
                    continue
                parent = PurePosixPath(shp_name).parent
                stem = PurePosixPath(shp_name).stem
                companions = [
                    item for item in all_members
                    if PurePosixPath(item.get("member", "")).parent == parent
                    and PurePosixPath(item.get("member", "")).stem.lower() == stem.lower()
                ]
                for item in companions:
                    safe = _safe_member(item.get("member", ""))
                    if not safe:
                        continue
                    local_path = root / safe
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    local_path.write_bytes(
                        container.get_blob_client(item["silver_path"]).download_blob().readall()
                    )
                local_shp = root / shp_name
                record = {
                    "layer_path": shp_name,
                    "source_sha256": next((m.get("sha256") for m in companions if PurePosixPath(m.get("member", "")).suffix.lower() == ".shp"), None),
                    "feature_count_catalogue": layer.get("feature_count"),
                    "field_count_catalogue": layer.get("field_count", 0),
                    "qa_status": "failed",
                    "blockers": [],
                    "warnings": [],
                    "fields": [],
                    "sample_records": [],
                    "geometry": {},
                    "crs": {},
                }
                try:
                    gdf = gpd.read_file(local_shp)
                    geom_col = gdf.geometry.name if hasattr(gdf, "geometry") else None
                    actual_count = int(len(gdf))
                    record["feature_count_actual"] = actual_count
                    record["feature_count_matches_catalogue"] = (
                        layer.get("feature_count") is None or actual_count == layer.get("feature_count")
                    )
                    if not record["feature_count_matches_catalogue"]:
                        record["warnings"].append("Feature count differs from the OGR catalogue.")
                    crs = gdf.crs
                    record["crs"] = {
                        "value": str(crs) if crs is not None else None,
                        "authority": f"{crs.to_authority()[0]}:{crs.to_authority()[1]}" if crs is not None and crs.to_authority() else None,
                        "present": crs is not None,
                    }
                    if crs is None:
                        record["blockers"].append("CRS is missing; do not combine or reproject until the source CRS is established.")
                    if geom_col and geom_col in gdf.columns:
                        geom = gdf.geometry
                        empty_count = int(geom.is_empty.fillna(False).sum())
                        null_geom_count = int(geom.isna().sum())
                        invalid_count = int((~geom.is_valid & ~geom.is_empty & geom.notna()).sum())
                        geom_types = {str(k): int(v) for k, v in geom.geom_type.value_counts(dropna=False).items()}
                        base_geom_types = {}
                        for geom_type, count in geom_types.items():
                            normalized_type = (
                                "Line" if "LineString" in geom_type or "Line" == geom_type
                                else "Polygon" if "Polygon" in geom_type
                                else "Point" if "Point" in geom_type
                                else geom_type
                            )
                            base_geom_types[normalized_type] = base_geom_types.get(normalized_type, 0) + count
                        bounds = [float(v) for v in gdf.total_bounds] if actual_count else []
                        invalid_mask = (~geom.is_valid & ~geom.is_empty & geom.notna())
                        invalid_indices = [str(idx) for idx in gdf.index[invalid_mask].tolist()[:100]]
                        record["geometry"] = {
                            "column": geom_col, "types": geom_types, "bounds": bounds,
                            "null_count": null_geom_count, "empty_count": empty_count,
                            "invalid_count": invalid_count, "invalid_feature_indices_sample": invalid_indices,
                            "invalid_indices_truncated": invalid_count > len(invalid_indices),
                        }
                        if null_geom_count:
                            record["warnings"].append(f"{null_geom_count} null geometries.")
                        if empty_count:
                            record["warnings"].append(f"{empty_count} empty geometries.")
                        if invalid_count:
                            record["warnings"].append(f"{invalid_count} invalid geometries; do not repair automatically.")
                        if len(geom_types) > 1:
                            if len(base_geom_types) == 1:
                                record["warnings"].append(
                                    "Mixed single/multi geometry variants found within one base geometry family; review target contract and normalise only in a derived copy if permitted."
                                )
                            else:
                                record["warnings"].append("Mixed geometry families found; assess splitting by geometry family before Gold export.")
                    else:
                        record["blockers"].append("No geometry column was detected.")
                    columns = [str(col) for col in gdf.columns if col != geom_col]
                    for col in columns:
                        series = gdf[col]
                        nulls = int(series.isna().sum())
                        non_null = series.dropna()
                        distinct = int(non_null.nunique(dropna=True))
                        examples = []
                        for value in non_null.drop_duplicates().head(8).tolist():
                            if hasattr(value, "item"):
                                value = value.item()
                            examples.append(str(value)[:160])
                        field = {
                            "name": col,
                            "dtype": str(series.dtype),
                            "row_count": actual_count,
                            "null_count": nulls,
                            "null_pct": round((nulls / actual_count) * 100, 2) if actual_count else None,
                            "distinct_non_null": distinct,
                            "sample_values": examples,
                        }
                        record["fields"].append(field)
                    identifier_fields = [
                        col for col in columns
                        if col.lower() in {"id", "fid", "objectid", "object_id", "globalid", "gid"}
                    ]
                    for col in identifier_fields:
                        non_null = gdf[col].dropna()
                        duplicate_count = int(non_null.duplicated().sum())
                        duplicate_values = non_null[non_null.duplicated(keep=False)].value_counts(dropna=True)
                        duplicate_examples = [
                            {"value": str(value)[:160], "occurrences": int(count)}
                            for value, count in duplicate_values.head(100).items()
                        ]
                        non_null_distinct = int(non_null.nunique(dropna=True))
                        is_constant_placeholder = (
                            len(non_null) > 1
                            and non_null_distinct == 1
                            and str(non_null.iloc[0]).strip() in {"0", "0.0", ""}
                        )
                        duplicate_indices = [
                            str(idx) for idx in gdf.index[gdf[col].notna() & gdf[col].duplicated(keep=False)].tolist()[:100]
                        ]
                        record.setdefault("identifier_checks", []).append({
                            "field": col, "null_count": int(gdf[col].isna().sum()),
                            "duplicate_non_null_count": duplicate_count,
                            "duplicate_distinct_value_count": int(len(duplicate_values)),
                            "distinct_non_null_value_count": non_null_distinct,
                            "constant_placeholder_suspected": is_constant_placeholder,
                            "identifier_assessment": (
                                "constant_placeholder_suspected"
                                if is_constant_placeholder
                                else "repeated_values_require_schema_review"
                            ) if duplicate_count else "no_repeated_non_null_values_detected",
                            "duplicate_values_sample": duplicate_examples,
                            "affected_feature_indices_sample": duplicate_indices,
                            "indices_truncated": duplicate_count > len(duplicate_indices),
                            "interpretation": "Repeated identifiers are flagged for review; they are not assumed to be duplicate features.",
                        })
                        if duplicate_count and is_constant_placeholder:
                            record["warnings"].append(
                                f"Identifier-like field {col} is constant ({str(non_null.iloc[0])}) across {len(non_null)} non-null records; likely placeholder/non-unique field, not evidence of duplicate features."
                            )
                        elif duplicate_count:
                            record["warnings"].append(f"Identifier field {col} has {duplicate_count} duplicate non-null values; determine whether it is intended to be unique.")
                    record["sample_records"] = [
                        {str(k): (None if pd.isna(v) else str(v)[:160]) for k, v in row.items() if k != geom_col}
                        for row in gdf.drop(columns=[geom_col], errors="ignore").head(5).to_dict(orient="records")
                    ]
                    if actual_count == 0:
                        record["blockers"].append("Layer contains zero features.")
                    if not columns:
                        record["blockers"].append("No non-geometry attribute fields found.")
                    record["resolution_actions"] = _quality_resolution_actions(record)
                    record["qa_status"] = "blocked" if record["blockers"] else ("review_warnings" if record["warnings"] else "passed_initial_checks")
                except Exception as exc:
                    record["blockers"].append(f"GeoPandas could not read or profile the layer: {type(exc).__name__}: {exc}"[:1000])
                    record["qa_status"] = "blocked"
                qa_layers.append(record)

        qa_by_path = {item.get("layer_path"): item for item in qa_layers}
        for layer in catalogue.get("layers", []):
            qa = qa_by_path.get(layer.get("layer_path"), {})
            layer["qa_status"] = qa.get("qa_status", "not_assessed")
        gold_matrix = _gold_readiness_matrix(catalogue.get("layers", []))
        blockers = sum(1 for item in qa_layers if item.get("qa_status") == "blocked")
        warnings = sum(1 for item in qa_layers if item.get("qa_status") == "review_warnings")
        result_doc = {
            "pipeline": "silver-gis-quality-v1",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_catalogue": catalogue_path,
            "silver_base": catalogue.get("silver_base"),
            "layer_count": len(qa_layers),
            "blocked_layer_count": blockers,
            "layers_with_warnings": warnings,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "layers": qa_layers,
            "gold_readiness": gold_matrix,
            "gold_generation_allowed": False,
            "gold_generation_note": "Gold generation remains gated on human approval of layer semantics, CRS, quality findings, exclusion rules and output schemas.",
        }
        report_path = f"{catalogue.get('silver_base', 'silver')}/silver_gis_quality_report.json"
        container.get_blob_client(report_path).upload_blob(
            json.dumps(result_doc, indent=2).encode("utf-8"), overwrite=True
        )
        # Refresh the catalogue's initial triage status, preserving original inspection metadata.
        container.get_blob_client(catalogue_path).upload_blob(
            json.dumps(catalogue, indent=2).encode("utf-8"), overwrite=True
        )
        return result_doc | {"report_path": report_path}
    finally:
        client.close()
