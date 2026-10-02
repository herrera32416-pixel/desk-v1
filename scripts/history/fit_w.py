#!/usr/bin/env python3
"""Refit each sport's blend weight w against the CLOSING line, with bootstrap CIs, and score
units at the recorded prices (box-side research; needs numpy/pandas).

Football (NFL, CFB): margin space, pred = A + w (FPI - A), A = -close home spread.
  w = LS slope of (margin - A) on (FPI - A), no intercept (w = 0 is the pure market).
MLB, NHL: win-prob space, logit p = logit q + w (logit m - logit q), q = no-vig close ML,
  m = ESPN predictor win % (MLB) / MoneyPuck pregame win % (NHL, comparison only). w by MLE.
CIs: 2000 cluster-bootstrap reps (cluster = season-week for football, game date for MLB/NHL), 90% and 95%.
Units: walk-forward by season (w and sigma fit on prior seasons only), flat 1u, bet when
edge >= 3pp vs the RECORDED price (never an assumed -110; rows without a price are skipped).
Output: data/history/fit_w.json
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

REPO = Path(__file__).resolve().parents[2]
H = REPO / "data" / "history"
RNG = np.random.default_rng(20261002)
B = 2000
EDGE = 0.03


def dec(a):
    a = np.asarray(a, float)
    return np.where(a > 0, 1 + a / 100, 1 + 100 / np.abs(a))


def imp(a):
    return 1 / dec(a)


def novig(h, a):
    x, y = imp(h), imp(a)
    return x / (x + y)


def ci(vals):
    v = np.asarray(vals)
    return {"ci90": [round(float(np.quantile(v, .05)), 3), round(float(np.quantile(v, .95)), 3)],
            "ci95": [round(float(np.quantile(v, .025)), 3), round(float(np.quantile(v, .975)), 3)]}


def boot(df, cl, stat):
    groups = df.groupby(cl).indices
    keys = np.array(list(groups))
    out = []
    for _ in range(B):
        idx = np.concatenate([groups[k] for k in RNG.choice(keys, len(keys))])
        out.append(stat(df.iloc[idx]))
    return out


def units_summary(p):
    p = np.asarray(p, float)
    if len(p) == 0:
        return {"bets": 0}
    bs = [RNG.choice(p, len(p)).sum() for _ in range(B)]
    return {"bets": int(len(p)), "W": int((p > 0).sum()), "L": int((p < 0).sum()), "P": int((p == 0).sum()),
            "units": round(float(p.sum()), 2), "roi": round(float(p.mean()), 4), **{k: v for k, v in ci(bs).items()}}


# ------------------------------------------------------------------ football
def fit_football(sport):
    files = sorted((H / sport).glob("*.csv"))
    g = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    g = g[g.margin.notna() & g.close_spread.notna() & g.fpi_home_margin.notna()].copy()
    g["A"] = -g.close_spread
    g["x"] = g.fpi_home_margin - g.A
    g["y"] = g.margin - g.A
    g["cl"] = g.season.astype(str) + "-" + g.week.astype(str)
    wfun = lambda d: float((d.x * d.y).sum() / (d.x ** 2).sum())
    w = wfun(g)
    sig = float(np.std(g.y - w * g.x))
    res = {"n_games": int(len(g)), "seasons": sorted(int(s) for s in g.season.unique()), "w": round(w, 3),
           **ci(boot(g, "cl", wfun)), "sigma_resid": round(sig, 2),
           "sigma_market_only": round(float(np.std(g.y)), 2), "track": "close (spread)"}
    # walk-forward units at recorded close prices (ATS + ML)
    pnl_ats, pnl_ml = [], []
    for s in sorted(g.season.unique()):
        tr, te = g[g.season < s], g[g.season == s]
        if len(tr) < 150:
            continue
        wt = wfun(tr)
        st = float(np.std(tr.y - wt * tr.x))
        pred = te.A + wt * te.x
        L = te.close_spread
        ph = 1 - norm.cdf((-L - pred) / st)
        hp, ap = te.close_h_sp_price, te.close_a_sp_price
        ok = hp.notna() & ap.notna()
        res_c = np.sign(te.margin + L)
        for side, p, price, r in ((1, ph, hp, res_c), (-1, 1 - ph, ap, -res_c)):
            e = p - imp(price.fillna(-110))
            b = ok & (e >= EDGE)
            pnl_ats += list(np.where(r[b] > 0, dec(price[b]) - 1, np.where(r[b] < 0, -1.0, 0.0)))
        hm, am_ = te.close_h_ml, te.close_a_ml
        okm = hm.notna() & am_.notna() & hm.abs().between(100, 500) & am_.abs().between(100, 500)
        q = novig(hm.fillna(-110), am_.fillna(-110))
        z = norm.ppf(np.clip(q, 1e-4, 1 - 1e-4))
        pw = norm.cdf(z + (pred - te.A) / st)
        rw = np.sign(te.margin)
        for p, price, r in ((pw, hm, rw), (1 - pw, am_, -rw)):
            e = p - imp(price.fillna(-110))
            b = okm & (e >= EDGE)
            pnl_ml += list(np.where(r[b] > 0, dec(price[b]) - 1, np.where(r[b] < 0, -1.0, 0.0)))
    res["walk_forward_units_ats_close"] = units_summary(pnl_ats)
    res["walk_forward_units_ml_close"] = units_summary(pnl_ml)
    return res


# ------------------------------------------------------------------ MLB / NHL (win-prob space)
def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def mle_w(d):
    lq, lm, y = logit(d.q.values), logit(d.m.values), d.y.values
    def nll(w):
        p = 1 / (1 + np.exp(-(lq + w * (lm - lq))))
        p = np.clip(p, 1e-6, 1 - 1e-6)
        return -np.sum(y * np.log(p) + (1 - y) * np.log(1 - p))
    lo, hi = -1.0, 2.0
    for _ in range(60):  # golden-section
        m1, m2 = lo + (hi - lo) * 0.382, lo + (hi - lo) * 0.618
        if nll(m1) < nll(m2):
            hi = m2
        else:
            lo = m1
    return float((lo + hi) / 2)


def fit_winprob(d, label):
    d = d[d.q.notna() & d.m.notna() & d.y.notna()].copy()
    w = mle_w(d)
    res = {"n_games": int(len(d)), "seasons": sorted(int(s) for s in d.season.unique()), "w": round(w, 3),
           **ci(boot(d, "date", mle_w)), "track": "close (ML, ESPN-listed final pregame line)", "model": label}
    bq = np.mean((d.q - d.y) ** 2)
    bm = np.mean((d.m - d.y) ** 2)
    res["brier_market"], res["brier_model"] = round(float(bq), 4), round(float(bm), 4)
    pnl = []
    for s in sorted(d.season.unique()):
        tr, te = d[d.season < s], d[d.season == s]
        if len(tr) < 500:
            continue
        wt = mle_w(tr)
        p = 1 / (1 + np.exp(-(logit(te.q.values) + wt * (logit(te.m.values) - logit(te.q.values)))))
        for pp, price, r in ((p, te.ml_home.values, te.y.values), (1 - p, te.ml_away.values, 1 - te.y.values)):
            e = pp - imp(price)
            b = e >= EDGE
            pnl += list(np.where(r[b] == 1, dec(price[b]) - 1, -1.0))
    res["walk_forward_units_ml_close"] = units_summary(pnl)
    return res


def load_espn(sport):
    d = pd.concat([pd.read_csv(f) for f in sorted((H / sport).glob("*.csv"))], ignore_index=True)
    d = d[d.home_score.notna() & d.away_score.notna()].copy()
    d["y"] = (d.home_score > d.away_score).astype(float)
    ok = d.ml_home.notna() & d.ml_away.notna() & (d.ml_home.abs() >= 100) & (d.ml_away.abs() >= 100)
    d["q"] = np.where(ok, novig(d.ml_home.where(ok, -110), d.ml_away.where(ok, -110)), np.nan)
    d["date"] = pd.to_datetime(d.date_utc, utc=True).dt.tz_convert("America/New_York").dt.date.astype(str)
    return d


ESPN2MP = {"TB": "TBL", "NJ": "NJD", "SJ": "SJS", "LA": "LAK", "UTAH": "UTA", "WSH": "WSH", "MON": "MTL"}


def nhl_with_moneypuck(d):
    mp = []
    for f in sorted(Path("/workspace/research/nhl-2026-10/mp").glob("20*_regular_season.csv")):
        x = pd.read_csv(f)
        x["date"] = pd.to_datetime(x.gamedate.astype(str).str[:8]).dt.date.astype(str)
        mp.append(x[["date", "home", "away", "home_win"]])
    mp = pd.concat(mp).drop_duplicates(["date", "home", "away"])
    d = d.copy()
    d["h2"] = d.home.map(lambda a: ESPN2MP.get(a, a))
    d["a2"] = d.away.map(lambda a: ESPN2MP.get(a, a))
    m = d.merge(mp, left_on=["date", "h2", "a2"], right_on=["date", "home", "away"], how="left", suffixes=("", "_mp"))
    m["m"] = m.home_win
    return m


def main(which):
    out = {}
    f = H / "fit_w.json"
    if f.exists():
        out = json.loads(f.read_text())
    for sp in which:
        if sp in ("nfl", "cfb"):
            out[sp] = fit_football(sp)
        elif sp == "mlb":
            d = load_espn("mlb")
            d["m"] = d.espn_home_win_pct / 100
            out[sp] = fit_winprob(d, "ESPN predictor winProbability")
        elif sp == "nhl":
            d = nhl_with_moneypuck(load_espn("nhl"))
            out[sp] = fit_winprob(d, "MoneyPuck pregame home_win (comparison only; not used live)")
            out[sp]["note"] = "ESPN has no NHL predictor; live DESK NHL model stays blank (market only)."
        r = out[sp]
        r["decision"] = ("use fitted w" if not (r["ci90"][0] <= 0 <= r["ci90"][1]) else "w = 0 (90% CI includes 0)")
        print(sp, json.dumps(r))
    f.write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
