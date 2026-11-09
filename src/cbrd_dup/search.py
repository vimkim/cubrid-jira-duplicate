"""Candidate search methods. Each is a factory from the corpus to a `Searcher`, registered in METHODS."""

from collections.abc import Callable

import bm25s

from cbrd_dup.corpus import Issue, rows
from cbrd_dup.evaluate import Query, Searcher
from cbrd_dup.jira import key_number
from cbrd_dup.text import tokenize


class Bm25Searcher:
    """Lexical BM25 over every row of every issue; an issue scores as its best visible row."""

    config = {
        "library": f"bm25s {bm25s.__version__}",
        "variant": "lucene",
        "k1": 1.5,
        "b": 0.75,
        "tokenizer": "tokenize",
    }

    def __init__(self, issues: list[Issue]):
        self._rows = [row for issue in issues for row in rows(issue)]
        self._retriever = bm25s.BM25()
        self._retriever.index([tokenize(row.text) for row in self._rows], show_progress=False)

    def search(self, query: Query, k: int) -> list[str]:
        scores = self._retriever.get_scores(tokenize(f"{query.title}\n\n{query.body}"))
        best: dict[str, float] = {}
        for i in scores.nonzero()[0].tolist():
            row = self._rows[i]
            if row.key != query.exclude and row.visible_before(query.before):
                best[row.key] = max(best.get(row.key, 0.0), float(scores[i]))
        ranked = sorted(best, key=lambda key: (-best[key], key_number(key)))
        return ranked[:k]


METHODS: dict[str, Callable[[list[Issue]], Searcher]] = {"bm25": Bm25Searcher}
