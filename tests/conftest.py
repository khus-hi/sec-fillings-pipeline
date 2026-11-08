"""Shared fixtures. Every test runs offline: EDGAR is faked with `responses`
and S3 with `moto`, so CI never needs network access, an email or AWS keys."""

from __future__ import annotations

import pytest
import responses

from ingestion.edgar_client import ARCHIVES_URL, SUBMISSIONS_URL, TICKERS_URL, EdgarClient

UA = "Test Runner test@example.com"

TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 19617, "ticker": "JPM", "title": "JPMorgan Chase & Co."},
    # After a holding-company reorganisation, the ticker points at the new entity.
    "2": {"cik_str": 2115436, "ticker": "XOM", "title": "ExxonMobil Holdings Corp"},
}


def _block(rows: list[tuple[str, str, str, str, str]]) -> dict:
    """Build EDGAR's column-wise filing block from (form, accession, filed, period, doc) rows."""
    return {
        "form": [r[0] for r in rows],
        "accessionNumber": [r[1] for r in rows],
        "filingDate": [r[2] for r in rows],
        "reportDate": [r[3] for r in rows],
        "primaryDocument": [r[4] for r in rows],
    }


# Apple: everything we need is in the "recent" block. Includes noise (10-Q, 8-K, 10-K/A).
AAPL_SUBMISSIONS = {
    "cik": "320193",
    "name": "Apple Inc.",
    "sic": "3571",
    "sicDescription": "Electronic Computers",
    "filings": {
        "recent": _block(
            [
                ("10-Q", "0000320193-25-000010", "2025-01-31", "2024-12-28", "q1.htm"),
                ("10-K", "0000320193-24-000123", "2024-11-01", "2024-09-28", "aapl-20240928.htm"),
                ("8-K", "0000320193-24-000100", "2024-10-31", "", "8k.htm"),
                ("10-K/A", "0000320193-24-000050", "2024-03-01", "2023-09-30", "amend.htm"),
                ("10-K", "0000320193-23-000106", "2023-11-03", "2023-09-30", "aapl-20230930.htm"),
            ]
        ),
        "files": [],
    },
}

# JPM: the recent block is flooded with prospectuses; older 10-Ks live in a history page.
JPM_SUBMISSIONS = {
    "cik": "19617",
    "name": "JPMORGAN CHASE & CO",
    "sic": "6021",
    "sicDescription": "National Commercial Banks",
    "filings": {
        "recent": _block(
            [
                ("424B2", "0001213900-25-000001", "2025-02-20", "", "p1.htm"),
                ("10-K", "0000019617-25-000270", "2025-02-14", "2024-12-31", "jpm-20241231.htm"),
                ("424B2", "0001213900-25-000002", "2025-02-10", "", "p2.htm"),
            ]
        ),
        "files": [{"name": "CIK0000019617-submissions-001.json"}],
    },
}
JPM_HISTORY = _block(
    [
        ("10-K", "0000019617-24-000225", "2024-02-16", "2023-12-31", "jpm-20231231.htm"),
        ("10-K", "0000019617-23-000231", "2023-02-21", "2022-12-31", "jpm-20221231.htm"),
    ]
)


# The new holdco: no 10-K yet, just the successor-registration 8-K12B.
XOM_HOLDCO_SUBMISSIONS = {
    "cik": "2115436",
    "name": "ExxonMobil Holdings Corp",
    "sic": "2911",
    "sicDescription": "Petroleum Refining",
    "filings": {
        "recent": _block([("8-K12B", "0002115436-26-000001", "2026-06-01", "", "8k12b.htm")]),
        "files": [],
    },
}
# The predecessor entity still holds the 10-K history.
XOM_OLD_SUBMISSIONS = {
    "cik": "34088",
    "name": "EXXON MOBIL CORP",
    "sic": "2911",
    "sicDescription": "Petroleum Refining",
    "filings": {
        "recent": _block(
            [
                ("10-K", "0000034088-26-000010", "2026-02-18", "2025-12-31", "xom-20251231.htm"),
                ("10-K", "0000034088-25-000010", "2025-02-19", "2024-12-31", "xom-20241231.htm"),
            ]
        ),
        "files": [],
    },
}


def doc_url(cik: int, accession: str, document: str) -> str:
    return ARCHIVES_URL.format(
        cik_int=cik, acc_nodash=accession.replace("-", ""), document=document
    )


@pytest.fixture
def edgar():
    """A fake EDGAR. Yields the `responses` mock so tests can count calls."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.get(TICKERS_URL, json=TICKERS)
        mock.get(SUBMISSIONS_URL.format(name="CIK0000320193.json"), json=AAPL_SUBMISSIONS)
        mock.get(SUBMISSIONS_URL.format(name="CIK0000019617.json"), json=JPM_SUBMISSIONS)
        mock.get(
            SUBMISSIONS_URL.format(name="CIK0000019617-submissions-001.json"), json=JPM_HISTORY
        )
        mock.get(SUBMISSIONS_URL.format(name="CIK0002115436.json"), json=XOM_HOLDCO_SUBMISSIONS)
        mock.get(SUBMISSIONS_URL.format(name="CIK0000034088.json"), json=XOM_OLD_SUBMISSIONS)
        for cik, block in [
            (34088, XOM_OLD_SUBMISSIONS["filings"]["recent"]),
            (320193, AAPL_SUBMISSIONS["filings"]["recent"]),
            (19617, JPM_SUBMISSIONS["filings"]["recent"]),
            (19617, JPM_HISTORY),
        ]:
            for acc, doc in zip(block["accessionNumber"], block["primaryDocument"], strict=True):
                body = f"<html><body>{acc} annual report</body></html>".encode()
                mock.get(doc_url(cik, acc, doc), body=body)
        yield mock


@pytest.fixture
def client() -> EdgarClient:
    # A very high rate so tests don't sleep.
    return EdgarClient(UA, max_requests_per_second=10_000)
