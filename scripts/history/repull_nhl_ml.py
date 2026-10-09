#!/usr/bin/env python3
"""Re-pull NHL moneylines for 2021-22..2023-24 (data/history/nhl/2021-2023.csv). 2026-10-09.

Why: the stored DraftKings pairs from ESPN core /odds are corrupt (both sides sum to ~0.83 implied; e.g. away +235 /
home -115). ESPN core lists many books per game; several are internally inconsistent. Fix: for each game take the
FIRST provider, in priority order, whose two ML sides form a sane market (implied sum 1.00-1.10). Priority:
ESPN BET > Caesars Sportsbook > MGM > Westgate > any other sane book. No sane book -> ml_home/ml_away blank and
ml_status=excluded (never guessed). Originals kept in ml_home_raw / ml_away_raw. Uses 'close' when the provider
has it, else 'current' (the post-game snapshot of the pregame line)."""
import csv, json, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

D = Path(__file__).resolve().parents[2] / "data" / "history" / "nhl"
PRI = ["ESPN BET", "Caesars Sportsbook", "MGM", "Westgate"]


def imp(a):
    a = float(a); return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def ml(side):
    for k in ("close", "current"):
        v = ((side.get(k) or {}).get("moneyLine") or {}).get("american")
        if v not in (None, "", "OFF"):
            return v.replace("EVEN", "100")
    v = side.get("moneyLine")
    return None if v is None else str(v)


def pull(eid):
    url = f"https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/events/{eid}/competitions/{eid}/odds?limit=50"
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
                items = json.loads(r.read()).get("items") or []
            break
        except Exception:
            time.sleep(1 + i)
    else:
        return None
    sane = {}
    for it in items:
        n = (it.get("provider") or {}).get("name") or ""
        if "live" in n.lower():
            continue
        h, a = ml(it.get("homeTeamOdds") or {}), ml(it.get("awayTeamOdds") or {})
        try:
            s = imp(h) + imp(a)
        except Exception:
            continue
        if 1.0 <= s <= 1.10 and n not in sane:
            sane[n] = (int(float(h)), int(float(a)), round(s, 4))
    for n in PRI + sorted(sane):
        if n in sane:
            return n, sane[n], len(sane)
    return ("", None, 0)


for y in sys.argv[1:] or ["2021", "2022", "2023"]:
    p = D / f"{y}.csv"
    rows = list(csv.DictReader(open(p)))
    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(lambda r: pull(r["espn_id"]), rows))
    ok = 0
    for r, x in zip(rows, res):
        r.setdefault("ml_home_raw", r["ml_home"]); r.setdefault("ml_away_raw", r["ml_away"])
        if r.get("ml_status") == "fixed":
            ok += 1; continue
        if x and x[1]:
            r["ml_home"], r["ml_away"] = x[1][0], x[1][1]
            r["ml_home_close"] = r["ml_away_close"] = r["ml_home_open"] = r["ml_away_open"] = ""
            r["ml_provider"], r["ml_status"], r["ml_sane_books"] = x[0], "fixed", x[2]
            ok += 1
        else:
            r["ml_home"] = r["ml_away"] = r["ml_home_close"] = r["ml_away_close"] = ""
            r["ml_provider"], r["ml_status"], r["ml_sane_books"] = "", "excluded" if x else "fetch_failed", 0
    cols = list(rows[0].keys())
    for r in rows:
        for c in cols:
            r.setdefault(c, "")
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    print(y, len(rows), "fixed", ok, flush=True)
