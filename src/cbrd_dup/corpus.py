
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from cbrd_dup.jira import KST, JiraClient, parse_created

if TYPE_CHECKING:
    from cbrd_dup.index import Index, SyncState

FIELDS = "summary,description,comment,status,resolution,issuetype,created,updated"
OVERLAP = timedelta(minutes=10)


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


class SyncResult(NamedTuple):
    state: "SyncState"
    fetched: int
    changed: int


@dataclass(frozen=True)
class Row:

    key: str
    kind: Literal["body", "comment"]
    issue_created: datetime
    written: datetime
    text: str

    def visible_before(self, t: datetime | None) -> bool:
        return t is None or (self.issue_created < t and self.written < t)


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


def sync_jql(state: "SyncState | None") -> str:
    if state is None:
        return "project = CBRD ORDER BY key ASC"
    since = (state.synced_at - OVERLAP).astimezone(KST)
    return f'project = CBRD AND updated >= "{since:%Y-%m-%d %H:%M}" ORDER BY key ASC'


def sync(client: JiraClient, index: "Index", now: datetime, full: bool = False) -> SyncResult:
    previous = index.state()
    state = None if full or previous is None or not previous.complete else previous
    index.record_attempt(now)
    raw_issues, total = client.search_all_counted(sync_jql(state), FIELDS)

    errors = []
    if len(raw_issues) != total:
        errors.append(f"fetched {len(raw_issues)} issues but JIRA reported a total of {total}")
    for raw in raw_issues:
        comment = raw["fields"].get("comment") or {}
        returned = len(comment.get("comments", []))
        if comment.get("total", returned) != returned:
            errors.append(f"{raw['key']}: JIRA reported {comment['total']} comments but returned {returned}")

    changed = index.apply(map(parse_issue, raw_issues), now, errors, replace_all=state is None)
    return SyncResult(index.state(), len(raw_issues), changed)
