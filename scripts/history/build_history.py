#!/usr/bin/env python3
"""Build the DESK history store from free public endpoints (run on the box, not in Actions).

ESPN (no key): site scoreboard (events, finals, OT/SO detail), core /odds (pickcenter-style
provider odds: spread, total, ML, prices, open/close where present), core /predictor
(FPI / win %). Every HTTP response is cached gzipped under --cache so re-runs are free.
NFL/CFB: also merged with the u3 research masters (nflverse closing lines + ESPN FPI).
NHL: also merged with NHL API REG/OT/SO finals and MoneyPuck pregame win % (comparison only,
non-commercial license). No Odds API calls (historical endpoint is paid).

Output: data/history/<sport>/<season>.csv  (one row per regular-season game)
Usage: python scripts/history/build_history.py nhl 2021 2026   (season = start year)
"""
from __future__ import annotations

import concurrent.futures as cf
import csv
import gzip
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CACHE = Path("/workspace/betbot-revamp/data/history/cache")
OUT = REPO / "data" / "history"
UA = {"User-Agent": "Mozilla/5.0 (desk-v1 history research)"}
SITE = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={d}{extra}"
CORE = "https://sports.core.api.espn.com/v2/sports/{core}/events/{i}/competitions/{i}/{what}"
SPORT = {
    "nhl": {"path": "hockey/nhl", "core": "hockey/leagues/nhl", "pred": False,
            "window": lambda y: (date(y, 9, 25), date(y + 1, 4, 25))},
    "mlb": {"path": "baseball/mlb", "core": "baseball/leagues/mlb", "pred": True,
            "window": lambda y: (date(y, 3, 15), date(y, 10, 3))},
}
BOOK_PREF = ["DraftKings", "ESPN BET", "Caesars Sportsbook", "Caesars Sportsbook (New Jersey)",
             "William Hill", "consensus", "Bet365", "BetMGM", "FanDuel"]


def get(url: str, tries: int = 4):
    key = hashlib.sha1(url.encode()).hexdigest()
    f = CACHE / key[:2] / f"{key}.json.gz"
    if f.exists():
        return json.loads(gzip.decompress(f.read_bytes()))
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                data = {"_status": e.code}
                break
            time.sleep(2 + 2 * k)
        except Exception:
            time.sleep(2 + 2 * k)
    else:
        return None  # not cached: retried next run
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(gzip.compress(json.dumps(data).encode()))
    return data


def am(x):
    try:
        if x in (None, "", 0, "0"):
            return None
        s = str(x).replace("+", "")
        return None if s.upper() == "EVEN" and False else (100 if s.upper() == "EVEN" else int(float(s)))
    except (TypeError, ValueError):
        return None


def pick_odds(js):
    items = [it for it in (js or {}).get("items") or [] if "live" not in (it.get("provider", {}).get("name", "").lower())]
    if not items:
        return {}, 0
    def rank(it):
        n = it.get("provider", {}).get("name", "")
        return BOOK_PREF.index(n) if n in BOOK_PREF else len(BOOK_PREF)
    items.sort(key=rank)
    # prefer a provider with both MLs
    it = next((x for x in items if am((x.get("homeTeamOdds") or {}).get("moneyLine")) and am((x.get("awayTeamOdds") or {}).get("moneyLine"))), items[0])
    h, a = it.get("homeTeamOdds") or {}, it.get("awayTeamOdds") or {}
    def oc(side, tag, fld):
        d = (side.get(tag) or {}).get(fld) or {}
        return am(d.get("american")) if isinstance(d, dict) else None
    row = {
        "odds_provider": it.get("provider", {}).get("name"), "odds_providers_n": len(items),
        "spread_home": it.get("spread"), "total": it.get("overUnder"),
        "ml_home": am(h.get("moneyLine")), "ml_away": am(a.get("moneyLine")),
        "sp_price_home": am(h.get("spreadOdds")), "sp_price_away": am(a.get("spreadOdds")),
        "over_price": am(it.get("overOdds")), "under_price": am(it.get("underOdds")),
        "ml_home_open": oc(h, "open", "moneyLine"), "ml_away_open": oc(a, "open", "moneyLine"),
        "ml_home_close": oc(h, "close", "moneyLine"), "ml_away_close": oc(a, "close", "moneyLine"),
    }
    return row, len(items)


def scoreboard_events(sp: str, y: int):
    cfg = SPORT[sp]
    d0, d1 = cfg["window"](y)
    days = [(d0 + timedelta(days=k)).strftime("%Y%m%d") for k in range((d1 - d0).days + 1)]
    extra = "&limit=100"
    with cf.ThreadPoolExecutor(8) as ex:
        pages = list(ex.map(lambda d: get(SITE.format(path=cfg["path"], d=d, extra=extra)), days))
    evs = {}
    for js in pages:
        for e in (js or {}).get("events") or []:
            if (e.get("season") or {}).get("type") != 2:
                continue
            st = e.get("status", {}).get("type", {})
            if not st.get("completed"):
                continue
            c = e["competitions"][0]
            comp = {x["homeAway"]: x for x in c["competitors"]}
            evs[e["id"]] = {
                "espn_id": e["id"], "season": y, "date_utc": e.get("date"),
                "home": comp["home"]["team"].get("abbreviation"), "away": comp["away"]["team"].get("abbreviation"),
                "home_name": comp["home"]["team"].get("displayName"), "away_name": comp["away"]["team"].get("displayName"),
                "home_score": comp["home"].get("score"), "away_score": comp["away"].get("score"),
                "status_detail": st.get("detail"), "periods": e.get("status", {}).get("period"),
                "neutral": c.get("neutralSite"),
            }
    return evs, len(days)


def enrich(sp: str, ev: dict):
    cfg = SPORT[sp]
    o, n = pick_odds(get(CORE.format(core=cfg["core"], i=ev["espn_id"], what="odds")))
    ev.update(o)
    if cfg["pred"]:
        p = get(CORE.format(core=cfg["core"], i=ev["espn_id"], what="predictor")) or {}
        st = {s.get("name"): s.get("value") for s in ((p.get("homeTeam") or {}).get("statistics") or [])}
        ev["espn_home_win_pct"] = st.get("winProbability", st.get("gameProjection"))
        ev["espn_pred_last_modified"] = p.get("lastModified")
    return ev


def build(sp: str, y0: int, y1: int):
    for y in range(y0, y1 + 1):
        evs, ndays = scoreboard_events(sp, y)
        with cf.ThreadPoolExecutor(10) as ex:
            rows = list(ex.map(lambda e: enrich(sp, e), evs.values()))
        rows.sort(key=lambda r: (r["date_utc"] or "", r["espn_id"]))
        if sp == "nhl":
            for r in rows:
                d = r.get("status_detail") or ""
                r["result_type"] = "SO" if "SO" in d else ("OT" if "OT" in d else "REG")
        out = OUT / sp / f"{y}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        cols = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("espn_id", "season", "date_utc", "home", "away"), k))
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        n_odds = sum(1 for r in rows if r.get("ml_home") is not None)
        n_pred = sum(1 for r in rows if r.get("espn_home_win_pct") is not None)
        print(f"{sp} {y}: {ndays} dates, {len(rows)} final games, ML odds {n_odds}, predictor {n_pred} -> {out}", flush=True)


if __name__ == "__main__":
    build(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
