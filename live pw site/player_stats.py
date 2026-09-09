"""
player_stats.py — per-player game logs, projections and profile data.

Sleeper exposes per-player weekly stats and projections; this module fetches
them, blends them into a single game log, and shapes the box score by
position. Results are cached in memory so repeated page views are free.

The league is half-PPR (0.5 per reception), so pts_half_ppr / pos_rank_half_ppr
are the headline numbers.
"""

import time

from sleeper_common import safe_get

SCORING = "half_ppr"          # matches the league's scoring settings
PTS_KEY = f"pts_{SCORING}"
RANK_KEY = f"pos_rank_{SCORING}"

_cache = {}
_TTL = 300                     # 5 minutes — live enough during games


def _cached(key, fetch_fn, ttl=_TTL):
    hit = _cache.get(key)
    if hit and time.time() - hit["ts"] < ttl:
        return hit["data"]
    try:
        data = fetch_fn()
    except Exception:
        if hit:
            return hit["data"]      # serve stale rather than break the page
        raise
    _cache[key] = {"ts": time.time(), "data": data}
    return data


# Box-score columns per position: (stat key, column label)
STAT_COLUMNS = {
    "QB":  [("pass_cmp", "Cmp"), ("pass_att", "Att"), ("pass_yd", "Pass Yds"),
            ("pass_td", "Pass TD"), ("pass_int", "INT"),
            ("rush_att", "Rush"), ("rush_yd", "Rush Yds"), ("rush_td", "Rush TD")],
    "RB":  [("rush_att", "Car"), ("rush_yd", "Rush Yds"), ("rush_td", "Rush TD"),
            ("rec_tgt", "Tgt"), ("rec", "Rec"), ("rec_yd", "Rec Yds"), ("rec_td", "Rec TD")],
    "WR":  [("rec_tgt", "Tgt"), ("rec", "Rec"), ("rec_yd", "Rec Yds"), ("rec_td", "Rec TD"),
            ("rush_att", "Car"), ("rush_yd", "Rush Yds")],
    "TE":  [("rec_tgt", "Tgt"), ("rec", "Rec"), ("rec_yd", "Rec Yds"), ("rec_td", "Rec TD")],
    "K":   [("fgm", "FGM"), ("fga", "FGA"), ("fgm_50p", "50+"), ("xpm", "XPM"), ("xpa", "XPA")],
    "DEF": [("def_sack", "Sack"), ("def_int", "INT"), ("def_fr", "FR"),
            ("def_td", "TD"), ("def_safe", "Saf"), ("pts_allow", "Pts All")],
}


def stat_columns_for(position):
    return STAT_COLUMNS.get(position, STAT_COLUMNS["WR"])


# Sleeper returns 999 as "not ranked" (e.g. it doesn't rank kickers weekly).
UNRANKED = 999


def _clean_rank(value):
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return None if n >= UNRANKED else n


def nfl_weeks(season):
    """Regular-season weeks in an NFL season (17 games from 2021, 16 before)."""
    return 18 if int(season) >= 2021 else 17


def _nfl_state():
    return _cached("nfl_state",
                   lambda: safe_get("https://api.sleeper.app/v1/state/nfl",
                                    timeout=15) or {},
                   ttl=900)


def last_week_played(season):
    """How far to run a game log. A finished season runs the whole way; the
    season in progress stops at the current week so we don't print a wall of
    zeroes for games nobody has played yet."""
    total = nfl_weeks(season)
    try:
        state = _nfl_state()
    except Exception:
        return total
    if str(season) == str(state.get("season")):
        if state.get("season_type") != "regular":
            # Preseason shows nothing yet; after the regular season it's done.
            return 0 if state.get("season_type") == "pre" else total
        return max(0, min(total, int(state.get("week") or 0)))
    return total


def _fetch_grouped(kind, player_id, season):
    """kind is 'stats' or 'projections'; returns {week: {...}}"""
    url = (f"https://api.sleeper.com/{kind}/nfl/player/{player_id}"
           f"?season_type=regular&season={season}&grouping=week")
    return safe_get(url, timeout=20) or {}


def get_player_game_log(player_id, season, position):
    """Weekly rows blending actual stats with projections, plus season totals."""
    def fetch():
        stats = _fetch_grouped("stats", player_id, season)
        projs = _fetch_grouped("projections", player_id, season)
        cols = stat_columns_for(position)

        # Every week of the season gets a row, not just the ones with a stat
        # line. A week a player sat out still scored his fantasy owner nothing,
        # so it belongs in the log — Sleeper returns null for bye weeks, which
        # would otherwise vanish from the table entirely.
        last = last_week_played(season)
        rows = []
        for wk in range(1, last + 1):
            s = (stats.get(str(wk)) or {})
            p = (projs.get(str(wk)) or {})
            s_stats = s.get("stats") or {}
            p_stats = p.get("stats") or {}
            played = bool(s_stats.get("gp"))
            opponent = s.get("opponent") or p.get("opponent")
            if played:
                status = "played"
            elif opponent:
                status = "dnp"          # team played, he didn't
            else:
                # Sleeper returns nothing for a week it has no game data —
                # a real bye, but equally PUP, IR or a mid-season signing.
                # We can't tell which, so don't claim one.
                status = "nogame"
            rows.append({
                "week":      wk,
                "opponent":  opponent,
                "is_away":   s.get("is_away_team"),
                # Didn't play means zero points for whoever started him.
                "points":    s_stats.get(PTS_KEY) if played else 0,
                "projected": p_stats.get(PTS_KEY),
                "pos_rank":  _clean_rank(s_stats.get(RANK_KEY)),
                "snaps":     s_stats.get("off_snp"),
                "played":    played,
                "status":    status,
                "box":       [(label, s_stats.get(key)) for key, label in cols],
            })

        played_rows = [r for r in rows if r["played"]]
        pts = [r["points"] for r in played_rows if r["points"] is not None]
        ranks = [r["pos_rank"] for r in played_rows if r["pos_rank"] is not None]
        totals = {
            "games":      len(played_rows),
            "missed":     sum(1 for r in rows if r["status"] != "played"),
            "total":      round(sum(pts), 2) if pts else 0,
            # Per game actually played — sitting out shouldn't drag the average.
            "avg":        round(sum(pts) / len(pts), 2) if pts else 0,
            "best":       max(pts) if pts else None,
            "worst":      min(pts) if pts else None,
            "avg_rank":   round(sum(ranks) / len(ranks), 1) if ranks else None,
            "best_rank":  min(ranks) if ranks else None,
        }
        return {"rows": rows, "totals": totals, "columns": [c[1] for c in cols]}

    return _cached(f"log:{player_id}:{season}", fetch)


def get_season_totals(player_id, season):
    """Season-long totals straight from Sleeper (includes season pos rank)."""
    def fetch():
        url = (f"https://api.sleeper.com/stats/nfl/player/{player_id}"
               f"?season_type=regular&season={season}")
        return (safe_get(url, timeout=20) or {}).get("stats") or {}
    return _cached(f"season:{player_id}:{season}", fetch, ttl=900)


def season_summary(season_totals, position):
    """Season-long rank plus cumulative box-score stats, shaped to match the
    game log's columns so they line up in a totals row."""
    if not season_totals:
        return None
    cols = stat_columns_for(position)
    return {
        "pos_rank":     _clean_rank(season_totals.get(RANK_KEY)),
        "overall_rank": _clean_rank(season_totals.get(f"rank_{SCORING}")),
        "points":       season_totals.get(PTS_KEY),
        "games":        season_totals.get("gp"),
        "snaps":        season_totals.get("off_snp"),
        "box":          [(label, season_totals.get(key)) for key, label in cols],
    }


def format_height(inches):
    try:
        n = int(inches)
        return f"{n // 12}'{n % 12}\""
    except (TypeError, ValueError):
        return None
