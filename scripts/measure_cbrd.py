#!/usr/bin/env python3
"""Reproduce the CBRD corpus numbers behind the duplicate-detection proposal.

Uses only anonymous reads of the public JIRA REST API on jira.cubrid.org.
Run: python3 -I scripts/measure_cbrd.py
"""
import json
import re
import statistics
import sys
import urllib.parse
import urllib.request
from collections import Counter

BASE = "http://jira.cubrid.org/rest/api/2/search"
HANGUL = re.compile(r"[가-힣]")
KEY = re.compile(r"CBRD-\d+")


def search(jql, fields="key", max_results=0):
    query = urllib.parse.urlencode({"jql": jql, "fields": fields, "maxResults": max_results})
    with urllib.request.urlopen(f"{BASE}?{query}", timeout=120) as resp:
        return json.load(resp)


def total(jql):
    return search(jql)["total"]


def show(label, value, jql=None):
    print(f"- {label}: {value}" + (f"  (JQL: `{jql}`)" if jql else ""))


def main():
    print("## Corpus size")
    show("visible CBRD issues", total("project = CBRD"), "project = CBRD")
    show("created before 2016", total("project = CBRD AND created < 2016-01-01"))
    first = search("project = CBRD AND created >= 2016-01-01 ORDER BY created ASC", "created", 1)["issues"][0]
    show("first issue created in 2016 or later", f"{first['key']} ({first['fields']['created'][:10]})")
    page = search("project = CBRD ORDER BY created DESC", "key", 1000)
    show("issues returned by one search request (maxResults=1000)", len(page["issues"]))

    print("\n## Creation rate")
    for year in range(2016, 2027):
        show(f"created in {year}", total(f"project = CBRD AND created >= {year}-01-01 AND created < {year + 1}-01-01"))
    for days in (30, 90, 180, 365):
        show(f"created in last {days} days", total(f"project = CBRD AND created >= -{days}d"))

    print("\n## Labeled duplicates")
    dup_jql = "project = CBRD AND resolution = Duplicate"
    dups = search(dup_jql, "issuelinks,comment,created,issuetype", 1000)["issues"]
    show("resolution = Duplicate", len(dups), dup_jql)
    show("of which created in 2026", sum(1 for i in dups if i["fields"]["created"].startswith("2026")))
    linked = mentioned = no_pointer = 0
    for issue in dups:
        fields = issue["fields"]
        links = [l for l in fields.get("issuelinks") or [] if l["type"]["name"] in ("Duplicate", "Relates")]
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
    recent = search("project = CBRD AND created >= -180d ORDER BY created DESC", "summary,description,comment", 1000)["issues"]
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


if __name__ == "__main__":
    sys.exit(main())
