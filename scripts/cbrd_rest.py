"""Anonymous, cached reads of the public CBRD JIRA REST search API.

Shared by the measurement scripts in this directory. Every network request
waits at least MIN_INTERVAL seconds after the previous one. Each raw response
is saved under data/raw/ (git ignores it) and reused on later runs unless the
caller passes refresh=True.
"""
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "http://jira.cubrid.org/rest/api/2/search"
ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
MIN_INTERVAL = 0.25
HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")

_last_request = 0.0
stats = {"network": 0, "cache": 0}


def _get(url):
    global _last_request
    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    for attempt in range(4):
        try:
            _last_request = time.monotonic()
            with urllib.request.urlopen(url, timeout=120) as resp:
                stats["network"] += 1
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as err:
            stats["network"] += 1
            if err.code == 400:
                return 400, json.loads(err.read().decode() or "{}")
            if attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def search(jql, fields="key", max_results=50, start_at=0, refresh=False, **params):
    """Run one search request. Returns (http_status, json_body)."""
    query = {"jql": jql, "fields": fields, "maxResults": max_results, "startAt": start_at, **params}
    url = f"{BASE}?{urllib.parse.urlencode(query)}"
    path = RAW_DIR / f"{hashlib.sha1(url.encode()).hexdigest()}.json"
    if path.exists() and not refresh:
        stats["cache"] += 1
        cached = json.loads(path.read_text())
        return cached["status"], cached["response"]
    status, body = _get(url)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    record = {"url": url, "fetched_at": datetime.now(timezone.utc).isoformat(), "status": status, "response": body}
    path.write_text(json.dumps(record, ensure_ascii=False))
    return status, body


def search_all(jql, fields, refresh=False, page=1000, **params):
    """Fetch every issue matching jql. Add a stable ORDER BY to jql yourself."""
    issues, start = [], 0
    while True:
        status, body = search(jql, fields, page, start, refresh, **params)
        if status != 200:
            raise RuntimeError(f"search failed ({status}): {body}")
        issues.extend(body["issues"])
        start += len(body["issues"])
        if not body["issues"] or start >= body["total"]:
            return issues


def parse_created(value):
    """Parse a JIRA timestamp such as 2024-03-05T14:23:11.000+0900."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def has_hangul(text):
    return bool(HANGUL.search(text or ""))


def nearest_rank(sorted_values, q):
    """Nearest-rank percentile (q in 0..1) of an ascending list."""
    if not sorted_values:
        return None
    rank = math.ceil(round(q * len(sorted_values), 9))
    return sorted_values[min(len(sorted_values), max(rank, 1)) - 1]
