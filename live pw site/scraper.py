"""
scraper.py — builds league_history.json from ESPN (2014–2024) and Sleeper (2025+).
Run this whenever you want to refresh league data.
"""

import json
import os
from datetime import datetime, timezone
from espn_api.football import League

from local_config import ESPN_SWID, ESPN_S2
from sleeper_common import (
    SLEEPER_LEAGUE_ID, reg_season_weeks, get_player_names,
    walk_league_chain, build_season,
)

# Approximate start of fantasy week 1 for each ESPN season (Wednesday before NFL kickoff)
NFL_WEEK1 = {
    2014: datetime(2014, 9, 3),
    2015: datetime(2015, 9, 9),
    2016: datetime(2016, 9, 7),
    2017: datetime(2017, 9, 6),
    2018: datetime(2018, 9, 5),
    2019: datetime(2019, 9, 4),
    2020: datetime(2020, 9, 9),
    2021: datetime(2021, 9, 8),
    2022: datetime(2022, 9, 7),
    2023: datetime(2023, 9, 6),
    2024: datetime(2024, 9, 4),
}

# ─── Output ───────────────────────────────────────────────────────────────────
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "league_history.json")

# ─── ESPN Config ──────────────────────────────────────────────────────────────
# Session cookies live in local_config.py (imported above) — keep them out of
# this file so it stays safe to share.
ESPN_LEAGUE_ID = 113291
ESPN_YEARS     = range(2014, 2025)  # 2014 through 2024


# ═══════════════════════════════════════════════════════════════════════════════
#  ESPN
# ═══════════════════════════════════════════════════════════════════════════════

def espn_owner_name(team):
    """Return 'First Last' for an ESPN team's primary owner."""
    if team.owners:
        o = team.owners[0]
        # ESPN stores some names with stray padding ("Chris " + " Nguyen"), which
        # would otherwise split one owner's history across two spellings.
        return " ".join(f"{o['firstName']} {o['lastName']}".split())
    return f"Unknown_{team.team_id}"


def date_to_week(dt, year):
    """Convert a datetime to approximate fantasy week number for a given ESPN season year."""
    start = NFL_WEEK1.get(int(year))
    if not start:
        return 0
    days = (dt - start).days
    if days < 0:
        return 0
    return days // 7 + 1


def get_espn_transactions(league, year):
    """
    Page through ESPN recent_activity to collect all transactions for the season.
    Returns list of transaction dicts in the same format as Sleeper transactions.
    For TRADED actions: msg['from'] is the team giving the player away.
    For all other actions: msg['to'] is the team receiving.
    """
    season_start = datetime(int(year), 8, 1)
    season_end   = datetime(int(year) + 1, 3, 1)

    raw = []
    offset = 0
    batch  = 500
    while True:
        try:
            activities = league.recent_activity(size=batch, offset=offset)
        except Exception as e:
            print(f"\n    (ESPN transactions unavailable: {e})", end=" ")
            break
        if not activities:
            break
        raw.extend(activities)
        if len(activities) < batch:
            break
        offset += batch

    transactions = []
    for activity in raw:
        # Naive UTC datetime (utcfromtimestamp is deprecated in Python 3.12+)
        dt = datetime.fromtimestamp(activity.date / 1000, tz=timezone.utc).replace(tzinfo=None)
        if not (season_start <= dt <= season_end):
            continue

        adds  = {}
        drops = {}
        txn_type = None

        for team, action, player, _ in activity.actions:
            owner = espn_owner_name(team) if team else "Unknown"
            pname = player.name if player and hasattr(player, "name") else str(player or "Unknown")

            if action == "FA ADDED":
                adds.setdefault(owner, []).append(pname)
                txn_type = "free_agent"
            elif action == "WAIVER ADDED":
                adds.setdefault(owner, []).append(pname)
                txn_type = "waiver"
            elif action == "DROPPED":
                drops.setdefault(owner, []).append(pname)
                if txn_type is None:
                    txn_type = "drop"
            elif action == "TRADED":
                # team is the one GIVING the player; recipient is determined below
                drops.setdefault(owner, []).append(pname)
                txn_type = "trade"

        # For trades: infer adds (each team receives everything the other teams gave)
        if txn_type == "trade" and drops:
            all_traders = list(drops.keys())
            for receiver in all_traders:
                received = [p for giver, players in drops.items()
                            if giver != receiver for p in players]
                if received:
                    adds[receiver] = received

        # Skip pure drops with no adds
        if not adds:
            continue
        if txn_type is None:
            continue

        week = date_to_week(dt, year)
        date_str = f"{dt.strftime('%b')} {dt.day}"

        transactions.append({
            "week": week,
            "date": date_str,
            "type": txn_type,
            "adds":  adds,
            "drops": drops,
        })

    # Sort chronologically
    transactions.sort(key=lambda t: t["week"])
    return transactions


def build_espn_season(year):
    print(f"  ESPN {year}...", end=" ", flush=True)
    try:
        league = League(league_id=ESPN_LEAGUE_ID, year=year, espn_s2=ESPN_S2, swid=ESPN_SWID)
    except Exception as e:
        print(f"FAILED ({e})")
        return None

    reg_weeks = getattr(league.settings, "reg_season_count", reg_season_weeks(year))

    # ── Standings ──
    teams = []
    try:
        standings = league.standings()
        ranked = list(enumerate(standings, 1))
    except Exception:
        ranked = list(enumerate(league.teams, 1))

    for rank, team in ranked:
        gp = team.wins + team.losses + team.ties
        teams.append({
            "name":               team.team_name,
            "team_id":            team.team_id,
            "owner":              espn_owner_name(team),
            "wins":               team.wins,
            "losses":             team.losses,
            "ties":               team.ties,
            "points_for":         round(team.points_for, 2),
            "points_against":     round(team.points_against, 2),
            "avg_points_for":     round(team.points_for  / gp, 1) if gp else 0,
            "avg_points_against": round(team.points_against / gp, 1) if gp else 0,
            "point_differential": round((team.points_for - team.points_against) / gp, 1) if gp else 0,
            "rank":               rank,
            "roster":             [{"name": p.name, "position": p.position} for p in team.roster],
        })

    # ── Weekly scores + H2H (regular season only) ──
    weekly_scores = {}
    head_to_head  = {espn_owner_name(t): {} for t in league.teams}

    for week in range(1, reg_weeks + 1):
        try:
            matchups = league.scoreboard(week=week)
        except Exception:
            weekly_scores[str(week)] = []
            continue

        week_results = []
        for m in matchups:
            if not getattr(m, 'away_team', None):
                continue
            hs, as_ = m.home_score, m.away_score
            ho = espn_owner_name(m.home_team)
            ao = espn_owner_name(m.away_team)

            week_results.append({
                "home_team":  m.home_team.team_name,
                "away_team":  m.away_team.team_name,
                "home_score": hs,
                "away_score": as_,
            })

            head_to_head[ho].setdefault(ao, {"wins": 0, "losses": 0, "ties": 0})
            head_to_head[ao].setdefault(ho, {"wins": 0, "losses": 0, "ties": 0})
            if hs > as_:
                head_to_head[ho][ao]["wins"]   += 1
                head_to_head[ao][ho]["losses"] += 1
            elif as_ > hs:
                head_to_head[ao][ho]["wins"]   += 1
                head_to_head[ho][ao]["losses"] += 1
            else:
                head_to_head[ho][ao]["ties"] += 1
                head_to_head[ao][ho]["ties"] += 1

        weekly_scores[str(week)] = week_results

    # ── Playoff bracket ──
    # Search up to 8 weeks past the regular season to find all playoff rounds.
    # Don't break on exception or empty scoreboard — skip and keep looking.
    bracket = []
    round_num = 1
    consecutive_empty = 0
    for playoff_week in range(reg_weeks + 1, reg_weeks + 9):
        try:
            matchups = league.scoreboard(week=playoff_week)
        except Exception as e:
            print(f"\n    (week {playoff_week} error: {e})", end=" ")
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue

        if not matchups:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue

        round_matchups = []
        for m in matchups:
            # Skip bye matchups (no opponent — seeds 1 and 2 in 6-team era)
            if not getattr(m, 'away_team', None):
                continue
            hs, as_ = m.home_score, m.away_score
            if not hs and not as_:
                continue
            winner = (m.home_team.team_name if hs > as_
                      else m.away_team.team_name if as_ > hs
                      else None)
            round_matchups.append({
                "team1":  m.home_team.team_name,
                "score1": round(hs, 2),
                "team2":  m.away_team.team_name,
                "score2": round(as_, 2),
                "winner": winner,
            })

        if round_matchups:
            bracket.append({"round": round_num, "matchups": round_matchups})
            round_num += 1
            consecutive_empty = 0
        else:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break

    # ── Draft ──
    draft_picks = []
    try:
        for pick in sorted(league.draft, key=lambda p: (p.round_num, p.round_pick)):
            owner = espn_owner_name(pick.team) if hasattr(pick, "team") and pick.team else "Unknown"
            position = "?"
            try:
                position = pick.position
            except AttributeError:
                pass
            player_name = getattr(pick, "playerName", None) or getattr(pick, "player_name", "Unknown")
            draft_picks.append({
                "round":    pick.round_num,
                "pick":     pick.round_pick,
                "player":   player_name,
                "position": position,
                "owner":    owner,
            })
    except Exception as e:
        print(f"\n    (draft unavailable for {year}: {e})", end=" ")

    transactions = get_espn_transactions(league, year)

    print(f"done. ({len(teams)} teams, {len(draft_picks)} draft picks, {len(bracket)} playoff rounds, {len(transactions)} transactions)")
    return {
        "teams":         teams,
        "weekly_scores": weekly_scores,
        "head_to_head":  head_to_head,
        "playoffs":      bracket,
        "draft":         draft_picks,
        "transactions":  transactions,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════

def build_all():
    history = {}

    print("=== ESPN seasons ===")
    for year in ESPN_YEARS:
        data = build_espn_season(year)
        if data:
            history[str(year)] = data

    print("\n=== Sleeper seasons ===")
    player_names = get_player_names()

    # Walk the previous_league_id chain (newest → oldest), then process oldest first
    chain = walk_league_chain(SLEEPER_LEAGUE_ID)
    print(f"  Found {len(chain)} Sleeper season(s).")
    for league_id, chain_season, _status in reversed(chain):
        prev_data = history.get(str(int(chain_season) - 1)) if chain_season.isdigit() else None
        season, _status, data = build_season(league_id, player_names, prev_data)
        history[season] = data

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=4, ensure_ascii=False)

    seasons = sorted(k for k in history if k.isdigit())
    print(f"\nDone. Saved to {OUTPUT_FILE}")
    print(f"All seasons: {', '.join(seasons)}")


if __name__ == "__main__":
    build_all()
