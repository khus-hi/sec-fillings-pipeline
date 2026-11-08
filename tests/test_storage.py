from datetime import date

import boto3
import pytest
from moto import mock_aws

from ingestion.storage import LocalStore, S3Store, raw_key


def test_raw_key_is_partitioned_by_filing_date():
    key = raw_key("10-K", date(2024, 11, 1), "0000320193", "0000320193-24-000123", ".HTM")
    assert key == "raw/edgar/10-K/filing_date=2024-11-01/0000320193_0000320193-24-000123.htm"


def test_raw_key_is_deterministic():
    args = ("10-K", date(2024, 11, 1), "0000320193", "0000320193-24-000123", "htm")
    assert raw_key(*args) == raw_key(*args)


@pytest.fixture
def s3_store():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="sec-raw")
        yield S3Store("sec-raw", client)


@pytest.fixture(params=["local", "s3"])
def store(request, tmp_path):
    """Run each test below against both backends: they must behave the same."""
    if request.param == "local":
        return LocalStore(tmp_path)
    return request.getfixturevalue("s3_store")


def test_put_get_exists(store):
    key = "raw/edgar/10-K/filing_date=2024-11-01/a.htm"
    assert not store.exists(key)
    store.put(key, b"hello", "text/html")
    assert store.exists(key)
    assert store.get(key) == b"hello"


def test_put_same_key_overwrites_not_duplicates(store):
    key = "raw/edgar/10-K/filing_date=2024-11-01/a.htm"
    store.put(key, b"v1", "text/html")
    store.put(key, b"v2", "text/html")
    assert store.list_keys() == [key]
    assert store.get(key) == b"v2"


def test_list_keys_sorted_and_prefixed(store):
    store.put("raw/edgar/10-K/filing_date=2024-11-01/b.htm", b"x", "text/html")
    store.put("raw/edgar/10-K/filing_date=2023-11-03/a.htm", b"x", "text/html")
    store.put("other/zzz.htm", b"x", "text/html")
    assert store.list_keys() == [
        "raw/edgar/10-K/filing_date=2023-11-03/a.htm",
        "raw/edgar/10-K/filing_date=2024-11-01/b.htm",
    ]


def test_uri(tmp_path, s3_store):
    assert s3_store.uri("raw/x.htm") == "s3://sec-raw/raw/x.htm"
    assert LocalStore(tmp_path).uri("raw/x.htm") == str(tmp_path / "raw/x.htm")


def test_ensure_bucket_creates_once():
    with mock_aws():
        store = S3Store("new-bucket", boto3.client("s3", region_name="us-east-1"))
        store.ensure_bucket()
        store.ensure_bucket()  # already exists: must not fail
        store.put("raw/a.htm", b"x", "text/html")
        assert store.list_keys("raw/") == ["raw/a.htm"]


def test_from_settings_ensures_bucket_only_for_local_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(S3Store, "ensure_bucket", lambda self: calls.append(self.bucket))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "local")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "local")

    local = S3Store.from_settings("sec-raw", "http://localhost:8333", "us-east-1")
    assert calls == ["sec-raw"]
    assert local.client.meta.config.s3["addressing_style"] == "path"

    S3Store.from_settings("sec-raw", None, "us-east-1")  # real AWS: never auto-create
    assert calls == ["sec-raw"]
