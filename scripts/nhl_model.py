#!/usr/bin/env python3
"""NHL goal-based (Poisson) price conversion — stdlib port of research/nhl-2026-10/sim/nhl_sim.py.

Regulation non-empty-net goals: independent Poisson(lh), Poisson(la) with draw inflation
TIE_INFL on the diagonal (plain Poisson under-predicts regulation ties ~16-17% vs 21-25% seen),
then an empirical empty-net transition (|margin ex-EN| -> final regulation margin), then:
  * regulation tie -> OT/SO: winner +1 goal; P(home wins OT/SO) = P_HOME_OT
  * ML (2-way) includes OT and SO
  * puck line: an OT/SO game is a 1-goal margin, so -1.5 loses past regulation
  * totals: final = regulation goals, +1 if the game went to OT/SO (SO winner counts 1 goal)
Constants (research/nhl-2026-10/sim/results/game_level_stats.json, free NHL API + MoneyPuck):
  P_HOME_OT 0.5243 (n=1,459 ties 2021-22..2025-26), TIE_INFL 1.50 (walk-forward fits 1.45-1.60),
  EN transitions from 2,614 MoneyPuck games (2023-24 + 2024-25).

market_lambdas() solves (lh, la) so the model reproduces the market's no-vig ML and no-vig
over % at the posted total. The result is a market-implied fair price for every market;
it is NOT an independent model (ESPN has no NHL predictor), so DESK shows it as "fair".
"""
from __future__ import annotations

import math

MAXG = 13
P_HOME_OT = 0.5243
TIE_INFL = 1.50
EN_TRANS = {
    0: {0: 0.9514, 1: 0.0324, 2: 0.009, 3: 0.0072},
    1: {0: 0.0024, 1: 0.5304, 2: 0.3945, 3: 0.0727},
    2: {2: 0.3853, 3: 0.6075, 4: 0.0072},
    3: {2: 0.0065, 3: 0.7101, 4: 0.2834},
    4: {3: 0.0149, 4: 0.9505, 5: 0.0347},
}


def _pois(lam: float) -> list[float]:
    p = [math.exp(-lam)]
    for k in range(1, MAXG):
        p.append(p[-1] * lam / k)
    return p


def final_dist(lh: float, la: float) -> dict[tuple[int, int], float]:
    """Joint distribution of FINAL (home, away) score incl. EN goals and the OT/SO +1."""
    ph, pa = _pois(lh), _pois(la)
    M = {}
    s = 0.0
    for i in range(MAXG):
        for j in range(MAXG):
            v = ph[i] * pa[j] * (TIE_INFL if i == j else 1.0)
            M[(i, j)] = v
            s += v
    F: dict[tuple[int, int], float] = {}

    def add(k, v):
        F[k] = F.get(k, 0.0) + v

    for (i, j), v in M.items():
        p = v / s
        if p < 1e-13:
            continue
        m = abs(i - j)
        for mf, q in EN_TRANS.get(m, {m: 1.0}).items():
            d = mf - m
            if m == 0:
                if d == 0:
                    add((i, j), p * q)
                else:
                    add((i + d, j), 0.5 * p * q)
                    add((i, j + d), 0.5 * p * q)
            elif d >= 0:
                add((i + d, j) if i > j else (i, j + d), p * q)
            else:
                add((i, j - d) if i > j else (i - d, j), p * q)
    out: dict[tuple[int, int], float] = {}
    for (i, j), p in F.items():
        if i == j:  # regulation tie -> OT/SO winner +1
            out[(i + 1, j)] = out.get((i + 1, j), 0.0) + p * P_HOME_OT
            out[(i, j + 1)] = out.get((i, j + 1), 0.0) + p * (1 - P_HOME_OT)
            out[("tie",)] = out.get(("tie",), 0.0) + p
        else:
            out[(i, j)] = out.get((i, j), 0.0) + p
    return out


def probs(lh: float, la: float, home_line: float | None = None, total: float | None = None) -> dict:
    D = final_dist(lh, la)
    reg_tie = D.pop(("tie",), 0.0)
    r = {"lam_home": lh, "lam_away": la, "reg_tie": reg_tie,
         "ml_home": sum(p for (i, j), p in D.items() if i > j)}
    if home_line is not None:
        cov = sum(p for (i, j), p in D.items() if (i - j) + home_line > 0)
        push = sum(p for (i, j), p in D.items() if (i - j) + home_line == 0)
        r["pl_home"] = cov / (1 - push) if push < 1 else None
    if total is not None:
        ov = sum(p for (i, j), p in D.items() if i + j > total)
        un = sum(p for (i, j), p in D.items() if i + j < total)
        r["over"] = ov / (ov + un) if ov + un > 0 else None
    return r


def _bisect(f, lo, hi, n=40):
    flo = f(lo)
    for _ in range(n):
        mid = 0.5 * (lo + hi)
        fm = f(mid)
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return 0.5 * (lo + hi)


def market_lambdas(q_ml_home: float, q_over: float, total: float):
    """Solve (lh, la): model ML home = q_ml_home and model P(over total | no push) = q_over."""
    def share_for(mu):
        return _bisect(lambda s: probs(mu * s, mu * (1 - s))["ml_home"] - q_ml_home, 0.2, 0.8, 28)

    def over_gap(mu):
        s = share_for(mu)
        return probs(mu * s, mu * (1 - s), total=total)["over"] - q_over

    mu = _bisect(over_gap, 3.0, 9.0, 26)
    s = share_for(mu)
    return mu * s, mu * (1 - s)
