from __future__ import annotations

from io import BytesIO

import pytest

from backend.app.persistence.objects import S3SourceObjectStore, SourceObjectStore


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}

    def put_object(self, **kwargs) -> None:
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs

    def get_object(self, **kwargs) -> dict:
        stored = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {"Body": BytesIO(stored["Body"])}

    def delete_object(self, **kwargs) -> None:
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)


def test_s3_source_store_uses_checksum_addressed_immutable_key() -> None:
    client = _FakeS3Client()
    store = S3SourceObjectStore(client=client, bucket="rag-sources", prefix="source")

    first = store.put(
        tenant_id="tenant-a",
        filename="policy.pdf",
        content=b"pdf-content",
        content_type="application/pdf",
    )
    second = store.put(
        tenant_id="tenant-a",
        filename="policy.pdf",
        content=b"pdf-content",
        content_type="application/pdf",
    )

    assert isinstance(store, SourceObjectStore)
    assert first == second
    assert first.uri.startswith(f"s3://rag-sources/source/tenant-a/{first.checksum}/")
    assert store.get(first.uri) == b"pdf-content"


def test_s3_source_store_rejects_uri_for_another_bucket() -> None:
    store = S3SourceObjectStore(client=_FakeS3Client(), bucket="rag-sources")

    with pytest.raises(ValueError, match="bucket"):
        store.get("s3://other-bucket/source/tenant-a/checksum/policy.pdf")
