#!/usr/bin/env python3
"""
Daily data publish: export the store to CSV, commit only the data files, push to GitHub.

  1. python weather_scraper.py --export <previous month>,<current month>   (EAT months)
     - takes the scraper's run lock; retries a few times if a scrape is mid-run
  2. git add data/exports data/stations.csv   (nothing else is ever staged)
  3. commit ONLY those paths if they changed          ("Data export YYYY-MM-DD: ...")
  4. push origin main if the local branch is ahead     (a failed push is retried on the next run)

Safety rails
  * never force-pushes, never rebases or merges: a diverged remote fails loudly (exit 3)
  * only commits paths under data/exports and data/stations.csv, even if other changes are staged
  * refuses to run off a branch other than `main` or without an `origin` remote (exit 2)
  * credentials come from the user's Git Credential Manager; nothing is stored here

Exit codes: 0 ok / nothing to do, 1 export failed, 2 git precondition failed, 3 commit/push failed.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
EAT = dt.timezone(dt.timedelta(hours=3))
DATA_PATHS = ["data/exports", "data/stations.csv"]
BRANCH = "main"


def run(cmd, timeout=300, check=False):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(cmd)} -> exit {p.returncode}: {(p.stderr or p.stdout).strip()[:400]}")
    return p


def export_months():
    now = dt.datetime.now(EAT)
    cur = now.strftime("%Y-%m")
    prev = (now.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")   # late revisions / month rollover
    return [prev, cur]


def main() -> int:
    months = export_months()

    # --- 2 preconditions
    br = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if br != BRANCH:
        print(f"refusing: on branch '{br}', expected '{BRANCH}'", file=sys.stderr)
        return 2
    if run(["git", "remote", "get-url", "origin"]).returncode:
        print("refusing: no 'origin' remote", file=sys.stderr)
        return 2

    # --- 1 export (retry while a scrape holds the lock)
    out = ""
    for attempt in range(1, 5):
        p = run([sys.executable, "weather_scraper.py", "--export", ",".join(months)])
        out = p.stdout
        if p.returncode:
            print(f"export failed (exit {p.returncode}): {(p.stderr or out).strip()[:400]}", file=sys.stderr)
            return 1
        if "skipped: another run in progress" not in out:
            break
        time.sleep(30)
    else:
        print("export failed: scraper lock held for 2 minutes", file=sys.stderr)
        return 1
    rows = sum(int(m.group(1)) for m in re.finditer(r"exported \d{4}-\d{2}: (\d+) rows", out))

    # --- 3 commit data paths only
    try:
        run(["git", "add", "--"] + DATA_PATHS, check=True)
        changed = run(["git", "diff", "--cached", "--quiet", "--"] + DATA_PATHS).returncode != 0
        if changed:
            stamp = dt.datetime.now(EAT).strftime("%Y-%m-%d")
            run(["git", "commit", "-q", "-m",
                 f"Data export {stamp}: {rows} readings in {' + '.join(months)}",
                 "-m", "Automated by publish_data.py (scheduled task UgandaWeatherPublish).",
                 "--"] + DATA_PATHS, check=True)

        # --- 4 push if ahead
        ahead = run(["git", "rev-list", "--count", f"origin/{BRANCH}..HEAD"], check=True).stdout.strip()
        if ahead == "0":
            print(f"OK  nothing to publish (exports unchanged, nothing unpushed); months={','.join(months)}")
            return 0
        pr = run(["git", "push", "origin", BRANCH], timeout=300)
        if pr.returncode:
            print(f"push failed (commit kept locally, will retry next run): {(pr.stderr or pr.stdout).strip()[:400]}",
                  file=sys.stderr)
            return 3
        head = run(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()
        print(f"OK  pushed {ahead} commit(s) to origin/{BRANCH} @ {head}; exported {rows} readings ({', '.join(months)})")
        return 0
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"git step failed: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
