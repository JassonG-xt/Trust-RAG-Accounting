"""Immutable source-document object storage interfaces and S3 adapter."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

_SAFE_TENANT = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class StoredObject:
    uri: str
    checksum: str
    size_bytes: int
    content_type: str


@runtime_checkable
class SourceObjectStore(Protocol):
    def put(
        self,
        *,
        tenant_id: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> StoredObject: ...

    def get(self, uri: str) -> bytes: ...

    def delete(self, uri: str) -> None: ...


class S3SourceObjectStore:
    """Checksum-addressed storage for original ingestion files."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "trustrag-sources",
        endpoint_url: str | None = None,
        region_name: str | None = None,
        client: Any | None = None,
    ) -> None:
        if not bucket.strip():
            raise ValueError("S3 bucket must not be empty")
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise ImportError("install trust-rag[production] for S3 support") from exc
            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name=region_name,
            )
        self._client = client

    def put(
        self,
        *,
        tenant_id: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> StoredObject:
        if not _SAFE_TENANT.fullmatch(tenant_id):
            raise ValueError("tenant_id contains unsupported characters")
        safe_filename = Path(filename).name
        if not safe_filename:
            raise ValueError("filename must not be empty")
        checksum = hashlib.sha256(content).hexdigest()
        key_parts = [part for part in (self._prefix, tenant_id, checksum) if part]
        key = "/".join([*key_parts, safe_filename])
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
            Metadata={"sha256": checksum, "tenant-id": tenant_id},
        )
        return StoredObject(
            uri=f"s3://{self._bucket}/{key}",
            checksum=checksum,
            size_bytes=len(content),
            content_type=content_type,
        )

    def get(self, uri: str) -> bytes:
        key = self._key_from_uri(uri)
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        return bytes(response["Body"].read())

    def delete(self, uri: str) -> None:
        key = self._key_from_uri(uri)
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def _key_from_uri(self, uri: str) -> str:
        parsed = urlparse(uri)
        if parsed.scheme != "s3" or parsed.netloc != self._bucket:
            raise ValueError("source URI must use the configured S3 bucket")
        key = parsed.path.lstrip("/")
        if not key:
            raise ValueError("source URI is missing an object key")
        return key
