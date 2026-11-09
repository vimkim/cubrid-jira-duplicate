from datetime import UTC, datetime

import httpx
from factories import FakeEmbedder, at, comment, issue, raw_issue

from cbrd_dup import corpus, index
from cbrd_dup.corpus import parse_issue, rows
from cbrd_dup.index import Index
from cbrd_dup.jira import JiraClient


def test_parse_issue_keeps_seconds_and_turns_nulls_into_empty_values():
    raw = raw_issue("CBRD-7", at(0), comments=[comment("1", "hello", at(5), at(9))], summary="Title")
    parsed = parse_issue(raw)
    assert parsed.description == ""
    assert parsed.resolution is None
    assert parsed.created == at(0)
    assert parsed.created.utcoffset().total_seconds() == 9 * 3600
    assert parsed.comments[0].updated == at(9)
    raw["fields"]["resolution"] = {"name": "Duplicate"}
    raw["fields"]["description"] = "body"
    assert parse_issue(raw).resolution == "Duplicate"
    assert parse_issue(raw).description == "body"


def test_rows_split_an_issue_into_body_and_comments_without_metadata():
    item = issue(
        "CBRD-1", at(0), "Title", "Desc", [comment("a", "early", at(5)), comment("b", "edited", at(6), at(60))]
    )
    item = item.model_copy(update={"resolution": "Duplicate", "status": "Closed"})
    body, early, edited = rows(item)
    assert (body.kind, body.text, body.written) == ("body", "Title\n\nDesc", at(0))
    assert (early.kind, early.text, early.written) == ("comment", "early", at(5))
    assert edited.written == at(60)
    assert all("Duplicate" not in r.text and "Closed" not in r.text for r in (body, early, edited))


def test_visible_before_is_strict_on_both_the_issue_and_the_row():
    item = issue("CBRD-1", at(0), "T", "D", [comment("a", "c", at(10), at(20))])
    body, edited = rows(item)
    assert body.visible_before(at(1))
    assert not body.visible_before(at(0))
    assert not edited.visible_before(at(20))
    assert edited.visible_before(at(21))
    assert edited.visible_before(None)
    late_issue_row = rows(issue("CBRD-2", at(30), "T", "D"))[0]
    assert not late_issue_row.visible_before(at(30))


def test_default_index_dir_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(index.HOME_ENV, str(tmp_path / "home"))
    assert index.default_index_dir() == tmp_path / "home"
    monkeypatch.delenv(index.HOME_ENV)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert index.default_index_dir() == tmp_path / "cache" / "cubrid-jira-dup"


class FakeJira:
    """Answers search requests from `pages`, keyed by JQL, and records every request."""

    def __init__(self):
        self.pages: dict[str, list[dict]] = {}
        self.total: int | None = None
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = request.url.params
        issues = self.pages[params["jql"]]
        start, size = int(params["startAt"]), int(params["maxResults"])
        total = len(issues) if self.total is None else self.total
        return httpx.Response(200, json={"total": total, "issues": issues[start : start + size]})

    def client(self, index: Index) -> JiraClient:
        return JiraClient(index.raw, refresh=True, min_interval=0, transport=httpx.MockTransport(self))

    @property
    def jqls(self) -> list[str]:
        return [r.url.params["jql"] for r in self.requests]


FULL = "project = CBRD ORDER BY key ASC"
FIRST = datetime(2026, 11, 9, 10, 5, 30, tzinfo=UTC)
SECOND = datetime(2026, 11, 9, 11, 0, 0, tzinfo=UTC)
INCREMENTAL = 'project = CBRD AND updated >= "2026-11-09 18:55" ORDER BY key ASC'


def keys(index: Index) -> list[str]:
    return [i.key for i in index.issues()]


def test_first_sync_fetches_everything_and_a_second_fetches_only_the_recent_changes(tmp_path):
    idx = Index(tmp_path)
    jira = FakeJira()
    jira.pages[FULL] = [raw_issue("CBRD-10", at(0)), raw_issue("CBRD-9", at(1)), raw_issue("CBRD-2", at(2))]
    with jira.client(idx) as client:
        result = corpus.sync(client, idx, FIRST)
    assert (result.fetched, result.changed) == (3, 3)
    assert result.state.complete
    assert keys(idx) == ["CBRD-2", "CBRD-9", "CBRD-10"]
    assert idx.state().model_dump() == {"synced_at": FIRST, "complete": True, "issues": 3, "errors": []}

    jira.pages[INCREMENTAL] = [raw_issue("CBRD-9", at(1), summary="edited"), raw_issue("CBRD-11", at(3))]
    with jira.client(idx) as client:
        result = corpus.sync(client, idx, SECOND)
    assert jira.jqls == [FULL, INCREMENTAL]
    assert (result.fetched, result.changed, result.state.issues) == (2, 2, 4)
    issues = {i.key: i for i in idx.issues()}
    assert list(issues) == ["CBRD-2", "CBRD-9", "CBRD-10", "CBRD-11"]
    assert issues["CBRD-9"].summary == "edited"
    assert idx.state().synced_at == SECOND
    assert idx.attempted_at() == SECOND
    assert {r.method for r in jira.requests} == {"GET"}
    assert {r.url.scheme for r in jira.requests} == {"https"}
    assert all("authorization" not in r.headers for r in jira.requests)


def test_syncing_twice_converges_to_the_same_snapshot(tmp_path):
    idx = Index(tmp_path)
    jira = FakeJira()
    jira.pages[FULL] = [raw_issue("CBRD-1", at(0), comments=[comment("c", "hi", at(3), at(4))])]
    jira.pages[INCREMENTAL] = jira.pages[FULL]
    with jira.client(idx) as client:
        corpus.sync(client, idx, FIRST)
        once = idx.snapshot().sha256
        result = corpus.sync(client, idx, SECOND)
    assert result.changed == 0
    assert idx.snapshot().sha256 == once


def test_a_comment_total_mismatch_marks_the_sync_incomplete_but_keeps_the_issues(tmp_path):
    idx = Index(tmp_path)
    jira = FakeJira()
    jira.pages[FULL] = [raw_issue("CBRD-1", at(0), comments=[comment("c", "hi", at(3))], total=2)]
    with jira.client(idx) as client:
        result = corpus.sync(client, idx, FIRST)
    assert not result.state.complete
    assert "CBRD-1" in result.state.errors[0]
    assert keys(idx) == ["CBRD-1"]

    jira.pages[FULL] = [raw_issue("CBRD-1", at(0))]
    with jira.client(idx) as client:
        result = corpus.sync(client, idx, SECOND)
    assert jira.jqls == [FULL, FULL]
    assert result.state.complete


def test_a_short_result_marks_the_sync_incomplete(tmp_path):
    idx = Index(tmp_path)
    jira = FakeJira()
    jira.pages[FULL] = [raw_issue("CBRD-1", at(0)), raw_issue("CBRD-2", at(1))]
    jira.total = 3
    with jira.client(idx) as client:
        result = corpus.sync(client, idx, FIRST)
    assert not result.state.complete
    assert "total of 3" in result.state.errors[0]
    assert result.state.issues == 2


def test_full_sync_drops_issues_jira_no_longer_returns(tmp_path):
    idx = Index(tmp_path)
    jira = FakeJira()
    jira.pages[FULL] = [raw_issue("CBRD-1", at(0), summary="gone"), raw_issue("CBRD-2", at(1))]
    with jira.client(idx) as client:
        corpus.sync(client, idx, FIRST)
    jira.pages[FULL] = [raw_issue("CBRD-2", at(1))]
    with jira.client(idx) as client:
        corpus.sync(client, idx, SECOND, full=True)
    assert keys(idx) == ["CBRD-2"]
    assert idx.bm25("titles_fts", ["gone"])[0].size == 0


def test_snapshot_reports_the_state_and_the_data_hash(tmp_path):
    idx = Index(tmp_path)
    jira = FakeJira()
    jira.pages[FULL] = [raw_issue("CBRD-1", at(0))]
    with jira.client(idx) as client:
        corpus.sync(client, idx, FIRST)
    snap = idx.snapshot()
    assert (snap.issues, snap.synced_at, snap.complete) == (1, FIRST, True)
    assert len(snap.sha256) == 64


def test_incremental_sync_replaces_a_changed_issue_and_embeds_only_changed_text(tmp_path):
    idx = Index(tmp_path)
    jira = FakeJira()
    kept = comment("a", "kept comment", at(3))
    jira.pages[FULL] = [
        raw_issue("CBRD-1", at(0), comments=[kept, comment("b", "walrus remark", at(4))], summary="heap"),
        raw_issue("CBRD-2", at(1), summary="vacuum"),
    ]
    embedder = FakeEmbedder()
    with jira.client(idx) as client:
        corpus.sync(client, idx, FIRST)
    assert idx.embed_pending(embedder) == 6
    assert idx.pending() == 0

    jira.pages[INCREMENTAL] = [
        raw_issue("CBRD-1", at(0), comments=[kept, comment("b", "zebra remark", at(4), at(9))], summary="heap")
    ]
    with jira.client(idx) as client:
        result = corpus.sync(client, idx, SECOND)
    assert result.changed == 1
    assert idx.bm25("rows_fts", ["walrus"])[0].size == 0
    assert idx.bm25("rows_fts", ["zebra"])[0].size == 1
    assert idx.pending() == 1
    embedder.calls.clear()
    assert idx.embed_pending(embedder) == 1
    assert embedder.texts == ["passage: zebra remark"]
    assert idx.arrays().row_embedded.all()


def test_a_new_embedding_model_reembeds_everything(tmp_path):
    idx = Index(tmp_path)
    idx.apply([issue("CBRD-1", at(0), "heap", "body")], FIRST, [], replace_all=True)
    idx.embed_pending(FakeEmbedder())
    other = FakeEmbedder()
    other.model_id = "another-model"
    assert idx.embed_pending(other) == 2
    assert idx.model() == "another-model"
