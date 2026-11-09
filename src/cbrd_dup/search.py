"""Candidate search over the local index, chosen by recall on the gold pairs (docs/requirements.md 7.4).

Each channel ranks issues on its own, after the time cutoff and the excluded key:

| channel | ranks issues by |
|---|---|
| L | BM25 of the best row (body or comment) for the whole query text |
| D | e5-small cosine of the best row for the whole query text |
| T | BM25 of the summary for the query title |
| TD | e5-small cosine of the summary for the query title |
| S | BM25 of the best row for the top two call-stack function names of the query, if it has a stack |

BM25 is Lucene's, as the measured bm25s library computes it, over the `text.tokenize` tokens of the whole
index: idf = ln(1 + (N - df + 0.5) / (df + 0.5)), term score = idf * tf / (tf + k1 * (1 - b + b * dl / avgdl)),
summed over the query tokens with repetition. N, df, and avgdl count every stored row (or summary), so the
statistics do not depend on the query time.

The method fuses the top `depth` issues of each channel with weighted reciprocal rank fusion and adds a
boost for issues created near the query time, before or after it. Ties go to the lower key number.
"""

import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import numpy as np

from cbrd_dup.embed import Embedder, as_query
from cbrd_dup.evaluate import Query
from cbrd_dup.index import Field, Index
from cbrd_dup.jira import key_number
from cbrd_dup.text import stack_functions, tokenize

NEG = -np.inf
DAY = 86400.0
DENSE = ("D", "TD")
K1 = 1.5
B = 0.75


@dataclass(frozen=True)
class Method:
    name: str
    weights: dict[str, float]
    recency: float = 0.0
    rrf_k: int = 10
    depth: int = 200
    recency_days: float = 7.0
    stack_frames: int = 2

    @property
    def dense(self) -> bool:
        return any(channel in self.weights for channel in DENSE)

    def config(self, model_id: str | None) -> dict:
        return asdict(self) | {
            "model": model_id if self.dense else None,
            "lexical": f"lucene bm25 as in bm25s, k1 {K1}, b {B}, text.tokenize tokens, repeated query tokens count",
            "fusion": "sum(w / (rrf_k + rank)) + recency / (1 + age_days / recency_days) / (rrf_k + 1)",
        }


METHODS = {
    "hybrid": Method("hybrid", {"L": 2, "D": 1, "T": 1, "TD": 1, "S": 4}, recency=2.0),
    "bm25": Method("bm25", {"L": 1}),
}


@dataclass(frozen=True)
class Ranking:
    keys: list[str]
    channel_ranks: dict[str, dict[str, int]]
    timings_ms: dict[str, float] = field(default_factory=dict)


def top(scores: np.ndarray, n: int) -> np.ndarray:
    """Positions of the n best finite scores; ties keep issue order, which is key-number order."""
    order = np.argsort(-scores, kind="stable")[:n]
    return order[np.isfinite(scores[order])]


class Searcher:
    """Runs one `Method` over an `Index`. Load once, then call `rank` or `search` per query."""

    def __init__(
        self,
        index: Index,
        method: Method,
        embedder: Embedder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if method.dense and embedder is None:
            raise ValueError(f"method {method.name} needs an embedder")
        self.index, self.method, self.embedder, self.clock = index, method, embedder, clock
        self.arrays = index.arrays()
        self.position = self.arrays.position
        self.numbers = np.array([key_number(key) for key in self.arrays.keys], np.int64)
        self.config = method.config(embedder.model_id if embedder else None)

    def search(self, query: Query, k: int) -> list[str]:
        return self.rank(query, k).keys

    def rank(self, query: Query, k: int) -> Ranking:
        a, m = self.arrays, self.method
        cutoff = np.inf if query.before is None else query.before.timestamp()
        excluded = self.position.get(query.exclude, -1) if query.exclude else -1
        issue_ok = (a.created < cutoff) & (np.arange(len(a.keys)) != excluded)
        row_ok = (a.row_created < cutoff) & (a.row_written < cutoff) & (a.row_issue != excluded)
        text = f"{query.title}\n\n{query.body}"

        started = time.perf_counter()
        if m.dense:
            text_vector, title_vector = self.embedder.embed([as_query(text), as_query(query.title)])
        embedded = time.perf_counter()

        scores: dict[str, np.ndarray] = {}
        if "L" in m.weights:
            scores["L"] = self._rows_bm25(tokenize(text), row_ok)
        if "D" in m.weights:
            scores["D"] = self._best_row(a.row_vectors @ text_vector, row_ok & a.row_embedded)
        if "T" in m.weights:
            scores["T"] = self._titles_bm25(tokenize(query.title), issue_ok)
        if "TD" in m.weights:
            scores["TD"] = np.where(issue_ok & a.title_embedded, a.title_vectors @ title_vector, NEG)
        if "S" in m.weights and (functions := stack_functions(query.body, m.stack_frames)):
            scores["S"] = self._rows_bm25(tokenize(" ".join(functions)), row_ok)

        fused = np.zeros(len(a.keys))
        channel_ranks = {}
        for channel, channel_scores in scores.items():
            order = top(channel_scores, m.depth)
            fused[order] += m.weights[channel] / (m.rrf_k + np.arange(1, len(order) + 1))
            channel_ranks[channel] = {a.keys[i]: rank for rank, i in enumerate(order.tolist(), 1)}
        fused[fused == 0] = NEG
        if m.recency:
            anchor = (query.before or self.clock()).timestamp()
            age_days = np.abs(anchor - a.created) / DAY
            fused += m.recency / (1 + age_days / m.recency_days) / (m.rrf_k + 1)
        keys = [a.keys[i] for i in top(fused, k)]
        timings = {"embed": (embedded - started) * 1000, "search": (time.perf_counter() - embedded) * 1000}
        return Ranking(keys, channel_ranks, timings)

    def _best_row(self, row_scores: np.ndarray, mask: np.ndarray) -> np.ndarray:
        out = np.full(len(self.arrays.keys), NEG)
        rows = np.nonzero(mask)[0]
        np.maximum.at(out, self.arrays.row_issue[rows], row_scores[rows])
        return out

    def _rows_bm25(self, tokens: list[str], row_ok: np.ndarray) -> np.ndarray:
        row_scores = self.bm25("rows", tokens)
        return self._best_row(row_scores, row_ok & (row_scores > 0))

    def _titles_bm25(self, tokens: list[str], issue_ok: np.ndarray) -> np.ndarray:
        scores = self.bm25("titles", tokens)
        return np.where(issue_ok & (scores > 0), scores, NEG)

    def bm25(self, field: Field, tokens: list[str]) -> np.ndarray:
        """The BM25 of every row (in `arrays.row_ids` order) or every summary (in issue order) for `tokens`."""
        a = self.arrays
        ids, lengths = (a.row_ids, a.row_tokens) if field == "rows" else (self.numbers, a.title_tokens)
        n, avgdl = len(ids), lengths.mean() if len(ids) else 1.0
        counts = Counter(tokens)
        positions, weights = [], []
        for term, docs, tfs in self.index.postings(field, counts):
            idf = math.log(1 + (n - len(docs) + 0.5) / (len(docs) + 0.5))
            at = np.searchsorted(ids, docs)
            # A sync in another process may have added documents after this searcher loaded the arrays.
            known = (at < n) & (ids[at.clip(max=n - 1)] == docs)
            at, tf = at[known], tfs[known]
            positions.append(at)
            weights.append(counts[term] * idf * tf / (tf + K1 * (1 - B + B * lengths[at] / avgdl)))
        if not positions:
            return np.zeros(n)
        return np.bincount(np.concatenate(positions), np.concatenate(weights), minlength=n)
