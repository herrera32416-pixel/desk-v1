# DESK history store (paper research; built 2026-10-02 CT)

One CSV per sport per season (season = start year), one row per regular-season game.
Built with `scripts/history/build_history.py` (NHL, MLB) on the box. Every HTTP response is cached
gzipped at `/workspace/betbot-revamp/data/history/cache/` (not committed). NFL and CFB rows come
from the u3 research masters (`betbot-revamp/research/upgrades-2026-10/data/*_master.csv`).
**No Odds API calls were used** (its historical endpoint is paid).

## Sources
| Sport | Finals | Lines / prices | Pregame model |
|---|---|---|---|
| NFL | nflverse games | nflverse closing spread, total, ML, spread prices | ESPN FPI predictor `teamPredPtDiff` (core API) |
| CFB | ESPN scoreboard (FBS, groups=80) | ESPN core `/odds` close (ESPN BET 2022-25, DraftKings 2026) | ESPN FPI predictor |
| MLB | ESPN scoreboard (regular season) | ESPN core `/odds`, first non-live provider by DK > ESPN BET > Caesars (`odds_provider` column) | ESPN predictor `winProbability` |
| NHL | ESPN scoreboard (Final/OT/SO → `result_type`) | ESPN core `/odds` (DraftKings / ESPN BET) | none: ESPN says "Predictor is not supported for hockey". MoneyPuck pregame `home_win` (2023-24..2025-26) used **for comparison only** (non-commercial license; not stored here, not used live) |

Cross-checks: NHL ESPN finals vs the NHL API (`api-web.nhle.com`, research/nhl-2026-10/sim): 6,517 games matched, REG/OT/SO and scores agree 100%.
Skipped: cfbfastR/CFBD (needs an API key), SP+, Massey, paid Odds API history.

## Coverage
| Sport | Seasons | Games | Lines/prices | Pregame model |
|---|---|---|---|---|
| NFL | 2022–2026 (5; 2026 to date) | 1,411 rows (1,188 final) | close spread/ML/total 100% of 2022-25; 79 of 2026 so far | FPI: 2023-26 only (2022 missing) → 904 games fit |
| CFB | 2022–2026 (5) | 4,614 rows (3,993 final) | close spread 95-100% 2022-25, ML 89-91% | FPI ≈100% |
| MLB | 2023–2026 (4) | 9,722 | ML 99-100% (2024: 2,406/2,430) | ESPN win % 99.8% |
| NHL | 2021-22 – 2026-27 (5 full + 16 games) | 6,549 | ML/puck line 99.8%; **totals missing for 2023-24 (111/1,315)** and 61 games in 2022-23 | none from ESPN; MoneyPuck 2023-25 for comparison (3,936 matched) |

Gaps: NFL 2022 has no FPI; NHL 2023-24 totals mostly absent in ESPN odds; ESPN odds are one provider's
final pregame line (open/close fields only on some seasons), so "close" = the ESPN-listed pregame line,
not a sharp consensus; MLB/NHL odds before 2023/2021 were not pulled (ESPN core odds are empty for 2019 NHL).

## Refit blend weight w vs the closing line (`scripts/history/fit_w.py` → `fit_w.json`)
Football: pred = A + w(FPI − A) on margins; MLB/NHL: logit p = logit q + w(logit m − logit q) on wins.
CIs: 2,000 cluster-bootstrap reps (season-week / game date). Units: walk-forward by season, 1u flat,
edge ≥3pp vs the recorded price only.

| Sport | Model input | Games | w | 90% CI | 95% CI | Decision | Walk-forward units at recorded close prices |
|---|---|---|---|---|---|---|---|
| NFL | ESPN FPI predictor (margin) | 904 (2023–2026) | -0.310 | [-0.523, -0.103] | [-0.566, -0.058] | use fitted w | ATS 13-8-0, +4.11u, 90% CI [-3.6, +11.7]; ML 6-6-0, -1.27u, 90% CI [-6.7, +4.2] |
| CFB | ESPN FPI predictor (margin) | 3,934 (2022–2026) | +0.034 | [-0.053, +0.118] | [-0.067, +0.137] | w = 0 (90% CI includes 0) | ATS 21-21-0, +7.65u, 90% CI [-5.9, +21.2]; ML 0 bets |
| MLB | ESPN predictor winProbability | 9,690 (2023–2026) | +0.059 | [-0.077, +0.187] | [-0.100, +0.216] | w = 0 (90% CI includes 0) | ML 0 bets |
| NHL | MoneyPuck pregame home_win (comparison only; not used live) | 3,936 (2023–2025) | -0.049 | [-0.457, +0.360] | [-0.551, +0.426] | w = 0 (90% CI includes 0) | ML 0 bets |

Live DESK (scripts/paper_model.py): NFL w = −0.31 (CI excludes 0; it **fades** FPI's disagreement with
the line — treat as fragile), CFB w = 0, NHL market only (MODEL blank; Poisson fair shown as info),
MLB off-season (w = 0 when it returns). Sigma = fitted residual SD: NFL 12.70, CFB 15.14.

NHL Poisson fair check (1,500 games with ML, total and ±1.5 prices): puck-line Brier 0.2350 (Poisson fair
from market ML+total) vs 0.2328 (market no-vig) — the conversion is a consistent pricer, not an edge.

## NHL ratings model (added Oct 2 2026) — PAPER ONLY

`scripts/nhl_ratings.py` (stdlib, deterministic, runs in GitHub Actions) → `scripts/nhl_model.py` (Poisson REG/OT/SO).

- Team attack/defense regulation-goal rates (final minus the OT/SO winner's +1; EN goals included), weighted Poisson MLE with
  time decay (half-life 120 days) and an L2 shrinkage prior (20 weighted games) toward league average; includes this season's games.
- Home ice, back-to-back (from the schedule), and the starting goalie's decayed, shrunk save % above league
  (goals saved / game, prior 1,500 shots) from the free NHL stats API `api.nhle.com/stats/rest/en/goalie/summary` (no key).
  Committed goalie game logs: `nhl/goalies_<seasonId>.csv` (2020-21 → 2026-27); the current season is re-fetched each run.
  Live starter = goalie log (Confirmed/Likely) when present, otherwise the team's recent-starter mix.
- This season's finals after the history build are appended to `nhl/live_finals.csv` by the daily run (ESPN scoreboard, one call per missing day).
- Tuning (half-life × shrinkage × goalie on/off) used 2021-22 (Nov 15+) and 2022-23 ML log loss only; test = 2023-24 → 2026-27 so far,
  each game predicted from a fit on games strictly before its date. Market = ESPN close no-vig (single provider).
- Blend: logit(p) = logit(q_market) + w·(logit(model) − logit(q_market)), w per market with a game-day cluster bootstrap CI; live w = 0
  unless the 90% CI excludes 0. Results: `nhl_model_fit.json` (`scripts/history/fit_nhl_model.py`).
- Gaps: totals missing for most of 2023-24 (ESPN), so O/U tests are smaller; the model knows nothing about roster changes,
  injuries, or skater lineups beyond what results show; goalie-log names are matched to NHL playerIds by name + team.

## 2026-10-09: NHL moneyline re-pull (2021-22, 2022-23, 2023-24)
The stored DraftKings ML pairs from ESPN core were corrupt (sides summed to ~0.83 implied). `scripts/history/repull_nhl_ml.py`
re-pulled every game from ESPN core `/odds` and kept the first internally sane book (implied sum 1.00–1.10) in order
ESPN BET > Caesars > MGM > Westgate. Result: 2021 1,269/1,272 fixed (Caesars/MGM), 2022 1,312/1,315 (ESPN BET/Caesars),
2023 1,314/1,315 (ESPN BET); 7 games with no sane book are blank with `ml_status=excluded`. Old values kept in
`ml_home_raw`/`ml_away_raw`; provider in `ml_provider`. Fixed medians: 1.036 / 1.041 / 1.040 implied (normal vig).
Any earlier NHL ML result that used 2021-23 (e.g. the "+51u raw ML" in nhl_model_fit.json) is invalid.

## 2026-10-09: NHL xG model (`scripts/nhl_xg.py`) + walk-forward backtest (`scripts/history/backtest_nhl_xg.py`)
2023-24+ only; target 0.5 MoneyPuck xG + 0.5 regulation goals (`data/history/nhl/xg_games.csv`). Results in
`nhl_xg_backtest.json`. It does not beat the market's log loss in any season/market, so NHL w stays locked at 0
(research line only; headline = market no-vig %).
