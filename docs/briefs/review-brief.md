# Review brief: requirements for the CBRD duplicate-issue checker

Date 2026-11-06. Work in `/home/vimkim/gh/cubrid-jira-duplicate-requirements` (branch `docs/requirements`).

## User goal

CBRD issues are created faster than one person can read them, so duplicates slip in. The user wants a tool that shows people and agents the existing CBRD issues related to a draft before they register it, and that also checks newly registered issues afterwards. The triage issue is [CBRD-27978](http://jira.cubrid.org/browse/CBRD-27978). Implementation has not started. The requirements document must be precise enough that an engineer can build and verify a PoC from it.

## What to review

`docs/requirements.md`. Supporting material is in `docs/issue-brief.md`, `docs/evidence.md`, `scripts/`, and `data/gold-pairs.jsonl`. You can read CBRD-27978 anonymously with `curl -s 'http://jira.cubrid.org/rest/api/2/issue/CBRD-27978?fields=summary,description'`.

The user made these decisions on 2026-11-06. Do not reopen them unless the evidence shows one cannot work:

- The PoC covers both the pre-registration check and the post-registration check.
- Drafts and candidate issues may be sent to the Anthropic API, paid with a company API key.
- The pass bar is recall@10 of at least 0.8 for candidates and precision of at least 0.8 for `duplicate` verdicts.

## Review questions

1. Is each requirement testable? Name each one that has no observable pass or fail condition, and propose the condition.
2. Do any two requirements contradict each other, or contradict the decision table in section 7?
3. Is any number wrong or unsupported? Check each number against `docs/evidence.md` or by rerunning the scripts.
4. Does the evaluation in section 6 leak the answer or bias the metric? Consider comments, links, description edits after registration, time ordering, and the bias of a gold set that contains only duplicates someone noticed.
5. What requirement is missing that the PoC will need? Consider failure modes, rate limits, and input size limits. Also consider sub-tasks and their parents, how the post-registration check handles pairs where both issues are new, and how the agent reacts to each exit code.
6. Does the document fix an implementation choice that should be left to measurement? Name it.
7. Is any Korean sentence ambiguous or hard to read on first pass?

## Output

- Write `docs/reviews/requirements-review.md`. List findings ranked from most to least severe. For each, give the section or requirement ID, the problem, the evidence, and a concrete proposed fix.
- Do not edit `docs/requirements.md`. Do not commit. Do not write to JIRA.
- Reply with the number of findings by severity and the path of the review file.
