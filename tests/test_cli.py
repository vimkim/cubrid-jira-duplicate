import json

import httpx
import pytest
from factories import FakeEmbedder, at, comment, issue, pair, raw_issue
from typer.testing import CliRunner

from cbrd_dup import cli
from cbrd_dup.cli import app
from cbrd_dup.index import Index
from cbrd_dup.jira import JiraClient, cache_key, search_url
from cbrd_dup.measure import gold
from cbrd_dup.store import DataDir, write_jsonl

runner = CliRunner()


def seed_cache(cache, jql, fields, params, issues):
    url = search_url(jql, fields, 1000, 0, **params)
    cache.mkdir(parents=True, exist_ok=True)
    record = {"url": url, "fetched_at": "x", "status": 200, "response": {"total": len(issues), "issues": issues}}
    (cache / f"{cache_key(url)}.json").write_text(json.dumps(record), encoding="utf-8")


def test_help_lists_measure():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "measure" in result.output


@pytest.mark.parametrize("name", ["corpus", "gold", "probe", "baseline", "gold-stats", "volume"])
def test_every_measure_command_takes_data_dir(name):
    result = runner.invoke(app, ["measure", name, "--help"])
    assert result.exit_code == 0
    assert "--data-dir" in result.output


def test_gold_stats_runs_offline_on_fixture_pairs(gold_pairs, tmp_path):
    write_jsonl(DataDir(tmp_path).gold, gold_pairs)
    result = runner.invoke(app, ["measure", "gold-stats", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "# Gold pair statistics (4 pairs, 3 with a visible candidate)" in result.output
    assert "## Summary language" in result.output


@pytest.mark.parametrize("command", ["gold-stats", "baseline"])
def test_commands_that_need_gold_pairs_say_what_to_run(command, tmp_path):
    result = runner.invoke(app, ["measure", command, "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "cubrid-jira-dup measure gold" in result.output
    assert not (tmp_path / "raw").exists()


def test_volume_rejects_a_malformed_reference_time(tmp_path):
    result = runner.invoke(app, ["measure", "volume", "--now", "yesterday", "--data-dir", str(tmp_path)])
    assert result.exit_code == 2
    assert not (tmp_path / "raw").exists()


def test_gold_runs_from_the_cache_with_zero_network_requests(gold_issues, tmp_path):
    cache = DataDir(tmp_path).raw
    seed_cache(cache, gold.DUP_JQL, gold.DUP_FIELDS, {}, gold_issues["duplicates"])
    keys = "CBRD-90, CBRD-95, CBRD-150, CBRD-9999"
    seed_cache(
        cache,
        f"key in ({keys}) ORDER BY key ASC",
        gold.CAND_FIELDS,
        {"validateQuery": "false"},
        gold_issues["candidates"],
    )
    result = runner.invoke(app, ["measure", "gold", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "- requests: network 0, cache 2" in result.output
    assert (tmp_path / "gold-pairs.jsonl").read_text(encoding="utf-8").count("\n") == 4


@pytest.mark.parametrize("args", [["sync"], ["eval", "candidates"], ["candidate-find"]])
def test_sync_and_eval_candidates_document_the_index_dir(args):
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0
    assert "--index-dir" in result.output


@pytest.fixture
def embedder(monkeypatch):
    fake = FakeEmbedder()
    monkeypatch.setattr(cli, "make_embedder", lambda index, threads: fake)
    return fake


def build_index(path, issues, embedder):
    index = Index(path)
    index.apply(issues, at(200), [], replace_all=True)
    index.embed_pending(embedder)
    embedder.calls.clear()
    return index


@pytest.mark.parametrize(("method", "found"), [("bm25", ["CBRD-1"]), ("hybrid", ["CBRD-1", "CBRD-2"])])
def test_eval_candidates_runs_offline_from_an_index_and_writes_json(tmp_path, embedder, method, found):
    issues = [
        issue("CBRD-1", at(0), "heap vacuum crash", "", [comment("1", "zebra", at(5))]),
        issue("CBRD-2", at(1), "apples", ""),
        issue("CBRD-9", at(100), "heap vacuum crash again", "see CBRD-1"),
    ]
    index = build_index(tmp_path / "index", issues, embedder)
    write_jsonl(DataDir(tmp_path / "data").gold, [pair("CBRD-9", "CBRD-1", "2024-03-05T14:24:51.000+0900")])
    out = tmp_path / "eval.json"
    args = ["eval", "candidates", "--index-dir", str(index.root), "--data-dir", str(tmp_path / "data")]
    result = runner.invoke(app, [*args, "--method", method, "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "## Test split (the gate)" in result.output
    assert "Query mask: identifiers" in result.output
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["queries"]["CBRD-9"]["ranked"] == found
    assert document["method"] == method
    assert document["split"]["seed"] == 27978


def test_eval_candidates_refuses_an_index_without_vectors(tmp_path, embedder):
    Index(tmp_path / "index").apply([issue("CBRD-1", at(0), "x")], at(200), [], replace_all=True)
    write_jsonl(DataDir(tmp_path).gold, [])
    args = ["eval", "candidates", "--index-dir", str(tmp_path / "index"), "--data-dir", str(tmp_path)]
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "run `cubrid-jira-dup sync`" in result.output


def test_eval_candidates_without_an_index_says_to_sync(tmp_path):
    write_jsonl(DataDir(tmp_path).gold, [])
    result = runner.invoke(
        app, ["eval", "candidates", "--index-dir", str(tmp_path / "none"), "--data-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "cubrid-jira-dup sync" in result.output


def test_eval_candidates_rejects_an_unknown_method(tmp_path):
    result = runner.invoke(app, ["eval", "candidates", "--method", "nope", "--index-dir", str(tmp_path)])
    assert result.exit_code == 2


ISSUES = [
    issue("CBRD-1", at(0), "heap_update crash in vacuum", "ER_HEAP_UNKNOWN_OBJECT in heap_file.c", [
        comment("1", "heap_update again", at(5))
    ]),
    issue("CBRD-2", at(10), "loaddb is slow", "loaddb takes an hour"),
    issue("CBRD-3", at(20), "vacuum heap crash", "core dump in heap_update with -1114"),
    issue("CBRD-4", at(30), "csql help typo", "typo in csql --help"),
]
CANDIDATE_FIELDS = {"rank", "key", "url", "summary", "issuetype", "status", "resolution", "created", "signals"}
SIGNALS = {"lexical_rank", "semantic_rank", "title_rank", "stack_rank", "created_days_before"}
SIGNALS |= {"shared_identifiers", "shared_error_codes"}


@pytest.fixture
def index(tmp_path, embedder):
    return build_index(tmp_path / "index", ISSUES, embedder)


def find(index, *args, input=None):
    return runner.invoke(app, ["candidate-find", *args, "--index-dir", str(index.root)], input=input)


def test_a_draft_file_gives_one_json_document_on_stdout(index, tmp_path):
    draft = tmp_path / "draft.md"
    draft.write_text("intro line\n# Crash in heap_update during vacuum\n\nER_HEAP_UNKNOWN_OBJECT, -1114, HEAP_UPDATE\n")
    result = find(index, str(draft), "--no-sync", "--json", "-k", "3")
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["schema"] == "cubrid-jira-dup/candidates/1"
    query = document["query"]
    assert (query["source"], query["key"], query["title"]) == ("draft", None, "Crash in heap_update during vacuum")
    assert (query["time"], query["time_reference"]) == (document["started_at"], "now")
    assert (document["k"], document["complete"], document["errors"]) == (3, True, [])
    assert document["index"]["issues"] == 4
    assert set(document["timings_ms"]) == {"sync", "load", "embed", "search", "total"}
    assert document["method"]["name"] == "hybrid"
    assert document["method"]["config"]["weights"] == {"L": 2, "D": 1, "T": 1, "TD": 1, "S": 4}
    candidates = document["candidates"]
    assert [c["rank"] for c in candidates] == [1, 2, 3]
    assert {c["key"] for c in candidates[:2]} == {"CBRD-1", "CBRD-3"}
    assert all(set(c) == CANDIDATE_FIELDS and set(c["signals"]) == SIGNALS for c in candidates)
    first = next(c for c in candidates if c["key"] == "CBRD-1")
    assert first["url"] == "https://jira.cubrid.org/browse/CBRD-1"
    assert first["signals"]["shared_identifiers"] == ["heap_update", "ER_HEAP_UNKNOWN_OBJECT"]
    assert first["signals"]["shared_error_codes"] == ["ER_HEAP_UNKNOWN_OBJECT"]
    assert first["signals"]["stack_rank"] is None
    assert first["signals"]["created_days_before"] > 900


def test_a_title_with_the_body_on_stdin(index):
    result = find(index, "--title", "loaddb is slow", "--no-sync", "--json", input="it takes an hour")
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["query"]["source"] == "title"
    assert document["candidates"][0]["key"] == "CBRD-2"


def test_an_issue_key_in_any_case_searches_with_its_text_and_excludes_itself(index):
    result = find(index, "cbrd-3", "--no-sync", "--json")
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    query = document["query"]
    assert (query["source"], query["key"], query["title"]) == ("key", "CBRD-3", "vacuum heap crash")
    keys = [c["key"] for c in document["candidates"]]
    assert "CBRD-3" not in keys
    assert keys[0] == "CBRD-1"


def test_an_issue_key_counts_recency_from_when_it_was_filed_in_both_directions(tmp_path, embedder):
    day = 86400
    issues = [
        issue("CBRD-1", at(0), "replication lag"),
        issue("CBRD-2", at(730 * day), "replication lag"),
        issue("CBRD-3", at(-3 * day), "replication lag"),
    ]
    index = build_index(tmp_path / "index", issues, embedder)
    result = find(index, "CBRD-1", "--no-sync", "--json")
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert (document["query"]["time"], document["query"]["time_reference"]) == (at(0).isoformat(), "issue created")
    candidates = document["candidates"]
    assert [c["key"] for c in candidates] == ["CBRD-3", "CBRD-2"]
    assert [c["signals"]["created_days_before"] for c in candidates] == [3.0, -730.0]
    assert "730.0 days after" in find(index, "CBRD-1", "--no-sync").stdout


def test_text_output_lists_the_keys_in_rank_order(index):
    as_json = json.loads(find(index, "CBRD-3", "--no-sync", "--json").stdout)
    result = find(index, "CBRD-3", "--no-sync")
    assert result.exit_code == 0
    listed = [line.split()[1] for line in result.stdout.splitlines() if line[:1].isdigit()]
    assert listed == [c["key"] for c in as_json["candidates"]]
    assert "https://jira.cubrid.org/browse/CBRD-1" in result.stdout
    assert "signals: lexical" in result.stdout


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "exactly one input"),
        (["CBRD-1", "--title", "t"], "exactly one input"),
        (["--title", "   "], "title is empty"),
        (["CBRD-404"], "not in the index"),
        (["no-such-draft.md"], "neither an issue key"),
        (["CBRD-1", "-k", "0"], "-k"),
        (["CBRD-1", "-k", "51"], "-k"),
    ],
)
def test_bad_input_exits_2_with_a_message_and_no_search(index, embedder, args, message):
    result = find(index, *args, "--no-sync", "--json", input="")
    assert result.exit_code == 2
    assert message in result.stderr
    assert result.stdout == ""
    assert embedder.calls == []


def test_a_body_on_stdin_over_200000_characters_is_refused_before_any_search(index, embedder):
    result = find(index, "--title", "big", "--no-sync", "--json", input="a" * 200_001)
    assert result.exit_code == 2
    assert "200,001 characters" in result.stderr
    assert (result.stdout, embedder.calls) == ("", [])


def test_max_chars_bounds_a_draft_file(index, embedder, tmp_path):
    draft = tmp_path / "draft.md"
    draft.write_text("# loaddb is slow\n\nit takes an hour\n")
    size = len(draft.read_text())
    assert find(index, str(draft), "--no-sync", "--max-chars", str(size)).exit_code == 0
    embedder.calls.clear()
    refused = find(index, str(draft), "--no-sync", "--max-chars", str(size - 1))
    assert refused.exit_code == 2
    assert "--max-chars" in refused.stderr
    assert embedder.calls == []


@pytest.mark.parametrize("via", ["draft file", "stdin"])
def test_input_that_is_not_utf8_exits_2_before_any_search(index, embedder, tmp_path, via):
    broken = b"# loaddb is slow \xff\n\nit takes an hour\n"
    if via == "draft file":
        draft = tmp_path / "draft.md"
        draft.write_bytes(broken)
        result = find(index, str(draft), "--no-sync", "--json")
    else:
        result = find(index, "--title", "loaddb is slow", "--no-sync", "--json", input=broken)
    assert result.exit_code == 2
    assert "UTF-8" in result.stderr
    assert result.stdout == ""
    assert embedder.calls == []


def test_a_draft_without_a_title_line_is_refused(index, tmp_path):
    draft = tmp_path / "draft.md"
    draft.write_text("no heading here\n")
    result = find(index, str(draft), "--no-sync")
    assert result.exit_code == 2
    assert "no `# ` title line" in result.stderr


def test_no_index_and_no_sync_exits_2(tmp_path, embedder):
    result = runner.invoke(app, ["candidate-find", "CBRD-1", "--no-sync", "--index-dir", str(tmp_path / "none")])
    assert result.exit_code == 2
    assert "cubrid-jira-dup sync" in result.stderr


def use_jira(monkeypatch, handler):
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    def client(cache_dir):
        return JiraClient(cache_dir, refresh=True, min_interval=0, transport=httpx.MockTransport(record))

    monkeypatch.setattr(cli, "jira_client", client)
    return requests


def test_a_sync_runs_first_and_a_complete_search_exits_0(index, monkeypatch):
    page = {"total": 1, "issues": [raw_issue("CBRD-5", at(40), summary="loaddb hangs")]}
    requests = use_jira(monkeypatch, lambda request: httpx.Response(200, json=page))
    result = find(index, "--title", "loaddb", "--json", input="")
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["complete"]
    assert document["index"]["issues"] == 5
    assert "CBRD-5" in [c["key"] for c in document["candidates"]]
    assert "updated >=" in requests[0].url.params["jql"]


def test_a_failed_sync_searches_the_existing_index_and_exits_2(index, monkeypatch):
    use_jira(monkeypatch, lambda request: httpx.Response(403, json={}))
    result = find(index, "CBRD-3", "--json")
    assert result.exit_code == 2
    document = json.loads(result.stdout)
    assert not document["complete"]
    assert "sync failed" in document["errors"][0]
    assert document["candidates"][0]["key"] == "CBRD-1"
    assert document["index"]["last_sync_attempt"] is not None
    assert "sync failed" in result.stderr


def test_the_first_run_builds_the_index_and_embeds_it(tmp_path, embedder, monkeypatch):
    page = {"total": 2, "issues": [raw_issue("CBRD-1", at(0), summary="heap"), raw_issue("CBRD-2", at(1))]}
    use_jira(monkeypatch, lambda request: httpx.Response(200, json=page))
    result = runner.invoke(app, ["candidate-find", "--title", "heap", "--index-dir", str(tmp_path / "new")], input="")
    assert result.exit_code == 0, result.stderr
    assert "first run" in result.stderr
    assert "embedded 4/4" in result.stderr
    assert Index(tmp_path / "new").pending() == 0


MANY = [issue(f"CBRD-{n}", at(n), f"issue {n}") for n in range(10, 30)]


@pytest.mark.parametrize(
    ("issues", "args", "threads", "embedded"),
    [
        (ISSUES, [], [4], [9, 2]),
        (ISSUES + MANY, [], [4, 40], [49, 2]),
        (ISSUES + MANY, ["--threads", "12"], [4, 12], [49, 2]),
        (ISSUES + MANY, ["--threads", "2"], [2, 2], [49, 2]),
    ],
)
def test_many_new_texts_use_the_bulk_threads_and_the_query_at_most_four(
    tmp_path, monkeypatch, issues, args, threads, embedded
):
    monkeypatch.setattr(cli, "default_threads", lambda: 40)
    fake = FakeEmbedder()
    asked = []
    monkeypatch.setattr(cli, "FastEmbedder", lambda models, threads: asked.append(threads) or fake)
    index = Index(tmp_path / "index")
    index.apply(issues, at(200), [], replace_all=True)
    result = runner.invoke(app, ["candidate-find", "CBRD-3", "--no-sync", "--index-dir", str(index.root), *args])
    assert result.exit_code == 0, result.stderr
    assert [len(call) for call in fake.calls] == embedded
    assert asked == threads


def test_find_help_says_the_query_uses_at_most_four_threads():
    result = runner.invoke(app, ["candidate-find", "--help"])
    assert "at most 4" in " ".join(result.output.split())
