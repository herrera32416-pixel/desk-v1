#!/usr/bin/env python3
"""Once-per-day guard for the daily-desk workflow (added 2026-10-04, weekend eval sec 6.5 / 8 #3).

GitHub cron is best-effort (Sun Oct 4 the 9:15am CT run never fired; ran by hand 10:06am CT), so the
workflow has 3 triggers (9:15, 9:23, 9:41 CT). Every trigger runs `check` first; only the first one
that finds no stamp for today's CT date does the work, and `stamp` is written (and committed with data/)
only after a successful board build. Stdlib only. PAPER ONLY.

  run_guard.py check  [--stamp PATH] [--date YYYY-MM-DD]   -> prints run=true|false (+ GITHUB_OUTPUT)
  run_guard.py stamp  [--stamp PATH] [--date ...] [--source ...]
  run_guard.py alert  [--stamp PATH] [--date ...] [--log PATH] [--repo DIR] [--deadline HH:MM] [--now ISO]
        box-side missed-run alert: if no success is stamped for today by the deadline (default 10:00 CT),
        append one line to the log (once per day). Reads the stamp from origin/main via `git fetch`
        (0 Odds API credits) and also accepts a github-actions "auto board + paper <date> CT" commit.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parent.parent
STAMP = ROOT / "data" / "run_stamp.json"
ALERT_LOG = Path("/workspace/eval/alerts/missed_runs.log")


def today_ct(now: datetime | None = None) -> str:
    return (now or datetime.now(CT)).astimezone(CT).date().isoformat()


def read_stamp_date(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return json.loads(text).get("date_ct")
    except (json.JSONDecodeError, AttributeError):
        return None


def gh_out(**kv):
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def cmd_check(a) -> int:
    d = a.date or today_ct()
    sd = read_stamp_date(Path(a.stamp).read_text() if Path(a.stamp).exists() else None)
    run = sd != d
    print(f"run={'true' if run else 'false'}  (today_ct={d}, stamped={sd})")
    gh_out(run="true" if run else "false")
    return 0


def cmd_stamp(a) -> int:
    d = a.date or today_ct()
    p = Path(a.stamp)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date_ct": d, "stamped_at_ct": datetime.now(CT).isoformat(timespec="seconds"),
                             "source": a.source}, indent=1) + "\n")
    print(f"stamped {d} -> {p}")
    return 0


def remote_evidence(repo: Path, d: str, fetch: bool) -> tuple[bool, str]:
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    if fetch:
        git("fetch", "--quiet", "origin")
    r = git("show", "origin/main:data/run_stamp.json")
    if r.returncode == 0 and read_stamp_date(r.stdout) == d:
        return True, "run_stamp.json"
    r = git("log", "origin/main", "-n", "20", "--format=%an|%s")
    for line in r.stdout.splitlines():
        an, _, s = line.partition("|")
        if an.startswith("github-actions") and s.strip() == f"auto board + paper {d} CT":
            return True, "auto-board commit"
    return False, "none"


def cmd_alert(a) -> int:
    now = datetime.fromisoformat(a.now).astimezone(CT) if a.now else datetime.now(CT)
    d = a.date or today_ct(now)
    hh, mm = map(int, a.deadline.split(":"))
    if (now.hour, now.minute) < (hh, mm):
        print(f"before deadline {a.deadline} CT — no check")
        return 0
    if a.stamp_file:
        ok = read_stamp_date(Path(a.stamp_file).read_text() if Path(a.stamp_file).exists() else None) == d
        how = "local stamp" if ok else "none"
    else:
        ok, how = remote_evidence(Path(a.repo), d, fetch=not a.no_fetch)
    log = Path(a.log)
    if ok:
        print(f"OK: {d} daily-desk success found ({how})")
        return 0
    tag = f"MISSED_RUN daily-desk {d}"
    if log.exists() and tag in log.read_text():
        print(f"already alerted for {d}")
        return 1
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        f.write(f"{now.isoformat(timespec='seconds')} {tag}: no successful run stamped by {a.deadline} CT "
                f"(triggers 9:15/9:23/9:41 CT). Run manually: Actions > daily-desk > Run workflow. PAPER ONLY.\n")
    print(f"ALERT written to {log}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "stamp", "alert"])
    ap.add_argument("--stamp", default=str(STAMP))
    ap.add_argument("--stamp-file", help="alert: read this local stamp instead of origin/main (tests)")
    ap.add_argument("--date")
    ap.add_argument("--source", default=os.environ.get("GITHUB_EVENT_NAME", "manual"))
    ap.add_argument("--log", default=str(ALERT_LOG))
    ap.add_argument("--repo", default=str(ROOT))
    ap.add_argument("--deadline", default="10:00")
    ap.add_argument("--now")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    return {"check": cmd_check, "stamp": cmd_stamp, "alert": cmd_alert}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
