"""The `cbrd-dup` command line."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from cbrd_dup.jira import JiraClient, JiraError
from cbrd_dup.measure import baseline as baseline_measure
from cbrd_dup.measure import corpus as corpus_measure
from cbrd_dup.measure import gold as gold_measure
from cbrd_dup.measure import gold_stats as gold_stats_measure
from cbrd_dup.measure import probe as probe_measure
from cbrd_dup.measure import volume as volume_measure
from cbrd_dup.store import DataDir

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

DataDirOption = Annotated[
    Path,
    typer.Option("--data-dir", help="Directory holding the JSONL files and the raw response cache (raw/)."),
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


def require_gold(data: DataDir) -> None:
    if not data.gold.exists():
        typer.echo(f"error: {data.gold} not found; run `cbrd-dup measure gold` first.", err=True)
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
