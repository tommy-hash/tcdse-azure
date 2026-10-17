"""Raw Bronze ingestion and integrity checks for Azure Storage."""
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath


def _storage():
    account_url = os.getenv("TCDSE_ADLS_ACCOUNT_URL", "").strip().rstrip("/")
    filesystem_name = os.getenv("TCDSE_ADLS_FILESYSTEM", "").strip()
    if not account_url or not filesystem_name:
        raise RuntimeError("Set TCDSE_ADLS_ACCOUNT_URL and TCDSE_ADLS_FILESYSTEM on the Container App.")
    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
    except ImportError as exc:
        raise RuntimeError("Azure Blob SDK dependencies are missing. Rebuild the Container App image.") from exc

    # Use the Blob endpoint for object uploads. Slash-delimited names preserve our
    # Bronze hierarchy without requiring DFS directory creation before every upload.
    if ".dfs." in account_url:
        account_url = account_url.replace(".dfs.", ".blob.")
    client = BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())
    container = client.get_container_client(filesystem_name)
    try:
        container.get_container_properties()
    except Exception as exc:
        client.close()
        code = getattr(exc, "error_code", None) or type(exc).__name__
        raise RuntimeError(
            f"Cannot access Azure Storage container {filesystem_name!r} at {account_url!r}. "
            f"Confirm the container name, account endpoint and managed-identity data permissions. Azure error: {code}: {exc}"
        ) from exc
    return client, container


def upload_raw_batch(files, country="Kenya", project="Kalobeyei Settlement", progress_hook=None):
    """Upload original bytes and a SHA-256 manifest. files is [{name, data: bytes}]."""
    if not files:
        raise ValueError("Choose at least one file.")
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    safe_country = re.sub(r"[^A-Za-z0-9_-]", "_", country).strip("_") or "unspecified"
    safe_project = re.sub(r"[^A-Za-z0-9_-]", "_", project).strip("_") or "unspecified"
    base = f"bronze/{safe_country}/{safe_project}/{batch_id}"
    client, container = _storage()
    manifest_files, seen_names = [], set()
    total_bytes = sum(len(item.get("data") or b"") for item in files)
    completed_before = 0
    try:
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
            path = f"{base}/raw/{safe_name}"
            digest = hashlib.sha256(data).hexdigest()
            try:
                def report_progress(current, total, offset=completed_before, filename=name):
                    if progress_hook:
                        progress_hook(min(100.0, ((offset + current) / max(total_bytes, 1)) * 100.0), filename, current, total)
                container.get_blob_client(path).upload_blob(data, overwrite=False, progress_hook=report_progress)
            except Exception as exc:
                code = getattr(exc, "error_code", None) or type(exc).__name__
                raise RuntimeError(f"Azure Blob upload failed for object {path!r}. Azure error: {code}: {exc}") from exc
            completed_before += len(data)
            if progress_hook:
                progress_hook(min(100.0, completed_before / max(total_bytes, 1) * 100.0), name, len(data), len(data))
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
        manifest_path = f"{base}/manifest.json"
        try:
            container.get_blob_client(manifest_path).upload_blob(json.dumps(manifest, indent=2).encode("utf-8"), overwrite=False)
        except Exception as exc:
            code = getattr(exc, "error_code", None) or type(exc).__name__
            raise RuntimeError(
                f"Raw file(s) uploaded, but manifest creation failed for object {manifest_path!r}. Azure error: {code}: {exc}"
            ) from exc
        return {"batch_id": batch_id, "base_path": base, "manifest_path": manifest_path, "files": manifest_files}
    finally:
        client.close()


def list_raw_batches(limit=100):
    """Read manifests from Bronze and return newest batches first."""
    client, container = _storage()
    batches = []
    try:
        for blob in container.list_blobs(name_starts_with="bronze/"):
            if not blob.name.endswith("/manifest.json"):
                continue
            try:
                raw = container.get_blob_client(blob.name).download_blob().readall()
                manifest = json.loads(raw.decode("utf-8"))
                batches.append({
                    "batch_id": manifest.get("batch_id", ""),
                    "created_utc": manifest.get("created_utc", ""),
                    "country": manifest.get("country", ""),
                    "project": manifest.get("project", ""),
                    "status": manifest.get("processing_status", "unknown"),
                    "manifest_path": blob.name,
                    "file_count": len(manifest.get("files", [])),
                    "total_bytes": sum(int(f.get("size_bytes", 0)) for f in manifest.get("files", [])),
                    "files": manifest.get("files", []),
                })
            except (ValueError, UnicodeDecodeError, KeyError):
                batches.append({"batch_id": "unreadable-manifest", "manifest_path": blob.name, "status": "manifest_error", "file_count": 0, "total_bytes": 0, "files": []})
        return sorted(batches, key=lambda x: x.get("created_utc", ""), reverse=True)[:limit]
    finally:
        client.close()


def verify_raw_batch(manifest_path):
    """Re-download each stored object and compare its bytes with manifest SHA-256."""
    client, container = _storage()
    try:
        manifest = json.loads(container.get_blob_client(manifest_path).download_blob().readall().decode("utf-8"))
        results = []
        for item in manifest.get("files", []):
            try:
                content = container.get_blob_client(item["path"]).download_blob().readall()
                actual = hashlib.sha256(content).hexdigest()
                expected = item.get("sha256", "")
                expected_size = int(item.get("size_bytes", -1))
                results.append({
                    "file": item.get("original_filename", item.get("stored_filename", item["path"])),
                    "path": item["path"], "expected_sha256": expected, "actual_sha256": actual,
                    "size_expected": expected_size, "size_actual": len(content),
                    "status": "PASS" if actual == expected and len(content) == expected_size else "FAIL",
                })
            except Exception as exc:
                results.append({"file": item.get("original_filename", item.get("path", "unknown")), "path": item.get("path", ""), "status": "ERROR", "detail": str(exc)})
        return results
    finally:
        client.close()



def purge_raw_batch(manifest_path, confirmation_batch_id, reason):
    """Delete raw objects for one confirmed batch but retain manifest/audit history."""
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("Enter a reason for purging this batch.")
    client, container = _storage()
    try:
        manifest_blob = container.get_blob_client(manifest_path)
        manifest = json.loads(manifest_blob.download_blob().readall().decode("utf-8"))
        batch_id = manifest.get("batch_id", "")
        if not batch_id or confirmation_batch_id != batch_id:
            raise ValueError("Confirmation batch ID does not match. No files were deleted.")
        if manifest.get("processing_status") == "purged":
            raise ValueError("This batch has already been purged.")
        deleted, errors = [], []
        for item in manifest.get("files", []):
            path = item.get("path", "")
            try:
                container.get_blob_client(path).delete_blob()
                deleted.append(path)
            except Exception as exc:
                errors.append({"path": path, "error": str(exc)})
        manifest["processing_status"] = "purged" if not errors else "purge_incomplete"
        manifest["purged_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["purge_reason"] = reason
        manifest["purge_deleted_paths"] = deleted
        manifest["purge_errors"] = errors
        manifest["downstream_invalidation_status"] = "pending_lineage_implementation"
        manifest_blob.upload_blob(json.dumps(manifest, indent=2).encode("utf-8"), overwrite=True)
        audit_path = manifest_path.rsplit("/", 1)[0] + "/purge-audit.json"
        audit = {"batch_id": batch_id, "manifest_path": manifest_path, "purged_utc": manifest["purged_utc"], "reason": reason, "deleted_paths": deleted, "errors": errors, "downstream_invalidation_status": "pending_lineage_implementation"}
        container.get_blob_client(audit_path).upload_blob(json.dumps(audit, indent=2).encode("utf-8"), overwrite=True)
        if errors:
            raise RuntimeError(f"Purging was incomplete: {len(deleted)} object(s) deleted, {len(errors)} failed. See purge audit.")
        return {"batch_id": batch_id, "deleted_count": len(deleted), "audit_path": audit_path, "status": manifest["processing_status"]}
    finally:
        client.close()
