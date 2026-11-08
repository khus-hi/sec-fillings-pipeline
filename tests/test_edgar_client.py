from datetime import date

import pytest
import responses

from ingestion.edgar_client import TICKERS_URL, EdgarClient, RateLimiter, pad_cik


def test_pad_cik():
    assert pad_cik(320193) == "0000320193"
    assert pad_cik("0000320193") == "0000320193"


@pytest.mark.parametrize("ua", [None, "", "just a name"])
def test_user_agent_must_contain_email(ua):
    with pytest.raises(ValueError, match="SEC_USER_AGENT"):
        EdgarClient(ua)


def test_user_agent_header_is_sent(edgar, client):
    client.ticker_to_cik()
    assert edgar.calls[0].request.headers["User-Agent"] == "Test Runner test@example.com"


def test_ticker_to_cik(edgar, client):
    assert client.ticker_to_cik() == {"AAPL": "0000320193", "JPM": "0000019617"}


def test_list_filings_keeps_only_exact_form_newest_first(edgar, client):
    filings = client.list_filings("320193", limit=5)
    assert [f.accession_number for f in filings] == [
        "0000320193-24-000123",
        "0000320193-23-000106",
    ]  # 10-Q, 8-K and the 10-K/A amendment are excluded
    first = filings[0]
    assert first.cik == "0000320193"
    assert first.company_name == "Apple Inc."
    assert first.sic == "3571"
    assert first.filing_date == date(2024, 11, 1)
    assert first.report_date == date(2024, 9, 28)
    assert first.url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm"
    )


def test_list_filings_respects_limit(edgar, client):
    assert len(client.list_filings("320193", limit=1)) == 1


def test_history_pages_fetched_only_when_needed(edgar, client):
    history = "CIK0000019617-submissions-001.json"

    # One 10-K is in the recent block, so asking for one must not touch the history page.
    assert len(client.list_filings("19617", limit=1)) == 1
    assert not any(history in c.request.url for c in edgar.calls)

    # Asking for three needs the older page.
    filings = client.list_filings("19617", limit=3)
    assert [f.filing_date.year for f in filings] == [2025, 2024, 2023]
    assert any(history in c.request.url for c in edgar.calls)


def test_retries_on_429_then_succeeds(client, monkeypatch):
    # urllib3's backoff would really sleep; make it instant.
    monkeypatch.setattr("urllib3.util.retry.Retry.sleep", lambda self, response=None: None)
    with responses.RequestsMock() as mock:
        mock.get(TICKERS_URL, status=429)
        mock.get(TICKERS_URL, json={"0": {"cik_str": 1, "ticker": "X", "title": "X"}})
        assert client.ticker_to_cik() == {"X": "0000000001"}
        assert len(mock.calls) == 2


def test_rate_limiter_spaces_calls():
    now = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(rate=5, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.wait()
    # First call is free; the next two each wait 1/5 s.
    assert slept == pytest.approx([0.2, 0.2])
