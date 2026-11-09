import pytest
from factories import FakeEmbedder, at, comment, issue, pair

from cbrd_dup import evaluate
from cbrd_dup.evaluate import Query, Run
from cbrd_dup.index import Index, Snapshot
from cbrd_dup.models import BaselineResult
from cbrd_dup.search import METHODS, Searcher

QUERY_TIME = at(1000)
QUERY_KEY = "CBRD-100"
EARLY = [comment("1", "zebra", at(100))]
LATE = [comment("2", "giraffe", at(2000)), comment("3", "walrus", at(200), at(3000))]


def corpus_issues(late_comments=True):
    return [
        issue("CBRD-1", at(0), "storage", "", EARLY + (LATE if late_comments else [])),
        issue("CBRD-5", at(10), "unrelated apples"),
        issue(QUERY_KEY, QUERY_TIME, "the query issue", "zebra giraffe walrus"),
        issue("CBRD-101", at(1001), "later", "zebra giraffe walrus"),
        issue("CBRD-102", QUERY_TIME, "same second", "zebra giraffe walrus"),
    ]


@pytest.fixture
def corpus():
    issues = corpus_issues()
    return issues, {i.key: i for i in issues}


def searcher(path, method, issues=None):
    index = Index(path)
    index.apply(issues or corpus_issues(), at(5000), [], replace_all=True)
    embedder = FakeEmbedder()
    index.embed_pending(embedder)
    return Searcher(index, METHODS[method], embedder if METHODS[method].dense else None)


def search_for(tmp_path, words, before=QUERY_TIME, method="bm25"):
    issues = corpus_issues()
    query = Query(words, "", before, QUERY_KEY)
    run = evaluate.evaluate(searcher(tmp_path, method), {QUERY_KEY: query}, {i.key: i for i in issues})
    return run.ranked[QUERY_KEY]


def test_words_in_an_early_comment_find_the_candidate(tmp_path):
    assert search_for(tmp_path, "zebra") == ["CBRD-1"]


@pytest.mark.parametrize("words", ["giraffe", "walrus", "giraffe walrus"])
def test_words_only_in_a_late_or_late_edited_comment_do_not(tmp_path, words):
    assert search_for(tmp_path, words) == []


def test_the_cutoff_is_what_hides_them(tmp_path):
    found = search_for(tmp_path, "walrus", before=None)
    assert "CBRD-1" in found
    assert QUERY_KEY not in found


@pytest.mark.parametrize("method", ["bm25", "hybrid"])
def test_later_and_same_second_issues_are_never_returned(tmp_path, method):
    found = search_for(tmp_path, "zebra giraffe walrus the query issue later same second", method=method)
    assert not {"CBRD-101", "CBRD-102", QUERY_KEY} & set(found)


@pytest.mark.parametrize("words", ["giraffe", "walrus", "zebra giraffe walrus storage"])
def test_hybrid_ranks_as_if_late_comments_did_not_exist(tmp_path, words):
    query = Query(words, "giraffe walrus", QUERY_TIME, QUERY_KEY)
    with_late = searcher(tmp_path / "a", "hybrid").rank(query, 20)
    without = searcher(tmp_path / "b", "hybrid", corpus_issues(late_comments=False)).rank(query, 20)
    assert with_late.keys == without.keys
    assert with_late.channel_ranks == without.channel_ranks
    assert set(with_late.keys) <= {"CBRD-1", "CBRD-5"}


STACK = "crash\n#0 0x7f in raise () from libc\n#1 0x0 in lock_get_new_entry (a=1) at lock.c:10\n#2 lock_object ()"


def test_the_stack_channel_matches_the_top_frames_and_only_runs_for_a_stack(tmp_path):
    issues = corpus_issues() + [issue("CBRD-7", at(20), "hang", "seen in lock_get_new_entry and lock_object")]
    found = searcher(tmp_path, "hybrid", issues)
    assert found.rank(Query("core dump", STACK, QUERY_TIME, QUERY_KEY), 20).channel_ranks["S"] == {"CBRD-7": 1}
    assert "S" not in found.rank(Query("core dump", "no stack here", QUERY_TIME, QUERY_KEY), 20).channel_ranks


@pytest.mark.parametrize(("words", "first"), [("alpha beta beta", "CBRD-2"), ("alpha alpha beta", "CBRD-1")])
def test_a_repeated_query_word_weighs_more(tmp_path, words, first):
    filler = [issue(f"CBRD-{n}", at(n), "filler", "other words") for n in range(3, 9)]
    issues = [issue("CBRD-1", at(0), "alpha filler"), issue("CBRD-2", at(1), "beta filler"), *filler]
    ranked = searcher(tmp_path, "bm25", issues).search(Query(words, "", QUERY_TIME, None), 2)
    assert ranked[0] == first


def test_recency_puts_the_newer_of_two_equal_issues_first(tmp_path):
    issues = [issue("CBRD-1", at(-100 * 86400), "same words"), issue("CBRD-2", at(500), "same words")]
    ranked = searcher(tmp_path, "hybrid", issues).rank(Query("same words", "", QUERY_TIME, None), 20)
    assert ranked.keys == ["CBRD-2", "CBRD-1"]


class Fixed:
    def __init__(self, keys):
        self.keys = keys

    def search(self, query, k):
        return self.keys


@pytest.mark.parametrize(
    ("keys", "message"),
    [
        ([QUERY_KEY], "query issue itself"),
        (["CBRD-101"], "created at or after"),
        (["CBRD-102"], "created at or after"),
        (["CBRD-404"], "not in the corpus"),
        (["CBRD-1", "CBRD-1"], "twice"),
    ],
)
def test_the_harness_raises_when_a_searcher_breaks_the_cutoff(corpus, keys, message):
    _, by_key = corpus
    queries = {QUERY_KEY: Query("x", "", QUERY_TIME, QUERY_KEY)}
    with pytest.raises(ValueError, match=message):
        evaluate.evaluate(Fixed(keys), queries, by_key)


def test_the_harness_accepts_earlier_issues_and_times_each_query(corpus):
    _, by_key = corpus
    queries = {QUERY_KEY: Query("x", "", QUERY_TIME, QUERY_KEY)}
    run = evaluate.evaluate(Fixed(["CBRD-5", "CBRD-1"]), queries, by_key)
    assert run.ranked == {QUERY_KEY: ["CBRD-5", "CBRD-1"]}
    assert run.latencies_ms[QUERY_KEY] >= 0


def test_eval_queries_use_only_the_masked_summary_and_description_of_earlier_pairs(corpus):
    _, by_key = corpus
    dup = issue(
        "CBRD-200",
        at(500),
        "Same as CBRD-5",
        "see https://jira.cubrid.org/browse/CBRD-1",
        [comment("9", "secret comment", at(600))],
    )
    by_key = by_key | {dup.key: dup, "CBRD-300": issue("CBRD-300", at(700))}
    pairs = [pair("CBRD-200", "CBRD-5"), pair("CBRD-300", "CBRD-1", candidate_before_duplicate=False)]
    queries = evaluate.eval_queries(pairs, by_key, evaluate.MASKS["keys"])
    assert list(queries) == ["CBRD-200"]
    query = queries["CBRD-200"]
    assert "CBRD-" not in query.title + query.body
    assert "secret" not in query.title + query.body
    assert (query.before, query.exclude) == (at(500), "CBRD-200")


def test_eval_queries_mask_identifiers_named_after_an_issue_by_default(corpus):
    _, by_key = corpus
    dup = issue("CBRD-200", at(500), "loaddb fails on cbrd_27516", "run on db25833 and CBRD27516, see CBRD-5")
    pairs = [pair("CBRD-200", "CBRD-5")]
    strict = evaluate.eval_queries(pairs, by_key | {dup.key: dup})["CBRD-200"]
    assert "27516" not in strict.title + strict.body
    assert "25833" not in strict.body
    assert strict.title.startswith("loaddb fails on")
    keys_only = evaluate.eval_queries(pairs, by_key | {dup.key: dup}, evaluate.MASKS["keys"])["CBRD-200"]
    assert "cbrd_27516" in keys_only.title


def synthetic_pairs():
    pairs = [pair(f"CBRD-{100 + i}", f"CBRD-{i + 1}", "2026-02-01T10:00:00.000+0900") for i in range(5)]
    pairs += [pair(f"CBRD-{200 + i}", f"CBRD-{50 + i}") for i in range(7)]
    pairs += [pair("CBRD-300", "CBRD-60"), pair("CBRD-301", "CBRD-60")]
    pairs += [pair("CBRD-402", "CBRD-401"), pair("CBRD-401", "CBRD-400")]
    pairs += [pair("CBRD-500", "CBRD-70", candidate_before_duplicate=False)]
    return pairs


def test_split_keeps_linked_queries_together_and_balances_2026():
    sides = evaluate.split(synthetic_pairs())
    assert "CBRD-500" not in sides
    assert sides["CBRD-300"] == sides["CBRD-301"]
    assert sides["CBRD-401"] == sides["CBRD-402"]
    in_2026 = [sides[f"CBRD-{100 + i}"] for i in range(5)]
    assert abs(in_2026.count("dev") - in_2026.count("test")) <= 1
    assert set(sides.values()) == {"dev", "test"}


def test_split_is_deterministic_and_ignores_input_order():
    pairs = synthetic_pairs()
    assert evaluate.split(pairs) == evaluate.split(pairs)
    assert evaluate.split(pairs) == evaluate.split(list(reversed(pairs)))


def small_run(ranks):
    ranked = {dup: [f"X-{n}" for n in range(1, 21)] for dup in ranks}
    for dup, rank in ranks.items():
        if rank is not None:
            ranked[dup][rank - 1] = f"CBRD-{dup[-1]}"
    return Run("bm25", ranked, {dup: 1.0 + i for i, dup in enumerate(ranks)})


SNAPSHOT = Snapshot(issues=7, synced_at=at(0), complete=True, sha256="ab" * 32)


def test_recall_counts_rank_ten_but_not_eleven():
    pairs = [pair("CBRD-100", "CBRD-1"), pair("CBRD-101", "CBRD-1")]
    sides = {"CBRD-100": "test", "CBRD-101": "test"}
    run = Run("bm25", {"CBRD-100": ["a"] * 9 + ["CBRD-1"], "CBRD-101": ["a"] * 10 + ["CBRD-1"]}, {})
    rows = {(m.subset, m.method): m for m in evaluate.metric_rows(run, pairs, sides, [])["test"]}
    row = rows["all", "bm25"]
    assert (row.pairs, row.found10, row.found20) == (2, 1, 2)
    assert row.wilson10 is not None
    assert row.bootstrap10 is not None
    assert rows["all", "bm25"] == evaluate.metric_rows(run, pairs, sides, [])["test"][0]


def baseline(variant, dup, rank):
    return BaselineResult(
        variant=variant,
        duplicate=dup,
        candidate="CBRD-1",
        provenance="duplicate_link",
        comment_dup_cue=None,
        duplicate_created="2025-05-01T10:00:00.000+0900",
        cross_language=False,
        same_minute=False,
        jql="",
        http_status=200,
        error=None,
        total_hits=None,
        rank=rank,
        in_top10=rank is not None and rank <= 10,
    )


def test_report_shows_splits_baselines_gate_and_snapshot():
    pairs = [
        pair("CBRD-100", "CBRD-1", "2026-02-01T10:00:00.000+0900"),
        pair("CBRD-101", "CBRD-1", "2026-02-01T10:00:00.000+0900"),
        pair("CBRD-102", "CBRD-1"),
        pair("CBRD-103", "CBRD-1"),
    ]
    sides = {"CBRD-100": "test", "CBRD-101": "dev", "CBRD-102": "test", "CBRD-103": "dev"}
    run = Run(
        "bm25",
        {f"CBRD-10{i}": ["CBRD-1"] if i != 2 else ["x"] for i in range(4)},
        {f"CBRD-10{i}": float(i + 1) for i in range(4)},
    )
    baselines = [baseline("and", "CBRD-102", None), baseline("or", "CBRD-102", 4)]
    text = evaluate.report(run, pairs, sides, baselines, SNAPSHOT)
    assert "Snapshot: 7 issues" in text
    assert "ab" * 32 in text
    assert "| test | 2 | 2 | 1 | 1 |" in text
    test_section = text.split("## Dev split")[0]
    assert "| all | bm25 | 2 | 1/2 (50.0%)" in test_section
    assert "| all | JQL OR (summary only) | 2 | 1/2 (50.0%)" in test_section
    assert "| all | JQL AND (summary only) | 2 | 0/2 (0.0%)" in test_section
    assert "| all | bm25 | 2 | 2/2 (100.0%)" in text.split("## Dev split")[1]
    assert "Gate (test split, recall@10 >= 0.8): all 0.500 FAIL; query created 2026 1.000 PASS" in text
    assert "median 2.5, p95 4.0, max 4.0" in text


def test_the_query_bootstrap_is_reproducible_and_tight_when_every_query_hits():
    assert evaluate.bootstrap_interval([(2, 2), (1, 1), (3, 3)]) == (1.0, 1.0)
    mixed = [(1, 2), (0, 1), (3, 3), (0, 2)]
    assert evaluate.bootstrap_interval(mixed) == evaluate.bootstrap_interval(mixed)
