#!/usr/bin/env python3
"""DESK auto board — runs on GitHub Actions, independent of the Grok Bot box.

One Odds API call per sport per day (regions=us, markets=h2h,spreads,totals,
oddsFormat=american => 3 credits each): americanfootball_nfl + americanfootball_ncaaf
+ icehockey_nhl + baseball_mlb => 4 sports x 3 = 12 credits/day
-> every NFL/CFB game kicking in the next 7 days (CT; NHL/MLB today + tomorrow) as BOARD 0u rows with LINES +
MARKET no-vig %, plus a PAPER side from the u3 market-heavy blend (scripts/paper_model.py,
ESPN FPI, free). PAPER picks are stamped to data/paper_ledger.json (no real bets).

Never writes model_* / why / CLEAR: those come only from Bet Bot Main via the
box publisher. The paper block lives in row["paper"] and data/paper*.json only.
Blank stays blank.

Merge rule: if data/today.json is Main-sourced for today's CT slate, Main's
rows / CLEARs / model / why are untouched; only NFL games kicking today (CT)
that are missing from it get appended as BOARD rows.

Ledger block (Written record, Grade via the box) is always preserved as-is.

Stdlib only. Exit codes: 0 ok / nothing to do, 2 API auth/usage stop,
3 network/other API error, 4 bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paper_model  # noqa: E402
import espn_odds  # noqa: E402  (free fallback: fills only games/markets the Odds API lacks)

CT = ZoneInfo("America/Chicago")
UTC = timezone.utc
SPORTS = {"NFL": "americanfootball_nfl", "CFB": "americanfootball_ncaaf",
          "NHL": "icehockey_nhl", "MLB": "baseball_mlb"}
# FREE TIER (500 credits/mo, since 2026-10-09; paid plan payment failed 10/8): Odds API is called only for these
# sports, and only when the FREE /events endpoint shows a game inside ODDS_CALL_HOURS. Everything else
# (MLB, quiet NFL/CFB days, games/markets the Odds API missed) comes from ESPN's public scoreboard odds, tagged.
PAID_SPORTS = [s.strip().upper() for s in os.environ.get("DESK_ODDS_SPORTS", "NFL,CFB,NHL").split(",") if s.strip()]
ODDS_CALL_HOURS = {"NFL": 36, "CFB": 36, "NHL": 30, "MLB": 30}
EVENTS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/events"
QUOTA_LOG = "odds_quota.jsonl"
# NHL: today + tomorrow (CT), ~15 games/day · MLB: today + tomorrow (CT), late season / postseason
WINDOW_BY_SPORT = {"NFL": 7, "CFB": 7, "NHL": 2, "MLB": 2}
API_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
SCHEMA = "desk-pwa/v1"
WINDOW_DAYS = 7
MIN_REMAINING = int(os.environ.get("DESK_MIN_REMAINING", "60"))  # leave room for Strawhat Desk (same key)  # never spend the last credits: skip further calls below this
BANNER = ("PAPER ONLY — no real bets. NFL + CFB + NHL + MLB: ML / spread (puck line / run line) / O-U with market and model %. "
          "Lines: The Odds API (free tier) first, ESPN public odds fill gaps (tagged per line). No Main card filed.")
MARKET_SRC = "The Odds API · no-vig = median across books of two-way de-vig (consensus line)"

# Display ids only (naming, not data). Washington = wsh to match Main's board.
NFL_ABBR = {
    "Arizona Cardinals": "ari", "Atlanta Falcons": "atl", "Baltimore Ravens": "bal",
    "Buffalo Bills": "buf", "Carolina Panthers": "car", "Chicago Bears": "chi",
    "Cincinnati Bengals": "cin", "Cleveland Browns": "cle", "Dallas Cowboys": "dal",
    "Denver Broncos": "den", "Detroit Lions": "det", "Green Bay Packers": "gb",
    "Houston Texans": "hou", "Indianapolis Colts": "ind", "Jacksonville Jaguars": "jax",
    "Kansas City Chiefs": "kc", "Las Vegas Raiders": "lv", "Los Angeles Chargers": "lac",
    "Los Angeles Rams": "lar", "Miami Dolphins": "mia", "Minnesota Vikings": "min",
    "New England Patriots": "ne", "New Orleans Saints": "no", "New York Giants": "nyg",
    "New York Jets": "nyj", "Philadelphia Eagles": "phi", "Pittsburgh Steelers": "pit",
    "San Francisco 49ers": "sf", "Seattle Seahawks": "sea", "Tampa Bay Buccaneers": "tb",
    "Tennessee Titans": "ten", "Washington Commanders": "wsh",
}


class ApiStop(Exception):
    """401 / 429 / OUT_OF_USAGE — hard stop, never retried."""


# ----------------------------------------------------------------- odds math
def implied(american: float) -> float:
    a = float(american)
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def novig_pair(p1: float, p2: float) -> tuple[float, float]:
    a, b = implied(p1), implied(p2)
    s = a + b
    return a / s, b / s


def pct(x: float | None) -> float | None:
    return None if x is None else round(100.0 * x, 1)


def best_price(cands: list[tuple[float, str]]) -> tuple[float | None, str]:
    """Highest American price = best for the bettor."""
    if not cands:
        return None, ""
    price, book = max(cands, key=lambda t: t[0])
    return int(price), book


def consensus_point(points: list[float]) -> float | None:
    """Most-quoted point across books; tie -> the one closest to the median."""
    if not points:
        return None
    counts: dict[float, int] = {}
    for p in points:
        counts[p] = counts.get(p, 0) + 1
    top = max(counts.values())
    tied = [p for p, c in counts.items() if c == top]
    med = statistics.median(points)
    return min(tied, key=lambda p: (abs(p - med), p))


# ------------------------------------------------------------------ helpers
def team_tokens(name: str) -> set[str]:
    return {p for p in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(p) > 2}


def same_game(a1: str, h1: str, a2: str, h2: str) -> bool:
    ta1, th1, ta2, th2 = team_tokens(a1), team_tokens(h1), team_tokens(a2), team_tokens(h2)
    return bool(ta1 & ta2) and bool(th1 & th2)


def eid_aliases(eid: str) -> set[str]:
    e = (eid or "").strip().lower()
    out = {e} if e else set()
    for a, b in (("was-", "wsh-"), ("wsh-", "was-")):
        if e.startswith(a):
            out.add(b + e[len(a):])
        if e.endswith("-" + a[:-1]):
            out.add(e[: -len(a) + 1] + b[:-1])
    return out


def kick_fields(iso: str) -> tuple[str, str, datetime]:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC)
    ct = dt.astimezone(CT)
    label = f"{ct.strftime('%a')} {ct.month}/{ct.day} {ct.strftime('%I:%M %p').lstrip('0')} CT"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), label, ct


# ---------------------------------------------------------------- API call
def fetch_odds(api_key: str, sport_key: str, timeout: int = 30) -> tuple[list[dict], dict]:
    qs = urllib.parse.urlencode({
        "apiKey": api_key, "regions": "us", "markets": "h2h,spreads,totals",
        "oddsFormat": "american", "dateFormat": "iso",
    })
    req = urllib.request.Request(f"{API_URL.format(sport=sport_key)}?{qs}", headers={"User-Agent": "desk-v1-auto-board"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            hdr = {k.lower(): v for k, v in r.headers.items()}
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        hdr = {k.lower(): v for k, v in (e.headers or {}).items()}
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        usage = {k: hdr.get(k) for k in ("x-requests-remaining", "x-requests-used", "x-requests-last")}
        print(f"[auto_board] API HTTP {e.code} usage={usage}", file=sys.stderr)
        if e.code in (401, 429) or "OUT_OF_USAGE" in body:
            raise ApiStop(f"HTTP {e.code}: {body[:200]}") from None
        raise RuntimeError(f"HTTP {e.code}: {body[:200]}") from None
    if "OUT_OF_USAGE" in body[:500]:
        raise ApiStop("OUT_OF_USAGE")
    usage = {k: hdr.get(k) for k in ("x-requests-remaining", "x-requests-used", "x-requests-last")}
    data = json.loads(body)
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected payload: {str(data)[:200]}")
    return data, usage


def has_game_soon(api_key: str, sport_key: str, hours: int) -> bool | None:
    """FREE call (events endpoint costs 0 credits). None = unknown (then we do call odds)."""
    qs = urllib.parse.urlencode({"apiKey": api_key, "dateFormat": "iso"})
    try:
        req = urllib.request.Request(f"{EVENTS_URL.format(sport=sport_key)}?{qs}", headers={"User-Agent": "desk-v1-auto-board"})
        with urllib.request.urlopen(req, timeout=30) as r:
            evs = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[auto_board] events check failed ({type(e).__name__}) — treating as unknown", file=sys.stderr)
        return None
    now = datetime.now(UTC)
    for ev in evs or []:
        try:
            t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if 0 < (t - now).total_seconds() <= hours * 3600:
            return True
    return False


def log_quota(data_dir: Path, now_ct: datetime, sport: str, u: dict) -> None:
    row = {"time_ct": now_ct.isoformat(timespec="seconds"), "repo": "desk-v1", "sport": sport,
           "last": u.get("x-requests-last"), "used": u.get("x-requests-used"), "remaining": u.get("x-requests-remaining")}
    with open(data_dir / QUOTA_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


# ------------------------------------------------------------- build rows
def build_row(ev: dict, slate_iso: str, sport: str = "NFL") -> dict:
    home, away = ev.get("home_team") or "", ev.get("away_team") or ""
    kick_utc, kick_ct, _ = kick_fields(ev["commence_time"])
    if sport == "NFL":
        ha, aa = NFL_ABBR.get(home, ""), NFL_ABBR.get(away, "")
        eid = f"{aa}-{ha}" if aa and ha else (ev.get("id") or "")
    else:
        eid = f"{sport.lower()}-{ev.get('id') or ''}"

    h2h, spreads, totals = [], [], []
    for bk in ev.get("bookmakers") or []:
        title = bk.get("title") or bk.get("key") or ""
        for m in bk.get("markets") or []:
            outs = {o.get("name"): o for o in m.get("outcomes") or []}
            if m.get("key") == "h2h" and home in outs and away in outs:
                h2h.append((title, outs[home]["price"], outs[away]["price"]))
            elif m.get("key") == "spreads" and home in outs and away in outs:
                hp, ap = outs[home].get("point"), outs[away].get("point")
                if hp is not None and ap is not None and abs(hp + ap) < 1e-9:
                    spreads.append((title, float(hp), outs[home]["price"], outs[away]["price"]))
            elif m.get("key") == "totals" and "Over" in outs and "Under" in outs:
                op, up = outs["Over"].get("point"), outs["Under"].get("point")
                if op is not None and op == up:
                    totals.append((title, float(op), outs["Over"]["price"], outs["Under"]["price"]))

    srcs = ev.get("line_sources") or {}
    row = {
        "line_sources": {"ml": srcs.get("h2h", ""), "spread": srcs.get("spreads", ""), "total": srcs.get("totals", "")},
        "line_source": ("espn" if srcs and set(srcs.values()) == {"espn"} else
                        "odds_api+espn" if "espn" in srcs.values() else "odds_api" if srcs else ""),
        "tag": "BOARD", "status": "HOLD", "sport": sport, "sport_raw": sport.lower(),
        "away": away, "home": home, "matchup": f"{away} at {home}",
        "selection": "", "market": "", "line": None, "price_american": None, "book": "",
        "units": 0,
        # model / Main fields: never filled by the auto board
        "model_win_pct": None, "market_win_pct": None, "edge_pct": None, "edge_pts": None,
        "model_over_pct": None, "model_under_pct": None,
        "fair_line": None, "fair_spread": None, "market_novig_pct": None, "shrunk_pct": None,
        "shrink_w": None, "model_source": "", "sim_source": "", "sim_flag": "", "gap_flag": "",
        "reject_code": "", "reject_note": "", "hold_reason": "", "ou_lean": "",
        "why": "", "why_source": "", "notes": BANNER,
        "prob_source": "auto board — market no-vig; PAPER blend in row.paper (not Main model)",
        "prob_method": "none",
        "kick_utc": kick_utc, "kick_ct": kick_ct, "event_id": eid,
        "toa_event_id": ev.get("id") or "", "slate_date_ct": slate_iso,
        "board_only": True, "auto": True, "books_count": len(ev.get("bookmakers") or []),
        "totals_source": "",
    }

    # Totals: consensus line, best price each side, median no-vig across books
    tl = consensus_point([t[1] for t in totals])
    at_t = [t for t in totals if t[1] == tl]
    row["total_line"] = tl
    row["over_price"], row["over_book"] = best_price([(t[2], t[0]) for t in at_t])
    row["under_price"], row["under_book"] = best_price([(t[3], t[0]) for t in at_t])
    ov = [novig_pair(t[2], t[3])[0] for t in at_t]
    row["market_over_pct"] = pct(statistics.median(ov)) if ov else None
    row["market_under_pct"] = round(100.0 - row["market_over_pct"], 1) if ov else None
    row["totals_books"] = len(at_t)
    if ov:
        row["totals_source"] = f"mkt no-vig · {len(at_t)} bk"

    # Spreads (home-line convention)
    sl = consensus_point([s[1] for s in spreads])
    at_s = [s for s in spreads if s[1] == sl]
    row["spread_home_line"] = sl
    row["spread_away_line"] = (-sl if sl else 0.0) if sl is not None else None
    row["spread_home_price"], row["spread_home_book"] = best_price([(s[2], s[0]) for s in at_s])
    row["spread_away_price"], row["spread_away_book"] = best_price([(s[3], s[0]) for s in at_s])
    hc = [novig_pair(s[2], s[3])[0] for s in at_s]
    row["market_home_cover_pct"] = pct(statistics.median(hc)) if hc else None
    row["market_away_cover_pct"] = round(100.0 - row["market_home_cover_pct"], 1) if hc else None
    row["spread_books"] = len(at_s)

    # Moneyline
    row["ml_home_price"], row["ml_home_book"] = best_price([(h[1], h[0]) for h in h2h])
    row["ml_away_price"], row["ml_away_book"] = best_price([(h[2], h[0]) for h in h2h])
    hm = [novig_pair(h[1], h[2])[0] for h in h2h]
    row["market_home_ml_pct"] = pct(statistics.median(hm)) if hm else None
    row["market_away_ml_pct"] = round(100.0 - row["market_home_ml_pct"], 1) if hm else None
    row["ml_books"] = len(h2h)
    return row


def is_main_sourced(doc: dict) -> bool:
    src = str(doc.get("source") or "").lower()
    if src:
        return src == "main"
    # legacy exports (before the source marker) — Main feed file named in summary
    return str((doc.get("summary") or {}).get("feed") or "").startswith("picks_feed")


def all_rows(doc: dict) -> list[dict]:
    card = doc.get("card") or {}
    return [*(card.get("clears") or []), *(card.get("fills") or []), *(card.get("holds") or [])]


def default_rules() -> dict:
    return {
        "clear": "CLEAR = Main written bankroll (units/clears)",
        "fill": "FILL = Main recommended side, not written",
        "hold": "HOLD/BOARD = full slate game with no Main side",
        "owner": "Bet Bot Main owns the card; Sports Betting shops + exports",
        "auto": ("Auto board (GitHub Actions, once daily 9:15am CT): NFL + CFB + NHL + MLB lines + MARKET no-vig %. "
                 "No Main model, no notes, no CLEAR. Main's feed replaces it when published."),
        "paper": ("PAPER ONLY (no real bets): football = market + w·(ESPN FPI − market), w refit vs closing "
                  "lines on history (NFL 0 since 2026-10-04 pending pregame-FPI refit, CFB 0); NHL = market only, w locked 0, MODEL % info only, Poisson fair shown; MLB = market only (fitted w was 0), MODEL blank, no stamps, no live CLEARs. Price = DraftKings/Betr/Bovada as returned by The Odds API (never invented). "
                  "PAPER pick if edge ≥3pp vs stamped price; gap >8pp vs no-vig = HOLD. 1u = $20 flat. "
                  "Stamped once on kick day, graded W/L/P from ESPN finals at the stamped price."),
        "ledger": "Ledger = Main written CLEAR grades only (Grade via the box). Paper ledger is separate (Paper tab).",
    }


def build_auto_doc(rows: list[dict], existing: dict | None, now_ct: datetime, usage: dict,
                   paper_meta: dict | None = None) -> dict:
    slate_iso = now_ct.date().isoformat()
    same_slate = bool(existing) and existing.get("slate_date_ct") == slate_iso
    ledger = (existing or {}).get("ledger")
    if same_slate:
        nfl_props = existing.get("nfl_props") or {"games": []}
        props_meta = existing.get("nfl_props_meta")
        card_props = (existing.get("card") or {}).get("props") or []
    else:
        nfl_props, props_meta, card_props = {"games": []}, None, []
    props_meta = props_meta or {"enabled": True, "max_per_game": 4, "cfb_props": False,
                                "events_with_props": len(nfl_props.get("games") or []), "feed": None}
    n = len(rows)
    with_ou = sum(1 for r in rows if r.get("market_over_pct") is not None)
    return {
        "schema": SCHEMA,
        "brand": "DESK V1",
        "source": "auto",
        "banner": BANNER,
        "slate_date_ct": slate_iso,
        "generated_at_ct": now_ct.isoformat(timespec="seconds"),
        "delivery": "pwa+json · auto board via GitHub Actions",
        "rules": default_rules(),
        "summary": {
            "published_clear": 0, "fill_total": 0, "fill_shown": 0,
            "hold_total": n, "board_total": n,
            "by_sport": {s: c for s in SPORTS if (c := sum(1 for r in rows if r.get("sport") == s))},
            "with_model_win_pct": 0, "with_market_win_pct": 0,
            "feed": "the-odds-api (auto) + paper u3 blend", "board_file": None,
            "props_total": len(card_props), "props_clear": 0, "props_file": None,
            "ledger_rows": (ledger or {}).get("row_count") or 0,
            "ledger_open": (ledger or {}).get("open_count"),
            "ledger_graded": (ledger or {}).get("graded_count"),
            "auto": {
                "window": f"{WINDOW_DAYS}d from start of {slate_iso} CT",
                "games": n, "with_market_ou_pct": with_ou,
                "with_market_spread_pct": sum(1 for r in rows if r.get("market_home_cover_pct") is not None),
                "with_market_ml_pct": sum(1 for r in rows if r.get("market_home_ml_pct") is not None),
                "market_source": MARKET_SRC,
                "requests_remaining": usage.get("x-requests-remaining"),
                "requests_used": usage.get("x-requests-used"),
                "requests_last": usage.get("x-requests-last"),
                "calls": usage.get("calls"),
                "credits_this_run": usage.get("credits_this_run"),
            },
        },
        "paper": paper_meta,
        "card": {"clears": [], "fills": [], "holds": rows, "props": card_props},
        "shop": {"anchors_written": [], "written_shop_notes": {}},
        "ledger": ledger,
        "nfl_props": nfl_props,
        "nfl_props_meta": props_meta,
    }


def merge_into_main(existing: dict, rows: list[dict], now_ct: datetime, usage: dict) -> tuple[dict, int]:
    """Append only today's (CT) NFL/CFB games missing from Main's card. Touch nothing else."""
    today = now_ct.date()
    have = all_rows(existing)
    have_ids = set().union(*[eid_aliases(str(r.get("event_id") or "")) for r in have]) if have else set()
    added = []
    for r in rows:
        if datetime.fromisoformat(r["kick_utc"].replace("Z", "+00:00")).astimezone(CT).date() != today:
            continue
        if eid_aliases(r["event_id"]) & have_ids:
            continue
        if any(same_game(h.get("away") or "", h.get("home") or "", r["away"], r["home"]) for h in have):
            continue
        added.append(r)
    if not added:
        return existing, 0
    doc = json.loads(json.dumps(existing))  # deep copy; Main content untouched
    doc.setdefault("card", {}).setdefault("holds", []).extend(added)
    s = doc.setdefault("summary", {})
    s["hold_total"] = (s.get("hold_total") or 0) + len(added)
    s["board_total"] = (s.get("board_total") or 0) + len(added)
    bs = s.setdefault("by_sport", {})
    for r in added:
        bs[r["sport"]] = (bs.get(r["sport"]) or 0) + 1
    doc["auto_merge"] = {
        "at_ct": now_ct.isoformat(timespec="seconds"),
        "appended": len(added),
        "appended_event_ids": [r["event_id"] for r in added],
        "note": "Main rows/CLEARs/model/why untouched; missing NFL/CFB games appended as BOARD (lines + market %, PAPER block).",
        "requests_remaining": usage.get("x-requests-remaining"),
    }
    return doc, len(added)


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[1] / "data"))
    ap.add_argument("--from-file", help="Offline: read a saved NFL Odds API JSON instead of calling the API (0 credits)")
    ap.add_argument("--from-file-cfb", help="Offline: read a saved NCAAF Odds API JSON (0 credits)")
    ap.add_argument("--from-file-nhl", help="Offline: read a saved NHL Odds API JSON (0 credits)")
    ap.add_argument("--from-file-mlb", help="Offline: read a saved MLB Odds API JSON (0 credits)")
    ap.add_argument("--now", help="Override 'now' (ISO, for tests)")
    ap.add_argument("--force", action="store_true", help="Call the API even if today's auto board already exists")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    now_ct = (datetime.fromisoformat(args.now).astimezone(CT) if args.now else datetime.now(CT))
    slate_iso = now_ct.date().isoformat()
    today_path = data_dir / "today.json"
    existing = None
    if today_path.exists():
        try:
            existing = json.loads(today_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[auto_board] existing today.json unreadable — refusing to overwrite", file=sys.stderr)
            return 4

    main_today = bool(existing) and existing.get("slate_date_ct") == slate_iso and is_main_sourced(existing)
    if (existing and existing.get("slate_date_ct") == slate_iso
            and str(existing.get("source") or "") == "auto" and not args.force
            and not (args.from_file or args.from_file_cfb or args.from_file_nhl or args.from_file_mlb)):
        print(f"[auto_board] auto board for {slate_iso} already present — 0 API calls (use --force)")
        return 0

    usage: dict = {"calls": 0, "credits_this_run": 0}
    events_by_sport: dict[str, list[dict]] = {}
    offline = {"NFL": args.from_file, "CFB": args.from_file_cfb, "NHL": args.from_file_nhl,
               "MLB": args.from_file_mlb}
    key = os.environ.get("THE_ODDS_API_KEY", "").strip()
    for sport, sport_key in SPORTS.items():
        try:
            if args.from_file or args.from_file_cfb or args.from_file_nhl or args.from_file_mlb:
                if not offline[sport]:
                    events_by_sport[sport] = []
                    continue
                raw = json.loads(Path(offline[sport]).read_text(encoding="utf-8"))
                evs = raw if isinstance(raw, list) else (raw.get("data") or raw.get("events") or [])
                print(f"[auto_board] {sport} offline file {offline[sport]}: {len(evs)} events (0 credits)")
            else:
                evs = []
                if sport not in PAID_SPORTS or not key:
                    if not key:
                        print(f"[auto_board] THE_ODDS_API_KEY not set — {sport} from ESPN only", file=sys.stderr)
                    events_by_sport[sport] = []
                    continue
                soon = has_game_soon(key, sport_key, ODDS_CALL_HOURS.get(sport, 30))
                if soon is False:
                    print(f"[auto_board] {sport}: no game within {ODDS_CALL_HOURS.get(sport)}h (free events check) — 0 credits, ESPN fills")
                    events_by_sport[sport] = []
                    continue
                rem = usage.get("x-requests-remaining")
                if rem is not None and float(rem) < MIN_REMAINING:
                    print(f"[auto_board] {sport}: SKIP call — only {rem} credits left (< {MIN_REMAINING})", file=sys.stderr)
                    events_by_sport[sport] = []
                    continue
                evs, u = fetch_odds(key, sport_key)
                usage.update({k: v for k, v in u.items() if v is not None})
                usage["calls"] += 1
                usage["credits_this_run"] += int(float(u.get("x-requests-last") or 0))
                usage[f"{sport}_x-requests-last"] = u.get("x-requests-last")
                log_quota(data_dir, now_ct, sport, u)
                print(f"[auto_board] {sport}: 1 API call · x-requests-last={u.get('x-requests-last')} "
                      f"x-requests-used={u.get('x-requests-used')} "
                      f"x-requests-remaining={u.get('x-requests-remaining')}")
            events_by_sport[sport] = evs
        except ApiStop as e:
            print(f"[auto_board] {sport} HARD STOP (no retry): {e}", file=sys.stderr)
            if not any(events_by_sport.values()):
                return 2
            events_by_sport[sport] = []
            break
        except Exception as e:  # network etc. — no retry
            print(f"[auto_board] {sport} API error (no retry): {type(e).__name__}: {e}", file=sys.stderr)
            events_by_sport[sport] = []
    # ESPN fallback (free): fill only games/markets missing from the Odds API; per-line source tags, no double count
    usage["espn"] = {}
    if not (args.from_file or args.from_file_cfb or args.from_file_nhl or args.from_file_mlb):
        for sport in SPORTS:
            try:
                ex = espn_odds.fetch(sport, WINDOW_BY_SPORT.get(sport, WINDOW_DAYS))
                events_by_sport[sport], st = espn_odds.merge(events_by_sport.get(sport) or [], ex)
                for e in events_by_sport[sport]:
                    e.setdefault("sport_key", SPORTS[sport])
                usage["espn"][sport] = st
                print(f"[auto_board] {sport} ESPN fallback: {st}")
            except Exception as e:
                print(f"[auto_board] {sport} ESPN fallback error: {type(e).__name__}: {e}", file=sys.stderr)
    if not any(events_by_sport.values()) and usage["calls"] == 0 and not (args.from_file or args.from_file_cfb or args.from_file_nhl or args.from_file_mlb):
        return 3

    # raw NHL events (Odds API + ESPN fallback) for the pre-puck goalie refresh (scripts/nhl_refresh.py, 0 credits)
    try:
        (data_dir / "nhl_events_today.json").write_text(json.dumps(
            {"slate_date_ct": slate_iso, "events": events_by_sport.get("NHL") or []}), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[auto_board] could not save NHL events: {e}", file=sys.stderr)

    start = now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
    rows_by_sport: dict[str, list[dict]] = {}
    paper_stats: dict[str, dict] = {}
    for sport, sport_key in SPORTS.items():
        rows, by_toa = [], {}
        end = start + timedelta(days=WINDOW_BY_SPORT.get(sport, WINDOW_DAYS))
        for ev in events_by_sport.get(sport) or []:
            if ev.get("sport_key") not in (None, sport_key) or not ev.get("commence_time"):
                continue
            _, _, kct = kick_fields(ev["commence_time"])
            # started games are skipped: The Odds API returns LIVE prices for them, never pregame
            if max(start, now_ct) < kct < end:
                rows.append(build_row(ev, slate_iso, sport))
                by_toa[ev.get("id") or ""] = ev
        rows.sort(key=lambda r: (r["kick_utc"], r["event_id"]))
        try:
            paper_stats[sport] = paper_model.attach_paper(sport, rows, by_toa, now_ct,
                                                          WINDOW_BY_SPORT.get(sport, WINDOW_DAYS))
        except Exception as e:  # ESPN down etc. -> board still publishes, paper blank
            print(f"[auto_board] {sport} paper model error: {type(e).__name__}: {e}", file=sys.stderr)
            paper_stats[sport] = {"games": len(rows), "error": str(e)[:200]}
        rows_by_sport[sport] = rows
        with_ou = sum(1 for r in rows if r.get("market_over_pct") is not None)
        print(f"[auto_board] {sport} games in window: {len(rows)} · market O/U %: {with_ou}/{len(rows)} · "
              f"paper {paper_stats[sport]}")
    rows = sorted([r for rs in rows_by_sport.values() for r in rs], key=lambda r: (r["kick_utc"], r["event_id"]))

    # ---- paper ledger: stamp today's PAPER picks (once per game, before kick)
    led_path = data_dir / "paper_ledger.json"
    ledger = paper_model.load_ledger(led_path)
    new = paper_model.stamp_today(ledger, rows_by_sport, now_ct)
    psum = paper_model.summarize(ledger, now_ct)
    for p in new:
        print(f"[auto_board] PAPER stamp {p['id']}: {p['selection']} {p['price_american']:+d} {p['book']} "
              f"edge {p['edge_pp']:+.1f}pp (model {p['model_pct']}% / no-vig {p['novig_pct']}%)")
    print(f"[auto_board] paper ledger {psum['record']} · {psum['units']:+.2f}u (${psum['usd']:+.2f}) · open {psum['open']}")
    paper_meta = {
        "label": "PAPER ONLY — no real bets", "model": paper_model.MODEL_NAME,
        "stats": paper_stats, "stamped_today": [p["id"] for p in new],
        "ledger_summary": psum, "ledger_file": "data/paper_ledger.json",
    }
    board = []
    stamped_ids = {p["id"] for p in ledger["picks"]}
    for r in rows:
        b = r.get("paper") or {}
        board.append({
            "sport": r["sport"], "matchup": r["matchup"], "kick_utc": r["kick_utc"], "kick_ct": r["kick_ct"],
            "event_id": r["event_id"], "espn_event_id": r.get("espn_event_id"), "status": b.get("status"),
            "best": b.get("best"), "picks": b.get("picks") or [], "by_market": b.get("by_market"),
            "note": b.get("note"), "fpi_home_margin": b.get("fpi_home_margin"),
            "mkt_home_margin": b.get("mkt_home_margin"), "blend_home_margin": b.get("blend_home_margin"),
            "stamped": any(paper_model.pick_id(r["sport"], r, m) in stamped_ids for m in ("ml", "spread", "total")),
            "stamped_markets": [m for m in ("ml", "spread", "total")
                                if paper_model.pick_id(r["sport"], r, m) in stamped_ids],
        })
    paper_doc = {
        "schema": "desk-paper/v1", "label": "PAPER ONLY — no real bets", "slate_date_ct": slate_iso,
        "generated_at_ct": now_ct.isoformat(timespec="seconds"), "model": ledger.get("model"),
        "stats": paper_stats, "summary": psum, "board": board,
        "credits": {k: usage.get(k) for k in ("calls", "credits_this_run", "x-requests-remaining",
                                              "x-requests-used", "NFL_x-requests-last", "CFB_x-requests-last",
                                              "NHL_x-requests-last", "espn")},
    }

    if main_today:
        doc, added = merge_into_main(existing, rows, now_ct, usage)
        if not added:
            print("[auto_board] Main card for today already has every NFL/CFB game — card unchanged")
        else:
            print(f"[auto_board] MERGE: Main-sourced today.json kept; appended {added} missing game(s)")
    else:
        doc = build_auto_doc(rows, existing, now_ct, usage, paper_meta)
        added = len(rows)
        print(f"[auto_board] AUTO board written for {slate_iso} (ledger preserved: {bool(doc.get('ledger'))})")

    if added:
        blob = json.dumps(doc, indent=2, ensure_ascii=False)
        (data_dir / f"{now_ct.strftime('%Y%m%d')}.json").write_text(blob, encoding="utf-8")
        today_path.write_text(blob, encoding="utf-8")
    paper_model.write_json(led_path, ledger)
    paper_model.write_json(data_dir / "paper.json", paper_doc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
