"""Sentence vectors from multilingual-e5-small on the CPU, behind a protocol so tests inject a fake.

The model runs through fastembed and onnxruntime, without torch. It is downloaded once, on first use,
into the directory the caller names (about 465 MB). e5 expects "query: " and "passage: " prefixes;
texts are cut at MAX_CHARS characters before tokenizing, and the tokenizer stops at 512 tokens.
"""

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np

MODEL_ID = "intfloat/multilingual-e5-small"
MODEL_FILE = "onnx/model.onnx"
DIM = 384
MAX_CHARS = 4000
# A batch of 32 texts at 512 tokens held about 1.8 GB of attention buffers at peak; 8 holds a quarter of that and
# embedded the corpus as fast on 16 threads (measured 2026-11-09).
BATCH = 8
QUERY_THREADS = 4


class Embedder(Protocol):
    model_id: str

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """One unit-length float32 row of width DIM per text, in input order."""
        ...


def as_query(text: str) -> str:
    return "query: " + text[:MAX_CHARS]


def as_passage(text: str) -> str:
    return "passage: " + text[:MAX_CHARS]


def default_threads() -> int:
    return max(1, (os.cpu_count() or 2) // 2)


def query_threads(threads: int | None) -> int:
    """Threads for embedding one query: at most QUERY_THREADS, since more add CPU time but little speed."""
    return min(threads or QUERY_THREADS, QUERY_THREADS)


class FastEmbedder:
    model_id = MODEL_ID

    def __init__(self, cache_dir: Path, threads: int):
        self.cache_dir = cache_dir
        self.threads = threads
        self._model = None

    def _load(self):
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType

        name = f"{MODEL_ID}-fp32"
        if name not in {m["model"] for m in TextEmbedding.list_supported_models()}:
            TextEmbedding.add_custom_model(
                model=name,
                pooling=PoolingType.MEAN,
                normalization=True,
                sources=ModelSource(hf=MODEL_ID),
                dim=DIM,
                model_file=MODEL_FILE,
            )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return TextEmbedding(name, cache_dir=str(self.cache_dir), threads=self.threads)

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIM), np.float32)
        if self._model is None:
            self._model = self._load()
        # Batches of similar length pad less; padding dominates the cost of long, uneven texts.
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        vectors = np.empty((len(texts), DIM), np.float32)
        for slot, vector in zip(order, self._model.embed([texts[i] for i in order], batch_size=BATCH), strict=True):
            vectors[slot] = vector
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True).clip(1e-12)
