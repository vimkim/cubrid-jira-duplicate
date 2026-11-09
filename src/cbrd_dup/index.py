
import hashlib
import json
import os
import sqlite3
import time
from array import array
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel

from cbrd_dup.corpus import Issue, rows
from cbrd_dup.embed import DIM, Embedder, as_passage
from cbrd_dup.jira import key_number
from cbrd_dup.text import tokenize

SCHEMA = "2"
HOME_ENV = "CUBRID_JIRA_DUP_HOME"
EMBED_CHUNK = 512
Field = Literal["rows", "titles"]
FIELDS: tuple[Field, ...] = ("rows", "titles")

TABLES = """
create table if not exists meta(name text primary key, value text not null);
create table if not exists issues(
    key text primary key, number integer not null unique, created real not null, updated real not null,
    data text not null);
create table if not exists rows(
    id integer primary key, key text not null, ordinal integer not null, kind text not null,
    issue_created real not null, written real not null, digest text not null, tokens integer not null, vector blob);
create index if not exists rows_by_key on rows(key);
create table if not exists titles(key text primary key, digest text not null, tokens integer not null, vector blob);
create table if not exists rows_postings(term text primary key, docs blob not null, tfs blob not null) without rowid;
create table if not exists titles_postings(term text primary key, docs blob not null, tfs blob not null) without rowid;
"""


class SyncState(BaseModel):
    synced_at: datetime | None
    complete: bool
    issues: int
    errors: list[str]


class Snapshot(BaseModel):

    issues: int
    synced_at: datetime | None
    complete: bool
    sha256: str


@dataclass(frozen=True)
class Arrays:

    keys: list[str]
    created: np.ndarray
    row_ids: np.ndarray
    row_issue: np.ndarray
    row_created: np.ndarray
    row_written: np.ndarray
    row_tokens: np.ndarray
    row_vectors: np.ndarray
    row_embedded: np.ndarray
    title_tokens: np.ndarray
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


def schema_of(db: sqlite3.Connection) -> str | None:
    if db.execute("select 1 from sqlite_master where name = 'meta'").fetchone() is None:
        return None
    found = db.execute("select value from meta where name = 'schema'").fetchone()
    return found[0] if found else None


def vectors_of(blobs: list[bytes | None]) -> tuple[np.ndarray, np.ndarray]:
    present = np.array([b is not None for b in blobs], bool)
    empty = bytes(4 * DIM)
    joined = b"".join(empty if b is None else b for b in blobs)
    return np.frombuffer(joined, np.float32).reshape(len(blobs), DIM), present


class Postings:

    def __init__(self, db: sqlite3.Connection, rebuild: bool):
        self.db, self.rebuild = db, rebuild
        self.removed: dict[Field, set[int]] = {f: set() for f in FIELDS}
        self.touched: dict[Field, set[str]] = {f: set() for f in FIELDS}
        self.added: dict[Field, defaultdict[str, tuple[array, array]]] = {
            f: defaultdict(lambda: (array("i"), array("i"))) for f in FIELDS
        }

    def remove(self, field: Field, doc: int, tokens: list[str]) -> None:
        self.removed[field].add(doc)
        self.touched[field].update(tokens)

    def add(self, field: Field, doc: int, tokens: list[str]) -> None:
        for term, tf in Counter(tokens).items():
            docs, tfs = self.added[field][term]
            docs.append(doc)
            tfs.append(tf)

    def write(self) -> None:
        empty = np.zeros(0, np.int32)
        for field in FIELDS:
            table = f"{field}_postings"
            if self.rebuild:
                self.db.execute(f"delete from {table}")
            removed = np.fromiter(self.removed[field], np.int32)
            added = self.added[field]
            for term in self.touched[field] | added.keys():
                found = self.db.execute(f"select docs, tfs from {table} where term = ?", (term,)).fetchone()
                docs, tfs = (np.frombuffer(blob, np.int32) for blob in found) if found else (empty, empty)
                keep = ~np.isin(docs, removed)
                new_docs, new_tfs = added.get(term, (empty, empty))
                docs = np.concatenate([docs[keep], np.asarray(new_docs, np.int32)])
                tfs = np.concatenate([tfs[keep], np.asarray(new_tfs, np.int32)])
                if docs.size:
                    self.db.execute(
                        f"insert or replace into {table} values (?, ?, ?)", (term, docs.tobytes(), tfs.tobytes())
                    )
                else:
                    self.db.execute(f"delete from {table} where term = ?", (term,))


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
            db = sqlite3.connect(self.path)
            if schema_of(db) not in (None, SCHEMA):
                db.close()
                for path in (self.path, self.path.with_name(self.path.name + "-journal")):
                    path.unlink(missing_ok=True)
                db = sqlite3.connect(self.path)
            db.execute("pragma synchronous = normal")
            with db:
                db.executescript(TABLES)
                db.execute("insert or replace into meta values ('schema', ?)", (SCHEMA,))
            self._db = db
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _meta(self, name: str) -> str | None:
        found = self.db.execute("select value from meta where name = ?", (name,)).fetchone()
        return found[0] if found else None

    def _set_meta(self, name: str, value: str) -> None:
        self.db.execute("insert or replace into meta values (?, ?)", (name, value))


    def state(self) -> SyncState | None:
        if not self.path.exists():
            return None
        complete = self._meta("complete")
        if complete is None:
            return None
        synced_at = self._meta("synced_at")
        return SyncState(
            synced_at=datetime.fromisoformat(synced_at) if synced_at else None,
            complete=complete == "1",
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
        changed = 0
        with self.db:
            postings = Postings(self.db, rebuild=replace_all)
            fetched = set()
            for issue in issues:
                if issue.key in fetched:
                    continue
                fetched.add(issue.key)
                changed += self._put(issue, postings)
            if replace_all:
                for (key,) in self.db.execute("select key from issues").fetchall():
                    if key not in fetched:
                        self._delete(key)
                        changed += 1
            postings.write()
            if not errors:
                self._set_meta("synced_at", synced_at.isoformat())
            self._set_meta("complete", "1" if not errors else "0")
            self._set_meta("errors", json.dumps(errors, ensure_ascii=False))
        return changed

    def _put(self, issue: Issue, postings: Postings) -> bool:
        data = issue.model_dump_json()
        number = key_number(issue.key)
        old = self.db.execute("select data from issues where key = ?", (issue.key,)).fetchone()
        old_rows = self.db.execute("select id, digest, vector from rows where key = ? order by ordinal", (issue.key,))
        old_rows = old_rows.fetchall()
        if old is not None and old[0] == data:
            if postings.rebuild:
                for (row_id, _, _), row in zip(old_rows, rows(issue), strict=True):
                    postings.add("rows", row_id, tokenize(row.text))
                postings.add("titles", number, tokenize(issue.summary))
            return False
        self.db.execute(
            "insert into issues values (?, ?, ?, ?, ?) on conflict(key) do update set"
            " created = excluded.created, updated = excluded.updated, data = excluded.data",
            (issue.key, number, epoch(issue.created), epoch(issue.updated), data),
        )
        if old is not None and not postings.rebuild:
            previous = Issue.model_validate_json(old[0])
            for (row_id, _, _), row in zip(old_rows, rows(previous), strict=True):
                postings.remove("rows", row_id, tokenize(row.text))
            postings.remove("titles", number, tokenize(previous.summary))
        kept = {row_digest: vector for _, row_digest, vector in old_rows if vector is not None}
        self.db.execute("delete from rows where key = ?", (issue.key,))
        for ordinal, row in enumerate(rows(issue)):
            row_digest = digest(row.text)
            tokens = tokenize(row.text)
            cursor = self.db.execute(
                "insert into rows (key, ordinal, kind, issue_created, written, digest, tokens, vector)"
                " values (?, ?, ?, ?, ?, ?, ?, ?)",
                (issue.key, ordinal, row.kind, epoch(row.issue_created), epoch(row.written), row_digest, len(tokens))
                + (kept.get(row_digest),),
            )
            postings.add("rows", cursor.lastrowid, tokens)
        title_tokens = tokenize(issue.summary)
        postings.add("titles", number, title_tokens)
        title_digest = digest(issue.summary)
        old_title = self.db.execute("select digest from titles where key = ?", (issue.key,)).fetchone()
        if old_title is None or old_title[0] != title_digest:
            self.db.execute(
                "insert or replace into titles values (?, ?, ?, NULL)", (issue.key, title_digest, len(title_tokens))
            )
        return True

    def _delete(self, key: str) -> None:
        self.db.execute("delete from rows where key = ?", (key,))
        self.db.execute("delete from titles where key = ?", (key,))
        self.db.execute("delete from issues where key = ?", (key,))


    def issues(self, keys: Iterable[str] | None = None) -> list[Issue]:
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
        return sum(
            self.db.execute(f"select count(*) from {table} where vector is null").fetchone()[0]
            for table in ("rows", "titles")
        )

    def arrays(self) -> Arrays:
        issues = self.db.execute("select key, created from issues order by number").fetchall()
        keys = [key for key, _ in issues]
        position = {key: n for n, key in enumerate(keys)}
        found = self.db.execute("select id, key, issue_created, written, tokens, vector from rows order by id")
        found = found.fetchall()
        row_vectors, row_embedded = vectors_of([r[5] for r in found])
        titles = {key: (n, vector) for key, n, vector in self.db.execute("select key, tokens, vector from titles")}
        title_vectors, title_embedded = vectors_of([titles[key][1] for key in keys])
        return Arrays(
            keys=keys,
            created=np.array([c for _, c in issues], float),
            row_ids=np.array([r[0] for r in found], np.int64),
            row_issue=np.array([position[r[1]] for r in found], np.int64),
            row_created=np.array([r[2] for r in found], float),
            row_written=np.array([r[3] for r in found], float),
            row_tokens=np.array([r[4] for r in found], np.int64),
            row_vectors=row_vectors,
            row_embedded=row_embedded,
            title_tokens=np.array([titles[key][0] for key in keys], np.int64),
            title_vectors=title_vectors,
            title_embedded=title_embedded,
        )

    def postings(self, field: Field, terms: Iterable[str]) -> Iterator[tuple[str, np.ndarray, np.ndarray]]:
        for term in terms:
            found = self.db.execute(f"select docs, tfs from {field}_postings where term = ?", (term,)).fetchone()
            if found:
                yield term, np.frombuffer(found[0], np.int32), np.frombuffer(found[1], np.int32)


    def embed_pending(self, embedder: Embedder, progress: Callable[[str], None] = lambda _: None) -> int:
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
