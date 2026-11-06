# Brief: turn the measurement scripts into a uv project

Date 2026-11-06. Work only in `/home/vimkim/gh/cubrid-jira-duplicate-uv` (branch `refactor/uv-project`). Public repo: https://github.com/vimkim/cubrid-jira-duplicate-poc.

## User goal

The user wants this repository to be a maintainable uv project that the PoC for a CBRD duplicate-issue checker (CBRD-27978) can grow into. The user asked for three things. Use well-known external libraries where they replace hand-written code. Install the command with `uv tool install`. Provide `just sync` and `just install` recipes that follow the convention of their other tools. The checker itself is out of scope. Another engineer will build it later inside this package.

## Current state

`scripts/` holds seven stand-alone Python files that run with `python3 -I scripts/<name>.py` from the repo root:

- `cbrd_rest.py`: shared anonymous REST client. It sleeps at least 0.25 s between requests and caches raw responses under `data/raw/<SHA-1 of URL>.json`.
- `measure_cbrd.py`: corpus counts. It uses `urllib` directly and has no cache.
- `build_gold.py`: writes `data/gold-pairs.jsonl`.
- `probe_text_order.py`: probes JIRA `text ~` behavior.
- `measure_baseline.py`: writes `data/baseline-results.jsonl`.
- `measure_gold_stats.py`: offline statistics.
- `measure_volume.py`: daily, monthly, and template counts.

`data/raw/` already holds 383 cached responses (ignored by git). With this cache every script except `measure_cbrd.py` runs with zero network requests.

## Target

- `pyproject.toml` for a uv project named `cubrid-jira-duplicate-poc`. Use `requires-python = ">=3.12"`, a src layout with package `cbrd_dup`, and the hatchling build backend. Commit `uv.lock`.
- Console script `cbrd-dup`, with subcommands `cbrd-dup measure corpus|gold|probe|baseline|gold-stats|volume`. Each takes the options its script takes today (`--refresh`, `--now`) plus `--data-dir` (default `./data`).
- Runtime libraries: `httpx` for HTTP, `tenacity` for bounded retries with backoff on timeouts, 429, and 5xx, `typer` for the CLI, and `pydantic` for the two JSONL record types (`GoldPair`, `BaselineResult`). Do not add a library for something a few lines of standard library already do clearly, such as the Wilson interval or nearest-rank percentiles.
- Dev group: `pytest` and `ruff`. Configure ruff in `pyproject.toml`. Format the new code with it.
- One HTTP client module owns anonymous access, pacing, retries, and the file cache. The cache key must stay the SHA-1 of the exact URL string the old code built, so the existing `data/raw/` stays valid. `measure corpus` uses the same client and cache as the rest.
- Offline pytest tests for what can break silently: the JQL sanitizing rules, gold-pair extraction from a small fixture, the Wilson interval, the cache key for a known request, and a CLI smoke test. No test may touch the network.
- `justfile`, following the convention in `/home/vimkim/gh/cubrid-jira/justfile` and `/home/vimkim/gh/csql-future/justfile`:
  - `default` lists recipes.
  - `setup` runs `uv sync`.
  - `test` runs pytest.
  - `check` runs ruff check and pytest.
  - `install` runs `uv tool install --reinstall .` and then checks that `"$(uv tool dir --bin)/cbrd-dup" --help` starts.
  - `sync` does the same as `install`. `daily-update-cubrid` calls `just sync`.
- Delete `scripts/` once the new commands pass the parity check below.
- Update the living docs to the new commands: `README.md`, `docs/evidence.md`, and `docs/requirements.md`. Leave `docs/issue-brief.md`, `docs/briefs/`, `docs/reviews/`, and `docs/jira/` unchanged, because they are dated records. Add one sentence to the README saying those records cite the old `scripts/` paths.

## Parity check (required before you finish)

1. Before changing any code, run every old script with the current cache and save its stdout and the two JSONL files to a directory outside the repo, in your scratchpad. Use `measure_volume.py --now "2026-11-06 17:34"`. Run `measure_cbrd.py` too, though it hits the network.
2. After the refactor, run each new command with the same cache and options. Its stdout must match the old stdout exactly, and `data/gold-pairs.jsonl` and `data/baseline-results.jsonl` must be byte-identical. The cached commands must report zero network requests.
3. `measure corpus` has no old cache, so compare it with an old run made right before it. Report any difference and explain it, for example an issue created between the two runs.
4. `just check` and `just install` must pass, and the installed `cbrd-dup measure gold-stats` must run from the repo root.

## Rules

- Do not commit. Leave all changes in the working tree. The coordinating session reviews the diff and commits.
- Do not push, do not write to JIRA, and do not touch `/home/vimkim/gh/cubrid-jira-duplicate` (the main checkout).
- Network access is limited to anonymous GETs on `http://jira.cubrid.org/rest/api/2/`. Prefer the cache.
- Keep comments to a non-obvious why. No narrating comments.

## Report

Reply with the final module layout, the dependency list with one line on why each is there, the parity results (per command: identical or the exact difference), the `just check` and `just install` results, and anything you could not finish.
