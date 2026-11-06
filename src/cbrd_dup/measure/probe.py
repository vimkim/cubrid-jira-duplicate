"""Probe how JIRA (jira.cubrid.org, Server 7.7.1) evaluates and orders `text ~` searches.

Anonymous reads only. Prints six checks:
  1. How several words inside one `text ~ "..."` are combined (AND or OR).
  2. Whether results without ORDER BY follow key, created, updated, or priority order,
     and whether two identical requests return the same order.
  3. Whether summary matches rank first (a sign of relevance ranking).
  4. Whether all words must appear in the same field.
  5. How a Hangul word with a particle attached matches.
  6. Which time zone JQL date literals use for an anonymous reader.
"""

from cbrd_dup.jira import JiraClient


def total(client: JiraClient, jql: str):
    status, body = client.search(jql, "key", 0)
    return body["total"] if status == 200 else f"error {status}"


def keys(client: JiraClient, jql: str, n: int = 1000, fields: str = "key") -> list[dict]:
    return client.search(jql, fields, n).require_ok()["issues"]


def operator_check(client: JiraClient) -> None:
    print("## 1. Several words in one text ~ clause")
    for a, b in (("vacuum", "overflow"), ("deadlock", "regression")):
        for text in (a, b, f"{a} {b}", f"{a} AND {b}", f"{a} OR {b}"):
            jql = f'project = CBRD AND text ~ "{text}"'
            print(f"- `{jql}`: {total(client, jql)}")


def order_check(client: JiraClient) -> None:
    print("\n## 2. Order without ORDER BY")
    for base in (
        'project = CBRD AND text ~ "vacuum overflow"',
        'project = CBRD AND text ~ "deadlock"',
        'project = CBRD AND text ~ "partition" AND created < "2025-01-01 00:00"',
    ):
        first = [i["key"] for i in keys(client, base, 50)]
        again = [i["key"] for i in keys(client, base, 50, "key,summary")]
        print(f"- `{base}`: top 5 {first[:5]}; same order when repeated: {first == again}")
        for order in ("key DESC", "key ASC", "created DESC", "updated DESC", "priority DESC, updated DESC"):
            ordered = [i["key"] for i in keys(client, f"{base} ORDER BY {order}")][: len(first)]
            print(f"  - equals ORDER BY {order}: {ordered == first}")


def relevance_check(client: JiraClient) -> None:
    print("\n## 3. Rank of issues whose summary contains every query word")
    for words in (["deadlock"], ["vacuum", "overflow"], ["loaddb"]):
        jql = f'project = CBRD AND text ~ "{" ".join(words)}"'
        issues = keys(client, jql, 1000, "summary")
        ranks = [n for n, i in enumerate(issues, 1) if all(w in (i["fields"]["summary"] or "").lower() for w in words)]
        rest = [n for n in range(1, len(issues) + 1) if n not in set(ranks)]
        print(
            f"- `{jql}`: {len(issues)} results; summary-match ranks {ranks[:30]}"
            f"{' ...' if len(ranks) > 30 else ''} ({len(ranks)} issues;"
            f" {sum(1 for r in ranks if r <= len(ranks))} of them in the top {len(ranks)};"
            f" median rank of the other results {rest[len(rest) // 2] if rest else '-'})"
        )


def same_field_check(client: JiraClient) -> None:
    print("\n## 4. Words split across fields")
    print("Issues with word A only in the summary and word B only in the description (neither word in comments):")
    for a, b in (("vacuum", "overflow"), ("loaddb", "error"), ("vacuum", "assert"), ("heap", "crash")):
        split = {
            i["key"]
            for i in keys(
                client,
                f'project = CBRD AND summary ~ "{a}" AND summary !~ "{b}" AND description ~ "{b}"'
                f' AND description !~ "{a}" AND comment !~ "{a}" AND comment !~ "{b}"',
            )
        }
        both = {i["key"] for i in keys(client, f'project = CBRD AND text ~ "{a} {b}"')}
        print(
            f'- A={a}, B={b}: {len(split)} such issues {sorted(split)}; returned by text ~ "{a} {b}":'
            f" {len(split & both)}"
        )


def hangul_check(client: JiraClient) -> None:
    print("\n## 5. Hangul word with a particle")
    for text in ("중복", "중복이", "중복을", "중복*"):
        jql = f'project = CBRD AND text ~ "{text}"'
        print(f"- `{jql}`: {total(client, jql)}")


def timezone_check(client: JiraClient) -> None:
    print("\n## 6. Time zone of JQL dates for an anonymous reader")
    body = client.search("key = CBRD-20247", "created", 1).require_ok()
    print(f"- CBRD-20247 created {body['issues'][0]['fields']['created']}")
    for minute in ("2016-05-19 12:31", "2016-05-19 12:32"):
        jql = f'key = CBRD-20247 AND created < "{minute}"'
        print(f"- `{jql}`: {total(client, jql)}")


def run(client: JiraClient) -> None:
    operator_check(client)
    order_check(client)
    relevance_check(client)
    same_field_check(client)
    hangul_check(client)
    timezone_check(client)
    print(f"\n- requests: network {client.network}, cache {client.cache}")
