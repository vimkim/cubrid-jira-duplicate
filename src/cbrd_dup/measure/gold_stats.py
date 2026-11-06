"""Time gap and language mix of the gold pairs. Offline: reads <data-dir>/gold-pairs.jsonl.

Time gap: for pairs whose candidate was created before the duplicate,
duplicate.created - candidate.created; for pairs whose candidate came later,
candidate.created - duplicate.created.
Language mix: share of pairs where exactly one of the two summaries contains Hangul.
"""

from cbrd_dup.jira import parse_created
from cbrd_dup.models import PROVENANCES, GoldPair
from cbrd_dup.stats import nearest_rank, pct
from cbrd_dup.store import DataDir, read_jsonl

DAY = 86400.0


def fmt_days(seconds: float) -> str:
    days = seconds / DAY
    return f"{days * 24:.1f}h" if days < 1 else f"{days:.1f}d"


def gap_seconds(pair: GoldPair) -> float:
    """duplicate.created - candidate.created, negative when the candidate came later."""
    return (parse_created(pair.duplicate_created) - parse_created(pair.candidate_created)).total_seconds()


def gap_table(title: str, pairs: list[GoldPair], sign: int) -> None:
    print(f"\n## {title}")
    print("| subset | pairs | <1 day | <7 days | <30 days | p10 | p25 | median | p75 | p90 | max |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    subsets = [("all", pairs)] + [(p, [r for r in pairs if r.provenance == p]) for p in PROVENANCES]
    subsets.append(
        ("comment_mention, dup cue", [r for r in pairs if r.provenance == "comment_mention" and r.comment_dup_cue])
    )
    subsets.append(("duplicate created in 2026", [r for r in pairs if r.duplicate_created.startswith("2026")]))
    for name, sub in subsets:
        gaps = sorted(sign * gap_seconds(r) for r in sub)
        if not gaps:
            continue
        shares = [pct(sum(1 for g in gaps if g < limit * DAY), len(gaps)) for limit in (1, 7, 30)]
        quantiles = [fmt_days(nearest_rank(gaps, q)) for q in (0.1, 0.25, 0.5, 0.75, 0.9)]
        print(f"| {name} | {len(gaps)} | " + " | ".join(shares + quantiles + [fmt_days(gaps[-1])]) + " |")


def run(data: DataDir) -> None:
    pairs = read_jsonl(data.gold, GoldPair)
    visible = [p for p in pairs if p.candidate_visible]
    print(f"# Gold pair statistics ({len(pairs)} pairs, {len(visible)} with a visible candidate)")
    print("Percentiles use the nearest-rank method.")

    gap_table(
        "Candidate created before the duplicate: duplicate.created - candidate.created",
        [p for p in visible if p.candidate_before_duplicate],
        1,
    )
    gap_table(
        "Candidate created after the duplicate: candidate.created - duplicate.created",
        [p for p in visible if not p.candidate_before_duplicate],
        -1,
    )

    # Issue level: the nearest earlier candidate is the one the index must already hold.
    nearest: dict[str, float] = {}
    for p in visible:
        if p.candidate_before_duplicate:
            gap = gap_seconds(p)
            nearest[p.duplicate] = min(gap, nearest.get(p.duplicate, gap))
    gaps = sorted(nearest.values())
    print(
        f"\n- issue level, nearest earlier candidate ({len(gaps)} duplicates): "
        + ", ".join(f"<{d}d {pct(sum(1 for g in gaps if g < d * DAY), len(gaps))}" for d in (1, 7, 30))
        + f", median {fmt_days(nearest_rank(gaps, 0.5))}"
    )

    print("\n## Summary language")
    print("| subset | pairs | exactly one Hangul | both Hangul | neither |")
    print("|---|---|---|---|---|")
    subsets = [
        ("all", visible),
        ("candidate earlier", [p for p in visible if p.candidate_before_duplicate]),
        ("candidate later", [p for p in visible if not p.candidate_before_duplicate]),
    ]
    subsets += [(p, [r for r in visible if r.provenance == p]) for p in PROVENANCES]
    subsets += [
        ("duplicate created in 2026", [r for r in visible if r.duplicate_created.startswith("2026")]),
        ("duplicate created before 2026", [r for r in visible if not r.duplicate_created.startswith("2026")]),
    ]
    for name, sub in subsets:
        a = [(r.duplicate_summary_hangul, r.candidate_summary_hangul) for r in sub]
        print(
            f"| {name} | {len(sub)} | {pct(sum(1 for x, y in a if x != y), len(a))} | "
            f"{pct(sum(1 for x, y in a if x and y), len(a))} | {pct(sum(1 for x, y in a if not x and not y), len(a))} |"
        )
