#!/usr/bin/env python3
"""Probe how JIRA (jira.cubrid.org, Server 7.7.1) evaluates and orders `text ~` searches.

Anonymous reads only. Prints four checks:
  1. How several words inside one `text ~ "..."` are combined (AND or OR).
  2. Whether results without ORDER BY follow key, created, updated, or priority order,
     and whether two identical requests return the same order.
  3. Whether summary matches rank first (a sign of relevance ranking).
  4. Whether all words must appear in the same field.
  5. How a Hangul word with a particle attached matches.
  6. Which time zone JQL date literals use for an anonymous reader.

Run from the worktree root:  python3 -I scripts/probe_text_order.py [--refresh]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cbrd_rest import search, stats  # noqa: E402

REFRESH = "--refresh" in sys.argv[1:]


def total(jql):
    status, body = search(jql, "key", 0, refresh=REFRESH)
    return body["total"] if status == 200 else f"error {status}"


def keys(jql, n=1000, fields="key"):
    status, body = search(jql, fields, n, refresh=REFRESH)
    assert status == 200, body
    return body["issues"]


def operator_check():
    print("## 1. Several words in one text ~ clause")
    for a, b in (("vacuum", "overflow"), ("deadlock", "regression")):
        for text in (a, b, f"{a} {b}", f"{a} AND {b}", f"{a} OR {b}"):
            jql = f'project = CBRD AND text ~ "{text}"'
            print(f"- `{jql}`: {total(jql)}")


def order_check():
    print("\n## 2. Order without ORDER BY")
    for base in ('project = CBRD AND text ~ "vacuum overflow"', 'project = CBRD AND text ~ "deadlock"',
                 'project = CBRD AND text ~ "partition" AND created < "2025-01-01 00:00"'):
        first = [i["key"] for i in keys(base, 50)]
        again = [i["key"] for i in keys(base, 50, "key,summary")]
        print(f"- `{base}`: top 5 {first[:5]}; same order when repeated: {first == again}")
        for order in ("key DESC", "key ASC", "created DESC", "updated DESC", "priority DESC, updated DESC"):
            ordered = [i["key"] for i in keys(f"{base} ORDER BY {order}")][:len(first)]
            print(f"  - equals ORDER BY {order}: {ordered == first}")


def relevance_check():
    print("\n## 3. Rank of issues whose summary contains every query word")
    for words in (["deadlock"], ["vacuum", "overflow"], ["loaddb"]):
        jql = f'project = CBRD AND text ~ "{" ".join(words)}"'
        issues = keys(jql, 1000, "summary")
        ranks = [n for n, i in enumerate(issues, 1) if all(w in (i["fields"]["summary"] or "").lower() for w in words)]
        rest = [n for n in range(1, len(issues) + 1) if n not in set(ranks)]
        print(f"- `{jql}`: {len(issues)} results; summary-match ranks {ranks[:30]}"
              f"{' ...' if len(ranks) > 30 else ''} ({len(ranks)} issues;"
              f" {sum(1 for r in ranks if r <= len(ranks))} of them in the top {len(ranks)};"
              f" median rank of the other results {rest[len(rest) // 2] if rest else '-'})")


def same_field_check():
    print("\n## 4. Words split across fields")
    print("Issues with word A only in the summary and word B only in the description (neither word in comments):")
    for a, b in (("vacuum", "overflow"), ("loaddb", "error"), ("vacuum", "assert"), ("heap", "crash")):
        split = {i["key"] for i in keys(
            f'project = CBRD AND summary ~ "{a}" AND summary !~ "{b}" AND description ~ "{b}"'
            f' AND description !~ "{a}" AND comment !~ "{a}" AND comment !~ "{b}"')}
        both = {i["key"] for i in keys(f'project = CBRD AND text ~ "{a} {b}"')}
        print(f"- A={a}, B={b}: {len(split)} such issues {sorted(split)}; returned by text ~ \"{a} {b}\":"
              f" {len(split & both)}")


def hangul_check():
    print("\n## 5. Hangul word with a particle")
    for text in ("중복", "중복이", "중복을", "중복*"):
        jql = f'project = CBRD AND text ~ "{text}"'
        print(f"- `{jql}`: {total(jql)}")


def timezone_check():
    print("\n## 6. Time zone of JQL dates for an anonymous reader")
    status, body = search("key = CBRD-20247", "created", 1, refresh=REFRESH)
    print(f"- CBRD-20247 created {body['issues'][0]['fields']['created']}")
    for minute in ("2016-05-19 12:31", "2016-05-19 12:32"):
        jql = f'key = CBRD-20247 AND created < "{minute}"'
        print(f"- `{jql}`: {total(jql)}")


def main():
    operator_check()
    order_check()
    relevance_check()
    same_field_check()
    hangul_check()
    timezone_check()
    print(f"\n- requests: network {stats['network']}, cache {stats['cache']}")


if __name__ == "__main__":
    sys.exit(main())
