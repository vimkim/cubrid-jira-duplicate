"""The `cubrid-jira-dup` command line."""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from cbrd_dup import corpus, evaluate, search
from cbrd_dup.embed import Embedder, FastEmbedder, default_threads
from cbrd_dup.index import Index, default_index_dir
from cbrd_dup.jira import JiraClient, JiraError
from cbrd_dup.measure import baseline as baseline_measure
from cbrd_dup.measure import corpus as corpus_measure
from cbrd_dup.measure import gold as gold_measure
from cbrd_dup.measure import gold_stats as gold_stats_measure
from cbrd_dup.measure import probe as probe_measure
from cbrd_dup.measure import volume as volume_measure
from cbrd_dup.models import BaselineResult, GoldPair
from cbrd_dup.store import DataDir, read_jsonl, write_text_atomic

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Duplicate-issue checker PoC for the CBRD JIRA project (CBRD-27978).",
)
measure = typer.Typer(
    no_args_is_help=True,
    help="Reproduce the measurements behind docs/evidence.md. All reads are anonymous and cached.",
)
app.add_typer(measure, name="measure")
eval_app = typer.Typer(no_args_is_help=True, help="Measure search methods offline against the gold pairs.")
app.add_typer(eval_app, name="eval")

DataDirOption = Annotated[
    Path,
    typer.Option("--data-dir", help="Directory holding the JSONL files and the raw response cache (raw/)."),
]
IndexDirOption = Annotated[
    Path | None,
    typer.Option(
        "--index-dir",
        help="Directory of the local index and model (default: $CUBRID_JIRA_DUP_HOME, else ~/.cache/cubrid-jira-dup).",
    ),
]
ThreadsOption = Annotated[
    int | None,
    typer.Option("--threads", min=1, help="CPU threads for embedding (default: half the logical CPUs)."),
]
RefreshOption = Annotated[bool, typer.Option("--refresh", help="Ignore cached responses and ask JIRA again.")]


@contextmanager
def jira_session(data_dir: Path, refresh: bool) -> Iterator[tuple[JiraClient, DataDir]]:
    data = DataDir(data_dir)
    try:
        with JiraClient(data.raw, refresh=refresh) as client:
            yield client, data
    except JiraError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err


def jira_client(cache_dir: Path) -> JiraClient:
    """The client every index sync uses; a seam for tests."""
    return JiraClient(cache_dir, refresh=True)


def make_embedder(index: Index, threads: int | None) -> Embedder:
    """The model every index embedding uses; a seam for tests."""
    return FastEmbedder(index.models, threads or default_threads())


def progress(message: str) -> None:
    typer.echo(message, err=True)


def require_gold(data: DataDir) -> None:
    if not data.gold.exists():
        typer.echo(f"error: {data.gold} not found; run `cubrid-jira-dup measure gold` first.", err=True)
        raise typer.Exit(1)


@measure.command("corpus")
def corpus_command(data_dir: DataDirOption = Path("data"), refresh: RefreshOption = False) -> None:
    """Corpus size, creation rate, labeled duplicates, language and size."""
    with jira_session(data_dir, refresh) as (client, _):
        corpus_measure.run(client)


@measure.command("gold")
def gold_command(data_dir: DataDirOption = Path("data"), refresh: RefreshOption = False) -> None:
    """Build data/gold-pairs.jsonl from issues resolved as Duplicate."""
    with jira_session(data_dir, refresh) as (client, data):
        gold_measure.run(client, data)


@measure.command("probe")
def probe_command(data_dir: DataDirOption = Path("data"), refresh: RefreshOption = False) -> None:
    """Probe how JIRA evaluates and orders `text ~` searches."""
    with jira_session(data_dir, refresh) as (client, _):
        probe_measure.run(client)


@measure.command("baseline")
def baseline_command(data_dir: DataDirOption = Path("data"), refresh: RefreshOption = False) -> None:
    """Rank the gold candidates in today's JQL text search; writes data/baseline-results.jsonl."""
    require_gold(DataDir(data_dir))
    with jira_session(data_dir, refresh) as (client, data):
        baseline_measure.run(client, data)


@measure.command("gold-stats")
def gold_stats_command(data_dir: DataDirOption = Path("data")) -> None:
    """Time gap and language mix of the gold pairs (offline)."""
    data = DataDir(data_dir)
    require_gold(data)
    gold_stats_measure.run(data)


@measure.command("volume")
def volume_command(
    data_dir: DataDirOption = Path("data"),
    refresh: RefreshOption = False,
    now: Annotated[
        datetime | None,
        typer.Option(
            parser=volume_measure.parse_now,
            metavar="YYYY-MM-DD HH:MM",
            help="Reference time in +0900. Fix it so a rerun reuses the cache (default: now).",
        ),
    ] = None,
) -> None:
    """Issues per day, the AI-Generated Context template share, and issues per month."""
    with jira_session(data_dir, refresh) as (client, _):
        volume_measure.run(client, now or volume_measure.current_minute())


@app.command("sync")
def sync_command(
    index_dir: IndexDirOption = None,
    full: Annotated[
        bool, typer.Option("--full", help="Refetch every issue and drop the ones JIRA no longer shows.")
    ] = False,
    threads: ThreadsOption = None,
) -> None:
    """Copy the CBRD issues and comments into the local index (anonymous reads), then embed new text."""
    index = Index(index_dir or default_index_dir())
    clock = time.perf_counter()
    try:
        with jira_client(index.raw) as client:
            state, fetched, changed = corpus.sync(client, index, datetime.now(UTC), full)
    except JiraError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err
    fetched_s = time.perf_counter() - clock
    embedded = index.embed_pending(make_embedder(index, threads), progress)
    typer.echo(f"index: {index.root}")
    typer.echo(f"issues: {state.issues} (fetched {fetched}, changed {changed}) in {fetched_s:.1f}s")
    typer.echo(f"embedded: {embedded} texts in {time.perf_counter() - clock - fetched_s:.1f}s")
    typer.echo(f"complete: {'yes' if state.complete else 'no'}")
    for error in state.errors:
        typer.echo(f"error: {error}", err=True)


@eval_app.command("candidates")
def eval_candidates_command(
    index_dir: IndexDirOption = None,
    data_dir: DataDirOption = Path("data"),
    method: Annotated[str, typer.Option("--method", help=f"One of: {', '.join(search.METHODS)}.")] = "hybrid",
    mask: Annotated[
        str, typer.Option("--mask", help="Hide CBRD-<n> keys only (keys) or also identifiers like cbrd_27516.")
    ] = "identifiers",
    threads: ThreadsOption = None,
    out: Annotated[Path | None, typer.Option("--out", help="Also write the run and its metric tables as JSON.")] = None,
) -> None:
    """Recall@10 and @20 of a candidate search on the gold pairs, from the local index (offline)."""
    if method not in search.METHODS:
        raise typer.BadParameter(
            f"unknown method {method!r}; choose from {', '.join(search.METHODS)}", param_hint="--method"
        )
    if mask not in evaluate.MASKS:
        raise typer.BadParameter(f"unknown mask {mask!r}; choose from {', '.join(evaluate.MASKS)}", param_hint="--mask")
    index = Index(index_dir or default_index_dir())
    data = DataDir(data_dir)
    require_gold(data)
    if not index.exists():
        typer.echo(f"error: no index in {index.root}; run `cubrid-jira-dup sync` first.", err=True)
        raise typer.Exit(1)
    chosen = search.METHODS[method]
    embedder = make_embedder(index, threads) if chosen.dense else None
    if embedder is not None and (index.pending() or index.model() != embedder.model_id):
        message = f"error: {index.root} lacks {embedder.model_id} vectors; run `cubrid-jira-dup sync` first."
        typer.echo(message, err=True)
        raise typer.Exit(1)
    issues = index.issues()
    by_key = {i.key: i for i in issues}
    pairs = read_jsonl(data.gold, GoldPair)
    baseline = read_jsonl(data.baseline, BaselineResult) if data.baseline.exists() else []
    snapshot = index.snapshot()

    searcher = search.Searcher(index, chosen, embedder)
    sides = evaluate.split(pairs)
    queries = evaluate.eval_queries(pairs, by_key, evaluate.MASKS[mask])
    run = evaluate.evaluate(searcher, queries, by_key, name=method)
    typer.echo(evaluate.report(run, pairs, sides, baseline, snapshot, mask))
    if out is not None:
        document = evaluate.document(run, pairs, sides, baseline, snapshot, searcher.config | {"mask": mask})
        write_text_atomic(out, json.dumps(document, ensure_ascii=False, indent=1) + "\n")
