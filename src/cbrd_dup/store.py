"""Where the data files live and how JSONL records are read and written."""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel


@dataclass(frozen=True)
class DataDir:
    root: Path

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def gold(self) -> Path:
        return self.root / "gold-pairs.jsonl"

    @property
    def baseline(self) -> Path:
        return self.root / "baseline-results.jsonl"


def show(path: Path) -> str:
    """The path as printed in reports: relative to the working directory when it lies below it."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def write_text_atomic(path: Path, text: str) -> None:
    """Write through a temporary file and rename, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(text, encoding="utf-8")
    partial.replace(path)


def read_jsonl[T: BaseModel](path: Path, model: type[T]) -> list[T]:
    with path.open(encoding="utf-8") as fh:
        return [model.model_validate_json(line) for line in fh if line.strip()]


def write_jsonl(path: Path, records: Iterable[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")
