"""NHL xG model (PAPER, research-only). Built 2026-10-09.

Same Poisson team-rating machinery as nhl_ratings.py (att/def, 120-day decay, shrinkage, home ice, back-to-back,
starting-goalie save-quality term) but:
  * TRAINED ONLY ON 2023-24 AND LATER (NHL shot-tracking change; older seasons dropped entirely);
  * target per team-game = 0.5 * MoneyPuck xG (flurry+score+venue adjusted, all situations) + 0.5 * regulation
    goals (variant F in /workspace/nhl-research/RESULTS.md: best puck-line/totals units of the 10 variants).
    A game with no xG yet falls back to goals only.
xG source: data/history/nhl/xg_games.csv (committed, 2023-24..), refreshed once per run from MoneyPuck's free
per-team game files for the current season (non-commercial personal use). Never used for stamps: NHL w stays
locked at 0 (nhl_ratings.NHL_W_LOCKED_ZERO); the board headline is the market no-vig %."""
from __future__ import annotations

import csv
import io
import urllib.request
from datetime import date, datetime

import nhl_ratings as R

XG_CSV = R.HIST / "xg_games.csv"
MIN_SEASON_START = date(2023, 8, 1)
ALPHA = 0.5
MP_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/regular/teams/{t}.csv"
MP_FIX = {"L.A": "LAK", "N.J": "NJD", "S.J": "SJS", "T.B": "TBL"}
MODEL_NAME = "NHL xG model v1 (2023+ only, 0.5 xG + 0.5 goals, goalie, b2b) — research only"


def load_xg() -> dict:
    out = {}
    if XG_CSV.exists():
        for r in csv.DictReader(open(XG_CSV)):
            out[(r["date_et"], r["home"], r["away"])] = (float(r["xg_home"]), float(r["xg_away"]), r.get("espn_id", ""))
    return out


def refresh_xg(today: date, timeout: int = 40) -> int:
    """Append current-season MoneyPuck team-game xG (HOME rows, situation=all) not yet in xg_games.csv."""
    have = load_xg()
    season = today.year if today.month >= 8 else today.year - 1
    new = []
    for t in sorted(R.NHL_TEAMS - {"ARI"}):
        try:
            req = urllib.request.Request(MP_URL.format(t=t), headers={"User-Agent": "Mozilla/5.0 (desk paper)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode("utf-8", "replace")
        except Exception:
            continue
        for row in csv.DictReader(io.StringIO(txt)):
            if row.get("season") != str(season) or row.get("situation") != "all" or row.get("home_or_away") != "HOME":
                continue
            if str(row.get("gameId", ""))[4:6] != "02":
                continue
            d = datetime.strptime(row["gameDate"], "%Y%m%d").date().isoformat()
            h, a = MP_FIX.get(row["playerTeam"], row["playerTeam"]), MP_FIX.get(row["opposingTeam"], row["opposingTeam"])
            if (d, h, a) in have:
                continue
            try:
                xh, xa = float(row["flurryScoreVenueAdjustedxGoalsFor"]), float(row["flurryScoreVenueAdjustedxGoalsAgainst"])
            except (KeyError, ValueError):
                continue
            have[(d, h, a)] = (xh, xa, "")
            new.append([row.get("gameId", ""), d, h, a, round(xh, 3), round(xa, 3), "moneypuck flurryScoreVenueAdj xG, all sit."])
    if new:
        with open(XG_CSV, "a", newline="") as f:
            csv.writer(f).writerows(new)
    return len(new)


def apply_xg_target(games: list[dict], xg: dict, alpha: float = ALPHA) -> dict:
    """Drop pre-2023-24 games; blend each game's regulation goals with xG. Returns coverage stats."""
    keep, hit = [], 0
    for g in games:
        if g["d"] < MIN_SEASON_START:
            continue
        x = xg.get((g["d"].isoformat(), g["h"], g["a"]))
        if x:
            hit += 1
            g["hr_goals"], g["ar_goals"] = g["hr"], g["ar"]
            g["hr"] = alpha * x[0] + (1 - alpha) * g["hr"]
            g["ar"] = alpha * x[1] + (1 - alpha) * g["ar"]
        keep.append(g)
    games[:] = keep
    return {"games_2023plus": len(keep), "with_xg": hit}


class XGLiveModel(R.LiveModel):
    """LiveModel with the xG target and the 2023+ training window."""

    def __init__(self, today: date, fetch: bool = True):
        self._xg_note = ""
        if fetch:
            try:
                self._xg_note = f"{refresh_xg(today)} new MoneyPuck xG games"
            except Exception as e:  # noqa: BLE001
                self._xg_note = f"xG refresh failed: {type(e).__name__}"
        orig = R.load_games

        def patched(extra=None):
            gs = orig(extra)
            self.xg_cov = apply_xg_target(gs, load_xg())
            return gs
        R.load_games = patched
        try:
            super().__init__(today, fetch)
        finally:
            R.load_games = orig
        self.notes.append(self._xg_note)
        self.notes.append(f"xG coverage {self.xg_cov}")
        self.model_name = MODEL_NAME
