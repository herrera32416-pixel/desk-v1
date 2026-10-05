#!/usr/bin/env python3
"""
NHL team-strength model (stdlib only, deterministic) — PAPER ONLY.

Regulation-goal Poisson rates per game:
  log lam_home = mu + HOME + att[h] - dfn[a] + B2B_OFF*b2b_h + B2B_DEF*b2b_a + G*gq_a
  log lam_away = mu        + att[a] - dfn[h] + B2B_OFF*b2b_a + B2B_DEF*b2b_h + G*gq_h
Fit = weighted Poisson MLE (time decay, half-life HALF_LIFE days) with a Gaussian (L2) shrinkage
prior on att/dfn toward 0 (PEN). Goals = final score minus the OT/SO winner's +1 (regulation goals,
empty-net goals included). gq = starting goalie's goals-saved-above-league per game (decayed,
shrunk save %), from the free NHL stats API (api.nhle.com, no key). The rates are converted to
ML / puck line / O-U with scripts/nhl_model.py (draw inflation, empty-net overlay, OT/SO split).
"""
from __future__ import annotations

import csv
import json
import math
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HIST = ROOT / "data" / "history" / "nhl"

# ESPN abbreviations -> NHL API abbreviations
ESPN2NHL = {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA", "ARI": "ARI"}
NHL_TEAMS = {"ANA", "ARI", "BOS", "BUF", "CAR", "CBJ", "CGY", "CHI", "COL", "DAL", "DET", "EDM", "FLA", "LAK",
             "MIN", "MTL", "NJD", "NSH", "NYI", "NYR", "OTT", "PHI", "PIT", "SEA", "SJS", "STL", "TBL", "TOR",
             "UTA", "VAN", "VGK", "WPG", "WSH"}

HALF_LIFE = 120.0     # days (tuned on 2021-22/2022-23 only; see scripts/history/fit_nhl_model.py)
PEN = 20.0            # L2 prior strength on att/dfn (in weighted-game units)
G_HALF_LIFE = 365.0   # goalie save% decay
G_PRIOR_SHOTS = 1500.0
SHOTS_PER_GAME = 28.5
MIN_W = 0.01


def nhl_abbr(a: str) -> str:
    a = (a or "").upper()
    return ESPN2NHL.get(a, a)


def et_date(iso_utc: str) -> date:
    """Local (ET-ish) calendar date of an ESPN UTC kickoff (UTC-5h is enough to land on the game day)."""
    dt = datetime.strptime(iso_utc.replace("Z", ""), "%Y-%m-%dT%H:%M")
    return (dt - timedelta(hours=5)).date()


# ------------------------------------------------------------------ data
def load_games(extra_rows: list[dict] | None = None) -> list[dict]:
    """Finals from data/history/nhl/*.csv (+ extra ESPN-shaped rows), regulation goals, sorted by date."""
    rows = []
    for f in sorted(HIST.glob("[0-9][0-9][0-9][0-9].csv")):
        rows += list(csv.DictReader(open(f)))
    rows += extra_rows or []
    seen, out = set(), []
    for r in rows:
        h, a = nhl_abbr(r["home"]), nhl_abbr(r["away"])
        if h not in NHL_TEAMS or a not in NHL_TEAMS or r.get("espn_id") in seen:
            continue
        try:
            hs, as_ = int(r["home_score"]), int(r["away_score"])
        except (TypeError, ValueError):
            continue
        rt = (r.get("result_type") or "REG").upper()
        if rt not in ("REG", "OT", "SO"):
            continue
        seen.add(r.get("espn_id"))
        hr, ar = hs, as_
        if rt in ("OT", "SO"):
            if hs > as_:
                hr -= 1
            else:
                ar -= 1
        out.append({**r, "h": h, "a": a, "d": et_date(r["date_utc"]), "hs": hs, "as": as_, "hr": hr, "ar": ar,
                    "rt": rt})
    out.sort(key=lambda g: (g["d"], g.get("espn_id") or ""))
    add_rest(out)
    return out


def add_rest(games: list[dict]) -> None:
    last: dict[str, date] = {}
    for g in games:
        for side, t in (("h", g["h"]), ("a", g["a"])):
            p = last.get(t)
            g["b2b_" + side] = 1 if (p is not None and (g["d"] - p).days == 1) else 0
        for t in (g["h"], g["a"]):
            last[t] = g["d"]


def b2b_today(games: list[dict], team: str, d: date) -> int:
    return int(any(g["d"] == d - timedelta(days=1) and team in (g["h"], g["a"]) for g in games[-40:]))


# ------------------------------------------------------------------ goalies (NHL stats API, free, no key)
def fetch_goalie_season(season_id: str, pause: float = 0.3) -> list[dict]:
    out, start = [], 0
    while True:
        u = ("https://api.nhle.com/stats/rest/en/goalie/summary?isAggregate=false&isGame=true"
             f"&start={start}&limit=100&sort=%5B%7B%22property%22:%22gameId%22,%22direction%22:%22ASC%22%7D,"
             "%7B%22property%22:%22playerId%22,%22direction%22:%22ASC%22%7D%5D"
             f"&cayenneExp=seasonId={season_id}%20and%20gameTypeId=2")
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 desk-v1-paper"})
        d = json.load(urllib.request.urlopen(req, timeout=30))
        out += d.get("data") or []
        start += 100
        if start >= (d.get("total") or 0):
            break
        time.sleep(pause)
    return [{"gameId": r["gameId"], "date": r["gameDate"], "team": r["teamAbbrev"], "pid": r["playerId"],
             "name": r["goalieFullName"], "gs": r.get("gamesStarted") or 0, "sa": r.get("shotsAgainst") or 0,
             "sv": r.get("saves") or 0} for r in out]


def load_goalie_rows(fetch_current: str | None = None) -> list[dict]:
    rows = []
    for f in sorted(HIST.glob("goalies_*.csv")):
        if fetch_current and f.stem.endswith(fetch_current):
            continue
        for r in csv.DictReader(open(f)):
            rows.append({**r, "gameId": int(r["gameId"]), "pid": int(r["pid"]), "gs": int(r["gs"]),
                         "sa": int(r["sa"]), "sv": int(r["sv"])})
    if fetch_current:
        try:
            rows += fetch_goalie_season(fetch_current)
        except Exception as e:  # noqa: BLE001 — offline: fall back to the committed snapshot
            print(f"[nhl_ratings] goalie fetch failed ({e}); using committed snapshot")
            f = HIST / f"goalies_{fetch_current}.csv"
            if f.exists():
                for r in csv.DictReader(open(f)):
                    rows.append({**r, "gameId": int(r["gameId"]), "pid": int(r["pid"]), "gs": int(r["gs"]),
                                 "sa": int(r["sa"]), "sv": int(r["sv"])})
    for r in rows:
        r["d"] = date.fromisoformat(r["date"])
    rows.sort(key=lambda r: (r["d"], r["gameId"]))
    return rows


class GoalieBook:
    """Pre-game goalie quality = goals saved above league avg per game (decayed, shrunk save %)."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.starter = {(r["d"], r["team"]): r for r in rows if r["gs"] == 1}

    def quality_asof(self, d: date) -> tuple[dict[int, float], float, dict[str, list]]:
        """Return ({pid: gq}, league sv, {team: [(pid, weight)]}) using games strictly before d."""
        sv, sa, tw = {}, {}, {}
        lsv = lsa = 0.0
        for r in self.rows:
            if r["d"] >= d:
                break
            age = (d - r["d"]).days
            w = 0.5 ** (age / G_HALF_LIFE)
            if w < MIN_W:
                continue
            sv[r["pid"]] = sv.get(r["pid"], 0.0) + w * r["sv"]
            sa[r["pid"]] = sa.get(r["pid"], 0.0) + w * r["sa"]
            lsv += w * r["sv"]
            lsa += w * r["sa"]
            if r["gs"] == 1 and age <= 60:
                tw.setdefault(r["team"], {}).setdefault(r["pid"], 0.0)
                tw[r["team"]][r["pid"]] += 0.5 ** (age / 20.0)
        lg = lsv / lsa if lsa else 0.9
        q = {p: ((sv[p] + lg * G_PRIOR_SHOTS) / (sa[p] + G_PRIOR_SHOTS) - lg) * SHOTS_PER_GAME for p in sv}
        team_mix = {t: sorted(m.items(), key=lambda x: -x[1]) for t, m in tw.items()}
        return q, lg, team_mix


def find_pid(rows: list[dict], team: str, name: str) -> int | None:
    """Match a goalie-log name (e.g. 'Garand' or 'Dylan Garand') to an NHL playerId, preferring the team."""
    nm = (name or "").strip().lower()
    if not nm:
        return None
    last = nm.split()[-1]
    best = None
    for r in reversed(rows):
        full = r["name"].lower()
        if full == nm or full.split()[-1] == last:
            if r["team"] == team:
                return r["pid"]
            best = best or r["pid"]
    return best


# ------------------------------------------------------------------ fit
def fit(games: list[dict], asof: date, gq_of=None, init: dict | None = None, half_life: float = HALF_LIFE,
        pen: float = PEN, sweeps: int = 6, use_goalie: bool = True) -> dict:
    """Weighted, shrunk Poisson fit on games before `asof`. gq_of(game, side) -> goalie quality or 0."""
    data = []
    for g in games:
        if g["d"] >= asof:
            continue
        w = 0.5 ** ((asof - g["d"]).days / half_life)
        if w < MIN_W:
            continue
        gh = ga = 0.0
        if use_goalie and gq_of:
            gh, ga = gq_of(g, "h"), gq_of(g, "a")
        # (scorer, defender, goals, home?, b2b scorer, b2b defender, defender goalie q)
        data.append((g["h"], g["a"], g["hr"], 1, g["b2b_h"], g["b2b_a"], ga, w))
        data.append((g["a"], g["h"], g["ar"], 0, g["b2b_a"], g["b2b_h"], gh, w))
    P = {"mu": math.log(3.0), "home": 0.05, "b2b_off": 0.0, "b2b_def": 0.0, "g": 0.0, "att": {}, "dfn": {}}
    if init:
        P.update({k: (dict(v) if isinstance(v, dict) else v) for k, v in init.items() if k in P})
    if not data:
        P["n"] = 0
        return P
    att, dfn = P["att"], P["dfn"]
    by_s, by_d = {}, {}
    for i, x in enumerate(data):
        by_s.setdefault(x[0], []).append(i)
        by_d.setdefault(x[1], []).append(i)
        att.setdefault(x[0], 0.0)
        dfn.setdefault(x[1], 0.0)

    def lam(x):
        s, dd, y, hm, bo, bd, gq, w = x
        return math.exp(P["mu"] + P["home"] * hm + att[s] - dfn[dd] + P["b2b_off"] * bo + P["b2b_def"] * bd
                        + P["g"] * gq)

    def newton(idx, deriv, prior=0.0, cur=0.0, sign=1.0):
        gsum = hsum = 0.0
        for i in idx:
            x = data[i]
            dv = deriv(x)
            if dv == 0:
                continue
            L = lam(x)
            gsum += x[7] * (x[2] - L) * dv
            hsum += x[7] * L * dv * dv
        gsum -= prior * cur
        hsum += prior
        return gsum / hsum if hsum > 0 else 0.0

    allidx = range(len(data))
    for _ in range(sweeps):
        P["mu"] += newton(allidx, lambda x: 1.0)
        P["home"] += newton(allidx, lambda x: x[3])
        P["b2b_off"] += newton(allidx, lambda x: x[4], prior=5.0, cur=P["b2b_off"])
        P["b2b_def"] += newton(allidx, lambda x: x[5], prior=5.0, cur=P["b2b_def"])
        if use_goalie and gq_of:
            P["g"] += newton(allidx, lambda x: x[6], prior=5.0, cur=P["g"])
        for t, idx in by_s.items():
            att[t] += newton(idx, lambda x: 1.0, prior=pen, cur=att[t])
        for t, idx in by_d.items():
            dfn[t] += newton(idx, lambda x: -1.0, prior=pen, cur=dfn[t])
        # identifiability: center att/dfn, push the mean into mu
        ma = sum(att.values()) / len(att)
        md = sum(dfn.values()) / len(dfn)
        for t in att:
            att[t] -= ma
        for t in dfn:
            dfn[t] -= md
        P["mu"] += ma - md
    P["n"] = len(data) // 2
    return P


def rates(P: dict, h: str, a: str, b2b_h: int, b2b_a: int, gq_h: float, gq_a: float) -> tuple[float, float]:
    """Expected regulation goals (incl. EN) for home and away."""
    att, dfn = P["att"], P["dfn"]
    lh = math.exp(P["mu"] + P["home"] + att.get(h, 0) - dfn.get(a, 0) + P["b2b_off"] * b2b_h
                  + P["b2b_def"] * b2b_a + P["g"] * gq_a)
    la = math.exp(P["mu"] + att.get(a, 0) - dfn.get(h, 0) + P["b2b_off"] * b2b_a
                  + P["b2b_def"] * b2b_h + P["g"] * gq_h)
    return lh, la


_K_CACHE: dict = {}


def to_poisson_inputs(lh: float, la: float) -> tuple[float, float]:
    """nhl_model.final_dist adds empty-net goals on top of its inputs; scale so its expected regulation
    total (EN incl., before the OT/SO +1) matches the rated regulation total."""
    import nhl_model  # same folder
    key = round(lh + la, 2)
    if key not in _K_CACHE:
        mu = lh + la

        def ereg(k):
            D = nhl_model.final_dist(k * mu / 2, k * mu / 2)
            D.pop(("tie",), None)
            # subtract the +1 OT/SO goal (present only in tied-in-regulation cells; reg_tie mass)
            tot = sum((i + j) * p for (i, j), p in D.items())
            tie = nhl_model.probs(k * mu / 2, k * mu / 2)["reg_tie"]
            return tot - tie

        lo, hi = 0.7, 1.05
        for _ in range(30):
            m = (lo + hi) / 2
            if ereg(m) > mu:
                hi = m
            else:
                lo = m
        _K_CACHE[key] = (lo + hi) / 2
    k = _K_CACHE[key]
    return lh * k, la * k


# ------------------------------------------------------------------ live (GitHub Actions)
LIVE_FINALS = HIST / "live_finals.csv"   # finals after the history build, appended by the daily run
FIT_JSON = ROOT / "data" / "history" / "nhl_model_fit.json"
LIVE_COLS = ["espn_id", "date_utc", "home", "away", "home_score", "away_score", "result_type", "season"]


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 desk-v1-paper"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def refresh_live_finals(today: date, max_days: int = 21) -> int:
    """Append ESPN regular-season NHL finals for days after the last stored final, up to yesterday."""
    have, last = {}, None
    for f in sorted(HIST.glob("[0-9][0-9][0-9][0-9].csv")) + ([LIVE_FINALS] if LIVE_FINALS.exists() else []):
        for r in csv.DictReader(open(f)):
            have[r["espn_id"]] = 1
            d = et_date(r["date_utc"])
            last = d if last is None or d > last else last
    if last is None:
        return 0
    start = max(last - timedelta(days=1), today - timedelta(days=max_days))
    new = []
    d = start
    while d < today:
        try:
            js = _get(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates={d:%Y%m%d}")
        except Exception as e:  # noqa: BLE001
            print(f"[nhl_ratings] ESPN scoreboard {d} failed: {e}")
            d += timedelta(days=1)
            continue
        for e in js.get("events") or []:
            try:
                st = e["status"]["type"]
                if not st.get("completed") or (e.get("season") or {}).get("type") != 2 or str(e["id"]) in have:
                    continue
                c = {x["homeAway"]: x for x in e["competitions"][0]["competitors"]}
                det = st.get("detail") or ""
                rt = "SO" if "SO" in det else ("OT" if "OT" in det else "REG")
                new.append({"espn_id": str(e["id"]), "date_utc": e["date"], "home": c["home"]["team"]["abbreviation"],
                            "away": c["away"]["team"]["abbreviation"], "home_score": c["home"]["score"],
                            "away_score": c["away"]["score"], "result_type": rt,
                            "season": (e.get("season") or {}).get("year", "")})
                have[str(e["id"])] = 1
            except (KeyError, IndexError, TypeError):
                continue
        d += timedelta(days=1)
    if new:
        exists = LIVE_FINALS.exists()
        with open(LIVE_FINALS, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LIVE_COLS)
            if not exists:
                w.writeheader()
            w.writerows(new)
    return len(new)


def season_id(d: date) -> str:
    y = d.year if d.month >= 8 else d.year - 1
    return f"{y}{y + 1}"


# 2026-10-04 (Luis-approved, weekend eval sec 3/7): NHL is PAPER/INFO ONLY. The blend weight is hard-locked
# to 0 on every market (ML, puck line, total) regardless of what nhl_model_fit.json says, so the raw MODEL %
# is displayed for information only and is never used for a pick or an edge. Walk-forward: ML log loss model
# .6713 vs market .6651, w_ML -0.015 CI90 [-0.268, 0.228]; PL/totals at w=1 clearly negative.
# Unlock only via an explicit, reviewed change (refit w > 0 with CI excluding 0 + positive units at real prices + BH).
NHL_W_LOCKED_ZERO = True


def fitted_weights() -> dict:
    """Blend weight per market from the walk-forward fit: w if its 90% CI excludes 0, else 0.
    While NHL_W_LOCKED_ZERO is True every weight is forced to 0 (fit kept in 'detail' for reference)."""
    out = {"ml": 0.0, "pl": 0.0, "tot": 0.0, "detail": {}}
    try:
        js = json.loads(FIT_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return out
    for k in ("ml", "pl", "tot"):
        t = (js.get("test") or {}).get(k) or {}
        lo, hi = (t.get("w_ci90") or [0, 0])
        out[k] = t.get("w", 0.0) if (lo > 0 or hi < 0) else 0.0
        out["detail"][k] = {"w_fit": t.get("w"), "w_ci90": t.get("w_ci90"), "model": t.get("model"),
                            "market": t.get("market")}
    out["params"] = js.get("params")
    if NHL_W_LOCKED_ZERO:
        for k in ("ml", "pl", "tot"):
            out[k] = 0.0
        out["locked_zero"] = "2026-10-04: NHL w locked to 0, model % info only"
    return out


class LiveModel:
    """Fit once per run on history + this season's finals; predict today's games."""

    def __init__(self, today: date, fetch: bool = True):
        self.today = today
        self.notes = []
        if fetch:
            try:
                n = refresh_live_finals(today)
                self.notes.append(f"{n} new finals appended")
            except Exception as e:  # noqa: BLE001
                self.notes.append(f"finals refresh failed: {e}")
        extra = list(csv.DictReader(open(LIVE_FINALS))) if LIVE_FINALS.exists() else []
        self.games = load_games(extra)
        self.grows = load_goalie_rows(fetch_current=season_id(today) if fetch else None)
        self.book = GoalieBook(self.grows)
        self.gq_now, self.lg_sv, self.team_mix = self.book.quality_asof(today)
        # pre-game goalie quality of historical starters, only for games inside the decay window
        cutoff = today - timedelta(days=int(HALF_LIFE * math.log2(1 / MIN_W)) + 1)
        gq, cache = {}, {}
        for g in self.games:
            if g["d"] < cutoff:
                continue
            if g["d"] not in cache:
                cache.clear()
                cache[g["d"]] = self.book.quality_asof(g["d"])[0]
            q = cache[g["d"]]
            for side, t in (("h", g["h"]), ("a", g["a"])):
                st = self.book.starter.get((g["d"], t))
                gq[(g.get("espn_id"), side)] = q.get(st["pid"], 0.0) if st else 0.0
        self.P = fit(self.games, today, gq_of=lambda g, s: gq.get((g.get("espn_id"), s), 0.0), sweeps=12)
        self.weights = fitted_weights()
        self.last_final = max(g["d"] for g in self.games).isoformat()
        self.this_season_games = sum(1 for g in self.games if season_id(g["d"]) == season_id(today))

    def goalie_q(self, team: str, logged: dict | None):
        if logged and logged.get("goalie"):
            pid = find_pid(self.grows, team, logged["goalie"])
            if pid is not None:
                return self.gq_now.get(pid, 0.0), f"{logged['goalie']} ({logged.get('status')})"
            return 0.0, f"{logged['goalie']} ({logged.get('status')}; no NHL stats → league avg)"
        mix = self.team_mix.get(team) or []
        tot = sum(w for _, w in mix)
        if not tot:
            return 0.0, "no recent starts → league avg"
        return sum(self.gq_now.get(p, 0.0) * w for p, w in mix) / tot, "recent-starter mix (no log entry)"

    def predict(self, home: str, away: str, d: date, gl_home: dict | None, gl_away: dict | None,
                home_line=None, total=None) -> dict:
        import nhl_model
        bh, ba = b2b_today(self.games, home, d), b2b_today(self.games, away, d)
        qh, sh = self.goalie_q(home, gl_home)
        qa, sa = self.goalie_q(away, gl_away)
        lh, la = rates(self.P, home, away, bh, ba, qh, qa)
        f = nhl_model.probs(*to_poisson_inputs(lh, la), home_line=home_line, total=total)
        f.update({"reg_goals_home": lh, "reg_goals_away": la, "b2b_home": bh, "b2b_away": ba,
                  "gq_home": qh, "gq_away": qa, "goalie_src_home": sh, "goalie_src_away": sa})
        return f
