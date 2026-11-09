
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from cbrd_dup.corpus import Issue, SyncResult
from cbrd_dup.embed import Embedder
from cbrd_dup.evaluate import Query
from cbrd_dup.index import Index
from cbrd_dup.jira import JiraError
from cbrd_dup.search import DAY, METHODS, Ranking, Searcher
from cbrd_dup.text import error_codes, identifiers

SCHEMA = "cubrid-jira-dup/candidates/1"
METHOD = METHODS["hybrid"]
ISSUE_KEY = re.compile(r"cbrd-(\d+)", re.IGNORECASE)
MAX_SHARED = 8
DRAFT_MAX_CHARS = 200_000
# Loading a second onnxruntime session costs about 2 s, so a few new texts reuse the query's model.
FEW_PENDING = 32
CHANNEL_SIGNALS = {"L": "lexical_rank", "D": "semantic_rank", "T": "title_rank", "S": "stack_rank"}


class InputError(ValueError):
    ...


@dataclass(frozen=True)
class Source:
    kind: Literal["draft", "key", "title"]
    title: str
    body: str
    key: str | None = None


def within(text: str, max_chars: int, what: str) -> str:
    if len(text) > max_chars:
        raise InputError(f"{what} has {len(text):,} characters, over the limit of {max_chars:,} (--max-chars)")
    return text


def read_draft(path: Path, max_chars: int) -> Source:
    if not path.is_file():
        raise InputError(f"{path} is neither an issue key such as CBRD-12345 nor a readable file")
    lines = within(path.read_text(encoding="utf-8"), max_chars, str(path)).splitlines()
    heading = next((n for n, line in enumerate(lines) if line.startswith("# ")), None)
    if heading is None:
        raise InputError(f"{path} has no `# ` title line")
    return text_source("draft", lines[heading][2:], "\n".join(lines[:heading] + lines[heading + 1 :]))


def text_source(kind: Literal["draft", "title"], title: str, body: str) -> Source:
    if not title.strip():
        raise InputError("the title is empty")
    return Source(kind, title.strip(), body.strip())


def parse_source(source: str | None, title: str | None, stdin: Callable[[], str], max_chars: int) -> Source:
    if (source is None) == (title is None):
        raise InputError("give exactly one input: a draft file, an issue key, or --title with the body on stdin")
    if title is not None:
        return text_source("title", title, within(stdin(), max_chars, "the body on stdin"))
    if match := ISSUE_KEY.fullmatch(source.strip()):
        return Source("key", "", "", f"CBRD-{int(match.group(1))}")
    return read_draft(Path(source), max_chars)


def shared(found: list[str], candidate: list[str]) -> list[str]:
    theirs = {item.lower() for item in candidate}
    first = {}
    for item in found:
        if item.lower() in theirs:
            first.setdefault(item.lower(), item)
    return list(first.values())[:MAX_SHARED]


def candidate_record(rank: int, issue: Issue, ranking: Ranking, query_text: str, at: datetime) -> dict:
    text = "\n\n".join([issue.summary, issue.description, *(c.body for c in issue.comments)])
    signals: dict = {name: ranking.channel_ranks.get(ch, {}).get(issue.key) for ch, name in CHANNEL_SIGNALS.items()}
    signals |= {
        "created_days_before": round((at - issue.created).total_seconds() / DAY, 1),
        "shared_identifiers": shared(identifiers(query_text), identifiers(text)),
        "shared_error_codes": shared(error_codes(query_text), error_codes(text)),
    }
    return {
        "rank": rank,
        "key": issue.key,
        "url": f"https://jira.cubrid.org/browse/{issue.key}",
        "summary": issue.summary,
        "issuetype": issue.issuetype,
        "status": issue.status,
        "resolution": issue.resolution,
        "created": issue.created.isoformat(),
        "signals": signals,
    }


def find(
    index: Index,
    source: Source,
    k: int,
    sync: Callable[[], SyncResult] | None,
    embedder: Callable[[], Embedder],
    query_embedder: Callable[[], Embedder],
    clock: Callable[[], datetime],
    progress: Callable[[str], None],
) -> dict:
    started_at = clock()
    started = time.perf_counter()
    errors: list[str] = []
    if sync is not None:
        if not index.exists():
            progress(f"no index in {index.root}; copying every CBRD issue (first run, a few minutes)")
        try:
            result = sync()
            errors += result.state.errors
        except JiraError as err:
            if not index.exists():
                raise InputError(f"sync failed and there is no index to search: {err}") from err
            errors.append(f"sync failed, searched the existing index: {err}")
    elif not index.exists():
        raise InputError(f"no index in {index.root}; run `cubrid-jira-dup sync` first")
    state = index.state()
    if not state.complete and sync is None:
        errors += state.errors or ["the last sync was incomplete"]
    query_model = query_embedder()
    if pending := index.pending():
        progress(f"embedding {pending} new texts")
        index.embed_pending(embedder() if pending > FEW_PENDING else query_model, progress)
    synced = time.perf_counter()

    query_time = started_at
    if source.kind == "key":
        found = index.issues([source.key])
        if not found:
            raise InputError(f"{source.key} is not in the index")
        source = Source("key", found[0].summary, found[0].description, source.key)
        query_time = found[0].created
    searcher = Searcher(index, METHOD, query_model, clock=lambda: query_time)
    loaded = time.perf_counter()
    ranking = searcher.rank(Query(source.title, source.body, None, source.key), k)
    by_key = {issue.key: issue for issue in index.issues(ranking.keys)}
    query_text = f"{source.title}\n\n{source.body}"
    candidates = [
        candidate_record(rank, by_key[key], ranking, query_text, query_time)
        for rank, key in enumerate(ranking.keys, 1)
    ]
    finished = time.perf_counter()

    return {
        "schema": SCHEMA,
        "query": {
            "source": source.kind,
            "key": source.key,
            "title": source.title,
            "time": query_time.isoformat(),
            "time_reference": "issue created" if source.kind == "key" else "now",
        },
        "k": k,
        "started_at": started_at.isoformat(),
        "finished_at": clock().isoformat(),
        "complete": not errors,
        "errors": errors,
        "index": {
            "path": str(index.root),
            "issues": state.issues,
            "synced_at": state.synced_at.isoformat(),
            "last_sync_attempt": attempt.isoformat() if (attempt := index.attempted_at()) else None,
            "complete": state.complete,
        },
        "method": {"name": METHOD.name, "config": searcher.config},
        "timings_ms": {
            "sync": round((synced - started) * 1000, 1),
            "load": round((loaded - synced) * 1000, 1),
            "embed": round(ranking.timings_ms["embed"], 1),
            "search": round((finished - loaded) * 1000 - ranking.timings_ms["embed"], 1),
            "total": round((finished - started) * 1000, 1),
        },
        "candidates": candidates,
    }


def text_report(document: dict) -> str:
    query = document["query"]
    head = query["key"] or query["title"]
    status = "complete" if document["complete"] else "INCOMPLETE"
    lines = [f"Candidates for {head} ({len(document['candidates'])}, {status})"]
    if not document["candidates"]:
        lines.append("No candidates.")
    for c in document["candidates"]:
        s = c["signals"]
        ranks = ", ".join(f"{name.removesuffix('_rank')} {s[name] or '-'}" for name in CHANNEL_SIGNALS.values())
        extra = "".join(
            f"; {label} {', '.join(s[field])}"
            for field, label in (("shared_identifiers", "identifiers"), ("shared_error_codes", "error codes"))
            if s[field]
        )
        days = s["created_days_before"]
        lines += [
            "",
            f"{c['rank']}. {c['key']} [{c['status']} / {c['resolution'] or 'Unresolved'}] {c['summary']}",
            f"   {c['url']}  created {c['created'][:10]} ({abs(days)} days {'before' if days >= 0 else 'after'})",
            f"   signals: {ranks}{extra}",
        ]
    return "\n".join(lines) + "\n"
