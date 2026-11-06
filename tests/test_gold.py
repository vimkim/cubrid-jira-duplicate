from pathlib import Path

import pytest

from cbrd_dup.measure import gold
from cbrd_dup.models import GoldPair
from cbrd_dup.store import DataDir, read_jsonl


def test_collect_pointers_merges_links_and_comments(gold_issues):
    pointers, missing = gold.collect_pointers(gold_issues["duplicates"][0])
    assert missing == 1
    assert sorted(pointers) == ["CBRD-90", "CBRD-95"]
    strongest = pointers["CBRD-90"]
    assert strongest.provenances == {"duplicate_link", "relates_link", "comment_mention"}
    assert [(link.type, link.direction, link.label) for link in strongest.links] == [
        ("Duplicate", "outward", "duplicates"),
        ("Relates", "outward", "relates to"),
    ]
    assert (strongest.comments, strongest.cue_comments) == (1, 1)
    assert pointers["CBRD-95"].provenances == {"comment_mention"}
    assert (pointers["CBRD-95"].comments, pointers["CBRD-95"].cue_comments) == (1, 0)


def test_collect_pointers_reads_inward_links_and_unlinked_issues(gold_issues):
    linked, unlinked = gold_issues["duplicates"][1:]
    pointers, missing = gold.collect_pointers(linked)
    assert missing == 0
    assert pointers["CBRD-150"].links[0].direction == "inward"
    assert pointers["CBRD-150"].links[0].label == "is duplicated by"
    assert gold.collect_pointers(unlinked) == ({}, 0)


def test_build_pairs_orders_and_ranks_provenance(gold_pairs):
    assert [(p.duplicate, p.candidate) for p in gold_pairs] == [
        ("CBRD-100", "CBRD-90"),
        ("CBRD-100", "CBRD-95"),
        ("CBRD-200", "CBRD-150"),
        ("CBRD-200", "CBRD-9999"),
    ]
    first, second, _, _ = gold_pairs
    assert first.provenance == "duplicate_link"
    assert first.provenances == ["duplicate_link", "relates_link", "comment_mention"]
    assert second.provenance == "comment_mention"


def test_build_pairs_fills_candidate_fields(gold_pairs):
    first, second, third, fourth = gold_pairs
    assert first.candidate_before_duplicate is True
    assert first.candidate_resolution == "Fixed"
    assert first.duplicate_summary_hangul is True
    assert first.candidate_summary_hangul is False
    assert first.comment_dup_cue is True
    assert second.candidate_before_duplicate is False
    assert second.candidate_summary_hangul is True
    assert second.comment_dup_cue is False
    assert third.comment_dup_cue is None
    assert third.candidate_before_duplicate is False
    assert third.candidate_resolution == "Duplicate"
    assert fourth.candidate_visible is False
    assert fourth.candidate_created is None
    assert fourth.candidate_before_duplicate is None
    assert fourth.candidate_summary is None
    assert fourth.comment_dup_cue is True


class FakeClient:
    """Answers the two kinds of query `gold.run` makes, from the fixture."""

    network = 0
    cache = 0

    def __init__(self, gold_issues):
        self.dups = gold_issues["duplicates"]
        self.candidates = gold_issues["candidates"]

    def search_all(self, jql, fields, page=1000, **params):
        if jql == gold.DUP_JQL:
            return self.dups
        assert jql == "key in (CBRD-90, CBRD-95, CBRD-150, CBRD-9999) ORDER BY key ASC"
        assert params == {"validateQuery": "false"}
        return self.candidates


def test_run_writes_jsonl_and_summary(gold_issues, gold_pairs, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = DataDir(Path("data"))
    gold.run(FakeClient(gold_issues), data)
    out = capsys.readouterr().out
    assert out.startswith("# Gold pairs written to data/gold-pairs.jsonl\n")
    assert "- duplicate issues (JQL `project = CBRD AND resolution = Duplicate ORDER BY key ASC`): 3" in out
    assert "- pairs: 4; candidate keys: 4; visible anonymously: 3" in out
    assert "- comments missing from search responses (total - returned): 1" in out

    lines = data.gold.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    # The file keeps the format the old scripts wrote: default separators, Hangul unescaped.
    assert lines[0].startswith('{"duplicate": "CBRD-100", "candidate": "CBRD-90", "duplicate_created": ')
    assert '"duplicate_summary": "Vacuum 오류 on partition"' in lines[0]
    assert read_jsonl(data.gold, GoldPair) == gold_pairs


def test_gold_pair_rejects_unknown_fields(gold_pairs):
    row = gold_pairs[0].model_dump()
    GoldPair.model_validate(row)
    with pytest.raises(ValueError, match="extra_forbidden"):
        GoldPair.model_validate({**row, "surprise": 1})
