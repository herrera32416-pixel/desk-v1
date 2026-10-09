#!/usr/bin/env python3
"""DESK PAPER model — u3 market-heavy blend for NFL + CFB sides (paper only, no real bets).

Runs deterministically on GitHub Actions (stdlib only, no LLM/agent steps).

Model (from betbot-revamp/research/upgrades-2026-10, u3_blend, FINAL_REPORT 2026-10-02):
  A     = market home margin = -(consensus home spread from The Odds API)
  fpi   = ESPN FPI predictor teamPredPtDiff for the home team (free public JSON)
  pred  = A + w * (fpi - A)            w = weight on FPI's disagreement (market weight = 1 - w)
  P(home cover at line L) = 1 - Phi((-L - pred) / sigma)
  P(home win)             = Phi(Phi^-1(q_ML no-vig) + (pred - A) / sigma)   (anchored to the ML market)
  w / sigma: refit vs the closing line on data/history (scripts/history/fit_w.py, data/history/fit_w.json):
    NFL w=0 since 2026-10-04 (was -0.31; FPI history stamped after game day -> possible lookahead,
    refit on verified pregame FPI pending) sigma=12.70 · CFB w=0 (CI includes 0) sigma=15.14
    (earlier u3 open-track weights 0.26/0.27 were vs openers; DESK prices later lines)
  Totals: no free projected total exists (ESPN predictor carries win % and point diff only),
  so O/U shows MARKET no-vig only and MODEL stays blank; totals are never stamped.
  Market no-vig per market = DraftKings two-way pair when DK quotes it, else Bovada, else Betr,
  else the consensus median across books (source labelled per market).

Ticket rule (SOP): price only from DraftKings / Betr / Bovada as returned by The Odds API
(never invented). Edge = model% - implied% of that price. PAPER pick if edge >= 3pp and
|model - that book's two-way no-vig| <= 8pp; gap > 8pp = HOLD; otherwise PASS. 1u = $20 flat.

Ledger: data/paper_ledger.json. A game is stamped once, on its kick day (CT), before kick
(games before 10:30am CT tomorrow, e.g. London 8:30am CT, are stamped the day before),
at the stamped book price. Grading uses ESPN's public scoreboard finals: W/L/P at the stamped
price. Blank data stays blank: no FPI or no SOP price -> no pick.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nhl_model  # noqa: E402

CT = ZoneInfo("America/Chicago")
UTC = timezone.utc
ND = NormalDist()

MODEL = {
    # Refit 2026-10-02 on data/history vs the CLOSING spread (scripts/history/fit_w.py -> data/history/fit_w.json):
    #   NFL w=-0.310, 90% CI [-0.523, -0.103] (904 games 2023-26) -> CI excludes 0 -> used (fades FPI's disagreement)
    #   CFB w=+0.034, 90% CI [-0.053, +0.118] (3,934 games 2022-26) -> CI includes 0 -> w = 0 (pure market)
    #   sigma = residual SD of margin vs the fitted pred: NFL 12.70, CFB 15.14
    # 2026-10-04 (Luis-approved, weekend eval /workspace/eval/2026-10-04-weekend-eval.md sec 8 #1):
    #   NFL w set -0.310 -> 0 (pure market; stop fading ESPN FPI). Reason: possible lookahead leakage --
    #   fpi_last_modified in data/history/nfl/*.csv is AFTER game day (2023 rows stamped 2024-02-26,
    #   2024 -> 2025-02-10, 2025 -> 2026-02-09, 2026 wk1-4 -> 2026-10-02), so the FPI margins the fit used
    #   are likely not pregame. The sign also flipped (FPI side 8-2-2 ATS wk5). Keep w = 0 until refit on
    #   verified pregame FPI snapshots. Fitted value kept for reference: w_fit=-0.310, CI90 [-0.523, -0.103].
    "NFL": {"w": 0.0, "w_fit_prev": -0.310, "sigma": 12.70, "league": "nfl", "path": "football/nfl", "groups": ""},
    "CFB": {"w": 0.0, "sigma": 15.14, "league": "college-football", "path": "football/college-football",
            "groups": "&groups=80&limit=400"},
    # NHL: ESPN has no NHL predictor ("Predictor is not supported for sport: hockey") -> no independent
    # model; MODEL blank, Poisson fair prices from the market ML + total shown for information only.
    "NHL": {"w": None, "sigma": None, "league": "nhl", "path": "hockey/nhl", "groups": "&limit=100"},
    # MLB (added 2026-10-05, Luis): market only. Fitted w was 0 -> no model, MODEL blank, no stamps,
    # no live CLEARs. ESPN scoreboard used only for event ids / grading; ESPN FPI is never called.
    "MLB": {"w": None, "sigma": None, "league": "mlb", "path": "baseball/mlb", "groups": "&limit=100"},
}
MODEL_NAME = "market + w·(FPI − market); NFL w=0 since 2026-10-04 (FPI history may be post-game, refit pending), CFB 0"
MODEL_SRC = ("betbot-revamp/research/upgrades-2026-10 u3_blend open|blend:fpi mean weekly weight; "
             "FINAL_REPORT 2026-10-02: every sport stays on paper")
EDGE_MIN = 3.0      # pp vs stamped price
GAP_MAX = 8.0       # pp vs market no-vig -> HOLD
ML_CAP = 500        # ML side only when both prices within +-100..500
UNIT_USD = 20
EARLY_CUTOFF_MIN = 10 * 60 + 30  # games before 10:30am CT tomorrow are stamped today (next run may be late)
SOP_KEYS = {"draftkings": "DraftKings", "bovada": "Bovada"}  # Betr matched by title below
SB_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={d}{g}"
PRED_URL = "https://sports.core.api.espn.com/v2/sports/football/leagues/{lg}/events/{i}/competitions/{i}/predictor"
UA = {"User-Agent": "desk-v1-paper-model (github actions)"}
LEDGER_SCHEMA = "desk-paper-ledger/v1"


# ----------------------------------------------------------------- small utils
def log(msg: str) -> None:
    print(f"[paper] {msg}", flush=True)


def get_json(url: str, tries: int = 3, timeout: int = 20):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            time.sleep(1 + k)
        except Exception:
            time.sleep(1 + k)
    return None


def implied(a: float) -> float:
    a = float(a)
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def dec(a: float) -> float:
    a = float(a)
    return 1 + a / 100.0 if a > 0 else 1 + 100.0 / -a


def novig(pa: float, pb: float) -> float:
    x, y = implied(pa), implied(pb)
    return x / (x + y)


def r1(x):
    return None if x is None else round(float(x), 1)


def norm_tokens(name: str) -> set[str]:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("'", "").replace("&", " and ")
    return {t for t in re.findall(r"[a-z0-9]+", s) if t not in {"the", "of", "university"}}


def name_score(a: str, b: str) -> float:
    ta, tb = norm_tokens(a), norm_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def sop_book(bk: dict) -> str:
    key = (bk.get("key") or "").lower()
    title = bk.get("title") or ""
    if key in SOP_KEYS:
        return SOP_KEYS[key]
    if key == "betr" or key.startswith("betr_") or title.strip().lower() == "betr":
        return "Betr"
    return ""


def kick_ct(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(CT)


# ----------------------------------------------------------------- ESPN pulls
def espn_events(sport: str, days: list[str]) -> dict[str, dict]:
    """Scoreboard per ESPN date (range queries are rejected). Returns {event_id: compact}."""
    cfg = MODEL[sport]
    out: dict[str, dict] = {}
    for d in days:
        js = get_json(SB_URL.format(path=cfg["path"], d=d, g=cfg["groups"]))
        for e in (js or {}).get("events") or []:
            try:
                c = e["competitions"][0]
                comp = {x["homeAway"]: x for x in c["competitors"]}
                st = e.get("status", {}).get("type", {})
                out[str(e["id"])] = {
                    "id": str(e["id"]), "date": e.get("date"),
                    "home": comp["home"]["team"].get("displayName") or "",
                    "away": comp["away"]["team"].get("displayName") or "",
                    "home_id": str(comp["home"]["team"].get("id")),
                    "away_id": str(comp["away"]["team"].get("id")),
                    "home_score": comp["home"].get("score"), "away_score": comp["away"].get("score"),
                    "completed": bool(st.get("completed")), "state": st.get("state"),
                    "status": st.get("name") or "",
                }
            except (KeyError, IndexError, TypeError):
                continue
    return out


def espn_fpi(sport: str, event_id: str):
    js = get_json(PRED_URL.format(lg=MODEL[sport]["league"], i=event_id))
    if not js:
        return None
    for s in ((js.get("homeTeam") or {}).get("statistics") or []):
        if s.get("name") == "teamPredPtDiff" and s.get("value") is not None:
            return {"home_pred_pt_diff": float(s["value"]), "last_modified": js.get("lastModified")}
    return None


def espn_days(start_ct: datetime, n_days: int) -> list[str]:
    return [(start_ct + timedelta(days=k)).strftime("%Y%m%d") for k in range(-1, n_days + 1)]


def match_espn(row: dict, events: dict[str, dict], used: set[str]):
    """Best ESPN event within 8h kick and both team names overlapping. Returns (event, swapped)."""
    try:
        k = datetime.fromisoformat(row["kick_utc"].replace("Z", "+00:00"))
    except Exception:
        return None, False
    best, best_s, best_sw = None, 0.0, False
    for ev in events.values():
        if ev["id"] in used or not ev.get("date"):
            continue
        try:
            ek = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if abs((ek - k).total_seconds()) > 8 * 3600:
            continue
        for sw in (False, True):
            eh, ea = (ev["away"], ev["home"]) if sw else (ev["home"], ev["away"])
            sh, sa = name_score(row["home"], eh), name_score(row["away"], ea)
            if sh <= 0 or sa <= 0:
                continue
            s = sh + sa - (0.01 if sw else 0)
            if s > best_s:
                best, best_s, best_sw = ev, s, sw
    if best is None or best_s < 0.5:
        return None, False
    return best, best_sw


# ----------------------------------------------------------------- the blend
SOP_ORDER = ("DraftKings", "Bovada", "Betr")
TOTALS_NOTE = "MODEL blank: no free projected total (ESPN predictor has win % and point diff only)"


def book_quotes(ev: dict, row: dict) -> dict:
    """Every book's two-sided quotes exactly as returned. {book_title: {'ml':(h,a), 'spread':(L,h,a), 'total':(T,o,u)}}"""
    home, away = row["home"], row["away"]
    out: dict[str, dict] = {}
    for bk in ev.get("bookmakers") or []:
        title = sop_book(bk) or bk.get("title") or bk.get("key") or ""
        q = out.setdefault(title, {"sop": bool(sop_book(bk))})
        for m in bk.get("markets") or []:
            outs = {o.get("name"): o for o in m.get("outcomes") or []}
            k = m.get("key")
            try:
                if k == "h2h" and home in outs and away in outs:
                    q["ml"] = (int(outs[home]["price"]), int(outs[away]["price"]))
                elif k == "spreads" and home in outs and away in outs:
                    hp, ap = outs[home].get("point"), outs[away].get("point")
                    if hp is not None and ap is not None and abs(hp + ap) < 1e-9:
                        q["spread"] = (float(hp), int(outs[home]["price"]), int(outs[away]["price"]))
                elif k == "totals" and "Over" in outs and "Under" in outs:
                    op, up = outs["Over"].get("point"), outs["Under"].get("point")
                    if op is not None and op == up:
                        q["total"] = (float(op), int(outs["Over"]["price"]), int(outs["Under"]["price"]))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def model_probs(sport: str, row: dict, fpi_home, q_ml_home):
    """Blend pred + functions giving model P for each market/side. None when not computable."""
    if fpi_home is None or row.get("spread_home_line") is None:
        return None
    cfg = MODEL[sport]
    w, sig = cfg["w"], cfg["sigma"]
    A = -float(row["spread_home_line"])
    pred = A + w * (fpi_home - A)

    def cover_home(L):
        return 1 - ND.cdf((-L - pred) / sig)

    def win_home(q):
        if q is None:
            return None
        z = ND.inv_cdf(min(max(q, 1e-4), 1 - 1e-4))
        return ND.cdf(z + (pred - A) / sig)

    return {"A": A, "pred": pred, "w": w, "sigma": sig, "cover_home": cover_home, "win_home": win_home}


def side_dict(side, label, line, price, book, novig_p, model_p):
    d = {"side": side, "label": label, "line": line, "price_american": price, "book": book,
         "novig_pct": r1(None if novig_p is None else 100 * novig_p),
         "model_pct": r1(None if model_p is None else 100 * model_p),
         "implied_pct": r1(None if price is None else 100 * implied(price)),
         "edge_pp": None, "gap_pp": None, "strong": False}
    if model_p is not None and price is not None:
        d["_edge"] = 100 * (model_p - implied(price))   # unrounded, used by the rules
        d["edge_pp"] = r1(d["_edge"])
        if novig_p is not None:
            d["_gap"] = 100 * (model_p - novig_p)
            d["gap_pp"] = r1(d["_gap"])
    return d


def ref_quote(quotes: dict, key: str):
    """Display reference: DraftKings pair, else Bovada, else Betr. None -> consensus."""
    for b in SOP_ORDER:
        if b in quotes and key in quotes[b]:
            return b, quotes[b][key]
    return None, None


def fmt_line(x: float) -> str:
    return "PK" if x == 0 else (f"+{x:g}" if x > 0 else f"{x:g}")


def three_markets(sport: str, row: dict, quotes: dict, mp) -> dict:
    """ML / Spread / O-U, both sides: price, book, no-vig % (DK pair if available), MODEL %, edge."""
    home, away = row["home"], row["away"]
    out = {}
    # --- moneyline
    b, q = ref_quote(quotes, "ml")
    if q:
        hp, ap = q
        nv_h, src, book_h, book_a = novig(hp, ap), f"{b} pair", b, b
    elif row.get("ml_home_price") is not None:
        hp, ap = row["ml_home_price"], row["ml_away_price"]
        nv_h = None if row.get("market_home_ml_pct") is None else row["market_home_ml_pct"] / 100
        src, book_h, book_a = f"consensus median ({row.get('ml_books')} bk)", row.get("ml_home_book"), row.get("ml_away_book")
    else:
        hp = ap = nv_h = None
    if hp is not None:
        pw = mp["win_home"](nv_h) if mp else None
        cap = 100 <= abs(hp) <= ML_CAP and 100 <= abs(ap) <= ML_CAP
        out["ml"] = {"novig_source": src, "stampable": cap,
                     "note": "" if cap else f"ML beyond ±{ML_CAP}: shown, never stamped",
                     "sides": [side_dict("away", f"{away} ML", None, ap, book_a, None if nv_h is None else 1 - nv_h,
                                         None if pw is None else 1 - pw),
                               side_dict("home", f"{home} ML", None, hp, book_h, nv_h, pw)]}
    # --- spread
    b, q = ref_quote(quotes, "spread")
    if q:
        L, hp, ap = q
        nv_h, src, book_h, book_a = novig(hp, ap), f"{b} pair", b, b
    elif row.get("spread_home_line") is not None and row.get("spread_home_price") is not None:
        L, hp, ap = row["spread_home_line"], row["spread_home_price"], row["spread_away_price"]
        nv_h = None if row.get("market_home_cover_pct") is None else row["market_home_cover_pct"] / 100
        src, book_h, book_a = f"consensus median ({row.get('spread_books')} bk)", row.get("spread_home_book"), row.get("spread_away_book")
    else:
        L = None
    if L is not None:
        pc = mp["cover_home"](L) if mp else None
        out["spread"] = {"novig_source": src, "stampable": True, "note": "", "line_home": L,
                         "sides": [side_dict("away", f"{away} {fmt_line(-L)}", -L, ap, book_a,
                                             None if nv_h is None else 1 - nv_h, None if pc is None else 1 - pc),
                                   side_dict("home", f"{home} {fmt_line(L)}", L, hp, book_h, nv_h, pc)]}
    # --- totals (market only: no free model total)
    b, q = ref_quote(quotes, "total")
    if q:
        T, op, up = q
        nv_o, src, book_o, book_u = novig(op, up), f"{b} pair", b, b
    elif row.get("total_line") is not None and row.get("over_price") is not None:
        T, op, up = row["total_line"], row["over_price"], row["under_price"]
        nv_o = None if row.get("market_over_pct") is None else row["market_over_pct"] / 100
        src, book_o, book_u = f"consensus median ({row.get('totals_books')} bk)", row.get("over_book"), row.get("under_book")
    else:
        T = None
    if T is not None:
        out["total"] = {"novig_source": src, "stampable": False, "note": TOTALS_NOTE, "line": T,
                        "sides": [side_dict("over", f"Over {T:g}", T, op, book_o, nv_o, None),
                                  side_dict("under", f"Under {T:g}", T, up, book_u, None if nv_o is None else 1 - nv_o, None)]}
    for m in out.values():  # stronger side = higher MODEL edge; no model -> none marked
        s = [x for x in m["sides"] if x["edge_pp"] is not None]
        if len(s) == 2 and s[0]["_edge"] != s[1]["_edge"]:
            max(s, key=lambda x: x["_edge"])["strong"] = True
    return out


def stamp_candidates(row: dict, quotes: dict, mp) -> dict[str, list[dict]]:
    """Every SOP-book price (DraftKings/Betr/Bovada) per market, each book at its own line."""
    home, away = row["home"], row["away"]
    c: dict[str, list[dict]] = {"ml": [], "spread": []}
    if not mp:
        return c
    q_ml = None if row.get("market_home_ml_pct") is None else row["market_home_ml_pct"] / 100
    dk = quotes.get("DraftKings", {}).get("ml")
    if dk:
        q_ml = novig(*dk)  # anchor ML to the DK pair when available, like the display
    for book, q in quotes.items():
        if not q.get("sop"):
            continue
        if "spread" in q:
            L, hp, ap = q["spread"]
            p = mp["cover_home"](L)
            nv = novig(hp, ap)
            c["spread"] += [side_dict("home", f"{home} {fmt_line(L)}", L, hp, book, nv, p),
                            side_dict("away", f"{away} {fmt_line(-L)}", -L, ap, book, 1 - nv, 1 - p)]
        if "ml" in q:
            hp, ap = q["ml"]
            if 100 <= abs(hp) <= ML_CAP and 100 <= abs(ap) <= ML_CAP:
                p = mp["win_home"](q_ml)
                if p is not None:
                    nv = novig(hp, ap)
                    c["ml"] += [side_dict("home", f"{home} ML", None, hp, book, nv, p),
                                side_dict("away", f"{away} ML", None, ap, book, 1 - nv, 1 - p)]
    return c


def blend_row(sport: str, row: dict, ev: dict, fpi_home) -> dict:
    """Paper block for one game: three markets for display + per-market stamp decision."""
    quotes = book_quotes(ev, row)
    dk_ml = quotes.get("DraftKings", {}).get("ml")
    q_ml = novig(*dk_ml) if dk_ml else (None if row.get("market_home_ml_pct") is None else row["market_home_ml_pct"] / 100)
    mp = model_probs(sport, row, fpi_home, q_ml)
    cfg = MODEL[sport]
    blk = {"label": "PAPER", "model": MODEL_NAME, "w_fpi": cfg["w"], "w_market": round(1 - cfg["w"], 3),
           "sigma": cfg["sigma"], "fpi_home_margin": None if fpi_home is None else round(fpi_home, 2),
           "status": "PASS", "pick": None, "best": None, "picks": [], "by_market": {}, "note": ""}
    blk["markets"] = three_markets(sport, row, quotes, mp)
    if mp:
        blk["mkt_home_margin"] = round(mp["A"], 2)
        blk["blend_home_margin"] = round(mp["pred"], 2)
        blk["fair_home_line"] = round(-mp["pred"], 1)
    else:
        blk["status"] = "BLANK"
        blk["note"] = "no ESPN FPI on file — MODEL blank" if fpi_home is None else "no consensus spread — MODEL blank"
        return blk
    cands = stamp_candidates(row, quotes, mp)
    statuses = []
    for mkt, cs in cands.items():
        if not cs:
            blk["by_market"][mkt] = {"status": "BLANK", "note": "no DraftKings/Betr/Bovada price returned"}
            continue
        best = max(cs, key=lambda x: (x["_edge"], x["price_american"]))
        best = {**best, "market": mkt, "team": home_or_away(row, best["side"]), "selection": best["label"]}
        if abs(best["_gap"]) > GAP_MAX:
            st, note = "HOLD", f"gap {best['_gap']:+.1f}pp vs no-vig > {GAP_MAX:g}pp"
        elif best["_edge"] >= EDGE_MIN:
            st, note = "PAPER", f"PAPER 1u (${UNIT_USD}) — no real bet"
            blk["picks"].append(best)
        else:
            st, note = "PASS", f"edge {best['_edge']:+.1f}pp < {EDGE_MIN:g}pp"
        blk["by_market"][mkt] = {"status": st, "note": note, "best": best}
        statuses.append((st, best, note))
    blk["by_market"]["total"] = {"status": "BLANK", "note": TOTALS_NOTE}
    if not statuses:
        blk["status"], blk["note"] = "BLANK", "no DraftKings/Betr/Bovada price returned"
        return blk
    rank = {"PAPER": 0, "HOLD": 1, "PASS": 2}
    st, best, note = min(statuses, key=lambda t: (rank[t[0]], -t[1]["_edge"]))
    blk["status"], blk["best"], blk["note"] = st, best, note
    blk["pick"] = best if st == "PAPER" else None
    return blk


def home_or_away(row: dict, side: str) -> str:
    return row["home"] if side == "home" else row["away"]


# ----------------------------------------------------------------- NHL
NHL_ABBR = {
    "Anaheim Ducks": "ANA", "Boston Bruins": "BOS", "Buffalo Sabres": "BUF", "Calgary Flames": "CGY",
    "Carolina Hurricanes": "CAR", "Chicago Blackhawks": "CHI", "Colorado Avalanche": "COL",
    "Columbus Blue Jackets": "CBJ", "Dallas Stars": "DAL", "Detroit Red Wings": "DET", "Edmonton Oilers": "EDM",
    "Florida Panthers": "FLA", "Los Angeles Kings": "LAK", "Minnesota Wild": "MIN", "Montréal Canadiens": "MTL",
    "Montreal Canadiens": "MTL", "Nashville Predators": "NSH", "New Jersey Devils": "NJD",
    "New York Islanders": "NYI", "New York Rangers": "NYR", "Ottawa Senators": "OTT", "Philadelphia Flyers": "PHI",
    "Pittsburgh Penguins": "PIT", "San Jose Sharks": "SJS", "Seattle Kraken": "SEA", "St Louis Blues": "STL",
    "St. Louis Blues": "STL", "Tampa Bay Lightning": "TBL", "Toronto Maple Leafs": "TOR", "Utah Mammoth": "UTA",
    "Utah Hockey Club": "UTA", "Vancouver Canucks": "VAN", "Vegas Golden Knights": "VGK",
    "Washington Capitals": "WSH", "Winnipeg Jets": "WPG",
}
GOALIE_LOGS = ("data/nhl_goalie_log.jsonl", "/workspace/research/nhl-2026-10/sit/goalie_log.jsonl")


def load_goalies(repo_root: Path) -> dict:
    """Latest goalie-log entry per (game date CT, team). Missing file -> {} (goalie stays blank)."""
    out: dict = {}
    for gp in GOALIE_LOGS:
        f = Path(gp) if gp.startswith("/") else repo_root / gp
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                g = json.loads(line)
                k = (str(g.get("game_start_ct") or "")[:10], g.get("team"))
            except (json.JSONDecodeError, AttributeError):
                continue
            if not g.get("goalie") or g.get("status") not in ("Confirmed", "Likely"):
                continue
            if k not in out or str(g.get("seen_time_ct") or "") >= str(out[k].get("seen_time_ct") or ""):
                out[k] = {"goalie": g["goalie"], "status": g["status"], "source": g.get("source"),
                          "seen_time_ct": g.get("seen_time_ct"), "file": f.name}
    return out


def nhl_block(row: dict, ev: dict, goalies: dict, lm=None) -> dict:
    """ML / puck line / O-U: market no-vig %, Poisson fair (from market), raw MODEL % (our NHL ratings model).
    Stamps use the blend logit(p) = logit(q) + w*(logit(model) - logit(q)) with the walk-forward-fitted w
    (0 unless its 90% CI excludes 0) and the usual edge >= 3pp / gap <= 8pp rules."""
    quotes = book_quotes(ev, row)
    wts = (lm.weights if lm else {}) or {}
    blk = {"label": "PAPER", "model": "NHL ratings model (team att/def goal rates, decay+shrinkage, home ice, "
                                      "back-to-back, starting goalie) -> Poisson REG/OT/SO",
           "status": "BLANK", "pick": None, "best": None, "picks": [], "by_market": {},
           "w": {k: wts.get(k, 0.0) for k in ("ml", "pl", "tot")},
           "note": "MODEL blank: NHL model unavailable this run" if not lm else ""}
    mk = three_markets("NHL", row, quotes, None)
    d = row["kick_utc"] and kick_ct(row["kick_utc"]).date().isoformat()
    ha, aa = NHL_ABBR.get(row["home"]), NHL_ABBR.get(row["away"])
    blk["goalies"] = {"home": goalies.get((d, ha)), "away": goalies.get((d, aa))}
    # starting-goalie check (DailyFaceoff via scripts/nhl_goalies.py): flag unless BOTH starters are Confirmed
    unconf = [side for side in ("away", "home") if not blk["goalies"][side] or blk["goalies"][side].get("status") != "Confirmed"]
    blk["goalie_flag"] = ("" if not unconf else
                          "GOALIE UNCONFIRMED (" + ", ".join(unconf) + ") — model uses the recent-starter mix / likely starter; re-check before puck drop")
    if lm is not None and getattr(lm, "model_name", ""):
        blk["model"] = lm.model_name
    try:
        nv_h = mk["ml"]["sides"][1]["novig_pct"] / 100
        T = mk["total"]["line"]
        nv_o = mk["total"]["sides"][0]["novig_pct"] / 100
        lh, la = nhl_model.market_lambdas(nv_h, nv_o, T)
        L = mk["spread"]["line_home"] if "spread" in mk else None
        f = nhl_model.probs(lh, la, home_line=L, total=T)
        fair = {"ml": (1 - f["ml_home"], f["ml_home"]), "total": (f["over"], 1 - f["over"])}
        if L is not None and f.get("pl_home") is not None:
            fair["spread"] = (1 - f["pl_home"], f["pl_home"])
        for k, (a, b) in fair.items():
            sides = mk[k]["sides"]
            sides[0]["fair_pct"], sides[1]["fair_pct"] = r1(100 * a), r1(100 * b)
        blk["fair"] = {"lam_home": round(lh, 3), "lam_away": round(la, 3), "reg_tie_pct": r1(100 * f["reg_tie"]),
                       "source": "Poisson + draw inflation 1.50 + empty-net overlay + OT/SO (P home 0.524), "
                                 "solved to the market no-vig ML and total"}
    except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError):
        blk["fair"] = None
    if lm and ha and aa:
        try:
            L = mk["spread"]["line_home"] if "spread" in mk else None
            T = mk["total"]["line"] if "total" in mk else None
            pr = lm.predict(ha, aa, kick_ct(row["kick_utc"]).date(), blk["goalies"]["home"], blk["goalies"]["away"],
                            home_line=L, total=T)
            blk["nhl_model"] = {
                "reg_goals_home": round(pr["reg_goals_home"], 2), "reg_goals_away": round(pr["reg_goals_away"], 2),
                "reg_tie_pct": r1(100 * pr["reg_tie"]), "b2b_home": pr["b2b_home"], "b2b_away": pr["b2b_away"],
                "goalie_home": pr["goalie_src_home"], "goalie_away": pr["goalie_src_away"],
                "gsax_pg_home": round(pr["gq_home"], 2), "gsax_pg_away": round(pr["gq_away"], 2)}
            first = {"ml": 1 - pr["ml_home"], "spread": None if pr.get("pl_home") is None else 1 - pr["pl_home"],
                     "total": pr.get("over")}
            wkey = {"ml": "ml", "spread": "pl", "total": "tot"}
            statuses = []
            for k, m in mk.items():
                p0 = first.get(k)
                if p0 is None:
                    continue
                sides = m["sides"]
                pa, pb = sides[0]["price_american"], sides[1]["price_american"]
                q0 = novig(pa, pb)
                w = wts.get(wkey[k], 0.0)
                new_sides = []
                for sd, pm, q in ((sides[0], p0, q0), (sides[1], 1 - p0, 1 - q0)):
                    nd = side_dict(sd["side"], sd["label"], sd["line"], sd["price_american"], sd["book"], q, pm)
                    nd["novig_pct"] = sd["novig_pct"]
                    if "fair_pct" in sd:
                        nd["fair_pct"] = sd["fair_pct"]
                    lq = math.log(q / (1 - q))
                    pbld = 1 / (1 + math.exp(-(lq + w * (math.log(pm / (1 - pm)) - lq))))
                    nd["blend_pct"] = r1(100 * pbld)
                    nd["_bedge"] = 100 * (pbld - implied(sd["price_american"]))
                    new_sides.append(nd)
                m["sides"] = new_sides
                m["note"] = (f"MODEL = raw NHL model (shown even at w=0). Stamps use blend w={w:g}"
                             + (" — 90% CI includes 0, so market only; never stamps" if w == 0 else ""))
                m["stampable"] = m.get("stampable", True) and w != 0
                if w == 0:
                    blk["by_market"][k] = {"status": "PASS", "note": f"w=0 (walk-forward CI includes 0): model shown, not used"}
                    statuses.append("PASS")
                    continue
                for nd in new_sides:
                    nd["strong"] = False
                best = max(new_sides, key=lambda x: x["_bedge"])
                best["strong"] = True
                gap = 100 * ((best["model_pct"] or 0) - (best["novig_pct"] or 0)) / 1.0
                b2 = {**best, "market": k, "team": home_or_away(row, best["side"]) if k != "total" else best["label"],
                      "selection": best["label"], "edge_pp": r1(best["_bedge"]), "model_pct": best["blend_pct"]}
                if not m.get("stampable", True):
                    st, note = "PASS", m.get("note") or "not stampable"
                elif abs(gap) > GAP_MAX:
                    st, note = "HOLD", f"gap {gap:+.1f}pp vs no-vig > {GAP_MAX:g}pp"
                elif best["_bedge"] >= EDGE_MIN:
                    st, note = "PAPER", f"PAPER 1u (${UNIT_USD}) — no real bet"
                    blk["picks"].append(b2)
                else:
                    st, note = "PASS", f"edge {best['_bedge']:+.1f}pp < {EDGE_MIN:g}pp"
                blk["by_market"][k] = {"status": st, "note": note, "best": b2}
                statuses.append(st)
            if statuses:
                blk["status"] = "PAPER" if "PAPER" in statuses else ("HOLD" if "HOLD" in statuses else "PASS")
                blk["pick"] = blk["picks"][0] if blk["picks"] else None
                blk["note"] = ("raw NHL model shown; w=0 on every market (walk-forward CI includes 0) — market only, no stamps"
                               if all(wts.get(x, 0.0) == 0 for x in ("ml", "pl", "tot")) else "")
        except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError) as e:
            blk["note"] = f"MODEL blank: NHL model error ({type(e).__name__})"
    blk["markets"] = mk
    return blk


def mlb_block(row: dict, ev: dict) -> dict:
    """ML / run line / O-U: market no-vig % only (fitted w = 0 -> no MODEL %, no edges, never stamped)."""
    mk = three_markets("MLB", row, book_quotes(ev, row), None)
    return {"label": "PAPER", "model": "none (market only, MODEL blank; fitted w = 0)",
            "status": "PASS" if mk else "BLANK", "pick": None, "best": None, "picks": [],
            "by_market": {k: {"status": "PASS", "note": "market only (w = 0) — no stamps", "best": None} for k in mk},
            "w": 0.0, "markets": mk,
            "note": "MLB market only: fitted w = 0 — MODEL blank, no paper stamps" if mk else "no market lines"}


def attach_paper(sport: str, rows: list[dict], events_by_toa: dict[str, dict], now_ct: datetime,
                 window_days: int) -> dict:
    """Adds row['paper'] (+ espn ids) to each row. Returns stats."""
    stats = {"games": len(rows), "espn_matched": 0, "with_fpi": 0, "paper": 0, "hold": 0, "pass": 0, "blank": 0,
             "paper_picks": 0}
    if not rows:
        return stats
    start = now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
    espn = espn_events(sport, espn_days(start, window_days))
    log(f"{sport}: ESPN scoreboard events {len(espn)}")
    used: set[str] = set()
    matched = []
    for r in rows:
        ev, sw = match_espn(r, espn, used)
        if ev:
            used.add(ev["id"])
            r["espn_event_id"] = ev["id"]
            r["espn_home_id"] = ev["away_id"] if sw else ev["home_id"]
            r["espn_away_id"] = ev["home_id"] if sw else ev["away_id"]
            r["espn_swapped"] = sw
            matched.append((r, ev, sw))
    stats["espn_matched"] = len(matched)
    if sport == "MLB":
        for r in rows:
            r["paper"] = mlb_block(r, events_by_toa.get(r.get("toa_event_id") or "", {}))
            st = r["paper"]["status"].lower()
            stats[st] = stats.get(st, 0) + 1
        return stats
    if sport == "NHL":
        goalies = load_goalies(Path(__file__).resolve().parents[1])
        lm = None
        try:
            import nhl_xg  # 2026-10-09: xG model (2023+ only, 0.5 xG + 0.5 goals); research-only, w locked 0
            lm = nhl_xg.XGLiveModel(now_ct.date())
            stats["nhl_model"] = {"last_final": lm.last_final, "season_games": lm.this_season_games,
                                  "w": {k: lm.weights.get(k) for k in ("ml", "pl", "tot")}, "notes": lm.notes,
                                  "home": round(lm.P["home"], 3), "b2b_off": round(lm.P["b2b_off"], 3),
                                  "b2b_def": round(lm.P["b2b_def"], 3), "goalie_coef": round(lm.P["g"], 3),
                                  "model": getattr(lm, "model_name", "")}
            log(f"NHL model: {stats['nhl_model']}")
        except Exception as e:  # noqa: BLE001 — never block the board; MODEL stays blank
            log(f"NHL model unavailable: {e}")
        for r in rows:
            r["paper"] = nhl_block(r, events_by_toa.get(r.get("toa_event_id") or "", {}), goalies, lm)
            st = r["paper"]["status"].lower()
            stats[st] = stats.get(st, 0) + 1
            stats["paper_picks"] += len(r["paper"].get("picks") or [])
        stats["with_model"] = sum(1 for r in rows if r["paper"].get("nhl_model"))
        stats["goalies_shown"] = sum(1 for r in rows for v in (r["paper"].get("goalies") or {}).values() if v)
        stats["with_fair"] = sum(1 for r in rows if r["paper"].get("fair"))
        return stats
    with cf.ThreadPoolExecutor(8) as ex:
        fpis = list(ex.map(lambda t: espn_fpi(sport, t[1]["id"]), matched))
    fpi_by_row = {id(t[0]): (f, t[2]) for t, f in zip(matched, fpis)}
    for r in rows:
        f, sw = fpi_by_row.get(id(r), (None, False))
        fpi_home = None
        if f:
            stats["with_fpi"] += 1
            fpi_home = -f["home_pred_pt_diff"] if sw else f["home_pred_pt_diff"]
        blk = blend_row(sport, r, events_by_toa.get(r.get("toa_event_id") or "", {}), fpi_home)
        if f:
            blk["fpi_last_modified"] = f.get("last_modified")
        elif not r.get("espn_event_id"):
            blk["note"] = "no ESPN match — MODEL blank"
        r["paper"] = blk
        stats[blk["status"].lower()] = stats.get(blk["status"].lower(), 0) + 1
        stats["paper_picks"] += len(blk.get("picks") or [])
    return stats


# ----------------------------------------------------------------- ledger
def load_ledger(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"schema": LEDGER_SCHEMA, "picks": []}


def pick_id(sport: str, row: dict, market: str = "") -> str:
    base = f"{sport.lower()}-{row.get('espn_event_id') or row.get('toa_event_id')}"
    return f"{base}-{market}" if market else base


def stamp_today(ledger: dict, rows_by_sport: dict[str, list[dict]], now_ct: datetime) -> list[dict]:
    """Stamp PAPER picks (one per market per game) for games kicking later today (CT). Never re-priced."""
    have = {p["id"] for p in ledger.get("picks") or []}
    new = []
    for sport, rows in rows_by_sport.items():
        for r in rows:
            blk = r.get("paper") or {}
            kc = kick_ct(r["kick_utc"])
            if kc <= now_ct:
                continue
            # kick day (CT), or tomorrow before the next 9:15am CT run can catch it (London 8:30am CT)
            early_tomorrow = kc.date() == now_ct.date() + timedelta(days=1) and kc.hour * 60 + kc.minute < EARLY_CUTOFF_MIN
            if kc.date() != now_ct.date() and not early_tomorrow:
                continue
            for pk in blk.get("picks") or []:
                pid = pick_id(sport, r, pk["market"])
                if pid in have:
                    continue
                new.append({
                    "id": pid, "label": "PAPER", "sport": sport, "slate_date_ct": now_ct.date().isoformat(),
                    "stamped_at_ct": now_ct.isoformat(timespec="seconds"), "kick_utc": r["kick_utc"],
                    "kick_ct": r.get("kick_ct"), "matchup": r.get("matchup"), "away": r["away"], "home": r["home"],
                    "espn_event_id": r.get("espn_event_id"), "espn_home_id": r.get("espn_home_id"),
                    "espn_away_id": r.get("espn_away_id"), "toa_event_id": r.get("toa_event_id"),
                    "market": pk["market"], "side": pk["side"], "team": pk.get("team"), "selection": pk["selection"],
                    "line": pk["line"], "price_american": pk["price_american"], "book": pk["book"],
                    "model_pct": pk["model_pct"], "novig_pct": pk["novig_pct"], "implied_pct": pk["implied_pct"],
                    "edge_pp": pk["edge_pp"], "gap_pp": pk["gap_pp"],
                    "fpi_home_margin": blk.get("fpi_home_margin"), "mkt_home_margin": blk.get("mkt_home_margin"),
                    "blend_home_margin": blk.get("blend_home_margin"), "w_fpi": blk.get("w_fpi"), "sigma": blk.get("sigma"),
                    "units": 1, "stake_usd": UNIT_USD, "status": "open", "result": None, "score": None,
                    "pnl_units": None, "pnl_usd": None,
                })
                have.add(pid)
    ledger.setdefault("picks", []).extend(new)
    return new


def settle(p: dict, ev: dict):
    try:
        hs, as_ = float(ev["home_score"]), float(ev["away_score"])
    except (TypeError, ValueError):
        return None
    # scores in our (Odds API) orientation via ESPN team ids
    if ev["home_id"] == p.get("espn_home_id"):
        our_h, our_a = hs, as_
    elif ev["away_id"] == p.get("espn_home_id"):
        our_h, our_a = as_, hs
    else:
        return None
    if p["market"] == "total":
        tot = our_h + our_a
        x = (tot - float(p["line"])) if p["side"] == "over" else (float(p["line"]) - tot)
    else:
        margin = (our_h - our_a) if p["side"] == "home" else (our_a - our_h)
        x = margin + float(p["line"]) if p["market"] == "spread" else margin
    res = "W" if x > 0 else ("L" if x < 0 else "P")
    pnl = round(dec(p["price_american"]) - 1, 4) if res == "W" else (-1.0 if res == "L" else 0.0)
    score = f"{p['away']} {int(our_a)} - {p['home']} {int(our_h)}"
    return res, pnl, score


def grade(ledger: dict, now_ct: datetime) -> list[dict]:
    open_p = [p for p in ledger.get("picks") or [] if p.get("status") == "open"]
    if not open_p:
        return []
    graded = []
    by_sport: dict[str, set[str]] = {}
    for p in open_p:
        kc = kick_ct(p["kick_utc"])
        if kc > now_ct:
            continue
        ds = by_sport.setdefault(p["sport"], set())
        for k in (-1, 0, 1):
            ds.add((kc + timedelta(days=k)).strftime("%Y%m%d"))
    events = {s: espn_events(s, sorted(d)) for s, d in by_sport.items()}
    for p in open_p:
        ev = events.get(p["sport"], {}).get(str(p.get("espn_event_id")))
        if not ev:
            continue
        if ev["completed"] and ev["state"] == "post" and "CANCEL" not in ev["status"] and "POSTPONE" not in ev["status"]:
            s = settle(p, ev)
            if not s:
                continue
            p["result"], p["pnl_units"], p["score"] = s
            p["pnl_usd"] = round(p["pnl_units"] * UNIT_USD, 2)
            p["status"] = "graded"
            p["graded_at_ct"] = now_ct.isoformat(timespec="seconds")
            p["score_source"] = f"ESPN scoreboard event {ev['id']} ({ev['status']})"
            graded.append(p)
        elif ("CANCEL" in ev["status"] or "POSTPONE" in ev["status"]) and \
                now_ct - kick_ct(p["kick_utc"]) > timedelta(days=3):
            p.update(status="void", result="VOID", pnl_units=0.0, pnl_usd=0.0,
                     graded_at_ct=now_ct.isoformat(timespec="seconds"),
                     score_source=f"ESPN {ev['status']} > 3 days")
            graded.append(p)
    return graded


def summarize(ledger: dict, now_ct: datetime) -> dict:
    picks = ledger.get("picks") or []

    def block(ps):
        g = [p for p in ps if p.get("status") == "graded"]
        w = sum(p["result"] == "W" for p in g)
        l_ = sum(p["result"] == "L" for p in g)
        pu = sum(p["result"] == "P" for p in g)
        u = round(sum(p["pnl_units"] or 0 for p in g), 2)
        return {"record": f"{w}-{l_}-{pu}", "w": w, "l": l_, "p": pu, "units": u,
                "usd": round(u * UNIT_USD, 2), "graded": len(g),
                "open": sum(p.get("status") == "open" for p in ps)}

    s = block(picks)
    s["by_sport"] = {sp: block([p for p in picks if p["sport"] == sp]) for sp in ("NFL", "CFB", "NHL", "MLB")}
    s["unit_usd"] = UNIT_USD
    s["label"] = "PAPER — no real bets"
    ledger["schema"] = LEDGER_SCHEMA
    ledger["updated_at_ct"] = now_ct.isoformat(timespec="seconds")
    ledger["model"] = {"name": MODEL_NAME, "source": MODEL_SRC,
                       "params": {k: ({"w_fpi": v["w"], "w_market": round(1 - v["w"], 3), "sigma": v["sigma"]}
                                      if v["w"] is not None else {"model": "none (market only, MODEL blank)"})
                                  for k, v in MODEL.items()},
                       "rules": {"edge_min_pp": EDGE_MIN, "gap_hold_pp": GAP_MAX, "unit_usd": UNIT_USD,
                                 "books": "DraftKings / Betr / Bovada as returned by The Odds API",
                                 "stamp": "once per market per game on kick day (CT) before kick; games before 10:30am CT are stamped the prior day; totals never (no model)",
                                 "grading": "ESPN public scoreboard finals; W/L/P at stamped price"}}
    ledger["summary"] = s
    return s


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Grade open PAPER picks (ESPN finals, free).")
    ap.add_argument("cmd", choices=["grade"])
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[1] / "data"))
    ap.add_argument("--now", help="override now (ISO)")
    a = ap.parse_args(argv)
    now_ct = datetime.fromisoformat(a.now).astimezone(CT) if a.now else datetime.now(CT)
    path = Path(a.data_dir) / "paper_ledger.json"
    led = load_ledger(path)
    if not path.exists() and not led["picks"]:
        log("no ledger yet — nothing to grade")
        return 0
    g = grade(led, now_ct)
    s = summarize(led, now_ct)
    for p in g:
        log(f"graded {p['id']} {p['selection']} {p['price_american']} {p['book']} -> {p['result']} "
            f"{p['pnl_units']:+.2f}u ({p.get('score')})")
    log(f"paper record {s['record']} · {s['units']:+.2f}u (${s['usd']:+.2f}) · open {s['open']}")
    write_json(path, led)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
