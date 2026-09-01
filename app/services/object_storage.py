from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from app.config import BASE_DIR, Settings, settings
from app.services.object_storage_local import LocalObjectStorageAdapter
from app.services.object_storage_remote import (
    MinioObjectStorageAdapter,
    OssObjectStorageAdapter,
)


def build_object_storage_adapter(config: Settings = settings):
    provider = config.document_storage_provider
    if provider == "local":
        root = Path(config.document_files_dir)
        if not root.is_absolute():
            root = BASE_DIR / root
        return LocalObjectStorageAdapter(
            root=root,
            endpoint_ref=config.document_storage_endpoint_ref,
            max_read_bytes=config.document_storage_max_read_bytes,
        )
    if provider == "minio":
        from minio import Minio

        parsed = urlparse(config.document_storage_endpoint_url)
        client = Minio(
            parsed.netloc,
            access_key=config.document_storage_access_key.get_secret_value(),
            secret_key=config.document_storage_secret_key.get_secret_value(),
            secure=parsed.scheme == "https",
            region=config.document_storage_region or None,
        )
        return MinioObjectStorageAdapter(
            client=client,
            endpoint_ref=config.document_storage_endpoint_ref,
            bucket=config.document_storage_bucket,
            max_read_bytes=config.document_storage_max_read_bytes,
        )
    if provider == "oss":
        import oss2

        auth = oss2.Auth(
            config.document_storage_access_key.get_secret_value(),
            config.document_storage_secret_key.get_secret_value(),
        )
        bucket_client = oss2.Bucket(
            auth,
            config.document_storage_endpoint_url,
            config.document_storage_bucket,
        )
        return OssObjectStorageAdapter(
            bucket_client=bucket_client,
            endpoint_ref=config.document_storage_endpoint_ref,
            bucket=config.document_storage_bucket,
            max_read_bytes=config.document_storage_max_read_bytes,
        )
    raise RuntimeError("document storage provider is unsupported")
