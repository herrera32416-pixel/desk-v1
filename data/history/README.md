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
