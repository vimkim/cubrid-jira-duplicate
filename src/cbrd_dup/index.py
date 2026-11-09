"""The local index: one SQLite database with the CBRD issues, their searchable rows, and their vectors.

`<root>/index.db` holds:

- `issues(key, number, created, updated, data)`: `data` is the `Issue` as JSON, times are epoch seconds.
- `rows(id, key, ordinal, kind, issue_created, written, digest, vector)`: `corpus.rows(issue)[ordinal]`.
  `digest` hashes the row text, so a sync keeps the vector of an unchanged row; `vector` is NULL until embedded.
- `titles(key, digest, vector)`: the summary vector of each issue.
- `rows_fts` and `titles_fts`: FTS5 over the `text.tokenize` tokens of each row and summary, keyed by
  `rows.id` and the issue number.
- `meta(name, value)`: schema version, embedding model, and the sync state.

`<root>/models/` caches the embedding model and `<root>/raw/` the JIRA responses of the last syncs.
"""

import hashlib
import json
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from cbrd_dup.corpus import Issue, rows
from cbrd_dup.embed import DIM, Embedder, as_passage
from cbrd_dup.jira import key_number
from cbrd_dup.text import tokenize

# Bump when the tables or `text.tokenize` change: an index of another version is rebuilt by the next sync.
SCHEMA = "1"
HOME_ENV = "CUBRID_JIRA_DUP_HOME"
FTS = "fts5(tokens, content='', contentless_delete=1, tokenize=\"unicode61 remove_diacritics 0 tokenchars '_'\")"
# One FTS5 query costs about (rows matched) x (query terms); summing chunks of terms gives the same BM25 faster.
CHUNK_TERMS = 32
EMBED_CHUNK = 512

TABLES = f"""
create table if not exists meta(name text primary key, value text not null);
create table if not exists issues(
    key text primary key, number integer not null unique, created real not null, updated real not null,
    data text not null);
create table if not exists rows(
    id integer primary key, key text not null, ordinal integer not null, kind text not null,
    issue_created real not null, written real not null, digest text not null, vector blob);
create index if not exists rows_by_key on rows(key);
create table if not exists titles(key text primary key, digest text not null, vector blob);
create virtual table if not exists rows_fts using {FTS};
create virtual table if not exists titles_fts using {FTS};
"""


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


@dataclass(frozen=True)
class Arrays:
    """The index as arrays for search. Issues are in key-number order, rows in id order."""

    keys: list[str]
    created: np.ndarray
    row_ids: np.ndarray
    row_issue: np.ndarray
    row_created: np.ndarray
    row_written: np.ndarray
    row_vectors: np.ndarray
    row_embedded: np.ndarray
    title_vectors: np.ndarray
    title_embedded: np.ndarray

    @property
    def position(self) -> dict[str, int]:
        return {key: n for n, key in enumerate(self.keys)}


def default_index_dir() -> Path:
    if home := os.environ.get(HOME_ENV):
        return Path(home)
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache) / "cubrid-jira-dup"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def epoch(when: datetime) -> float:
    return when.timestamp()


def vectors_of(blobs: list[bytes | None]) -> tuple[np.ndarray, np.ndarray]:
    present = np.array([b is not None for b in blobs], bool)
    empty = bytes(4 * DIM)
    joined = b"".join(empty if b is None else b for b in blobs)
    return np.frombuffer(joined, np.float32).reshape(len(blobs), DIM), present


class Index:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "index.db"
        self.models = root / "models"
        self.raw = root / "raw"
        self._db: sqlite3.Connection | None = None

    def exists(self) -> bool:
        return self.path.exists() and self.state() is not None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path)
            self._db.execute("pragma synchronous = normal")
            with self._db:
                self._db.executescript(TABLES)
            if self._meta("schema") not in (None, SCHEMA):
                self._reset()
            with self._db:
                self._set_meta("schema", SCHEMA)
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _reset(self) -> None:
        """Drop everything, so the next sync fetches and indexes every issue again."""
        with self._db:
            for table in ("meta", "issues", "rows", "titles", "rows_fts", "titles_fts"):
                self._db.execute(f"drop table {table}")
            self._db.executescript(TABLES)

    def _meta(self, name: str) -> str | None:
        found = self.db.execute("select value from meta where name = ?", (name,)).fetchone()
        return found[0] if found else None

    def _set_meta(self, name: str, value: str) -> None:
        self.db.execute("insert or replace into meta values (?, ?)", (name, value))

    # ---- sync state

    def state(self) -> SyncState | None:
        if not self.path.exists():
            return None
        synced_at = self._meta("synced_at")
        if synced_at is None:
            return None
        return SyncState(
            synced_at=datetime.fromisoformat(synced_at),
            complete=self._meta("complete") == "1",
            issues=self.db.execute("select count(*) from issues").fetchone()[0],
            errors=json.loads(self._meta("errors") or "[]"),
        )

    def attempted_at(self) -> datetime | None:
        value = self._meta("attempted_at") if self.path.exists() else None
        return datetime.fromisoformat(value) if value else None

    def record_attempt(self, when: datetime) -> None:
        with self.db:
            self._set_meta("attempted_at", when.isoformat())

    def apply(self, issues: Iterable[Issue], synced_at: datetime, errors: list[str], replace_all: bool) -> int:
        """Store fetched issues and the new sync state in one transaction; returns how many issues changed.

        With `replace_all`, issues missing from `issues` are dropped. An unchanged issue keeps its rows.
        """
        changed = 0
        with self.db:
            fetched = set()
            for issue in issues:
                fetched.add(issue.key)
                changed += self._put(issue)
            if replace_all:
                for (key,) in self.db.execute("select key from issues").fetchall():
                    if key not in fetched:
                        self._delete(key)
                        changed += 1
            self._set_meta("synced_at", synced_at.isoformat())
            self._set_meta("complete", "1" if not errors else "0")
            self._set_meta("errors", json.dumps(errors, ensure_ascii=False))
        return changed

    def _put(self, issue: Issue) -> bool:
        data = issue.model_dump_json()
        old = self.db.execute("select data from issues where key = ?", (issue.key,)).fetchone()
        if old is not None and old[0] == data:
            return False
        number = key_number(issue.key)
        self.db.execute(
            "insert into issues values (?, ?, ?, ?, ?) on conflict(key) do update set"
            " created = excluded.created, updated = excluded.updated, data = excluded.data",
            (issue.key, number, epoch(issue.created), epoch(issue.updated), data),
        )
        kept = {}
        for row_id, row_digest, vector in self.db.execute(
            "select id, digest, vector from rows where key = ?", (issue.key,)
        ).fetchall():
            if vector is not None:
                kept[row_digest] = vector
            self.db.execute("delete from rows_fts where rowid = ?", (row_id,))
        self.db.execute("delete from rows where key = ?", (issue.key,))
        for ordinal, row in enumerate(rows(issue)):
            row_digest = digest(row.text)
            cursor = self.db.execute(
                "insert into rows (key, ordinal, kind, issue_created, written, digest, vector)"
                " values (?, ?, ?, ?, ?, ?, ?)",
                (issue.key, ordinal, row.kind, epoch(row.issue_created), epoch(row.written), row_digest)
                + (kept.get(row_digest),),
            )
            self.db.execute(
                "insert into rows_fts (rowid, tokens) values (?, ?)", (cursor.lastrowid, " ".join(tokenize(row.text)))
            )
        title_digest = digest(issue.summary)
        old_title = self.db.execute("select digest from titles where key = ?", (issue.key,)).fetchone()
        if old_title is None or old_title[0] != title_digest:
            self.db.execute("insert or replace into titles values (?, ?, NULL)", (issue.key, title_digest))
            self.db.execute("delete from titles_fts where rowid = ?", (number,))
            self.db.execute(
                "insert into titles_fts (rowid, tokens) values (?, ?)", (number, " ".join(tokenize(issue.summary)))
            )
        return True

    def _delete(self, key: str) -> None:
        for (row_id,) in self.db.execute("select id from rows where key = ?", (key,)).fetchall():
            self.db.execute("delete from rows_fts where rowid = ?", (row_id,))
        self.db.execute("delete from rows where key = ?", (key,))
        self.db.execute("delete from titles_fts where rowid = ?", (key_number(key),))
        self.db.execute("delete from titles where key = ?", (key,))
        self.db.execute("delete from issues where key = ?", (key,))

    # ---- reading

    def issues(self, keys: Iterable[str] | None = None) -> list[Issue]:
        """Issues in key-number order: all of them, or those of `keys` that exist."""
        if keys is None:
            found = self.db.execute("select data from issues order by number").fetchall()
        else:
            wanted = sorted(set(keys), key=key_number)
            found = [
                hit
                for key in wanted
                if (hit := self.db.execute("select data from issues where key = ?", (key,)).fetchone())
            ]
        return [Issue.model_validate_json(data) for (data,) in found]

    def snapshot(self) -> Snapshot:
        state = self.state()
        if state is None:
            raise FileNotFoundError(f"{self.path} has no synced corpus; run `cubrid-jira-dup sync` first")
        sha = hashlib.sha256()
        for (data,) in self.db.execute("select data from issues order by number"):
            sha.update(data.encode())
            sha.update(b"\n")
        return Snapshot(issues=state.issues, synced_at=state.synced_at, complete=state.complete, sha256=sha.hexdigest())

    def model(self) -> str | None:
        return self._meta("model") if self.path.exists() else None

    def pending(self) -> int:
        """Rows and titles still without a vector."""
        return sum(
            self.db.execute(f"select count(*) from {table} where vector is null").fetchone()[0]
            for table in ("rows", "titles")
        )

    def arrays(self) -> Arrays:
        issues = self.db.execute("select key, created from issues order by number").fetchall()
        keys = [key for key, _ in issues]
        position = {key: n for n, key in enumerate(keys)}
        found = self.db.execute("select id, key, issue_created, written, vector from rows order by id").fetchall()
        row_vectors, row_embedded = vectors_of([r[4] for r in found])
        titles = dict(self.db.execute("select key, vector from titles").fetchall())
        title_vectors, title_embedded = vectors_of([titles.get(key) for key in keys])
        return Arrays(
            keys=keys,
            created=np.array([c for _, c in issues], float),
            row_ids=np.array([r[0] for r in found], np.int64),
            row_issue=np.array([position[r[1]] for r in found], np.int64),
            row_created=np.array([r[2] for r in found], float),
            row_written=np.array([r[3] for r in found], float),
            row_vectors=row_vectors,
            row_embedded=row_embedded,
            title_vectors=title_vectors,
            title_embedded=title_embedded,
        )

    def bm25(self, table: str, tokens: Iterable[str]) -> tuple[np.ndarray, np.ndarray]:
        """FTS5 BM25 of each matching rowid of `rows_fts` or `titles_fts`, higher is better.

        A token repeated n times in the query counts n times, as in the measured method. BM25 is a sum over
        query terms, so terms are queried in groups of equal count and each group's sum is multiplied by it.
        """
        if table not in ("rows_fts", "titles_fts"):
            raise ValueError(table)
        by_count: dict[int, list[str]] = {}
        for term, count in Counter(tokens).items():
            by_count.setdefault(count, []).append(term)
        scores: dict[int, float] = {}
        query = f"select rowid, bm25({table}) from {table} where {table} match ?"
        for count, terms in by_count.items():
            for start in range(0, len(terms), CHUNK_TERMS):
                match = " OR ".join(f'"{t}"' for t in terms[start : start + CHUNK_TERMS])
                for rowid, score in self.db.execute(query, (match,)):
                    scores[rowid] = scores.get(rowid, 0.0) - count * score
        return np.fromiter(scores.keys(), np.int64, len(scores)), np.fromiter(scores.values(), float, len(scores))

    # ---- embedding

    def embed_pending(self, embedder: Embedder, progress: Callable[[str], None] = lambda _: None) -> int:
        """Embed every row and title without a vector, committing each chunk so an interrupt loses little."""
        if self._meta("model") != embedder.model_id:
            with self.db:
                self.db.execute("update rows set vector = NULL")
                self.db.execute("update titles set vector = NULL")
                self._set_meta("model", embedder.model_id)
        pending_rows = self.db.execute("select id, key, ordinal from rows where vector is null").fetchall()
        pending_titles = [k for (k,) in self.db.execute("select key from titles where vector is null")]
        needed = {key for _, key, _ in pending_rows} | set(pending_titles)
        by_key = {issue.key: issue for issue in self.issues(needed)}
        jobs = [("rows", row_id, rows(by_key[key])[ordinal].text) for row_id, key, ordinal in pending_rows]
        jobs += [("titles", key, by_key[key].summary) for key in pending_titles]
        jobs.sort(key=lambda job: len(job[2]))
        started = time.perf_counter()
        for start in range(0, len(jobs), EMBED_CHUNK):
            chunk = jobs[start : start + EMBED_CHUNK]
            vectors = embedder.embed([as_passage(text) for _, _, text in chunk])
            with self.db:
                for (table, ident, _), vector in zip(chunk, vectors, strict=True):
                    column = "id" if table == "rows" else "key"
                    blob = np.asarray(vector, np.float32).tobytes()
                    self.db.execute(f"update {table} set vector = ? where {column} = ?", (blob, ident))
            done = start + len(chunk)
            progress(f"embedded {done}/{len(jobs)} texts in {time.perf_counter() - started:.0f}s")
        return len(jobs)
