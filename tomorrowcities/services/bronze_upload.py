"""Raw Bronze ingestion and integrity checks for ADLS Gen2."""
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError


def _storage():
    account_url = os.getenv("TCDSE_ADLS_ACCOUNT_URL", "").strip()
    filesystem_name = os.getenv("TCDSE_ADLS_FILESYSTEM", "").strip()
    if not account_url or not filesystem_name:
        raise RuntimeError("Set TCDSE_ADLS_ACCOUNT_URL and TCDSE_ADLS_FILESYSTEM on the Container App.")
    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.filedatalake import DataLakeServiceClient
    except ImportError as exc:
        raise RuntimeError("Azure upload dependencies are missing. Rebuild the Container App image.") from exc
    client = DataLakeServiceClient(account_url=account_url, credential=DefaultAzureCredential())
    filesystem = client.get_file_system_client(filesystem_name)
    try:
        filesystem.get_file_system_properties()
    except AzureError as exc:
        client.close()
        code = getattr(exc, "error_code", None) or type(exc).__name__
        raise RuntimeError(
            f"Could not access ADLS filesystem {filesystem_name!r} at {account_url!r}. "
            "Check that TCDSE_ADLS_FILESYSTEM is the exact filesystem/container name, "
            "the account URL belongs to that storage account, and the managed identity has data access. "
            f"Azure error: {code}: {exc}"
        ) from exc
    return client, filesystem



def _ensure_directories(filesystem, path):
    """Create each directory level in order; ADLS requires parent paths to exist."""
    current = ""
    for part in path.strip("/").split("/"):
        current = f"{current}/{part}" if current else part
        try:
            filesystem.get_directory_client(current).create_directory()
        except ResourceExistsError:
            pass
        except AzureError as exc:
            code = getattr(exc, "error_code", None) or type(exc).__name__
            raise RuntimeError(
                f"Azure failed while creating ADLS directory {current!r}. "
                f"Azure error: {code}: {exc}"
            ) from exc


def upload_raw_batch(files, country="Kenya", project="Kalobeyei Settlement"):
    """Upload original bytes and a SHA-256 manifest. files is [{name, data: bytes}]."""
    if not files:
        raise ValueError("Choose at least one file.")
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    safe_country = re.sub(r"[^A-Za-z0-9_-]", "_", country).strip("_") or "unspecified"
    safe_project = re.sub(r"[^A-Za-z0-9_-]", "_", project).strip("_") or "unspecified"
    base = f"bronze/{safe_country}/{safe_project}/{batch_id}/raw"
    service, filesystem = _storage()
    manifest_files, seen_names = [], set()
    try:
        _ensure_directories(filesystem, base)
        for item in files:
            name = PurePosixPath(str(item.get("name", "")).replace("\\", "/")).name
            data = item.get("data")
            if not name or name in {".", ".."} or not isinstance(data, bytes):
                raise ValueError("Each file must have a filename and raw bytes.")
            safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .") or "uploaded-file"
            if safe_name in seen_names:
                stem, dot, suffix = safe_name.rpartition(".")
                safe_name = f"{stem or suffix}-{uuid.uuid4().hex[:6]}{dot + suffix if stem else ''}"
            seen_names.add(safe_name)
            path = f"{base}/{safe_name}"
            digest = hashlib.sha256(data).hexdigest()
            try:
                filesystem.get_file_client(path).upload_data(data, overwrite=False)
            except AzureError as exc:
                code = getattr(exc, "error_code", None) or type(exc).__name__
                raise RuntimeError(f"Azure failed uploading raw file to {path!r}. Azure error: {code}: {exc}") from exc
            manifest_files.append({
                "original_filename": name, "stored_filename": safe_name, "path": path,
                "size_bytes": len(data), "sha256": digest, "processing_status": "uploaded_unprocessed",
            })
        manifest = {
            "batch_id": batch_id, "created_utc": datetime.now(timezone.utc).isoformat(),
            "country": country, "project": project, "stage": "bronze",
            "processing_status": "uploaded_unprocessed", "files": manifest_files,
            "notes": "Original bytes preserved. No extraction, transformation, or model training was performed.",
        }
        manifest_path = f"bronze/{safe_country}/{safe_project}/{batch_id}/manifest.json"
        try:
            filesystem.get_file_client(manifest_path).upload_data(json.dumps(manifest, indent=2).encode("utf-8"), overwrite=False)
        except AzureError as exc:
            code = getattr(exc, "error_code", None) or type(exc).__name__
            raise RuntimeError(
                f"Raw file(s) were uploaded, but manifest creation failed at {manifest_path!r}. "
                f"Azure error: {code}: {exc}"
            ) from exc
        return {"batch_id": batch_id, "base_path": base.rsplit("/raw", 1)[0], "manifest_path": manifest_path, "files": manifest_files}
    finally:
        service.close()


def list_raw_batches(limit=100):
    """Read manifests from Bronze and return newest batches first."""
    service, filesystem = _storage()
    batches = []
    try:
        try:
            paths = filesystem.get_paths(path="bronze", recursive=True)
            for item in paths:
                if item.is_directory or not item.name.endswith("/manifest.json"):
                    continue
                try:
                    raw = filesystem.get_file_client(item.name).download_file().readall()
                    manifest = json.loads(raw.decode("utf-8"))
                    batches.append({
                        "batch_id": manifest.get("batch_id", ""),
                        "created_utc": manifest.get("created_utc", ""),
                        "country": manifest.get("country", ""),
                        "project": manifest.get("project", ""),
                        "status": manifest.get("processing_status", "unknown"),
                        "manifest_path": item.name,
                        "file_count": len(manifest.get("files", [])),
                        "total_bytes": sum(int(f.get("size_bytes", 0)) for f in manifest.get("files", [])),
                        "files": manifest.get("files", []),
                    })
                except (ValueError, UnicodeDecodeError, KeyError):
                    batches.append({"batch_id": "unreadable-manifest", "manifest_path": item.name, "status": "manifest_error", "file_count": 0, "total_bytes": 0, "files": []})
        except ResourceNotFoundError:
            return []
        return sorted(batches, key=lambda x: x.get("created_utc", ""), reverse=True)[:limit]
    finally:
        service.close()


def verify_raw_batch(manifest_path):
    """Re-download each stored object and compare its bytes with manifest SHA-256."""
    service, filesystem = _storage()
    try:
        manifest = json.loads(filesystem.get_file_client(manifest_path).download_file().readall().decode("utf-8"))
        results = []
        for item in manifest.get("files", []):
            try:
                content = filesystem.get_file_client(item["path"]).download_file().readall()
                actual = hashlib.sha256(content).hexdigest()
                expected = item.get("sha256", "")
                results.append({
                    "file": item.get("original_filename", item.get("stored_filename", item["path"])),
                    "path": item["path"], "expected_sha256": expected, "actual_sha256": actual,
                    "size_expected": int(item.get("size_bytes", -1)), "size_actual": len(content),
                    "status": "PASS" if actual == expected and len(content) == int(item.get("size_bytes", -1)) else "FAIL",
                })
            except Exception as exc:
                results.append({"file": item.get("original_filename", item.get("path", "unknown")), "path": item.get("path", ""), "status": "ERROR", "detail": str(exc)})
        return results
    finally:
        service.close()

