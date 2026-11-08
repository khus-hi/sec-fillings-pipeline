"""Central configuration. Every setting comes from environment variables (.env),
so no secret, email address or machine-specific path is ever hard-coded."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Repo root = the folder that contains this package.
REPO_ROOT = Path(__file__).resolve().parent.parent
COMPANIES_CSV = REPO_ROOT / "ingestion" / "companies.csv"


@dataclass(frozen=True)
class Settings:
    sec_user_agent: str | None
    data_dir: Path
    duckdb_path: Path
    filings_per_company: int = 3
    storage_backend: str = "s3"  # s3 (SeaweedFS or AWS) | local
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None  # set for a local S3 server, empty for AWS
    aws_region: str = "us-east-1"
    max_requests_per_second: float = 5.0  # SEC allows 10; we stay well under


def _resolve(path_str: str) -> Path:
    path = Path(path_str)
    return path if path.is_absolute() else REPO_ROOT / path


def get_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env")
    return Settings(
        sec_user_agent=os.getenv("SEC_USER_AGENT") or None,
        data_dir=_resolve(os.getenv("DATA_DIR", "data")),
        duckdb_path=_resolve(os.getenv("DUCKDB_PATH", "data/warehouse.duckdb")),
        filings_per_company=int(os.getenv("FILINGS_PER_COMPANY", "3")),
        storage_backend=os.getenv("STORAGE_BACKEND", "s3").strip().lower(),
        s3_bucket=os.getenv("S3_BUCKET") or None,
        s3_endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
        aws_region=os.getenv("AWS_REGION", "us-east-1"),
        max_requests_per_second=float(os.getenv("SEC_MAX_RPS", "5")),
    )


def get_store(settings: Settings):
    """Build the raw-zone backend chosen in .env."""
    from ingestion.storage import LocalStore, S3Store

    if settings.storage_backend == "local":
        return LocalStore(settings.data_dir)
    if settings.storage_backend == "s3":
        if not settings.s3_bucket:
            raise ValueError("STORAGE_BACKEND=s3 needs S3_BUCKET in .env")
        return S3Store.from_settings(
            settings.s3_bucket, settings.s3_endpoint_url, settings.aws_region
        )
    raise ValueError(f"Unknown STORAGE_BACKEND={settings.storage_backend!r} (use s3 or local)")
