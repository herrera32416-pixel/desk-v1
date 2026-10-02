#!/usr/bin/env python3
"""Walk-forward evaluation + blend-weight fit for scripts/nhl_ratings.py (PAPER ONLY, stdlib).

Tune (half-life, shrinkage, goalie on/off) on 2021-22 (from Nov 15) + 2022-23 ML log loss only;
test out of sample on 2023-24 .. 2026-27 (so far). Each game is predicted with a fit on games strictly
before its date. Market = ESPN close no-vig (single provider). Writes data/history/nhl_model_fit.json.
"""
from __future__ import annotations

import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nhl_model  # noqa: E402
import nhl_ratings as R  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EPS = 1e-6


def amer_dec(a):
    a = float(a)
    return 1 + (a / 100 if a > 0 else 100 / -a)


def nv(a, b):
    try:
        pa, pb = 1 / amer_dec(a), 1 / amer_dec(b)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return pa / (pa + pb)


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build_gq(games, book):
    """Pre-game goalie quality of each game's actual starters (data known before puck drop except who starts)."""
    out, cache = {}, {}
    for g in games:
        d = g["d"]
        if d not in cache:
            cache.clear()
            cache[d] = book.quality_asof(d)
        q, _, mix = cache[d]
        for side, t in (("h", g["h"]), ("a", g["a"])):
            st = book.starter.get((d, t))
            out[(g.get("espn_id"), side)] = q.get(st["pid"], 0.0) if st else 0.0
    return out


def walk(games, gq, start, end, hl, pen, use_goalie):
    gq_of = (lambda g, s: gq.get((g.get("espn_id"), s), 0.0))
    preds, P, cur = [], None, None
    for g in games:
        if g["d"] < start or g["d"] > end:
            continue
        if g["d"] != cur:
            cur = g["d"]
            P = R.fit(games, cur, gq_of=gq_of, init=P, half_life=hl, pen=pen, sweeps=4 if P else 10,
                      use_goalie=use_goalie)
        lh, la = R.rates(P, g["h"], g["a"], g["b2b_h"], g["b2b_a"], gq_of(g, "h") if use_goalie else 0,
                         gq_of(g, "a") if use_goalie else 0)
        preds.append((g, lh, la))
    return preds


def score_ml(preds):
    ll = n = 0
    for g, lh, la in preds:
        ph = nhl_model.probs(*R.to_poisson_inputs(lh, la))["ml_home"]
        y = 1 if g["hs"] > g["as"] else 0
        ll -= math.log(max(EPS, ph if y else 1 - ph))
        n += 1
    return ll / n


def market_rows(preds):
    """One row per (game, market) with model prob, market no-vig, outcome, prices."""
    rows = []
    for g, lh, la in preds:
        a, b = R.to_poisson_inputs(lh, la)
        L, T = fnum(g.get("spread_home")), fnum(g.get("total"))
        f = nhl_model.probs(a, b, home_line=L, total=T)
        hs, as_ = g["hs"], g["as"]
        q = nv(g.get("ml_home"), g.get("ml_away"))
        if q is not None:
            rows.append({"mkt": "ml", "d": g["d"], "m": f["ml_home"], "q": q, "y": int(hs > as_),
                         "pa": g["ml_home"], "pb": g["ml_away"], "id": g["espn_id"]})
        q = nv(g.get("sp_price_home"), g.get("sp_price_away"))
        if q is not None and L is not None and f.get("pl_home") is not None and abs(L) == 1.5:
            rows.append({"mkt": "pl", "d": g["d"], "m": f["pl_home"], "q": q, "y": int(hs - as_ + L > 0),
                         "pa": g["sp_price_home"], "pb": g["sp_price_away"], "id": g["espn_id"]})
        q = nv(g.get("over_price"), g.get("under_price"))
        if q is not None and T is not None and f.get("over") is not None and hs + as_ != T:
            rows.append({"mkt": "tot", "d": g["d"], "m": f["over"], "q": q, "y": int(hs + as_ > T),
                         "pa": g["over_price"], "pb": g["under_price"], "id": g["espn_id"]})
    return rows


def lg(p):
    p = min(1 - EPS, max(EPS, p))
    return math.log(p / (1 - p))


def sg(x):
    return 1 / (1 + math.exp(-x))


def blend(q, m, w):
    return sg(lg(q) + w * (lg(m) - lg(q)))


def metrics(rows, key):
    n = len(rows)
    if not n:
        return None
    ll = -sum(math.log(max(EPS, key(r) if r["y"] else 1 - key(r))) for r in rows) / n
    br = sum((key(r) - r["y"]) ** 2 for r in rows) / n
    return {"n": n, "logloss": round(ll, 5), "brier": round(br, 5)}


def fit_w(rows):
    """MLE of w in logit(p) = logit(q) + w*(logit(m)-logit(q)) (Newton)."""
    w = 0.0
    for _ in range(30):
        g = h = 0.0
        for r in rows:
            z = lg(r["m"]) - lg(r["q"])
            p = blend(r["q"], r["m"], w)
            g += (r["y"] - p) * z
            h += p * (1 - p) * z * z
        if h <= 0:
            break
        step = g / h
        w += step
        if abs(step) < 1e-7:
            break
    return w


def boot_w(rows, reps=1000, seed=7):
    by = {}
    for r in rows:
        by.setdefault(r["d"], []).append(r)
    days = list(by)
    rng = random.Random(seed)
    ws = []
    for _ in range(reps):
        s = []
        for _ in days:
            s += by[rng.choice(days)]
        ws.append(fit_w(s))
    ws.sort()
    return [round(ws[int(0.05 * reps)], 3), round(ws[int(0.95 * reps) - 1], 3)], \
           [round(ws[int(0.025 * reps)], 3), round(ws[int(0.975 * reps) - 1], 3)]


def units(rows, w_by_season, edge_pp=3.0, gap_pp=8.0, reps=1000):
    """Paper bets at recorded (ESPN close) prices: blended edge >= 3pp vs no-vig and |model-market| <= 8pp.
    90% CI of total units from a game-day cluster bootstrap."""
    res = {"bets": 0, "w": 0, "l": 0, "units": 0.0}
    per_day = {}
    for r in rows:
        w = w_by_season(r)
        if w == 0:
            continue
        p = blend(r["q"], r["m"], w)
        for side, ps, qs, price, win in ((0, p, r["q"], r["pa"], r["y"] == 1), (1, 1 - p, 1 - r["q"], r["pb"], r["y"] == 0)):
            ms = r["m"] if side == 0 else 1 - r["m"]
            if 100 * (ps - qs) >= edge_pp and abs(100 * (ms - qs)) <= gap_pp:
                res["bets"] += 1
                u = amer_dec(price) - 1 if win else -1.0
                res["w" if win else "l"] += 1
                res["units"] += u
                per_day[r["d"]] = per_day.get(r["d"], 0.0) + u
    res["units"] = round(res["units"], 2)
    if per_day:
        rng, days = random.Random(11), list(per_day)
        sims = sorted(sum(per_day[rng.choice(days)] for _ in days) for _ in range(reps))
        res["units_ci90"] = [round(sims[int(0.05 * reps)], 1), round(sims[int(0.95 * reps) - 1], 1)]
    return res


def season_of(d: date) -> int:
    return d.year if d.month >= 8 else d.year - 1


def main():
    games = R.load_games()
    book = R.GoalieBook(R.load_goalie_rows())
    gq = build_gq(games, book)
    cov = sum(1 for g in games if book.starter.get((g["d"], g["h"])) and book.starter.get((g["d"], g["a"])))
    print(f"games {len(games)}; both starters matched {cov}", flush=True)
    # ---- tune on 2021-22 (Nov 15+) and 2022-23 only
    grid = []
    if "--grid-cache" in sys.argv:   # reuse the logged tuning grid (same code, same data)
        grid = [tuple(x) for x in json.load(open(sys.argv[sys.argv.index("--grid-cache") + 1]))]
    for hl in (() if grid else (60, 120, 240)):
        for pen in (5, 20, 60):
            for ug in (False, True):
                p = walk(games, gq, date(2021, 11, 15), date(2023, 7, 1), hl, pen, ug)
                s = score_ml(p)
                grid.append((s, hl, pen, ug))
                print(f"tune hl={hl} pen={pen} goalie={ug}: ML logloss {s:.5f} (n={len(p)})", flush=True)
    grid.sort()
    _, hl, pen, ug = grid[0]
    print("chosen", hl, pen, ug, flush=True)
    # ---- out of sample
    tune_rows = market_rows(walk(games, gq, date(2021, 11, 15), date(2023, 7, 1), hl, pen, ug))
    test_rows = market_rows(walk(games, gq, date(2023, 8, 1), date(2027, 7, 1), hl, pen, ug))
    out = {"generated": date.today().isoformat(), "params": {"half_life_days": hl, "pen": pen, "goalie": ug},
           "tune_grid": [{"ml_logloss": round(s, 5), "half_life": a, "pen": b, "goalie": c} for s, a, b, c in grid],
           "test": {}, "starters_matched_games": cov, "games": len(games)}
    for mk in ("ml", "pl", "tot"):
        tr = [r for r in test_rows if r["mkt"] == mk]
        w = fit_w(tr)
        ci90, ci95 = boot_w(tr)
        # walk-forward w: each test season uses w fitted on all earlier OOS rows (tune seasons + earlier test)
        allr = [r for r in tune_rows + test_rows if r["mkt"] == mk]
        wf = {}
        for s in sorted({season_of(r["d"]) for r in tr}):
            prior = [r for r in allr if season_of(r["d"]) < s]
            wp = fit_w(prior)
            lo, hi = boot_w(prior, reps=300)[0]
            wf[s] = round(wp, 3) if lo > 0 else 0.0   # same rule as live: 0 unless the CI excludes 0
        u_wf = units(tr, lambda r: wf[season_of(r["d"])])
        u_raw = units(tr, lambda r: 1.0)
        out["test"][mk] = {
            "seasons": sorted({season_of(r["d"]) for r in tr}),
            "model": metrics(tr, lambda r: r["m"]), "market": metrics(tr, lambda r: r["q"]),
            "w": round(w, 3), "w_ci90": ci90, "w_ci95": ci95, "w_walkforward_by_season": wf,
            "units_walkforward_w": u_wf, "units_raw_model_w1": u_raw,
            "by_season": {s: {"model": metrics([r for r in tr if season_of(r["d"]) == s], lambda r: r["m"]),
                              "market": metrics([r for r in tr if season_of(r["d"]) == s], lambda r: r["q"])}
                          for s in sorted({season_of(r["d"]) for r in tr})}}
        print(mk, json.dumps(out["test"][mk]), flush=True)
    (ROOT / "data" / "history" / "nhl_model_fit.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
