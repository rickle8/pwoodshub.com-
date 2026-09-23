"""
projections.py — roster ratings from the consensus player board.

A team's rating is the projected weekly output of its best legal starting
lineup, so depth only counts where the lineup can actually use it. The
per-player numbers come from consensus.py, which blends an expert projection
with the draft and trade markets.
"""

import time

import consensus
from sleeper_common import safe_get

SEASON_GAMES = 17          # projections are season totals; we want per-week
_ALL_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

# Slot rules live in lineups.py so the projected best lineup and the
# hindsight best lineup can never drift apart.
from lineups import DEFAULT_LINEUP, FLEX_POSITIONS, best_lineup  # noqa: E402

_cache = {}
_TTL = 6 * 3600            # projections move slowly; twice a day is plenty


def _cached(key, fetch_fn, ttl=_TTL):
    hit = _cache.get(key)
    if hit and time.time() - hit["ts"] < ttl:
        return hit["data"]
    try:
        data = fetch_fn()
    except Exception:
        if hit:
            return hit["data"]      # stale ratings beat no ratings
        raise
    _cache[key] = {"ts": time.time(), "data": data}
    return data


def get_season_projections(season, players=None):
    """{player_id: consensus half-PPR points for the full season}.

    Falls back to the raw expert projection if the consensus board can't be
    built (e.g. FantasyCalc is down and the position map is unavailable).
    """
    if players:
        board = consensus.build_board(season, players)
        if board:
            # In-season the board is rest-of-season; scale it back to a
            # 17-game pace so the per-week maths below stays the same.
            return {pid: v["points"] * SEASON_GAMES / (v.get("weeks") or SEASON_GAMES)
                    for pid, v in board.items() if v["points"]}

    def fetch():
        qs = "&".join(f"position[]={p}" for p in _ALL_POSITIONS)
        url = (f"https://api.sleeper.com/projections/nfl/{season}"
               f"?season_type=regular&{qs}&order_by=pts_half_ppr")
        rows = safe_get(url, timeout=25) or []
        out = {}
        for row in rows:
            pts = (row.get("stats") or {}).get("pts_half_ppr")
            pid = row.get("player_id")
            if pid is not None and pts:
                out[str(pid)] = float(pts)
        return out
    return _cached(f"proj:{season}", fetch)


def optimal_lineup(roster, projections, lineup=None):
    """Best legal starting lineup by projection.

    Returns (weekly_points, starters) where starters is a list of
    (slot, player dict, weekly projection), best slot first.
    """
    lineup = lineup or DEFAULT_LINEUP
    by_id = {str(p.get("player_id")): p for p in roster if p.get("player_id")}
    weekly = {pid: projections[pid] / SEASON_GAMES
              for pid in by_id if projections.get(pid)}
    positions = {pid: p.get("position") for pid, p in by_id.items()}

    total, chosen = best_lineup(weekly, positions, lineup)
    return total, [(slot, by_id[pid], val) for slot, pid, val in chosen]


def roster_ratings(teams, season, players=None, lineup=None):
    """{owner: {"raw", "rating", "starters", "covered"}} for one season's teams.

    `raw` is the projected weekly lineup total. Projections are *average*
    outcomes, so a whole league of raw totals lands well below what teams
    really score — the spread between teams is the signal, not the level.
    `rating` rescales the raw numbers so the league averages 100, which keeps
    them readable and lets the odds model recentre them on the league's real
    scoring average.
    """
    try:
        projections = get_season_projections(season, players)
    except Exception:
        return {}
    if not projections:
        return {}

    out = {}
    for t in teams:
        roster = t.get("roster") or []
        raw, starters = optimal_lineup(roster, projections, lineup)
        covered = sum(1 for p in roster if str(p.get("player_id")) in projections)
        out[t["owner"]] = {"raw": raw, "starters": starters,
                           "covered": covered, "roster_size": len(roster)}

    raws = [v["raw"] for v in out.values() if v["raw"]]
    if not raws:
        return {}
    mean = sum(raws) / len(raws)
    for v in out.values():
        v["rating"] = round(v["raw"] / mean * 100, 1) if mean else 100.0
    return out
