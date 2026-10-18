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
