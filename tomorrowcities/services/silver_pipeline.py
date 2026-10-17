"""First-pass Bronze-to-Silver preparation for Kalobeyei raw batches.

Preserves Bronze objects. Extracts safe archive members from MPK/ZIP-like files
and writes a machine-readable inventory to Silver. It does not fabricate Gold data.
"""
import hashlib
import json
import os
import posixpath
import re
import zipfile
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


def run_silver_inventory(manifest_path):
    """Create a Silver inventory and safely extract archive members from one batch."""
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
                    import io
                    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                        for member in archive.infolist():
                            if member.is_dir():
                                continue
                            safe = _safe_member(member.filename)
                            if not safe:
                                report["warnings"].append(f"Skipped unsafe archive path: {member.filename}")
                                continue
                            if member.file_size > 250 * 1024 * 1024:
                                report["warnings"].append(f"Skipped member over 250 MB: {safe}")
                                continue
                            # Keep package structure below the source-specific extracted directory.
                            target = f"{silver_base}/extracted/{PurePosixPath(name).stem}/{safe}"
                            data = archive.read(member)
                            container.get_blob_client(target).upload_blob(data, overwrite=True)
                            report["extracted_members"].append({
                                "member": safe, "silver_path": target, "size_bytes": len(data),
                                "sha256": hashlib.sha256(data).hexdigest(), "type": _classify(safe),
                            })
                            extracted_count += 1
                    report["processing_status"] = "extracted" if report["extracted_members"] else "inventoried"
                except zipfile.BadZipFile:
                    report["processing_status"] = "inventory_only"
                    report["warnings"].append("Package is not a ZIP-readable archive. No extraction was attempted.")
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
