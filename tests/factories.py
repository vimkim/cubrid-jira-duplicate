import hashlib
from datetime import datetime, timedelta

import numpy as np

from cbrd_dup.corpus import Comment, Issue
from cbrd_dup.embed import DIM
from cbrd_dup.jira import parse_created
from cbrd_dup.models import GoldPair
from cbrd_dup.text import tokenize

T0 = parse_created("2024-03-05T14:23:11.000+0900")


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def comment(cid: str, body: str, created: datetime, updated: datetime | None = None) -> Comment:
    return Comment(id=cid, created=created, updated=updated or created, body=body)


def issue(key: str, created: datetime, summary: str = "", description: str = "", comments=()) -> Issue:
    return Issue(
        key=key,
        summary=summary,
        description=description,
        status="Open",
        resolution=None,
        issuetype="Bug",
        created=created,
        updated=created,
        comments=list(comments),
    )


def raw_issue(key: str, created: datetime, comments=(), total: int | None = None, summary: str = "s") -> dict:
    listed = [{"id": c.id, "created": stamp(c.created), "updated": stamp(c.updated), "body": c.body} for c in comments]
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": None,
            "status": {"name": "Open"},
            "resolution": None,
            "issuetype": {"name": "Bug"},
            "created": stamp(created),
            "updated": stamp(created),
            "comment": {"total": len(listed) if total is None else total, "comments": listed},
        },
    }


def pair(duplicate: str, candidate: str, created: str = "2025-05-01T10:00:00.000+0900", **fields) -> GoldPair:
    values = {
        "duplicate": duplicate,
        "candidate": candidate,
        "duplicate_created": created,
        "candidate_created": "2020-01-01T00:00:00.000+0900",
        "provenance": "duplicate_link",
        "provenances": ["duplicate_link"],
        "links": [],
        "candidate_before_duplicate": True,
        "duplicate_summary_hangul": False,
        "candidate_summary_hangul": False,
        "comment_dup_cue": None,
        "candidate_visible": True,
        "duplicate_issuetype": "Bug",
        "candidate_issuetype": "Bug",
        "candidate_resolution": None,
        "duplicate_summary": "d",
        "candidate_summary": "c",
    } | fields
    return GoldPair(**values)


class FakeEmbedder:

    model_id = "fake-hash"

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        vectors = np.full((len(texts), DIM), 1e-3, np.float32)
        for row, text in enumerate(texts):
            for token in tokenize(text.split(": ", 1)[1]):
                vectors[row, int(hashlib.md5(token.encode()).hexdigest(), 16) % (DIM - 1) + 1] += 1
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    @property
    def texts(self) -> list[str]:
        return [text for call in self.calls for text in call]
