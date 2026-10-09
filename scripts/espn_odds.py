"""ESPN public scoreboard odds -> FALLBACK lines (free, no key).

Used ONLY to fill games or markets that The Odds API did not return. Every filled market is tagged
source="espn" (provider named, e.g. DraftKings via ESPN); Odds API markets are never overwritten, so a line is
never counted twice. Blank stays blank: a market ESPN does not quote is left out, never guessed.

Output events use The Odds API event shape (home_team, away_team, commence_time, bookmakers[...]) so the
existing row builders can read them unchanged. Stdlib only."""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
PATHS = {"NFL": "football/nfl", "CFB": "football/college-football", "NHL": "hockey/nhl",
         "NBA": "basketball/nba", "MLB": "baseball/mlb"}
BOOK_KEY = "espn_fallback"


def _num(x):
    try:
        s = str(x).strip().lower().lstrip("ou")
        if s in ("", "even", "ev"):
            return 100.0 if s else None
        return float(s)
    except (TypeError, ValueError):
        return None


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (desk paper board)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def scoreboard(sport: str, day: datetime) -> list[dict]:
    q = f"dates={day.strftime('%Y%m%d')}&limit=400"
    if sport == "CFB":
        q += "&groups=80"  # all FBS
    return _get(f"https://site.api.espn.com/apis/site/v2/sports/{PATHS[sport]}/scoreboard?{q}").get("events") or []


def to_toa_event(ev: dict) -> dict | None:
    comp = (ev.get("competitions") or [{}])[0]
    teams = {c.get("homeAway"): c for c in comp.get("competitors") or []}
    if "home" not in teams or "away" not in teams:
        return None
    if ((comp.get("status") or {}).get("type") or {}).get("state") not in (None, "pre"):
        return None  # started/final: ESPN shows live/closing numbers, never use as pregame
    home = teams["home"]["team"].get("displayName") or ""
    away = teams["away"]["team"].get("displayName") or ""
    odds = (comp.get("odds") or [None])[0]
    out = {"id": f"espn-{ev.get('id')}", "espn_event_id": str(ev.get("id")), "home_team": home, "away_team": away,
           "commence_time": ev.get("date"), "bookmakers": []}
    if not odds:
        return out
    prov = ((odds.get("provider") or {}).get("name")) or "ESPN"
    mk = []
    ml = odds.get("moneyline") or {}
    h = _num(((ml.get("home") or {}).get("close") or {}).get("odds"))
    a = _num(((ml.get("away") or {}).get("close") or {}).get("odds"))
    if h is not None and a is not None:
        mk.append({"key": "h2h", "outcomes": [{"name": home, "price": h}, {"name": away, "price": a}]})
    ps = odds.get("pointSpread") or {}
    hc, ac = (ps.get("home") or {}).get("close") or {}, (ps.get("away") or {}).get("close") or {}
    hl, al, hp, ap = _num(hc.get("line")), _num(ac.get("line")), _num(hc.get("odds")), _num(ac.get("odds"))
    if None not in (hl, al, hp, ap) and abs(hl + al) < 1e-9:
        mk.append({"key": "spreads", "outcomes": [{"name": home, "point": hl, "price": hp},
                                                  {"name": away, "point": al, "price": ap}]})
    tt = odds.get("total") or {}
    oc, uc = (tt.get("over") or {}).get("close") or {}, (tt.get("under") or {}).get("close") or {}
    ol, ul, op, up = _num(oc.get("line")), _num(uc.get("line")), _num(oc.get("odds")), _num(uc.get("odds"))
    if None not in (ol, ul, op, up) and ol == ul:
        mk.append({"key": "totals", "outcomes": [{"name": "Over", "point": ol, "price": op},
                                                 {"name": "Under", "point": ul, "price": up}]})
    if mk:
        out["bookmakers"].append({"key": BOOK_KEY, "title": f"{prov} (via ESPN)", "markets": mk})
    return out


def fetch(sport: str, days: int, now: datetime | None = None) -> list[dict]:
    now = (now or datetime.now(timezone.utc)).astimezone(CT)
    seen, out = set(), []
    for i in range(max(days, 1)):
        try:
            evs = scoreboard(sport, now + timedelta(days=i))
        except Exception:
            continue
        for ev in evs:
            if ev.get("id") in seen:
                continue
            seen.add(ev.get("id"))
            t = to_toa_event(ev)
            if t:
                out.append(t)
    return out


def _tok(name):
    return {p for p in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(p) > 2}


def _match(a, b):
    try:
        ta = datetime.fromisoformat(a["commence_time"].replace("Z", "+00:00"))
        tb = datetime.fromisoformat(b["commence_time"].replace("Z", "+00:00"))
        if abs((ta - tb).total_seconds()) > 6 * 3600:
            return False
    except Exception:
        return False
    return bool(_tok(a["home_team"]) & _tok(b["home_team"])) and bool(_tok(a["away_team"]) & _tok(b["away_team"]))


def merge(toa_events: list[dict], espn_events: list[dict]) -> tuple[list[dict], dict]:
    """Odds API first. ESPN adds (a) whole games the Odds API lacks, (b) only the markets an Odds API game lacks.
    Each event gets line_sources {h2h|spreads|totals: 'odds_api'|'espn'}. Returns (events, stats)."""
    stats = {"odds_api_games": len(toa_events), "espn_games_added": 0, "espn_markets_filled": 0}
    out = []
    for e in toa_events:
        have = {m.get("key") for b in e.get("bookmakers") or [] for m in b.get("markets") or []}
        e["line_sources"] = {k: "odds_api" for k in have}
        out.append(e)
    for x in espn_events:
        hit = next((e for e in out if _match(e, x)), None)
        xb = (x.get("bookmakers") or [None])[0]
        if hit is None:
            if xb:
                x["line_sources"] = {m["key"]: "espn" for m in xb["markets"]}
                stats["espn_games_added"] += 1
                out.append(x)
            continue
        hit.setdefault("espn_event_id", x.get("espn_event_id"))
        if not xb:
            continue
        miss = [m for m in xb["markets"] if m["key"] not in hit["line_sources"]]
        if miss:
            hit.setdefault("bookmakers", []).append({**xb, "markets": miss})
            for m in miss:
                hit["line_sources"][m["key"]] = "espn"
                stats["espn_markets_filled"] += 1
    return out, stats
