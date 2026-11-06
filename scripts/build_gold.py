#!/usr/bin/env python3
"""Build (duplicate, candidate original) gold pairs from CBRD issues resolved as Duplicate.

Uses only anonymous reads of the public JIRA REST API. Writes data/gold-pairs.jsonl,
one line per (duplicate, candidate) pair, and prints a summary.

Run from the worktree root:  python3 -I scripts/build_gold.py [--refresh]

Provenance, strongest first (a pair found several ways keeps the strongest one as
`provenance` and lists all of them in `provenances`):
  duplicate_link   the duplicate has an issue link of type "Duplicate" to the candidate
  relates_link     the duplicate has an issue link of type "Relates" to the candidate
  comment_mention  a comment on the duplicate names the candidate's CBRD key
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cbrd_rest import ROOT, has_hangul, parse_created, search_all, stats  # noqa: E402

OUT = ROOT / "data" / "gold-pairs.jsonl"
DUP_JQL = "project = CBRD AND resolution = Duplicate ORDER BY key ASC"
DUP_FIELDS = "summary,created,issuetype,issuelinks,comment"
CAND_FIELDS = "summary,created,issuetype,status,resolution"
KEY = re.compile(r"CBRD-\d+")
DUP_CUE = re.compile(r"중복|dup", re.IGNORECASE)
RANK = {"duplicate_link": 0, "relates_link": 1, "comment_mention": 2}
LINK_PROVENANCE = {"Duplicate": "duplicate_link", "Relates": "relates_link"}


def collect_pointers(issue):
    """Return {candidate_key: info} for one duplicate issue."""
    found = {}
    fields = issue["fields"]
    for link in fields.get("issuelinks") or []:
        provenance = LINK_PROVENANCE.get(link["type"]["name"])
        other = link.get("outwardIssue") or link.get("inwardIssue")
        if not provenance or not other:
            continue
        direction = "outward" if "outwardIssue" in link else "inward"
        label = link["type"]["outward" if direction == "outward" else "inward"]
        info = found.setdefault(other["key"], {"provenances": set(), "links": [], "cue_comments": 0, "comments": 0})
        info["provenances"].add(provenance)
        info["links"].append({"type": link["type"]["name"], "direction": direction, "label": label})
    comment = fields.get("comment") or {}
    comments = comment.get("comments", [])
    for body in (c.get("body") or "" for c in comments):
        cue = bool(DUP_CUE.search(body))
        for key in set(KEY.findall(body)) - {issue["key"]}:
            info = found.setdefault(key, {"provenances": set(), "links": [], "cue_comments": 0, "comments": 0})
            info["provenances"].add("comment_mention")
            info["comments"] += 1
            info["cue_comments"] += cue
    return found, comment.get("total", len(comments)) - len(comments)


def fetch_candidates(keys, refresh):
    """Fetch candidate issues visible anonymously, in chunks. validateQuery=false
    makes JIRA skip keys that do not exist or are not visible instead of failing."""
    found = {}
    keys = sorted(keys, key=lambda k: int(k.split("-")[1]))
    for start in range(0, len(keys), 200):
        chunk = keys[start:start + 200]
        jql = f"key in ({', '.join(chunk)}) ORDER BY key ASC"
        for issue in search_all(jql, CAND_FIELDS, refresh, validateQuery="false"):
            found[issue["key"]] = issue
    return found


def main():
    refresh = "--refresh" in sys.argv[1:]
    dups = search_all(DUP_JQL, DUP_FIELDS, refresh)
    pointers, truncated_comments = {}, 0
    for issue in dups:
        pointers[issue["key"]], missing = collect_pointers(issue)
        truncated_comments += missing
    candidate_keys = {key for found in pointers.values() for key in found}
    candidates = fetch_candidates(candidate_keys, refresh)

    rows = []
    for issue in dups:
        dup_fields = issue["fields"]
        dup_created = parse_created(dup_fields["created"])
        for key, info in sorted(pointers[issue["key"]].items(), key=lambda kv: int(kv[0].split("-")[1])):
            provenances = sorted(info["provenances"], key=RANK.get)
            cand = candidates.get(key)
            cf = cand["fields"] if cand else None
            before = parse_created(cf["created"]) < dup_created if cf else None
            rows.append({
                "duplicate": issue["key"],
                "candidate": key,
                "duplicate_created": dup_fields["created"],
                "candidate_created": cf["created"] if cf else None,
                "provenance": provenances[0],
                "provenances": provenances,
                "links": info["links"],
                "candidate_before_duplicate": before,
                "duplicate_summary_hangul": has_hangul(dup_fields["summary"]),
                "candidate_summary_hangul": has_hangul(cf["summary"]) if cf else None,
                "comment_dup_cue": info["cue_comments"] > 0 if info["comments"] else None,
                "candidate_visible": cand is not None,
                "duplicate_issuetype": dup_fields["issuetype"]["name"],
                "candidate_issuetype": cf["issuetype"]["name"] if cf else None,
                "candidate_resolution": (cf.get("resolution") or {}).get("name") if cf else None,
                "duplicate_summary": dup_fields["summary"],
                "candidate_summary": cf["summary"] if cf else None,
            })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def per_issue(provenance):
        return {r["duplicate"] for r in rows if provenance in r["provenances"]}

    with_link = per_issue("duplicate_link") | per_issue("relates_link")
    comment_only = per_issue("comment_mention") - with_link
    print(f"# Gold pairs written to {OUT.relative_to(ROOT)}")
    print(f"- duplicate issues (JQL `{DUP_JQL}`): {len(dups)}")
    print(f"- issues with a Duplicate or Relates link: {len(with_link)}; comment mention only: {len(comment_only)};"
          f" no pointer: {len(dups) - len(with_link) - len(comment_only)}")
    print(f"- issues with a Duplicate-type link: {len(per_issue('duplicate_link'))}")
    print(f"- comments missing from search responses (total - returned): {truncated_comments}")
    print(f"- pairs: {len(rows)}; candidate keys: {len(candidate_keys)}; visible anonymously: {len(candidates)}")
    print("- pairs by primary provenance:", dict(Counter(r["provenance"] for r in rows)))
    for prov in RANK:
        sub = [r for r in rows if r["provenance"] == prov]
        vis = [r for r in sub if r["candidate_visible"]]
        before = sum(1 for r in vis if r["candidate_before_duplicate"])
        line = f"  - {prov}: {len(sub)} pairs from {len({r['duplicate'] for r in sub})} issues; visible {len(vis)};"
        line += f" candidate earlier {before}, later {len(vis) - before}"
        if prov == "comment_mention":
            line += f"; comment has dup cue {sum(1 for r in sub if r['comment_dup_cue'])}"
        print(line)
    dl = [r for r in rows if "duplicate_link" in r["provenances"]]
    dl_issues_earlier = {r["duplicate"] for r in dl if r["candidate_before_duplicate"]}
    print(f"- Duplicate-link issues with an earlier linked candidate: {len(dl_issues_earlier)}")
    print("- Duplicate link directions:", dict(Counter(
        link["label"] for r in rows for link in r["links"] if link["type"] == "Duplicate")))
    print(f"- requests: network {stats['network']}, cache {stats['cache']}")


if __name__ == "__main__":
    sys.exit(main())
