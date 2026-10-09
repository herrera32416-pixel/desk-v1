#!/usr/bin/env python3
"""Walk-forward backtest: NHL xG model (scripts/nhl_xg.py spec) vs the market. PAPER research, box only (numpy/scipy).
Each game day D: fit on games strictly before D (2023-24+ only, 120d half-life, pen 20), predict D's games.
Market = ESPN single-book pregame line from data/history/nhl/<season>.csv (2023-24 ML re-pulled 2026-10-09), de-vigged.
Bets: flat 1u = $20 at the recorded price when model edge >= 3pp vs the price's implied % and |model - no-vig| <= 8pp.
Writes data/history/nhl_xg_backtest.json."""
import csv, json, math, sys
from collections import defaultdict
from datetime import date
from pathlib import Path
import numpy as np
from scipy.optimize import minimize

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import nhl_model, nhl_ratings as R, nhl_xg  # noqa: E402

EDGE, GAP, UNIT = 3.0, 8.0, 20.0


def imp(a):
    a = float(a); return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def payout(a):
    a = float(a); return a / 100 if a > 0 else 100 / -a


def run(alpha):
    m = nhl_xg.XGLiveModel(date(2026, 10, 10), fetch=False) if alpha is not None else None
    G = m.games
    if alpha != nhl_xg.ALPHA:  # rebuild target for other alphas (0 = goals only, same 2023+ window)
        xg = nhl_xg.load_xg()
        for g in G:
            x = xg.get((g["d"].isoformat(), g["h"], g["a"]))
            gh, ga = g.get("hr_goals", g["hr"]), g.get("ar_goals", g["ar"])
            g["hr"], g["ar"] = (alpha * x[0] + (1 - alpha) * gh, alpha * x[1] + (1 - alpha) * ga) if x else (gh, ga)
    teams = sorted({g["h"] for g in G} | {g["a"] for g in G}); ti = {t: i for i, t in enumerate(teams)}; NT = len(teams)
    qcache = {}
    def gq(g, side):
        if g["d"] not in qcache:
            qcache.clear(); qcache[g["d"]] = m.book.quality_asof(g["d"])[0]
        st = m.book.starter.get((g["d"], g[side]))
        return qcache[g["d"]].get(st["pid"], 0.0) if st else 0.0
    gqh = np.array([gq(g, "h") for g in G]); gqa = np.array([gq(g, "a") for g in G])
    hi = np.array([ti[g["h"]] for g in G]); ai = np.array([ti[g["a"]] for g in G])
    dn = np.array([g["d"].toordinal() for g in G]); n = len(G)
    S = np.r_[hi, ai]; T = np.r_[ai, hi]; Y = np.r_[[g["hr"] for g in G], [g["ar"] for g in G]].astype(float)
    HM = np.r_[np.ones(n), np.zeros(n)]
    BO = np.r_[[g["b2b_h"] for g in G], [g["b2b_a"] for g in G]].astype(float)
    BD = np.r_[[g["b2b_a"] for g in G], [g["b2b_h"] for g in G]].astype(float)
    GQ = np.r_[gqa, gqh]; DN = np.r_[dn, dn]; k = 2 * NT
    def fit(idx, w, x0):
        s, t, y, hm, bo, bd, q = S[idx], T[idx], Y[idx], HM[idx], BO[idx], BD[idx], GQ[idx]
        def f(p):
            mu, home, b_o, b_d, Gc = p[k:k + 5]; att, dfn = p[:NT], p[NT:k]
            eta = mu + home * hm + att[s] - dfn[t] + b_o * bo + b_d * bd + Gc * q; lam = np.exp(eta)
            nll = (w * (lam - y * eta)).sum() + 10 * (att @ att + dfn @ dfn) + 2.5 * (b_o**2 + b_d**2 + Gc**2)
            r = w * (lam - y); gr = np.zeros_like(p)
            gr[:NT] = np.bincount(s, r, NT) + 20 * att; gr[NT:k] = -np.bincount(t, r, NT) + 20 * dfn
            gr[k] = r.sum(); gr[k + 1] = (r * hm).sum(); gr[k + 2] = (r * bo).sum() + 5 * b_o
            gr[k + 3] = (r * bd).sum() + 5 * b_d; gr[k + 4] = (r * q).sum() + 5 * Gc
            return nll, gr
        return minimize(f, x0, jac=True, method="L-BFGS-B", options={"maxiter": 400}).x
    x0 = np.r_[np.zeros(k), math.log(3.0), 0.05, 0, 0, 0]; preds = {}
    start = date(2023, 11, 15).toordinal()
    for D in sorted(set(dn)):
        if D < start: continue
        msk = DN < D; age = D - DN[msk]; w = 0.5 ** (age / 120.0); keep = w > 0.01
        idx = np.where(msk)[0][keep]; x0 = fit(idx, w[keep], x0)
        mu, home, b_o, b_d, Gc = x0[k:k + 5]; att, dfn = x0[:NT], x0[NT:k]
        for i in np.where(dn == D)[0]:
            g = G[i]
            lh = math.exp(mu + home + att[hi[i]] - dfn[ai[i]] + b_o * g["b2b_h"] + b_d * g["b2b_a"] + Gc * gqa[i])
            la = math.exp(mu + att[ai[i]] - dfn[hi[i]] + b_o * g["b2b_a"] + b_d * g["b2b_h"] + Gc * gqh[i])
            preds[i] = R.to_poisson_inputs(lh, la)
    return G, preds


def score(G, preds):
    acc = defaultdict(lambda: defaultdict(lambda: {"n": 0, "ll_m": 0.0, "ll_q": 0.0, "bets": 0, "w": 0, "l": 0, "p": 0, "u": 0.0}))
    for i, (lh, la) in preds.items():
        g = G[i]; ss = f"{g['d'].year if g['d'].month >= 8 else g['d'].year - 1}"
        hs, as_ = g["hs"], g["as"]
        mk = []
        try:
            L = float(g["spread_home"]) if g.get("spread_home") not in (None, "") else None
            T = float(g["total"]) if g.get("total") not in (None, "") else None
        except ValueError:
            L = T = None
        f = nhl_model.probs(lh, la, home_line=L, total=T)
        if g.get("ml_home") not in (None, "") and g.get("ml_away") not in (None, "") and g.get("ml_status", "fixed") != "excluded":
            mk.append(("ml", f["ml_home"], g["ml_home"], g["ml_away"], 1 if hs > as_ else 0, False))
        if L is not None and f.get("pl_home") is not None and g.get("sp_price_home") and g.get("sp_price_away"):
            mar = hs - as_ + L
            mk.append(("pl", f["pl_home"], g["sp_price_home"], g["sp_price_away"], 1 if mar > 0 else 0, mar == 0))
        if T is not None and f.get("over") is not None and g.get("over_price") and g.get("under_price"):
            tot = hs + as_
            mk.append(("tot", f["over"], g["over_price"], g["under_price"], 1 if tot > T else 0, tot == T))
        for name, p, pa, pb, y, push in mk:
            try:
                ia, ib = imp(pa), imp(pb)
            except ValueError:
                continue
            if not (1.0 <= ia + ib <= 1.12):
                continue
            q = ia / (ia + ib); p = min(max(p, 1e-4), 1 - 1e-4)
            for key in (ss, "ALL_TEST" if ss != "2023" else None):
                if key is None: continue
                a = acc[name][key]
                if not push:
                    a["n"] += 1
                    a["ll_m"] -= math.log(p if y else 1 - p); a["ll_q"] -= math.log(q if y else 1 - q)
                for side_p, side_q, price, win in ((p, q, pa, y == 1), (1 - p, 1 - q, pb, y == 0)):
                    e = 100 * (side_p - imp(price))
                    if e >= EDGE and abs(100 * (side_p - side_q)) <= GAP:
                        a["bets"] += 1
                        if push: a["p"] += 1
                        elif win: a["w"] += 1; a["u"] += payout(price)
                        else: a["l"] += 1; a["u"] -= 1
    out = {}
    for name, d in acc.items():
        out[name] = {s: {"n": a["n"], "logloss_model": round(a["ll_m"] / max(a["n"], 1), 4),
                         "logloss_market": round(a["ll_q"] / max(a["n"], 1), 4),
                         "bets": a["bets"], "record": f"{a['w']}-{a['l']}-{a['p']}", "units": round(a["u"], 2),
                         "usd_at_1u_20": round(a["u"] * UNIT, 2)} for s, a in sorted(d.items())}
    return out


if __name__ == "__main__":
    res = {"built_ct": "2026-10-09", "method": __doc__, "variants": {}}
    for name, alpha in (("xg50_goals50 (live spec)", 0.5), ("goals_only_2023plus", 0.0), ("xg_only_2023plus", 1.0)):
        G, P = run(alpha)
        res["variants"][name] = score(G, P)
        print(name, json.dumps({k: v.get("ALL_TEST") for k, v in res["variants"][name].items()}), flush=True)
    (ROOT / "data" / "history" / "nhl_xg_backtest.json").write_text(json.dumps(res, indent=1))
