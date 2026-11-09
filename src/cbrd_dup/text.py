"""Issue-text helpers: language detection, key masking, tokenizing, and safe JQL text searches."""

import re
from typing import Literal

from cbrd_dup.jira import parse_created

HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
# Lucene query-syntax characters, plus the JQL string delimiters " and \.
SPECIAL = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')
OPERATORS = {"AND", "OR", "NOT", "TO"}

ISSUE_KEY = re.compile(r"CBRD-\d+", re.IGNORECASE)
KEY_PLACEHOLDER = "#"
TOKEN_RUN = re.compile(r"[a-z0-9_]+|[가-힣ㄱ-ㅎㅏ-ㅣ]+")

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


def mask_keys(text: str) -> str:
    """Replace every CBRD-<digits>, also inside URLs, so a draft cannot name its original."""
    return ISSUE_KEY.sub(KEY_PLACEHOLDER, text)


def tokenize(text: str) -> list[str]:
    """Lowercase ASCII runs whole plus their `_` parts, and Hangul runs as character bigrams."""
    tokens: list[str] = []
    for run in TOKEN_RUN.findall(text.lower()):
        if HANGUL.match(run):
            tokens.extend([run] if len(run) == 1 else (run[i : i + 2] for i in range(len(run) - 1)))
            continue
        tokens.append(run)
        parts = [part for part in run.split("_") if part]
        if len(parts) >= 2:
            tokens.extend(parts)
    return tokens
