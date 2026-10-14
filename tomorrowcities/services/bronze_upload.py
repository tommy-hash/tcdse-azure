"""Upload immutable raw training-case files to ADLS Gen2 using the Container App identity."""
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath


def upload_raw_batch(files):
    """Upload list of {name, data} items and a manifest; return uploaded paths."""
    account_url = os.getenv("TCDSE_ADLS_ACCOUNT_URL", "").strip()
    filesystem_name = os.getenv("TCDSE_ADLS_FILESYSTEM", "").strip()
    if not account_url or not filesystem_name:
        raise RuntimeError(
            "Azure storage is not configured. Set TCDSE_ADLS_ACCOUNT_URL and "
            "TCDSE_ADLS_FILESYSTEM on the Container App before uploading."
        )
    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.filedatalake import DataLakeServiceClient
    except ImportError as exc:
        raise RuntimeError("Azure upload dependencies are missing. Rebuild the Container App image.") from exc

    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    credential = DefaultAzureCredential()
    service = DataLakeServiceClient(account_url=account_url, credential=credential)
    filesystem = service.get_file_system_client(filesystem_name)
    base = f"bronze/raw-training-case/{batch_id}"
    manifest_files = []
    try:
        for item in files:
            name = PurePosixPath(str(item["name"]).replace("\\", "/")).name
            data = item["data"]
            if not name or name in {".", ".."} or not isinstance(data, bytes):
                raise ValueError("Each upload must have a filename and raw bytes.")
            safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .") or "uploaded-file"
            path = f"{base}/{safe_name}"
            file_client = filesystem.get_file_client(path)
            file_client.upload_data(data, overwrite=False)
            manifest_files.append({
                "original_filename": name,
                "stored_filename": safe_name,
                "path": path,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        manifest = {
            "batch_id": batch_id,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "purpose": "raw-training-case",
            "processing_status": "uploaded_unprocessed",
            "files": manifest_files,
            "notes": "Original bytes preserved. No extraction, transformation, or model training was performed by this upload.",
        }
        manifest_path = f"{base}/manifest.json"
        filesystem.get_file_client(manifest_path).upload_data(
            json.dumps(manifest, indent=2).encode("utf-8"), overwrite=False
        )
        return {"batch_id": batch_id, "base_path": base, "manifest_path": manifest_path, "files": manifest_files}
    finally:
        service.close()
