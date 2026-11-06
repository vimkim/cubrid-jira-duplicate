#!/usr/bin/env python3
"""Measure the search a reporter can run in JIRA today against the gold pairs.

For each duplicate issue that has at least one gold candidate created earlier than
itself, run one anonymous search built from the duplicate's summary:

    project = CBRD AND text ~ "<summary words>" AND created < "<duplicate created, minute>"

The JQL has no ORDER BY, so JIRA ranks by relevance (see scripts/probe_text_order.py).
Then record the rank of each earlier candidate. Two query variants run:

  and  the summary words as typed. JIRA 7.7 joins them with AND (the brief's baseline).
  or   the same words joined with OR. Supplementary: what JIRA's own ranking can do
       when no single field has to contain every word.

Reads data/gold-pairs.jsonl (run scripts/build_gold.py first). Writes
data/baseline-results.jsonl and prints recall tables.

Run from the worktree root:  python3 -I scripts/measure_baseline.py [--refresh]
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cbrd_rest import ROOT, parse_created, search, search_all, stats  # noqa: E402

GOLD = ROOT / "data" / "gold-pairs.jsonl"
OUT = ROOT / "data" / "baseline-results.jsonl"
FETCH = 50
# Lucene query-syntax characters, plus the JQL string delimiters " and \.
SPECIAL = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')
OPERATORS = {"AND", "OR", "NOT", "TO"}
PROVENANCES = ("duplicate_link", "relates_link", "comment_mention")
CUTOFFS = (1, 3, 5, 10, 50)


def summary_words(summary):
    words = SPECIAL.sub(" ", summary).split()
    # Upper-case AND/OR/NOT are Lucene operators; the index is case-insensitive.
    return [w.lower() if w in OPERATORS else w for w in words]


def build_jql(words, variant, created):
    text = (" OR " if variant == "or" else " ").join(words)
    minute = parse_created(created).strftime("%Y-%m-%d %H:%M")
    return f'project = CBRD AND text ~ "{text}" AND created < "{minute}"'


def pct(n, d):
    return f"{n}/{d} ({100 * n / d:.1f}%)" if d else "0/0"


def wilson(n, d, z=1.96):
    """95% Wilson score interval for n successes out of d, as percentages."""
    if not d:
        return "-"
    p = n / d
    centre = (p + z * z / (2 * d)) / (1 + z * z / d)
    half = z * ((p * (1 - p) / d + z * z / (4 * d * d)) ** 0.5) / (1 + z * z / d)
    return f"{100 * (centre - half):.1f}-{100 * (centre + half):.1f}%"


def main():
    refresh = "--refresh" in sys.argv[1:]
    pairs = [json.loads(line) for line in GOLD.open()]
    earlier = [p for p in pairs if p["candidate_before_duplicate"]]
    by_dup = defaultdict(list)
    for p in earlier:
        by_dup[p["duplicate"]].append(p)

    results = []
    for variant in ("and", "or"):
        for dup, dup_pairs in sorted(by_dup.items(), key=lambda kv: int(kv[0].split("-")[1])):
            words = summary_words(dup_pairs[0]["duplicate_summary"])
            jql = build_jql(words, variant, dup_pairs[0]["duplicate_created"])
            status, body = search(jql, "key", FETCH, refresh=refresh)
            keys = [i["key"] for i in body.get("issues", [])] if status == 200 else []
            for p in dup_pairs:
                rank = keys.index(p["candidate"]) + 1 if p["candidate"] in keys else None
                results.append({
                    "variant": variant,
                    "duplicate": dup,
                    "candidate": p["candidate"],
                    "provenance": p["provenance"],
                    "comment_dup_cue": p["comment_dup_cue"],
                    "duplicate_created": p["duplicate_created"],
                    "cross_language": p["duplicate_summary_hangul"] != p["candidate_summary_hangul"],
                    "same_minute": p["candidate_created"][:16] == p["duplicate_created"][:16],
                    "jql": jql,
                    "http_status": status,
                    "error": None if status == 200 else body.get("errorMessages"),
                    "total_hits": body.get("total") if status == 200 else None,
                    "rank": rank,
                    "in_top10": rank is not None and rank <= 10,
                })

    with OUT.open("w") as fh:
        for row in results:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"# Baseline search results written to {OUT.relative_to(ROOT)}")
    print(f"- earlier-candidate pairs: {len(earlier)} from {len(by_dup)} duplicate issues")
    print(f"- pairs whose candidate shares the duplicate's creation minute (unfindable by construction): "
          f"{sum(1 for r in results if r['variant'] == 'and' and r['same_minute'])}")
    for variant in ("and", "or"):
        rows = [r for r in results if r["variant"] == variant]
        per_query = {r["duplicate"]: r for r in rows}.values()
        errors = sum(1 for r in per_query if r["http_status"] != 200)
        zero = sum(1 for r in per_query if r["total_hits"] == 0)
        hits = sorted(r["total_hits"] for r in per_query if r["total_hits"] is not None)
        print(f"\n## Variant `{variant}`")
        print(f"- queries: {len(per_query)}; HTTP errors: {errors}; zero-hit queries: {pct(zero, len(per_query))};"
              f" median hits: {hits[len(hits) // 2] if hits else None}")
        header = "| subset | pairs | " + " | ".join(f"recall@{k}" for k in CUTOFFS) + " | recall@10 95% CI |"
        print(header)
        print("|" + "---|" * (len(CUTOFFS) + 3))
        subsets = [("all", rows)] + [(p, [r for r in rows if r["provenance"] == p]) for p in PROVENANCES]
        subsets += [
            ("comment_mention, dup cue", [r for r in rows if r["provenance"] == "comment_mention" and r["comment_dup_cue"]]),
            ("comment_mention, no cue", [r for r in rows if r["provenance"] == "comment_mention" and not r["comment_dup_cue"]]),
            ("duplicate created in 2026", [r for r in rows if r["duplicate_created"].startswith("2026")]),
            ("duplicate created before 2026", [r for r in rows if not r["duplicate_created"].startswith("2026")]),
            ("exactly one summary has Hangul", [r for r in rows if r["cross_language"]]),
            ("same script (both or neither Hangul)", [r for r in rows if not r["cross_language"]]),
        ]
        for name, sub in subsets:
            cells = [pct(sum(1 for r in sub if r["rank"] and r["rank"] <= k), len(sub)) for k in CUTOFFS]
            cells.append(wilson(sum(1 for r in sub if r["in_top10"]), len(sub)))
            print(f"| {name} | {len(sub)} | " + " | ".join(cells) + " |")
        # Issue level: a reporter needs only one of the earlier candidates in the top 10.
        found = defaultdict(bool)
        best = {}
        for r in rows:
            found[r["duplicate"]] |= r["in_top10"]
            if r["duplicate"] not in best or PROVENANCES.index(r["provenance"]) < PROVENANCES.index(best[r["duplicate"]]):
                best[r["duplicate"]] = r["provenance"]
        findable = [r for r in rows if not r["same_minute"]]
        print(f"- excluding same-minute pairs: recall@10 {pct(sum(1 for r in findable if r['in_top10']), len(findable))}")
        print(f"- issue level, any earlier candidate in top 10: {pct(sum(found.values()), len(found))}"
              f" (95% CI {wilson(sum(found.values()), len(found))})")
        for p in PROVENANCES:
            dups = [d for d in found if best[d] == p]
            print(f"  - strongest provenance {p}: {pct(sum(found[d] for d in dups), len(dups))}")
    leakage_check(results, refresh)
    print(f"\n- requests: network {stats['network']}, cache {stats['cache']}")


def leakage_check(results, refresh):
    """Today's index holds comments added after the duplicate was created. Count found
    candidates that carry such a comment naming the duplicate's key (an upper bound on
    hits that the reporter could not have had at the time)."""
    found = [r for r in results if r["in_top10"]]
    keys = sorted({r["candidate"] for r in found}, key=lambda k: int(k.split("-")[1]))
    comments = {}
    for start in range(0, len(keys), 200):
        jql = f"key in ({', '.join(keys[start:start + 200])}) ORDER BY key ASC"
        for issue in search_all(jql, "comment", refresh):
            comments[issue["key"]] = (issue["fields"].get("comment") or {}).get("comments", [])
    created = {json.loads(line)["duplicate"]: json.loads(line)["duplicate_created"] for line in GOLD.open()}
    print("\n## Possible leakage among top-10 hits")
    for variant in ("and", "or"):
        rows = [r for r in found if r["variant"] == variant]
        late = sum(1 for r in rows if any(
            r["duplicate"] in (c.get("body") or "")
            and parse_created(c["created"]) > parse_created(created[r["duplicate"]])
            for c in comments.get(r["candidate"], [])))
        print(f"- variant `{variant}`: {late} of {len(rows)} top-10 hits have a later comment on the candidate"
              f" that names the duplicate key")


if __name__ == "__main__":
    sys.exit(main())
