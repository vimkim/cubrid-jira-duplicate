import json

import pytest
from factories import at, comment, issue, pair
from typer.testing import CliRunner

from cbrd_dup import corpus
from cbrd_dup.cli import app
from cbrd_dup.corpus import IndexDir, SyncState
from cbrd_dup.jira import cache_key, search_url
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


@pytest.mark.parametrize("args", [["sync"], ["eval", "candidates"]])
def test_sync_and_eval_candidates_document_the_index_dir(args):
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0
    assert "--index-dir" in result.output


def test_eval_candidates_runs_offline_from_an_index_and_writes_json(tmp_path):
    index = IndexDir(tmp_path / "index")
    issues = [
        issue("CBRD-1", at(0), "heap vacuum crash", "", [comment("1", "zebra", at(5))]),
        issue("CBRD-9", at(100), "heap vacuum crash again", "see CBRD-1"),
    ]
    corpus.write_issues(index, issues)
    corpus.write_state(index, SyncState(synced_at=at(200), complete=True, issues=2, errors=[]))
    write_jsonl(DataDir(tmp_path / "data").gold, [pair("CBRD-9", "CBRD-1", "2024-03-05T14:24:51.000+0900")])
    out = tmp_path / "eval.json"
    args = ["eval", "candidates", "--index-dir", str(index.root), "--data-dir", str(tmp_path / "data")]
    result = runner.invoke(app, [*args, "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "## Test split (the gate)" in result.output
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["queries"]["CBRD-9"]["ranked"] == ["CBRD-1"]
    assert document["method"] == "bm25"
    assert document["split"]["seed"] == 27978


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
