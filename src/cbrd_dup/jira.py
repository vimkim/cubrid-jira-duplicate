"""Anonymous, cached reads of the public CBRD JIRA REST search API.

This module is the only code that talks to JIRA. Every network request waits at least `min_interval`
seconds after the previous one, transient failures are retried with backoff, and each raw response is
saved under `<cache_dir>/<SHA-1 of the request URL with an http scheme>.json` and reused on later runs
unless `refresh` is set.
"""

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, Self
from urllib.parse import urlencode

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

BASE = "https://jira.cubrid.org/rest/api/2/search"
MIN_INTERVAL = 0.25
TIMEOUT = 120.0
ATTEMPTS = 4
KST = timezone(timedelta(hours=9))


class JiraError(RuntimeError):
    """A request failed for good, or JIRA answered with an unexpected status."""


class _Transient(JiraError):
    """HTTP 429 or 5xx: worth another attempt."""


class Reply(NamedTuple):
    status: int
    body: dict

    def require_ok(self) -> dict:
        if self.status != 200:
            raise JiraError(f"search failed ({self.status}): {self.body}")
        return self.body


def search_url(jql: str, fields: str = "key", max_results: int = 50, start_at: int = 0, **params) -> str:
    """Build the request URL. Its exact text is the cache key, so keep the parameter order stable."""
    query = {"jql": jql, "fields": fields, "maxResults": max_results, "startAt": start_at, **params}
    return f"{BASE}?{urlencode(query)}"


def cache_key(url: str) -> str:
    """Hash the URL as `http://`, the scheme existing cache files were keyed under before the move to HTTPS."""
    legacy = "http://" + url.split("://", 1)[1]
    return hashlib.sha1(legacy.encode(), usedforsecurity=False).hexdigest()


def key_number(key: str) -> int:
    """The number in an issue key, for sorting CBRD-123 numerically."""
    return int(key.split("-")[1])


def parse_created(value: str) -> datetime:
    """Parse a JIRA timestamp such as 2024-03-05T14:23:11.000+0900."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


class JiraClient:
    def __init__(
        self,
        cache_dir: Path,
        *,
        refresh: bool = False,
        min_interval: float = MIN_INTERVAL,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.min_interval = min_interval
        self.network = 0
        self.cache = 0
        self._sleep = sleep
        self._last_request = 0.0
        self._http = httpx.Client(transport=transport, timeout=TIMEOUT, follow_redirects=False)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self._http.close()

    def search(self, jql: str, fields: str = "key", max_results: int = 50, start_at: int = 0, **params) -> Reply:
        """Run one search request. HTTP 400 (an invalid query) is returned and cached like a success."""
        url = search_url(jql, fields, max_results, start_at, **params)
        path = self.cache_dir / f"{cache_key(url)}.json"
        if path.exists() and not self.refresh:
            self.cache += 1
            cached = json.loads(path.read_text(encoding="utf-8"))
            return Reply(cached["status"], cached["response"])
        reply = self._fetch(url)
        record = {
            "url": url,
            "fetched_at": datetime.now(UTC).isoformat(),
            "status": reply.status,
            "response": reply.body,
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Replace atomically so an interrupted run cannot leave a truncated entry that poisons later runs.
        partial = path.with_suffix(".tmp")
        partial.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        partial.replace(path)
        return reply

    def search_all(self, jql: str, fields: str, page: int = 1000, **params) -> list[dict]:
        """Fetch every issue matching jql. Add a stable ORDER BY to jql yourself."""
        return self.search_all_counted(jql, fields, page, **params)[0]

    def search_all_counted(self, jql: str, fields: str, page: int = 1000, **params) -> tuple[list[dict], int]:
        """Like `search_all`, also returning the `total` JIRA reported on the last page."""
        issues: list[dict] = []
        while True:
            body = self.search(jql, fields, page, len(issues), **params).require_ok()
            issues.extend(body["issues"])
            if not body["issues"] or len(issues) >= body["total"]:
                return issues, body["total"]

    def _fetch(self, url: str) -> Reply:
        retrying = Retrying(
            stop=stop_after_attempt(ATTEMPTS),
            wait=wait_exponential(multiplier=2, max=30),
            retry=retry_if_exception_type((httpx.TransportError, _Transient)),
            sleep=self._sleep,
            reraise=True,
        )
        try:
            return retrying(self._get_once, url)
        except httpx.TransportError as err:
            raise JiraError(f"request failed after {ATTEMPTS} attempts: {err!r}") from err

    def _get_once(self, url: str) -> Reply:
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()
        response = self._http.get(url)
        self.network += 1
        status = response.status_code
        if status == 429 or status >= 500:
            raise _Transient(f"HTTP {status} for {url}")
        if status not in (200, 400):
            raise JiraError(f"HTTP {status} for {url}")
        return Reply(status, response.json() if response.content else {})
