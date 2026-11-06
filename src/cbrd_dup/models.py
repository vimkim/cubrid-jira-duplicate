"""Record types of the two JSONL data files. Timestamps stay JIRA's strings so files round-trip byte for byte."""

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

# Strongest first: a pair found several ways keeps the strongest as `provenance`.
Provenance = Literal["duplicate_link", "relates_link", "comment_mention"]
PROVENANCES: tuple[Provenance, ...] = get_args(Provenance)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Link(_Record):
    type: str
    direction: Literal["outward", "inward"]
    label: str


class GoldPair(_Record):
    """One (duplicate, candidate original) pair from an issue resolved as Duplicate."""

    duplicate: str
    candidate: str
    duplicate_created: str
    candidate_created: str | None
    provenance: Provenance
    provenances: list[Provenance]
    links: list[Link]
    candidate_before_duplicate: bool | None
    duplicate_summary_hangul: bool
    candidate_summary_hangul: bool | None
    comment_dup_cue: bool | None
    candidate_visible: bool
    duplicate_issuetype: str
    candidate_issuetype: str | None
    candidate_resolution: str | None
    duplicate_summary: str
    candidate_summary: str | None


class BaselineResult(_Record):
    """Where one search a reporter can run today ranks one gold candidate."""

    variant: Literal["and", "or"]
    duplicate: str
    candidate: str
    provenance: Provenance
    comment_dup_cue: bool | None
    duplicate_created: str
    cross_language: bool
    same_minute: bool
    jql: str
    http_status: int
    error: list[str] | None
    total_hits: int | None
    rank: int | None
    in_top10: bool
