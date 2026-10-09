#!/usr/bin/env python3
"""Pre-puck NHL refresh (0 Odds API credits): re-check starting goalies (DailyFaceoff), then recompute the NHL
research block for today's NHL rows from the lines saved by the morning board (data/nhl_events_today.json).
Prices are never re-pulled or changed here; the market no-vig headline is unchanged; only goalies / flag /
research model line update. No stamps (NHL w is locked at 0)."""
import json, sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import nhl_goalies, paper_model  # noqa: E402

CT = ZoneInfo("America/Chicago")


def main():
    now = datetime.now(CT)
    nhl_goalies.main()
    today = ROOT / "data" / "today.json"
    evf = ROOT / "data" / "nhl_events_today.json"
    if not today.exists() or not evf.exists():
        print("[nhl_refresh] no board/events yet — goalies logged only"); return 0
    doc = json.loads(today.read_text()); ev = json.loads(evf.read_text())
    if doc.get("slate_date_ct") != now.date().isoformat() or ev.get("slate_date_ct") != doc.get("slate_date_ct"):
        print("[nhl_refresh] board is not today's — goalies logged only"); return 0
    by_id = {e.get("id"): e for e in ev.get("events") or []}
    card = doc.get("card") or {}
    rows = [r for k in ("clears", "fills", "holds") for r in card.get(k) or [] if r.get("sport") == "NHL" and r.get("auto")]
    rows = [r for r in rows if datetime.fromisoformat(r["kick_utc"].replace("Z", "+00:00")) > now]  # not started
    if not rows:
        print("[nhl_refresh] no upcoming NHL rows"); return 0
    stats = paper_model.attach_paper("NHL", rows, by_id, now, 2)
    for r in rows:
        r["paper"]["refreshed_ct"] = now.isoformat(timespec="seconds")
    blob = json.dumps(doc, indent=2, ensure_ascii=False)
    today.write_text(blob, encoding="utf-8")
    (ROOT / "data" / f"{now:%Y%m%d}.json").write_text(blob, encoding="utf-8")
    print(f"[nhl_refresh] {len(rows)} NHL rows refreshed · flags {sum(1 for r in rows if r['paper'].get('goalie_flag'))} · {stats.get('goalies_shown')} goalies shown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
