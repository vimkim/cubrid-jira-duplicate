# Evidence brief: numbers that set the duplicate-checker requirements

Date 2026-11-06. Work in `/home/vimkim/gh/cubrid-jira-duplicate-requirements` (branch `docs/requirements`).

## User goal

CBRD issues are created faster than one person can read them, so duplicates slip in. The user wants a tool that shows people and agents the existing CBRD issues related to a draft before they register it, and that also checks newly registered issues afterwards. The triage issue is [CBRD-27978](http://jira.cubrid.org/browse/CBRD-27978). Another session is writing the requirements document. Your job is to measure the facts that set its numbers. Do not design or build the checker.

## Context you can rely on

- `docs/issue-brief.md` and `scripts/measure_cbrd.py` hold the corpus facts measured so far. Read both first.
- The public REST endpoint `http://jira.cubrid.org/rest/api/2/search` works without credentials and returns up to 1,000 issues per request, with `description`, `comment`, and `issuelinks` when you ask for them.
- 249 CBRD issues have `resolution = Duplicate`. Anonymous reads show 112 with a Duplicate or Relates link, 121 that name another CBRD key only in a comment, and 16 with no pointer. 72 have a Duplicate-type link, and 50 of those link to an issue created earlier than themselves.

## Tasks

1. **Build the gold pairs.** Write `scripts/build_gold.py` that writes `data/gold-pairs.jsonl`. One line per (duplicate, candidate original) pair. Record both keys, both `created` timestamps, the provenance (`duplicate_link`, `relates_link`, or `comment_mention`), whether the candidate was created before the duplicate, and whether each summary contains Hangul. For `comment_mention`, also record whether the comment that names the key contains a duplicate cue such as "중복", "duplicate", or "dup". Use anonymous reads only.
2. **Measure the current baseline.** For each pair whose candidate was created before the duplicate, run the search a reporter could do today: `project = CBRD AND text ~ "<words from the duplicate's summary>" AND created < "<duplicate created>"`. Strip JQL special characters and keep the query valid. Record whether the candidate appears in the first 10 results and at what rank. First find out how JIRA orders `text ~` results when the JQL has no `ORDER BY`, and report what you found. Report recall@10 separately for each provenance. Sleep at least 0.2 seconds between requests and cache raw responses under `data/raw/`, which git ignores.
3. **Measure the time gap.** For pairs with an earlier candidate, report the distribution of `duplicate.created - candidate.created`. Give the share under 1 day, under 7 days, and under 30 days. Do the same for pairs whose candidate came later. These numbers decide how fresh the index must be.
4. **Measure the language mix.** Report the share of pairs where exactly one of the two summaries contains Hangul.
5. **Size the post-registration check.** For the last 90 days, report issues created per day as median, 90th percentile, and maximum.
6. **Measure the agent-template share.** Count issues whose description contains the phrase `AI-Generated Context` among issues created in the last 30, 90, and 180 days, next to the totals. A quick check today gave 48 of 400, 75 of 775, and 124 of 1,191.

## Output

- Write `docs/evidence.md` in Korean. Give each number with the exact command or script that regenerates it, the sample size, and any caveat. Separate measured values from your inferences.
- Keep the scripts runnable with `python3 -I scripts/<name>.py` from the worktree root.
- Do not commit. The coordinating session reviews and commits your files.
- Do not write to JIRA, and do not use `--yes` with any `cubrid-jira` command.

When you finish, reply in Korean with the key numbers and the paths of the files you wrote.
