"""Raw zone storage: MinIO / AWS S3, or a local folder for quick experiments.

Both backends use the same Hive-style key layout (see `raw_key`):

    raw/edgar/10-K/filing_date=YYYY-MM-DD/<cik>_<accession>.htm

The ingestion code only talks to the small `RawStore` interface, so moving from
MinIO on a laptop to real S3 is a .env change (S3_ENDPOINT_URL), not a code change.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Protocol

RAW_PREFIX = "raw/edgar"


def raw_key(form: str, filing_date: date, cik: str, accession_number: str, ext: str) -> str:
    """Deterministic object key for one filing.

    The same filing always maps to the same key, so writing it twice overwrites
    instead of creating a duplicate. Partitioning by *filing* date (not by the day
    we happened to download it) keeps that true across re-runs on different days.
    """
    ext = ext.lstrip(".").lower() or "htm"
    return (
        f"{RAW_PREFIX}/{form}/filing_date={filing_date.isoformat()}/{cik}_{accession_number}.{ext}"
    )


class RawStore(Protocol):
    def put(self, key: str, body: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def list_keys(self, prefix: str = RAW_PREFIX) -> list[str]: ...

    def uri(self, key: str) -> str: ...


class LocalStore:
    """A folder on disk that mimics a bucket. Handy for tests and offline work."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, body: bytes, content_type: str = "text/html") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file, then rename: a crash never leaves a half-written file
        # under the real name.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list_keys(self, prefix: str = RAW_PREFIX) -> list[str]:
        base = self._path(prefix)
        if not base.exists():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in base.rglob("*")
            if p.is_file() and not p.name.endswith(".tmp")
        )

    def uri(self, key: str) -> str:
        return str(self._path(key))


class S3Store:
    """An S3 bucket: AWS, or MinIO when an endpoint URL is set.

    A single put_object is atomic in S3 (readers see the old object or the new one,
    never half of it), so no temp-file trick is needed here.
    """

    def __init__(self, bucket: str, client) -> None:
        self.bucket = bucket
        self.client = client

    @classmethod
    def from_settings(cls, bucket: str, endpoint_url: str | None, region: str) -> S3Store:
        import boto3  # imported here so local-only runs don't need boto3 configured

        # Credentials come from the standard AWS chain: AWS_ACCESS_KEY_ID and
        # AWS_SECRET_ACCESS_KEY from .env (MinIO's root user/password), or ~/.aws.
        client = boto3.client("s3", endpoint_url=endpoint_url or None, region_name=region)
        return cls(bucket, client)

    def put(self, key: str, body: bytes, content_type: str = "text/html") -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as err:
            if err.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def list_keys(self, prefix: str = RAW_PREFIX) -> list[str]:
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return sorted(keys)

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"
