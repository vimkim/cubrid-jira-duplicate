"""Reproduce the CBRD corpus numbers behind the duplicate-detection proposal.

Anonymous reads of the public JIRA REST API, through the shared client and its cache. Windows such as
`created >= -30d` are relative to the day of the request, so pass --refresh to measure today's corpus.
"""

import re
import statistics
from collections import Counter
from datetime import datetime

from cbrd_dup.jira import JiraClient
from cbrd_dup.stats import nearest_rank

# Syllables only, unlike text.HANGUL, so the counts stay comparable with the ones published earlier.
HANGUL = re.compile(r"[가-힣]")
KEY = re.compile(r"CBRD-\d+")


def search(client: JiraClient, jql: str, fields: str = "key", max_results: int = 0) -> dict:
    return client.search(jql, fields, max_results).require_ok()


def total(client: JiraClient, jql: str) -> int:
    return search(client, jql)["total"]


def days_to_resolve(issue: dict) -> float:
    fields = issue["fields"]
    created = datetime.strptime(fields["created"][:19], "%Y-%m-%dT%H:%M:%S")
    resolved = datetime.strptime(fields["resolutiondate"][:19], "%Y-%m-%dT%H:%M:%S")
    return (resolved - created).total_seconds() / 86400


def show(label: str, value, jql: str | None = None) -> None:
    print(f"- {label}: {value}" + (f"  (JQL: `{jql}`)" if jql else ""))


def run(client: JiraClient) -> None:
    print("## Corpus size")
    show("visible CBRD issues", total(client, "project = CBRD"), "project = CBRD")
    show("created before 2016", total(client, "project = CBRD AND created < 2016-01-01"))
    first = search(client, "project = CBRD AND created >= 2016-01-01 ORDER BY created ASC", "created", 1)["issues"][0]
    show("first issue created in 2016 or later", f"{first['key']} ({first['fields']['created'][:10]})")
    page = search(client, "project = CBRD ORDER BY created DESC", "key", 1000)
    show("issues returned by one search request (maxResults=1000)", len(page["issues"]))

    print("\n## Creation rate")
    for year in range(2016, 2027):
        show(
            f"created in {year}",
            total(client, f"project = CBRD AND created >= {year}-01-01 AND created < {year + 1}-01-01"),
        )
    for days in (30, 90, 180, 365):
        show(f"created in last {days} days", total(client, f"project = CBRD AND created >= -{days}d"))

    print("\n## Labeled duplicates")
    dup_jql = "project = CBRD AND resolution = Duplicate"
    dups = search(client, dup_jql, "issuelinks,comment,created,resolutiondate,issuetype", 1000)["issues"]
    show("resolution = Duplicate", len(dups), dup_jql)
    show("of which created in 2026", sum(1 for i in dups if i["fields"]["created"].startswith("2026")))
    delays = sorted(days_to_resolve(i) for i in dups if i["fields"]["created"].startswith("2026"))
    median, p75 = statistics.median(delays), nearest_rank(delays, 0.75)
    show(
        "2026 duplicates, days from created to resolved: median / p75 (nearest rank) / over 28 days",
        f"{median:.1f} / {p75:.1f} / {sum(d > 28 for d in delays)} of {len(delays)}",
    )
    linked = mentioned = no_pointer = 0
    for issue in dups:
        fields = issue["fields"]
        links = [link for link in fields.get("issuelinks") or [] if link["type"]["name"] in ("Duplicate", "Relates")]
        comments = " ".join(c["body"] for c in (fields.get("comment") or {}).get("comments", []))
        if links:
            linked += 1
        elif set(KEY.findall(comments)) - {issue["key"]}:
            mentioned += 1
        else:
            no_pointer += 1
    show("point to another issue by Duplicate or Relates link", linked)
    show("no such link, but a comment names another CBRD key", mentioned)
    show("no pointer found", no_pointer)
    show("issue types", Counter(i["fields"]["issuetype"]["name"] for i in dups).most_common())

    print("\n## Language and size (newest 1,000 issues created in the last 180 days)")
    recent_jql = "project = CBRD AND created >= -180d ORDER BY created DESC"
    recent = search(client, recent_jql, "summary,description,comment", 1000)["issues"]
    summaries = [i["fields"]["summary"] or "" for i in recent]
    descriptions = [i["fields"].get("description") or "" for i in recent]
    comment_lists = [(i["fields"].get("comment") or {}).get("comments", []) for i in recent]
    comment_bodies = [c["body"] for cs in comment_lists for c in cs]
    show("sample size", len(recent))
    show("summaries containing Hangul", sum(1 for s in summaries if HANGUL.search(s)))
    show("descriptions containing Hangul", sum(1 for d in descriptions if HANGUL.search(d)))
    lengths = sorted(len(d) for d in descriptions)
    show("description chars median / p90", f"{statistics.median(lengths):.0f} / {lengths[int(0.9 * len(lengths))]}")
    counts = [len(cs) for cs in comment_lists]
    show("comments per issue median / max", f"{statistics.median(counts):.0f} / {max(counts)}")
    show("description + comment text (MB)", round((sum(lengths) + sum(map(len, comment_bodies))) / 1e6, 1))
