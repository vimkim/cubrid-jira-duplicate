"""Build (duplicate, candidate original) gold pairs from CBRD issues resolved as Duplicate.

Writes <data-dir>/gold-pairs.jsonl, one line per pair, and prints a summary.

Provenance, strongest first (a pair found several ways keeps the strongest one as
`provenance` and lists all of them in `provenances`):
  duplicate_link   the duplicate has an issue link of type "Duplicate" to the candidate
  relates_link     the duplicate has an issue link of type "Relates" to the candidate
  comment_mention  a comment on the duplicate names the candidate's CBRD key
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from cbrd_dup.jira import JiraClient, key_number, parse_created
from cbrd_dup.models import PROVENANCES, GoldPair, Link, Provenance
from cbrd_dup.store import DataDir, show, write_jsonl
from cbrd_dup.text import has_hangul

DUP_JQL = "project = CBRD AND resolution = Duplicate ORDER BY key ASC"
DUP_FIELDS = "summary,created,issuetype,issuelinks,comment"
CAND_FIELDS = "summary,created,issuetype,status,resolution"
KEY = re.compile(r"CBRD-\d+")
DUP_CUE = re.compile(r"중복|dup", re.IGNORECASE)
LINK_PROVENANCE: dict[str, Provenance] = {"Duplicate": "duplicate_link", "Relates": "relates_link"}


@dataclass
class Pointer:
    """Everything one duplicate issue says about one candidate."""

    provenances: set[Provenance] = field(default_factory=set)
    links: list[Link] = field(default_factory=list)
    comments: int = 0
    cue_comments: int = 0


def collect_pointers(issue: dict) -> tuple[dict[str, Pointer], int]:
    """Return ({candidate_key: Pointer}, comments missing from the response) for one duplicate issue."""
    found: dict[str, Pointer] = {}
    fields = issue["fields"]
    for link in fields.get("issuelinks") or []:
        provenance = LINK_PROVENANCE.get(link["type"]["name"])
        other = link.get("outwardIssue") or link.get("inwardIssue")
        if not provenance or not other:
            continue
        direction = "outward" if "outwardIssue" in link else "inward"
        pointer = found.setdefault(other["key"], Pointer())
        pointer.provenances.add(provenance)
        pointer.links.append(Link(type=link["type"]["name"], direction=direction, label=link["type"][direction]))
    comment = fields.get("comment") or {}
    comments = comment.get("comments", [])
    for body in (c.get("body") or "" for c in comments):
        cue = bool(DUP_CUE.search(body))
        for key in set(KEY.findall(body)) - {issue["key"]}:
            pointer = found.setdefault(key, Pointer())
            pointer.provenances.add("comment_mention")
            pointer.comments += 1
            pointer.cue_comments += cue
    return found, comment.get("total", len(comments)) - len(comments)


def fetch_candidates(client: JiraClient, keys: set[str]) -> dict[str, dict]:
    """Fetch candidate issues visible anonymously, in chunks. validateQuery=false
    makes JIRA skip keys that do not exist or are not visible instead of failing."""
    found = {}
    ordered = sorted(keys, key=key_number)
    for start in range(0, len(ordered), 200):
        jql = f"key in ({', '.join(ordered[start : start + 200])}) ORDER BY key ASC"
        for issue in client.search_all(jql, CAND_FIELDS, validateQuery="false"):
            found[issue["key"]] = issue
    return found


def build_pairs(
    dups: list[dict], pointers: dict[str, dict[str, Pointer]], candidates: dict[str, dict]
) -> list[GoldPair]:
    rows = []
    for issue in dups:
        dup_fields = issue["fields"]
        dup_created = parse_created(dup_fields["created"])
        for key, pointer in sorted(pointers[issue["key"]].items(), key=lambda kv: key_number(kv[0])):
            provenances = sorted(pointer.provenances, key=PROVENANCES.index)
            cand = candidates.get(key)
            cf = cand["fields"] if cand else None
            rows.append(
                GoldPair(
                    duplicate=issue["key"],
                    candidate=key,
                    duplicate_created=dup_fields["created"],
                    candidate_created=cf["created"] if cf else None,
                    provenance=provenances[0],
                    provenances=provenances,
                    links=pointer.links,
                    candidate_before_duplicate=parse_created(cf["created"]) < dup_created if cf else None,
                    duplicate_summary_hangul=has_hangul(dup_fields["summary"]),
                    candidate_summary_hangul=has_hangul(cf["summary"]) if cf else None,
                    comment_dup_cue=pointer.cue_comments > 0 if pointer.comments else None,
                    candidate_visible=cand is not None,
                    duplicate_issuetype=dup_fields["issuetype"]["name"],
                    candidate_issuetype=cf["issuetype"]["name"] if cf else None,
                    candidate_resolution=(cf.get("resolution") or {}).get("name") if cf else None,
                    duplicate_summary=dup_fields["summary"],
                    candidate_summary=cf["summary"] if cf else None,
                )
            )
    return rows


def run(client: JiraClient, data: DataDir) -> None:
    dups = client.search_all(DUP_JQL, DUP_FIELDS)
    pointers, truncated_comments = {}, 0
    for issue in dups:
        pointers[issue["key"]], missing = collect_pointers(issue)
        truncated_comments += missing
    candidate_keys = {key for found in pointers.values() for key in found}
    candidates = fetch_candidates(client, candidate_keys)
    rows = build_pairs(dups, pointers, candidates)
    write_jsonl(data.gold, rows)

    def per_issue(provenance):
        return {r.duplicate for r in rows if provenance in r.provenances}

    with_link = per_issue("duplicate_link") | per_issue("relates_link")
    comment_only = per_issue("comment_mention") - with_link
    print(f"# Gold pairs written to {show(data.gold)}")
    print(f"- duplicate issues (JQL `{DUP_JQL}`): {len(dups)}")
    print(
        f"- issues with a Duplicate or Relates link: {len(with_link)}; comment mention only: {len(comment_only)};"
        f" no pointer: {len(dups) - len(with_link) - len(comment_only)}"
    )
    print(f"- issues with a Duplicate-type link: {len(per_issue('duplicate_link'))}")
    print(f"- comments missing from search responses (total - returned): {truncated_comments}")
    print(f"- pairs: {len(rows)}; candidate keys: {len(candidate_keys)}; visible anonymously: {len(candidates)}")
    print("- pairs by primary provenance:", dict(Counter(r.provenance for r in rows)))
    for prov in PROVENANCES:
        sub = [r for r in rows if r.provenance == prov]
        vis = [r for r in sub if r.candidate_visible]
        before = sum(1 for r in vis if r.candidate_before_duplicate)
        line = f"  - {prov}: {len(sub)} pairs from {len({r.duplicate for r in sub})} issues; visible {len(vis)};"
        line += f" candidate earlier {before}, later {len(vis) - before}"
        if prov == "comment_mention":
            line += f"; comment has dup cue {sum(1 for r in sub if r.comment_dup_cue)}"
        print(line)
    dl = [r for r in rows if "duplicate_link" in r.provenances]
    dl_issues_earlier = {r.duplicate for r in dl if r.candidate_before_duplicate}
    print(f"- Duplicate-link issues with an earlier linked candidate: {len(dl_issues_earlier)}")
    print(
        "- Duplicate link directions:",
        dict(Counter(link.label for r in rows for link in r.links if link.type == "Duplicate")),
    )
    print(f"- requests: network {client.network}, cache {client.cache}")
