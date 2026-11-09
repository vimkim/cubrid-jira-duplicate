
import random
import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal, Protocol

import numpy as np

from cbrd_dup.corpus import Issue
from cbrd_dup.index import Snapshot
from cbrd_dup.jira import key_number, parse_created
from cbrd_dup.models import BaselineResult, GoldPair
from cbrd_dup.stats import nearest_rank, pct, wilson_interval
from cbrd_dup.text import mask_key_identifiers, mask_keys

Side = Literal["dev", "test"]
SEED = 27978
K = 20
RESAMPLES = 2000
GATE = 0.8
Ranks = dict[tuple[str, str], int | None]
Interval = tuple[float, float]
MASKS: dict[str, Callable[[str], str]] = {"identifiers": mask_key_identifiers, "keys": mask_keys}


@dataclass(frozen=True)
class Query:
    title: str
    body: str
    before: datetime | None = None
    exclude: str | None = None


class Searcher(Protocol):
    def search(self, query: Query, k: int) -> list[str]: ...


@dataclass(frozen=True)
class Run:
    name: str
    ranked: dict[str, list[str]]
    latencies_ms: dict[str, float]


@dataclass(frozen=True)
class Metric:
    split: str
    subset: str
    method: str
    pairs: int
    found10: int
    found20: int
    wilson10: Interval | None
    wilson20: Interval | None
    bootstrap10: Interval | None


def is_2026(pair: GoldPair) -> bool:
    return parse_created(pair.duplicate_created).year == 2026


def eval_queries(
    pairs: list[GoldPair], issues: Mapping[str, Issue], mask: Callable[[str], str] = mask_key_identifiers
) -> dict[str, Query]:
    keys = sorted({p.duplicate for p in pairs if p.candidate_before_duplicate}, key=key_number)
    return {
        key: Query(mask(issues[key].summary), mask(issues[key].description), issues[key].created, key)
        for key in keys
    }


def split(pairs: list[GoldPair], seed: int = SEED) -> dict[str, Side]:
    earlier = [p for p in pairs if p.candidate_before_duplicate]
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        while parent.setdefault(node, node) != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for p in earlier:
        parent[find(p.duplicate)] = find(p.candidate)
    members: dict[str, set[str]] = defaultdict(set)
    for node in list(parent):
        members[find(node)].add(node)
    queries = {p.duplicate for p in earlier}
    queries_2026 = {p.duplicate for p in earlier if is_2026(p)}

    components = sorted(members.values(), key=lambda m: min(key_number(k) for k in m))
    random.Random(seed).shuffle(components)
    components.sort(key=lambda m: not m & queries_2026)

    sides: dict[str, Side] = {}
    count = {"dev": [0, 0], "test": [0, 0]}
    for component in components:
        n_2026, n_all = len(component & queries_2026), len(component & queries)
        if n_2026:
            side: Side = min(("dev", "test"), key=lambda s: tuple(count[s]))
        else:
            side = min(("dev", "test"), key=lambda s: count[s][1])
        count[side][0] += n_2026
        count[side][1] += n_all
        sides.update((key, side) for key in component & queries)
    return sides


def evaluate(
    searcher: Searcher, queries: Mapping[str, Query], issues: Mapping[str, Issue], k: int = K, name: str = "search"
) -> Run:
    ranked: dict[str, list[str]] = {}
    latencies: dict[str, float] = {}
    for key, query in queries.items():
        start = time.perf_counter()
        keys = searcher.search(query, k)
        latencies[key] = (time.perf_counter() - start) * 1000
        check_cutoff(key, query, keys, issues)
        ranked[key] = keys
    return Run(name, ranked, latencies)


def check_cutoff(key: str, query: Query, returned: list[str], issues: Mapping[str, Issue]) -> None:
    seen: set[str] = set()
    for found in returned:
        if found in (key, query.exclude):
            raise ValueError(f"{key}: the search returned the query issue itself")
        if found not in issues:
            raise ValueError(f"{key}: the search returned {found}, which is not in the corpus")
        if found in seen:
            raise ValueError(f"{key}: the search returned {found} twice")
        seen.add(found)
        if query.before is not None and issues[found].created >= query.before:
            raise ValueError(f"{key}: the search returned {found}, created at or after the query")


SUBSETS: dict[str, Callable[[GoldPair], bool]] = {
    "all": lambda p: True,
    "duplicate_link": lambda p: p.provenance == "duplicate_link",
    "relates_link": lambda p: p.provenance == "relates_link",
    "comment_mention": lambda p: p.provenance == "comment_mention",
    "query created 2026": is_2026,
    "query created before 2026": lambda p: not is_2026(p),
    "exactly one summary has Hangul": lambda p: p.duplicate_summary_hangul != p.candidate_summary_hangul,
    "same script": lambda p: p.duplicate_summary_hangul == p.candidate_summary_hangul,
}
JQL_LABEL = {"and": "JQL AND (summary only)", "or": "JQL OR (summary only)"}


def run_ranks(run: Run, pairs: list[GoldPair]) -> Ranks:
    ranks: Ranks = {}
    for p in pairs:
        keys = run.ranked.get(p.duplicate, [])
        ranks[p.duplicate, p.candidate] = keys.index(p.candidate) + 1 if p.candidate in keys else None
    return ranks


def baseline_ranks(results: list[BaselineResult], variant: str) -> Ranks:
    return {(r.duplicate, r.candidate): r.rank for r in results if r.variant == variant}


def hit(rank: int | None, k: int) -> bool:
    return rank is not None and rank <= k


def bootstrap_interval(per_query: list[tuple[int, int]]) -> Interval:
    found = np.array([f for f, _ in per_query])
    total = np.array([n for _, n in per_query])
    picks = np.random.default_rng(SEED).integers(0, len(per_query), size=(RESAMPLES, len(per_query)))
    recall = found[picks].sum(axis=1) / total[picks].sum(axis=1)
    low, high = np.percentile(recall, [2.5, 97.5])
    return float(low), float(high)


def measure(split_name: str, subset: str, method: str, pairs: list[GoldPair], ranks: Ranks) -> Metric:
    found10 = sum(hit(ranks.get((p.duplicate, p.candidate)), 10) for p in pairs)
    found20 = sum(hit(ranks.get((p.duplicate, p.candidate)), 20) for p in pairs)
    if not pairs:
        return Metric(split_name, subset, method, 0, 0, 0, None, None, None)
    per_query: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for p in pairs:
        per_query[p.duplicate][0] += hit(ranks.get((p.duplicate, p.candidate)), 10)
        per_query[p.duplicate][1] += 1
    return Metric(
        split_name,
        subset,
        method,
        len(pairs),
        found10,
        found20,
        wilson_interval(found10, len(pairs)),
        wilson_interval(found20, len(pairs)),
        bootstrap_interval([(f, n) for f, n in per_query.values()]),
    )


def metric_rows(
    run: Run, pairs: list[GoldPair], sides: Mapping[str, Side], baseline_results: list[BaselineResult]
) -> dict[str, list[Metric]]:
    earlier = [p for p in pairs if p.candidate_before_duplicate]
    methods = [(run.name, run_ranks(run, earlier))]
    methods += [
        (JQL_LABEL[variant], baseline_ranks(baseline_results, variant))
        for variant in ("and", "or")
        if any(r.variant == variant for r in baseline_results)
    ]
    splits = {
        "test": [p for p in earlier if sides.get(p.duplicate) == "test"],
        "dev": [p for p in earlier if sides.get(p.duplicate) == "dev"],
        "all": earlier,
    }
    return {
        name: [
            measure(name, subset, method, [p for p in members if keep(p)], ranks)
            for subset, keep in SUBSETS.items()
            for method, ranks in methods
        ]
        for name, members in splits.items()
    }


def split_counts(pairs: list[GoldPair], sides: Mapping[str, Side]) -> dict[str, dict[str, int]]:
    earlier = [p for p in pairs if p.candidate_before_duplicate]
    counts = {}
    for side in ("dev", "test"):
        mine = [p for p in earlier if sides.get(p.duplicate) == side]
        counts[side] = {
            "queries": len({p.duplicate for p in mine}),
            "pairs": len(mine),
            "queries_2026": len({p.duplicate for p in mine if is_2026(p)}),
            "pairs_2026": sum(is_2026(p) for p in mine),
        }
    return counts


def span(interval: Interval | None) -> str:
    return "-" if interval is None else f"{100 * interval[0]:.1f}-{100 * interval[1]:.1f}%"


def table(metrics: list[Metric]) -> list[str]:
    lines = [
        "| subset | method | pairs | recall@10 | Wilson 95% | recall@20 | Wilson 95% | query bootstrap 95% @10 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in metrics:
        lines.append(
            f"| {m.subset} | {m.method} | {m.pairs} | {pct(m.found10, m.pairs)} | {span(m.wilson10)} |"
            f" {pct(m.found20, m.pairs)} | {span(m.wilson20)} | {span(m.bootstrap10)} |"
        )
    return lines


def gate_line(test: list[Metric], method: str) -> str:
    def verdict(subset: str) -> str:
        m = next(m for m in test if m.subset == subset and m.method == method)
        recall = m.found10 / m.pairs if m.pairs else 0.0
        return f"{subset} {recall:.3f} {'PASS' if recall >= GATE else 'FAIL'}"

    return f"Gate (test split, recall@10 >= {GATE}): {verdict('all')}; {verdict('query created 2026')}"


def report(
    run: Run,
    pairs: list[GoldPair],
    sides: Mapping[str, Side],
    baseline_results: list[BaselineResult],
    snapshot: Snapshot,
    mask: str = "identifiers",
) -> str:
    tables = metric_rows(run, pairs, sides, baseline_results)
    counts = split_counts(pairs, sides)
    latencies = sorted(run.latencies_ms.values())
    out = [
        f"# Candidate recall: {run.name}",
        "",
        f"Snapshot: {snapshot.issues} issues, synced_at {snapshot.synced_at.isoformat()},"
        f" complete {snapshot.complete}, issue data sha256 {snapshot.sha256}",
        "",
        f"Pairs are the gold pairs whose candidate was created before the duplicate. Split seed {SEED}.",
        f"Query mask: {mask} (requirements 7.2).",
        "Pairs of one query issue are not independent, so read the query bootstrap interval first.",
        "",
        "## Split",
        "",
        "| side | queries | pairs | 2026 queries | 2026 pairs |",
        "|---|---|---|---|---|",
        *(
            f"| {side} | {c['queries']} | {c['pairs']} | {c['queries_2026']} | {c['pairs_2026']} |"
            for side, c in counts.items()
        ),
    ]
    for name, heading in (("test", "Test split (the gate)"), ("dev", "Dev split"), ("all", "All pairs")):
        out += ["", f"## {heading}", "", *table(tables[name])]
        if name == "test":
            out += ["", gate_line(tables["test"], run.name)]
    out += [
        "",
        "JQL rows are summary-only searches made through JIRA without the 7.2 rules, recorded to rank 50.",
        "",
        "## Latency per query (ms)",
        "",
        f"median {statistics.median(latencies):.1f}, p95 {nearest_rank(latencies, 0.95):.1f}, max {latencies[-1]:.1f}",
    ]
    return "\n".join(out) + "\n"


def document(
    run: Run,
    pairs: list[GoldPair],
    sides: Mapping[str, Side],
    baseline_results: list[BaselineResult],
    snapshot: Snapshot,
    config: dict,
) -> dict:
    return {
        "method": run.name,
        "config": config,
        "snapshot": snapshot.model_dump(mode="json"),
        "split": {"seed": SEED, "sides": dict(sides), "counts": split_counts(pairs, sides)},
        "k": K,
        "queries": {key: {"ranked": keys, "latency_ms": run.latencies_ms[key]} for key, keys in run.ranked.items()},
        "tables": {
            name: [asdict(m) for m in metrics]
            for name, metrics in metric_rows(run, pairs, sides, baseline_results).items()
        },
    }
