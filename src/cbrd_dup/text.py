"""Issue-text helpers: language detection and turning a summary into a safe JQL text search."""

import re
from typing import Literal

from cbrd_dup.jira import parse_created

HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
# Lucene query-syntax characters, plus the JQL string delimiters " and \.
SPECIAL = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')
OPERATORS = {"AND", "OR", "NOT", "TO"}

Variant = Literal["and", "or"]


def has_hangul(text: str | None) -> bool:
    return bool(HANGUL.search(text or ""))


def summary_words(summary: str) -> list[str]:
    words = SPECIAL.sub(" ", summary).split()
    # Upper-case AND/OR/NOT are Lucene operators; the index is case-insensitive.
    return [w.lower() if w in OPERATORS else w for w in words]


def text_search_jql(words: list[str], variant: Variant, created: str) -> str:
    """Search for an issue's words among issues created before it, to the minute (JQL has no seconds)."""
    text = (" OR " if variant == "or" else " ").join(words)
    minute = parse_created(created).strftime("%Y-%m-%d %H:%M")
    return f'project = CBRD AND text ~ "{text}" AND created < "{minute}"'
