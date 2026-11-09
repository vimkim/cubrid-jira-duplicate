"""Issue-text helpers: language detection, key masking, tokenizing, safe JQL text searches, and code signals."""

import re
from typing import Literal

from cbrd_dup.jira import parse_created

HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
# Lucene query-syntax characters, plus the JQL string delimiters " and \.
SPECIAL = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')
OPERATORS = {"AND", "OR", "NOT", "TO"}

ISSUE_KEY = re.compile(r"CBRD-\d+", re.IGNORECASE)
# Identifiers that carry an issue number: cbrd_27516, CBRD27516, and test databases such as db25833.
KEY_IDENTIFIER = re.compile(r"cbrd[-_]?\d+|(?<![a-z0-9])db_?\d{5}(?!\d)", re.IGNORECASE)
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


def mask_key_identifiers(text: str) -> str:
    """`mask_keys`, and also identifiers named after an issue. Evaluation queries use this (requirements 7.2)."""
    return KEY_IDENTIFIER.sub(KEY_PLACEHOLDER, text)


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


FRAME = re.compile(r"^\s*#\d+\s+(?:0x[0-9a-fA-F]+\s+in\s+)?([A-Za-z_][\w:~<>]*)\s*\(", re.MULTILINE)
NOISE_FRAMES = {
    "raise", "abort", "__assert_fail", "gsignal", "abort_handler", "__GI_raise", "__GI_abort",
    "__assert_fail_base", "er_set", "er_set_with_oserror", "_start", "main", "clone", "start_thread",
}
IDENTIFIER = re.compile(
    r"\b[A-Za-z_][\w\-]*\.(?:c|cc|cpp|cxx|h|hpp|hh|java|py|sh|sql|y|l)\b"
    r"|\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"
    r"|\b(?:[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+)\b"
)
ERROR_CODE = re.compile(r"\bER_[A-Z0-9_]+\b|(?<![\w.])-\d{2,5}(?![\w.])")


def stack_functions(text: str, n: int = 2) -> list[str]:
    """The first `n` distinct function names of a gdb-style call stack, skipping abort and signal frames."""
    names: list[str] = []
    for name in FRAME.findall(text):
        if name not in NOISE_FRAMES and name not in names:
            names.append(name)
            if len(names) == n:
                break
    return names


def identifiers(text: str) -> list[str]:
    """Source file names, snake_case and CamelCase names, in order of first appearance."""
    return list(dict.fromkeys(IDENTIFIER.findall(text)))


def error_codes(text: str) -> list[str]:
    """ER_* names and negative error numbers such as -1114, in order of first appearance."""
    return list(dict.fromkeys(ERROR_CODE.findall(text)))
