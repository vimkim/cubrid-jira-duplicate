#!/usr/bin/env python3
"""Size the post-registration check and the agent-template share. Anonymous reads only.

1. Issues created per calendar day (server time, +0900) over the 90 full days
   before the reference date: median, 90th percentile, maximum.
2. Issues whose description contains "AI-Generated Context" among issues created
   in the 30, 90, and 180 days before the reference time, next to the totals.
3. Issues created per month since 2025-01, with the median description length.

Run from the worktree root:
    python3 -I scripts/measure_volume.py [--now "YYYY-MM-DD HH:MM"] [--refresh]
--now fixes the reference time (default: now, +0900) so a rerun reuses the cached
responses in data/raw/ and prints the same numbers.
"""
import argparse
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cbrd_rest import nearest_rank, parse_created, search, search_all, stats  # noqa: E402

KST = timezone(timedelta(hours=9))
PHRASE = "AI-Generated Context"


def pct(n, d):
    return f"{n}/{d} ({100 * n / d:.1f}%)" if d else "0/0"


def daily_volume(now, refresh):
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=90)
    jql = f'project = CBRD AND created >= "{start:%Y-%m-%d}" AND created < "{end:%Y-%m-%d}" ORDER BY key ASC'
    issues = search_all(jql, "created,issuetype", refresh)
    created = [parse_created(i["fields"]["created"]).astimezone(KST) for i in issues]
    per_day = Counter(c.date() for c in created)
    days = [(start + timedelta(days=n)).date() for n in range(90)]
    counts = sorted(per_day.get(d, 0) for d in days)
    weekday = sorted(per_day.get(d, 0) for d in days if d.weekday() < 5)
    weekend = sorted(per_day.get(d, 0) for d in days if d.weekday() >= 5)
    peak_day = max(days, key=lambda d: per_day.get(d, 0))
    per_hour = Counter(c.replace(minute=0, second=0) for c in created)
    per_minute = Counter(c.replace(second=0) for c in created)
    peak_hour = max(per_hour, key=per_hour.get)
    print(f"## Issues created per day, {days[0]} .. {days[-1]} (90 days, +0900)")
    print(f"- JQL: `{jql}`")
    print(f"- issues: {len(created)}; days: {len(days)} ({len(weekday)} weekdays, {len(weekend)} weekend days)")
    print(f"- all days: median {statistics.median(counts):g}, p90 {nearest_rank(counts, 0.9)},"
          f" max {counts[-1]} ({peak_day}), mean {statistics.mean(counts):.1f}, zero-issue days {counts.count(0)}")
    print(f"- weekdays: median {statistics.median(weekday):g}, p90 {nearest_rank(weekday, 0.9)}, max {weekday[-1]}")
    print(f"- weekend days: median {statistics.median(weekend):g}, p90 {nearest_rank(weekend, 0.9)}, max {weekend[-1]}")
    for recent in (14, 7):
        tail = sorted(per_day.get(d, 0) for d in days[-recent:])
        tail_weekday = sorted(per_day.get(d, 0) for d in days[-recent:] if d.weekday() < 5)
        print(f"- last {recent} days: total {sum(tail)}, median {statistics.median(tail):g}, max {tail[-1]};"
              f" weekdays median {statistics.median(tail_weekday):g}")
    peak_minute = max(per_minute, key=per_minute.get)
    batched = [(c, i) for c, i in zip(created, issues) if per_minute[c.replace(second=0)] >= 2]
    print(f"- busiest clock hour: {per_hour[peak_hour]} issues ({peak_hour:%Y-%m-%d %H}:00);"
          f" busiest minute: {per_minute[peak_minute]} issues ({peak_minute:%Y-%m-%d %H:%M})")
    print(f"- issues created in a minute that holds 2+ issues: {pct(len(batched), len(created))}"
          f" in {sum(1 for v in per_minute.values() if v >= 2)} minutes; their types:"
          f" {dict(Counter(i['fields']['issuetype']['name'] for _, i in batched).most_common())}")
    by_week = Counter(d.isocalendar()[:2] for d in created)
    print("- issues per ISO week:", ", ".join(f"{y}-W{w:02d} {n}" for (y, w), n in sorted(by_week.items())))


def template_share(now, refresh):
    oldest = now - timedelta(days=180)
    jql = f'project = CBRD AND created >= "{oldest:%Y-%m-%d %H:%M}" ORDER BY key ASC'
    issues = search_all(jql, "created,description", refresh)
    print(f"\n## Descriptions containing \"{PHRASE}\" (reference time {now:%Y-%m-%d %H:%M} +0900)")
    print(f"- fetch JQL: `{jql}`; the 30- and 90-day windows are subsets by `created`.")
    print("| window | created | exact substring | case-insensitive | JQL phrase search |")
    print("|---|---|---|---|---|")
    for days in (30, 90, 180):
        cutoff = now - timedelta(days=days)
        sub = [i for i in issues if parse_created(i["fields"]["created"]) >= cutoff]
        descs = [i["fields"].get("description") or "" for i in sub]
        exact = sum(1 for d in descs if PHRASE in d)
        loose = sum(1 for d in descs if PHRASE.lower() in d.lower())
        phrase_jql = f'project = CBRD AND created >= "{cutoff:%Y-%m-%d %H:%M}" AND description ~ "\\"{PHRASE}\\""'
        status, body = search(phrase_jql, "key", 0, refresh=refresh)
        jql_count = body.get("total") if status == 200 else f"error {status}"
        print(f"| last {days} days (since {cutoff:%Y-%m-%d %H:%M}) | {len(sub)} | {pct(exact, len(sub))} |"
              f" {pct(loose, len(sub))} | {jql_count} |")
    print(f"- phrase JQL form: `project = CBRD AND created >= \"<cutoff>\" AND description ~ \"\\\"{PHRASE}\\\"\"`")
    by_month = Counter()
    hits = Counter()
    for i in issues:
        month = i["fields"]["created"][:7]
        by_month[month] += 1
        hits[month] += PHRASE in (i["fields"].get("description") or "")
    print("- by month (template / created):", ", ".join(f"{m} {hits[m]}/{by_month[m]}" for m in sorted(by_month)))


def monthly_profile(now, refresh):
    """Issues created per month since 2025-01 with the median description length,
    to show when the writing style and volume changed."""
    jql = f'project = CBRD AND created >= "2025-01-01" AND created < "{now:%Y-%m-%d %H:%M}" ORDER BY key ASC'
    issues = search_all(jql, "created,description", refresh)
    months = {}
    for i in issues:
        months.setdefault(i["fields"]["created"][:7], []).append(i["fields"].get("description") or "")
    print(f"\n## Issues created per month since 2025-01 (until {now:%Y-%m-%d %H:%M} +0900)")
    print(f"- JQL: `{jql}`")
    print("| month | created | median description chars | descriptions with \"%s\" |" % PHRASE)
    print("|---|---|---|---|")
    for month in sorted(months):
        descs = months[month]
        print(f"| {month} | {len(descs)} | {statistics.median(len(d) for d in descs):.0f} |"
              f" {pct(sum(1 for d in descs if PHRASE in d), len(descs))} |")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", help='reference time "YYYY-MM-DD HH:MM" in +0900 (default: now)')
    parser.add_argument("--refresh", action="store_true", help="ignore cached responses")
    args = parser.parse_args()
    now = (datetime.strptime(args.now, "%Y-%m-%d %H:%M").replace(tzinfo=KST) if args.now
           else datetime.now(KST).replace(second=0, microsecond=0))
    print(f"# Volume measurements, reference time {now:%Y-%m-%d %H:%M} +0900\n")
    daily_volume(now, args.refresh)
    template_share(now, args.refresh)
    monthly_profile(now, args.refresh)
    print(f"\n- requests: network {stats['network']}, cache {stats['cache']}")


if __name__ == "__main__":
    sys.exit(main())
