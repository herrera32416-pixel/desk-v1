#!/usr/bin/env python3
"""Starting-goalie check (free, no key): DailyFaceoff /starting-goalies/<date> -> data/nhl_goalie_log.jsonl.

Run before the board and again before puck drop (workflow 'nhl-goalies'). Each team gets one row per check with
status Confirmed / Likely / Unconfirmed exactly as DailyFaceoff labels it (never upgraded). paper_model reads only
Confirmed/Likely rows; anything else falls back to the recent-starter mix and the NHL row is FLAGGED."""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "nhl_goalie_log.jsonl"
sys.path.insert(0, str(ROOT / "scripts"))
from paper_model import NHL_ABBR  # noqa: E402

URL = "https://www.dailyfaceoff.com/starting-goalies/{d}"


def fetch(day: str) -> list[dict]:
    req = urllib.request.Request(URL.format(d=day), headers={"User-Agent": "Mozilla/5.0 (desk paper board)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        s = r.read().decode("utf-8", "replace")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', s, re.S)
    return (json.loads(m.group(1))["props"]["pageProps"].get("data") or []) if m else []


def norm_status(x: str | None) -> str:
    x = (x or "").strip().lower()
    return "Confirmed" if x.startswith("confirm") else "Likely" if x.startswith("likely") else "Unconfirmed"


def main(days: int = 2) -> int:
    now = datetime.now(CT)
    rows = []
    for i in range(days):
        day = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            games = fetch(day)
        except Exception as e:  # noqa: BLE001
            print(f"[goalies] {day}: DailyFaceoff fetch failed ({type(e).__name__}) — no rows, board flags goalies", file=sys.stderr)
            continue
        for g in games:
            try:
                start = datetime.fromisoformat(str(g.get("dateGmt")).replace("Z", "+00:00")).astimezone(CT)
            except Exception:
                start = None
            for side in ("home", "away"):
                team = NHL_ABBR.get(g.get(f"{side}TeamName") or "")
                name = g.get(f"{side}GoalieName")
                if not team or not name:
                    continue
                rows.append({"game_id": g.get("id"), "team": team, "goalie": name, "source": "dailyfaceoff",
                             "status": norm_status(g.get(f"{side}NewsStrengthName")),
                             "seen_time_ct": now.isoformat(timespec="seconds"), "source_url": URL.format(d=day),
                             "game_start_ct": start.isoformat(timespec="seconds") if start else f"{day}T00:00:00-05:00",
                             "news": (g.get(f"{side}NewsDetails") or "")[:200]})
    if rows:
        with open(LOG, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    c = {s: sum(1 for r in rows if r["status"] == s) for s in ("Confirmed", "Likely", "Unconfirmed")}
    print(f"[goalies] {len(rows)} rows logged {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
