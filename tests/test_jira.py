import json

import httpx
import pytest

from cbrd_dup.jira import JiraClient, JiraError, cache_key, search_url

# The request behind data/raw/0dcefa628f217cf70b63239f704ac41517235b22.json, written by the old scripts.
KNOWN_URL = (
    "http://jira.cubrid.org/rest/api/2/search?jql=project+%3D+CBRD+AND+resolution+%3D+Duplicate+ORDER+BY+key+ASC"
    "&fields=summary%2Ccreated%2Cissuetype%2Cissuelinks%2Ccomment&maxResults=1000&startAt=0"
)
KNOWN_HTTPS_URL = KNOWN_URL.replace("http://", "https://", 1)
KNOWN_KEY = "0dcefa628f217cf70b63239f704ac41517235b22"
JQL = "project = CBRD AND resolution = Duplicate ORDER BY key ASC"
FIELDS = "summary,created,issuetype,issuelinks,comment"


def make_client(tmp_path, handler, **kwargs):
    sleeps: list[float] = []
    client = JiraClient(
        tmp_path / "raw", min_interval=0, transport=httpx.MockTransport(handler), sleep=sleeps.append, **kwargs
    )
    return client, sleeps


def ok(request):
    return httpx.Response(200, json={"total": 0, "issues": []})


def test_cache_key_for_known_request():
    assert search_url(JQL, FIELDS, 1000, 0) == KNOWN_HTTPS_URL
    assert cache_key(search_url(JQL, FIELDS, 1000, 0)) == KNOWN_KEY
    assert cache_key(KNOWN_URL) == KNOWN_KEY


def test_requests_go_straight_to_https(tmp_path):
    seen = []

    def record(request):
        seen.append(request.url)
        return ok(request)

    client, _ = make_client(tmp_path, record)
    client.search(JQL, FIELDS, 1000, 0)
    assert [(url.scheme, url.host) for url in seen] == [("https", "jira.cubrid.org")]


def test_redirect_is_reported_not_followed_or_cached(tmp_path):
    location = KNOWN_HTTPS_URL.replace("%", "%25")
    calls = []

    def redirecting(request):
        calls.append(request.url)
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(400, json={"errorMessages": ["The character '%' is a reserved JQL character."]})

    client, _ = make_client(tmp_path, redirecting)
    with pytest.raises(JiraError):
        client.search(JQL, FIELDS, 1000, 0)
    assert not list((tmp_path / "raw").glob("*.json"))


def test_extra_params_follow_the_standard_ones():
    url = search_url("key in (CBRD-1)", "key", 200, 0, validateQuery="false")
    assert url.endswith("&maxResults=200&startAt=0&validateQuery=false")


def test_cache_written_by_old_scripts_is_reused(tmp_path):
    cache = tmp_path / "raw"
    cache.mkdir()
    record = {"url": KNOWN_URL, "fetched_at": "2026-11-06T08:35:12+00:00", "status": 200, "response": {"total": 7}}
    (cache / f"{KNOWN_KEY}.json").write_text(json.dumps(record), encoding="utf-8")

    def refuse(request):
        raise AssertionError("cached request went to the network")

    client, _ = make_client(tmp_path, refuse)
    assert client.search(JQL, FIELDS, 1000, 0) == (200, {"total": 7})
    assert (client.network, client.cache) == (0, 1)


def test_response_is_cached_in_the_old_record_format(tmp_path):
    client, _ = make_client(tmp_path, ok)
    client.search(JQL, FIELDS, 1000, 0)
    client.search(JQL, FIELDS, 1000, 0)
    assert (client.network, client.cache) == (1, 1)
    record = json.loads((tmp_path / "raw" / f"{KNOWN_KEY}.json").read_text(encoding="utf-8"))
    assert list(record) == ["url", "fetched_at", "status", "response"]
    assert record["url"] == KNOWN_URL
    assert not list((tmp_path / "raw").glob("*.tmp"))


def test_refresh_ignores_the_cache(tmp_path):
    client, _ = make_client(tmp_path, ok, refresh=True)
    client.search(JQL)
    client.search(JQL)
    assert (client.network, client.cache) == (2, 0)


def test_invalid_query_reply_is_returned_and_cached(tmp_path):
    def bad_query(request):
        return httpx.Response(400, json={"errorMessages": ["bad jql"]})

    client, sleeps = make_client(tmp_path, bad_query)
    assert client.search("nonsense") == (400, {"errorMessages": ["bad jql"]})
    assert client.search("nonsense").status == 400
    assert (client.network, client.cache, sleeps) == (1, 1, [])


def test_retries_429_and_5xx_with_backoff(tmp_path):
    statuses = iter([503, 429, 200])

    def flaky(request):
        status = next(statuses)
        return httpx.Response(status, json={"total": 1, "issues": []})

    client, sleeps = make_client(tmp_path, flaky)
    assert client.search(JQL).status == 200
    assert client.network == 3
    assert sleeps == [2, 4]


def test_gives_up_after_four_attempts(tmp_path):
    client, sleeps = make_client(tmp_path, lambda request: httpx.Response(503))
    with pytest.raises(JiraError, match="HTTP 503"):
        client.search(JQL)
    assert client.network == 4
    assert sleeps == [2, 4, 8]
    assert not (tmp_path / "raw").exists()


def test_retries_timeouts_then_raises(tmp_path):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = make_client(tmp_path, slow)
    with pytest.raises(JiraError, match="4 attempts"):
        client.search(JQL)


def test_other_client_errors_are_not_retried(tmp_path):
    client, sleeps = make_client(tmp_path, lambda request: httpx.Response(404))
    with pytest.raises(JiraError, match="HTTP 404"):
        client.search(JQL)
    assert (client.network, sleeps) == (1, [])


def test_requests_are_paced(tmp_path):
    client, sleeps = make_client(tmp_path, ok)
    client.min_interval = 1.0
    client.search("one")
    client.search("two")
    assert len(sleeps) == 1
    assert 0.9 < sleeps[0] <= 1.0


def test_search_all_pages_until_total(tmp_path):
    def paged(request):
        start = int(request.url.params["startAt"])
        issues = [{"key": f"CBRD-{n}"} for n in range(start, min(start + 2, 5))]
        return httpx.Response(200, json={"total": 5, "issues": issues})

    client, _ = make_client(tmp_path, paged)
    issues = client.search_all(JQL, "key", page=2)
    assert [i["key"] for i in issues] == [f"CBRD-{n}" for n in range(5)]
    assert client.network == 3


def test_search_all_raises_on_invalid_query(tmp_path):
    client, _ = make_client(tmp_path, lambda request: httpx.Response(400, json={"errorMessages": ["bad"]}))
    with pytest.raises(JiraError, match="search failed .400."):
        client.search_all("nonsense", "key")
