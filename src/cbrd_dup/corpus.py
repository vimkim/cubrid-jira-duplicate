"""The local copy of the CBRD corpus: issue types, the searchable rows of an issue, and sync from JIRA.

The index directory holds `issues.jsonl` (one `Issue` per line, by key number), `sync.json` (what the
last sync saw), and `raw/` (the response cache of sync requests). JIRA JSON is parsed once, in
`parse_issue`; everything else works on the typed `Issue`.
"""

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from cbrd_dup.jira import KST, JiraClient, key_number, parse_created
from cbrd_dup.store import write_text_atomic

FIELDS = "summary,description,comment,status,resolution,issuetype,created,updated"
OVERLAP = timedelta(minutes=10)
HOME_ENV = "CUBRID_JIRA_DUP_HOME"


class Comment(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    created: datetime
    updated: datetime
    body: str


class Issue(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    summary: str
    description: str
    status: str
    resolution: str | None
    issuetype: str
    created: datetime
    updated: datetime
    comments: list[Comment]


class SyncState(BaseModel):
    synced_at: datetime
    complete: bool
    issues: int
    errors: list[str]


class Snapshot(BaseModel):
    """Which corpus a measurement ran on."""

    issues: int
    synced_at: datetime
    complete: bool
    sha256: str


class SyncResult(NamedTuple):
    state: SyncState
    fetched: int


@dataclass(frozen=True)
class Row:
    """One searchable unit of an issue: its summary and description, or one comment."""

    key: str
    kind: Literal["body", "comment"]
    issue_created: datetime
    written: datetime
    text: str

    def visible_before(self, t: datetime | None) -> bool:
        return t is None or (self.issue_created < t and self.written < t)


@dataclass(frozen=True)
class IndexDir:
    root: Path

    @property
    def issues(self) -> Path:
        return self.root / "issues.jsonl"

    @property
    def state(self) -> Path:
        return self.root / "sync.json"

    @property
    def raw(self) -> Path:
        return self.root / "raw"


def default_index_dir() -> Path:
    if home := os.environ.get(HOME_ENV):
        return Path(home)
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache) / "cubrid-jira-dup"


def parse_issue(raw: dict) -> Issue:
    fields = raw["fields"]
    comments = [
        Comment(
            id=c["id"],
            created=parse_created(c["created"]),
            updated=parse_created(c["updated"]),
            body=c.get("body") or "",
        )
        for c in (fields.get("comment") or {}).get("comments", [])
    ]
    return Issue(
        key=raw["key"],
        summary=fields["summary"],
        description=fields.get("description") or "",
        status=fields["status"]["name"],
        resolution=(fields.get("resolution") or {}).get("name"),
        issuetype=fields["issuetype"]["name"],
        created=parse_created(fields["created"]),
        updated=parse_created(fields["updated"]),
        comments=comments,
    )


def rows(issue: Issue) -> list[Row]:
    body = Row(issue.key, "body", issue.created, issue.created, f"{issue.summary}\n\n{issue.description}")
    return [body] + [
        Row(issue.key, "comment", issue.created, max(c.created, c.updated), c.body) for c in issue.comments
    ]


def read_issues(index: IndexDir) -> list[Issue]:
    with index.issues.open(encoding="utf-8") as fh:
        return [Issue.model_validate_json(line) for line in fh if line.strip()]


def write_issues(index: IndexDir, issues: list[Issue]) -> None:
    ordered = sorted(issues, key=lambda i: key_number(i.key))
    write_text_atomic(index.issues, "".join(i.model_dump_json() + "\n" for i in ordered))


def read_state(index: IndexDir) -> SyncState | None:
    if not index.state.exists():
        return None
    return SyncState.model_validate_json(index.state.read_text(encoding="utf-8"))


def write_state(index: IndexDir, state: SyncState) -> None:
    write_text_atomic(index.state, state.model_dump_json(indent=2) + "\n")


def snapshot(index: IndexDir) -> Snapshot:
    state = read_state(index)
    if state is None:
        raise FileNotFoundError(f"{index.state} not found; run `cubrid-jira-dup sync` first")
    return Snapshot(
        issues=state.issues,
        synced_at=state.synced_at,
        complete=state.complete,
        sha256=hashlib.sha256(index.issues.read_bytes()).hexdigest(),
    )


def sync_jql(state: SyncState | None) -> str:
    if state is None:
        return "project = CBRD ORDER BY key ASC"
    since = (state.synced_at - OVERLAP).astimezone(KST)
    return f'project = CBRD AND updated >= "{since:%Y-%m-%d %H:%M}" ORDER BY key ASC'


def sync(client: JiraClient, index: IndexDir, now: datetime, full: bool = False) -> SyncResult:
    """Bring the index up to date. `now` is when the caller started, before the first request.

    The next sync overlaps this one by ten minutes and refetches everything after an incomplete one.
    Issues that stop being visible are not detected, except by a full sync.
    """
    previous = read_state(index) if index.issues.exists() else None
    state = None if full or previous is None or not previous.complete else previous
    jql = sync_jql(state)
    raw_issues, total = client.search_all_counted(jql, FIELDS)

    errors = []
    if len(raw_issues) != total:
        errors.append(f"fetched {len(raw_issues)} issues but JIRA reported a total of {total}")
    for raw in raw_issues:
        comment = raw["fields"].get("comment") or {}
        returned = len(comment.get("comments", []))
        if comment.get("total", returned) != returned:
            errors.append(f"{raw['key']}: JIRA reported {comment['total']} comments but returned {returned}")

    kept = {} if state is None else {i.key: i for i in read_issues(index)}
    kept.update((i.key, i) for i in map(parse_issue, raw_issues))
    new_state = SyncState(synced_at=now, complete=not errors, issues=len(kept), errors=errors)
    write_issues(index, list(kept.values()))
    write_state(index, new_state)
    return SyncResult(new_state, len(raw_issues))
