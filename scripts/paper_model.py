#!/usr/bin/env python3
"""DESK PAPER model — u3 market-heavy blend for NFL + CFB sides (paper only, no real bets).

Runs deterministically on GitHub Actions (stdlib only, no LLM/agent steps).

Model (from betbot-revamp/research/upgrades-2026-10, u3_blend, FINAL_REPORT 2026-10-02):
  A     = market home margin = -(consensus home spread from The Odds API)
  fpi   = ESPN FPI predictor teamPredPtDiff for the home team (free public JSON)
  pred  = A + w * (fpi - A)            w = weight on FPI's disagreement (market weight = 1 - w)
  P(home cover at line L) = 1 - Phi((-L - pred) / sigma)
  P(home win)             = Phi(Phi^-1(q_ML no-vig) + (pred - A) / sigma)   (anchored to the ML market)
  w / sigma = u3 'open|blend:fpi' mean weekly walk-forward weight and last refit residual SD:
    NFL w=0.262 (~74% market) sigma=13.01 · CFB w=0.274 (~73% market) sigma=15.36

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

CT = ZoneInfo("America/Chicago")
UTC = timezone.utc
ND = NormalDist()

MODEL = {
    "NFL": {"w": 0.262, "sigma": 13.01, "league": "nfl", "groups": ""},
    "CFB": {"w": 0.274, "sigma": 15.36, "league": "college-football", "groups": "&groups=80&limit=400"},
}
MODEL_NAME = "u3 market-heavy blend (market + w·(FPI − market))"
MODEL_SRC = ("betbot-revamp/research/upgrades-2026-10 u3_blend open|blend:fpi mean weekly weight; "
             "FINAL_REPORT 2026-10-02: every sport stays on paper")
EDGE_MIN = 3.0      # pp vs stamped price
GAP_MAX = 8.0       # pp vs market no-vig -> HOLD
ML_CAP = 500        # ML side only when both prices within +-100..500
UNIT_USD = 20
EARLY_CUTOFF_MIN = 10 * 60 + 30  # games before 10:30am CT tomorrow are stamped today (next run may be late)
SOP_KEYS = {"draftkings": "DraftKings", "bovada": "Bovada"}  # Betr matched by title below
SB_URL = "https://site.api.espn.com/apis/site/v2/sports/football/{lg}/scoreboard?dates={d}{g}"
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
        js = get_json(SB_URL.format(lg=cfg["league"], d=d, g=cfg["groups"]))
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
def consensus_novig_ml(row: dict):
    v = row.get("market_home_ml_pct")
    return None if v is None else float(v) / 100.0


def candidates(ev: dict, row: dict) -> list[dict]:
    """SOP-book prices exactly as returned: spreads (each book's own line) and ML."""
    home, away = row["home"], row["away"]
    out = []
    for bk in ev.get("bookmakers") or []:
        book = sop_book(bk)
        if not book:
            continue
        for m in bk.get("markets") or []:
            outs = {o.get("name"): o for o in m.get("outcomes") or []}
            if home not in outs or away not in outs:
                continue
            h, a = outs[home], outs[away]
            if h.get("price") is None or a.get("price") is None:
                continue
            if m.get("key") == "spreads":
                hp, ap = h.get("point"), a.get("point")
                if hp is None or ap is None or abs(hp + ap) > 1e-9:
                    continue
                out.append({"market": "spread", "book": book, "home_line": float(hp),
                            "home_price": int(h["price"]), "away_price": int(a["price"])})
            elif m.get("key") == "h2h":
                out.append({"market": "ml", "book": book,
                            "home_price": int(h["price"]), "away_price": int(a["price"])})
    return out


def blend_row(sport: str, row: dict, ev: dict, fpi_home: float) -> dict:
    """Returns the paper block for one game. Never fills anything it cannot compute."""
    cfg = MODEL[sport]
    w, sig = cfg["w"], cfg["sigma"]
    blk = {"label": "PAPER", "model": MODEL_NAME, "w_fpi": w, "w_market": round(1 - w, 3), "sigma": sig,
           "fpi_home_margin": round(fpi_home, 2), "status": "PASS", "pick": None, "best": None,
           "note": ""}
    L = row.get("spread_home_line")
    if L is None:
        blk["status"], blk["note"] = "BLANK", "no consensus spread"
        return blk
    A = -float(L)
    pred = A + w * (fpi_home - A)
    blk["mkt_home_margin"] = round(A, 2)
    blk["blend_home_margin"] = round(pred, 2)
    blk["fair_home_line"] = round(-pred, 1)
    q_ml = consensus_novig_ml(row)
    cands = []
    for c in candidates(ev, row):
        if c["market"] == "spread":
            p_home = 1 - ND.cdf((-c["home_line"] - pred) / sig)
            q_home = novig(c["home_price"], c["away_price"])
            for side, p, q, price, line in (("home", p_home, q_home, c["home_price"], c["home_line"]),
                                            ("away", 1 - p_home, 1 - q_home, c["away_price"], -c["home_line"])):
                cands.append({"market": "spread", "side": side, "line": line, "price_american": price,
                              "book": c["book"], "model_pct": 100 * p, "novig_pct": 100 * q,
                              "implied_pct": 100 * implied(price)})
        else:
            hp, ap = c["home_price"], c["away_price"]
            if q_ml is None or not (100 <= abs(hp) <= ML_CAP and 100 <= abs(ap) <= ML_CAP):
                continue
            z = ND.inv_cdf(min(max(q_ml, 1e-4), 1 - 1e-4))
            p_home = ND.cdf(z + (pred - A) / sig)
            q_home = novig(hp, ap)
            for side, p, q, price in (("home", p_home, q_home, hp), ("away", 1 - p_home, 1 - q_home, ap)):
                cands.append({"market": "ml", "side": side, "line": None, "price_american": price,
                              "book": c["book"], "model_pct": 100 * p, "novig_pct": 100 * q,
                              "implied_pct": 100 * implied(price)})
    if not cands:
        blk["status"], blk["note"] = "BLANK", "no DraftKings/Betr/Bovada price returned"
        return blk
    for c in cands:
        c["edge_pp"] = c["model_pct"] - c["implied_pct"]
        c["gap_pp"] = c["model_pct"] - c["novig_pct"]
    best = max(cands, key=lambda c: (round(c["edge_pp"], 6), c["market"] == "spread", c["price_american"]))
    team = row["home"] if best["side"] == "home" else row["away"]
    if best["market"] == "spread":
        ln = best["line"]
        sel = f"{team} {'+' if ln > 0 else ''}{ln:g}" if ln != 0 else f"{team} PK"
    else:
        sel = f"{team} ML"
    best_out = {k: (r1(v) if k.endswith("_pct") or k.endswith("_pp") else v) for k, v in best.items()}
    best_out["selection"] = sel
    best_out["team"] = team
    blk["best"] = best_out
    if abs(best["gap_pp"]) > GAP_MAX:
        blk["status"] = "HOLD"
        blk["note"] = f"gap {best['gap_pp']:+.1f}pp vs no-vig > {GAP_MAX:g}pp"
    elif best["edge_pp"] >= EDGE_MIN:
        blk["status"] = "PAPER"
        blk["pick"] = best_out
        blk["note"] = f"PAPER 1u (${UNIT_USD}) — no real bet"
    else:
        blk["note"] = f"edge {best['edge_pp']:+.1f}pp < {EDGE_MIN:g}pp"
    return blk


def attach_paper(sport: str, rows: list[dict], events_by_toa: dict[str, dict], now_ct: datetime,
                 window_days: int) -> dict:
    """Adds row['paper'] (+ espn ids) to each row. Returns stats."""
    stats = {"games": len(rows), "espn_matched": 0, "with_fpi": 0, "paper": 0, "hold": 0, "pass": 0, "blank": 0}
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
    with cf.ThreadPoolExecutor(8) as ex:
        fpis = list(ex.map(lambda t: espn_fpi(sport, t[1]["id"]), matched))
    fpi_by_row = {id(t[0]): (f, t[2]) for t, f in zip(matched, fpis)}
    for r in rows:
        f, sw = fpi_by_row.get(id(r), (None, False))
        if not f:
            r["paper"] = {"label": "PAPER", "status": "BLANK", "pick": None, "best": None,
                          "note": "no ESPN FPI on file" if r.get("espn_event_id") else "no ESPN match",
                          "model": MODEL_NAME}
            stats["blank"] += 1
            continue
        stats["with_fpi"] += 1
        fpi_home = -f["home_pred_pt_diff"] if sw else f["home_pred_pt_diff"]
        blk = blend_row(sport, r, events_by_toa.get(r.get("toa_event_id") or "", {}), fpi_home)
        blk["fpi_last_modified"] = f.get("last_modified")
        r["paper"] = blk
        stats[blk["status"].lower()] = stats.get(blk["status"].lower(), 0) + 1
    return stats


# ----------------------------------------------------------------- ledger
def load_ledger(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"schema": LEDGER_SCHEMA, "picks": []}


def pick_id(sport: str, row: dict) -> str:
    return f"{sport.lower()}-{row.get('espn_event_id') or row.get('toa_event_id')}"


def stamp_today(ledger: dict, rows_by_sport: dict[str, list[dict]], now_ct: datetime) -> list[dict]:
    """Stamp PAPER picks for games kicking later today (CT). Once per game; never re-priced."""
    have = {p["id"] for p in ledger.get("picks") or []}
    new = []
    for sport, rows in rows_by_sport.items():
        for r in rows:
            blk = r.get("paper") or {}
            pk = blk.get("pick")
            if blk.get("status") != "PAPER" or not pk:
                continue
            kc = kick_ct(r["kick_utc"])
            if kc <= now_ct:
                continue
            # kick day (CT), or tomorrow before the next 9:15am CT run can catch it (London 8:30am CT)
            early_tomorrow = kc.date() == now_ct.date() + timedelta(days=1) and kc.hour * 60 + kc.minute < EARLY_CUTOFF_MIN
            if kc.date() != now_ct.date() and not early_tomorrow:
                continue
            pid = pick_id(sport, r)
            if pid in have:
                continue
            new.append({
                "id": pid, "label": "PAPER", "sport": sport, "slate_date_ct": now_ct.date().isoformat(),
                "stamped_at_ct": now_ct.isoformat(timespec="seconds"), "kick_utc": r["kick_utc"],
                "kick_ct": r.get("kick_ct"), "matchup": r.get("matchup"), "away": r["away"], "home": r["home"],
                "espn_event_id": r.get("espn_event_id"), "espn_home_id": r.get("espn_home_id"),
                "espn_away_id": r.get("espn_away_id"), "toa_event_id": r.get("toa_event_id"),
                "market": pk["market"], "side": pk["side"], "team": pk["team"], "selection": pk["selection"],
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
    margin = (our_h - our_a) if p["side"] == "home" else (our_a - our_h)
    if p["market"] == "spread":
        x = margin + float(p["line"])
    else:
        x = margin
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
    s["by_sport"] = {sp: block([p for p in picks if p["sport"] == sp]) for sp in ("NFL", "CFB")}
    s["unit_usd"] = UNIT_USD
    s["label"] = "PAPER — no real bets"
    ledger["schema"] = LEDGER_SCHEMA
    ledger["updated_at_ct"] = now_ct.isoformat(timespec="seconds")
    ledger["model"] = {"name": MODEL_NAME, "source": MODEL_SRC,
                       "params": {k: {"w_fpi": v["w"], "w_market": round(1 - v["w"], 3), "sigma": v["sigma"]}
                                  for k, v in MODEL.items()},
                       "rules": {"edge_min_pp": EDGE_MIN, "gap_hold_pp": GAP_MAX, "unit_usd": UNIT_USD,
                                 "books": "DraftKings / Betr / Bovada as returned by The Odds API",
                                 "stamp": "once per game on kick day (CT) before kick; games before 10:30am CT are stamped the prior day",
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
