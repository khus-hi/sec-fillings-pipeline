"""Thin client for the free SEC EDGAR APIs.

Endpoints used (no API key needed):
  - https://www.sec.gov/files/company_tickers.json          ticker -> CIK
  - https://data.sec.gov/submissions/CIK##########.json      a company's filing index
  - https://www.sec.gov/Archives/edgar/data/<cik>/<acc>/<doc> the filing document itself

SEC's fair-access rules: send a User-Agent that names you and gives a contact email,
and stay under 10 requests per second. Break them and SEC blocks your IP for a while.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{document}"

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def pad_cik(cik: int | str) -> str:
    """EDGAR CIKs are up to 10 digits; the submissions API wants them zero-padded."""
    return str(int(cik)).zfill(10)


@dataclass(frozen=True)
class Filing:
    """One filing as listed in a company's submissions index (metadata only)."""

    cik: str  # zero-padded, 10 digits
    company_name: str
    sic: str | None
    sic_description: str | None
    form: str
    accession_number: str  # e.g. 0000320193-24-000123
    filing_date: date
    report_date: date | None  # period end the 10-K covers
    primary_document: str  # e.g. aapl-20240928.htm

    @property
    def url(self) -> str:
        return ARCHIVES_URL.format(
            cik_int=int(self.cik),
            acc_nodash=self.accession_number.replace("-", ""),
            document=self.primary_document,
        )


class RateLimiter:
    """Allow at most `rate` calls per second by spacing calls evenly.

    Simple and predictable: before each request, sleep until at least 1/rate seconds
    have passed since the previous one. Thread-safe so a later stage can parallelise.
    """

    def __init__(self, rate: float, clock=time.monotonic, sleep=time.sleep) -> None:
        self.min_interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._last = float("-inf")
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            delay = self._last + self.min_interval - now
            if delay > 0:
                self._sleep(delay)
                now += delay
            self._last = now


class EdgarClient:
    def __init__(
        self,
        user_agent: str | None,
        max_requests_per_second: float = 5.0,
        session: requests.Session | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not user_agent or not _EMAIL_RE.search(user_agent):
            raise ValueError(
                "SEC_USER_AGENT must be set in .env and include a contact email, "
                'e.g. SEC_USER_AGENT="Jane Doe jane@example.com"'
            )
        self.timeout = timeout
        self.limiter = RateLimiter(max_requests_per_second)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        # Retry transient failures (rate limited, server errors) with exponential backoff:
        # waits 1s, 2s, 4s, 8s. 404s are not retried: a missing file will stay missing.
        retry = Retry(
            total=4,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def _get(self, url: str) -> requests.Response:
        self.limiter.wait()
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def ticker_to_cik(self) -> dict[str, str]:
        """Map every listed ticker (upper case) to its zero-padded CIK."""
        data = self._get(TICKERS_URL).json()
        return {row["ticker"].upper(): pad_cik(row["cik_str"]) for row in data.values()}

    def list_filings(self, cik: str, form: str = "10-K", limit: int = 3) -> list[Filing]:
        """Newest `limit` filings of type `form` for one company, newest first.

        The submissions JSON holds the most recent ~1,000 filings inline. Big filers
        (banks issue hundreds of prospectuses a year) can push their 10-Ks out of that
        window, so we only fetch the older history pages when we still need more.
        """
        sub = self._get(SUBMISSIONS_URL.format(name=f"CIK{pad_cik(cik)}.json")).json()
        company = {
            "cik": pad_cik(sub.get("cik", cik)),
            "company_name": sub.get("name", ""),
            "sic": sub.get("sic") or None,
            "sic_description": sub.get("sicDescription") or None,
        }

        found = _filings_from_block(sub["filings"]["recent"], company, form)
        for page in sub["filings"].get("files", []):
            if len(found) >= limit:
                break
            older = self._get(SUBMISSIONS_URL.format(name=page["name"])).json()
            found.extend(_filings_from_block(older, company, form))

        found.sort(key=lambda f: f.filing_date, reverse=True)
        return found[:limit]

    def download(self, filing: Filing) -> bytes:
        return self._get(filing.url).content


def _filings_from_block(block: dict, company: dict, form: str) -> list[Filing]:
    """EDGAR stores filings column-wise (one list per field). Zip them into rows and
    keep only the exact form we want. "10-K/A" amendments are excluded on purpose:
    they are often partial (e.g. only Part III) and would double-count a fiscal year."""
    out: list[Filing] = []
    for i, this_form in enumerate(block.get("form", [])):
        if this_form != form:
            continue
        report = block.get("reportDate", [""] * (i + 1))[i]
        out.append(
            Filing(
                **company,
                form=this_form,
                accession_number=block["accessionNumber"][i],
                filing_date=date.fromisoformat(block["filingDate"][i]),
                report_date=date.fromisoformat(report) if report else None,
                primary_document=block["primaryDocument"][i],
            )
        )
    return out
