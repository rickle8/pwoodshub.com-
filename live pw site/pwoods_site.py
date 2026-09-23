from flask import (Flask, render_template, request, jsonify, abort, redirect, url_for,
                   send_from_directory)
import hashlib
import hmac
import json
import os
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock

import requests

from sleeper_common import (
    SLEEPER_LEAGUE_ID, OWNER_TEAM_NAMES,
    safe_get, resolve_owner, get_player_names,
)
from recaps import compute_week_awards, played_weeks
import player_stats
import projections
import consensus
import lineups
import trades
import trade_machine
import keepers
import draft_review
import push

app = Flask(__name__)

LEAGUE_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "league_history.json")
CHAT_LOG_FILE       = os.path.join(os.path.dirname(__file__), "chat_log.json")
CHAT_USERS_FILE     = os.path.join(os.path.dirname(__file__), "chat_users.json")
PREFS_FILE          = os.path.join(os.path.dirname(__file__), "user_prefs.json")
NOTES_FILE          = os.path.join(os.path.dirname(__file__), "league_notes.json")

# Sleeper's per-player stats endpoint returns nothing before this season, so
# there's no point walking a long career back any further than it.
SLEEPER_STATS_EARLIEST = 2009

# Klipy powers the chat GIF picker. Key lives in local_config.py (or the
# KLIPY_API_KEY env var). With no key the GIF button is hidden entirely.
try:
    from local_config import KLIPY_API_KEY
except ImportError:
    KLIPY_API_KEY = ""
KLIPY_API_KEY = KLIPY_API_KEY or os.environ.get("KLIPY_API_KEY", "")

# Guards read-modify-write cycles on the small JSON files (chat log, chat
# users, prefs) so concurrent requests can't clobber each other's writes.
_file_lock = Lock()


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return default


def _save_json_atomic(path, data, indent=2):
    """Write via a temp file + rename, the way update_sleeper.py already does.

    A plain open(path, "w") truncates first, so a crash or a restart mid-write
    leaves a half-written file that _load_json can only recover from by
    discarding — i.e. the chat log or everyone's notes, gone.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)


def load_prefs():
    return _load_json(PREFS_FILE, {})


def save_prefs(prefs):
    _save_json_atomic(PREFS_FILE, prefs)


def get_regular_season_weeks(year):
    """14 weeks from 2021 onward (NFL added 17th game); 13 weeks before that."""
    return 14 if int(year) >= 2021 else 13


_data_cache: dict = {"mtime": None, "data": {}}


def load_league_data() -> dict:
    """Load league data from JSON, re-parsing only when the file has changed on disk.
    On unchanged requests this is a single os.stat() call — essentially free."""
    try:
        mtime = os.path.getmtime(LEAGUE_HISTORY_FILE)
    except OSError:
        return _data_cache["data"]

    if mtime == _data_cache["mtime"] and _data_cache["data"]:
        return _data_cache["data"]

    try:
        # Explicit utf-8: Windows would otherwise default to cp1252 and choke
        # on any non-ASCII name in the file.
        with open(LEAGUE_HISTORY_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"Error loading league data: {e}")
        return _data_cache["data"]

    # Normalise known duplicate owner names (add entries here after platform migrations)
    name_aliases = {
        "Chris  Nguyen": "Chris Nguyen",  # extra space from ESPN data
    }
    for year_data in raw.values():
        if not isinstance(year_data, dict):
            continue
        for team in year_data.get("teams", []):
            team["owner"] = name_aliases.get(team.get("owner"), team.get("owner"))
        h2h = year_data.get("head_to_head", {})
        for old, canonical in name_aliases.items():
            # Outer key: fold the aliased owner's own row into the canonical one.
            if old in h2h:
                h2h.setdefault(canonical, {})
                for opp, record in h2h[old].items():
                    h2h[canonical].setdefault(opp, {"wins": 0, "losses": 0, "ties": 0})
                    for k in ("wins", "losses", "ties"):
                        h2h[canonical][opp][k] += record.get(k, 0)
                del h2h[old]
            # Inner keys: every *opponent's* row still refers to the old name.
            # Leaving these behind makes the table asymmetric — the aliased
            # owner's own record merges, but everyone else's games against him
            # fail the membership guard in calculate_historical_head_to_head()
            # and vanish.
            for opps in h2h.values():
                if old in opps:
                    record = opps.pop(old)
                    merged = opps.setdefault(canonical, {"wins": 0, "losses": 0, "ties": 0})
                    for k in ("wins", "losses", "ties"):
                        merged[k] += record.get(k, 0)

    # Strip any non-year keys (e.g. _latest_sleeper_id, retired_members)
    data = {k: v for k, v in raw.items() if k.isdigit()}
    _data_cache.update({"mtime": mtime, "data": data})
    return data


league_data = load_league_data()


@app.before_request
def refresh_league_data():
    """Check for file changes on each page request and reload only when necessary.
    Skipped for chat/prefs/gif/notes-api endpoints since they don't use league data."""
    if request.endpoint in ('get_messages', 'chat', 'static', 'get_prefs', 'set_prefs',
                            'gif_search', 'add_note', 'delete_note'):
        return
    global league_data
    league_data = load_league_data()


@app.after_request
def ensure_uid_cookie(response):
    """Assign a persistent UUID cookie if the visitor doesn't have one yet.
    This acts as a stable per-device/browser identifier for storing preferences."""
    if not request.cookies.get('pw-uid'):
        uid = str(uuid.uuid4())
        # secure only when the request itself was HTTPS, so this still works
        # over plain http on the LAN dev server.
        response.set_cookie('pw-uid', uid, max_age=60 * 60 * 24 * 365 * 10,
                            samesite='Lax', secure=request.is_secure, httponly=True)
    return response


@app.route('/api/prefs', methods=['GET'])
def get_prefs():
    uid = request.cookies.get('pw-uid', '')
    with _file_lock:
        prefs = load_prefs()
    defaults = {'theme': 'light', 'bg_color': '#add8e6'}
    return jsonify({**defaults, **prefs.get(uid, {})})


@app.route('/api/prefs', methods=['POST'])
def set_prefs():
    uid = request.cookies.get('pw-uid', '')
    if not uid:
        return jsonify({'error': 'no uid'}), 400
    data = request.get_json() or {}
    with _file_lock:
        prefs = load_prefs()
        entry = prefs.setdefault(uid, {})
        for key in ('theme', 'bg_color'):
            if key in data:
                entry[key] = data[key]
        save_prefs(prefs)
    return jsonify({'ok': True})

def aggregate_owner_stats():
    """Aggregate wins, losses, ties, and points_for per owner across all years."""
    owner_stats = {}
    for year_data in league_data.values():
        for team in year_data.get("teams", []):
            owner = team.get("owner", "Unknown")
            if owner not in owner_stats:
                owner_stats[owner] = {"wins": 0, "losses": 0, "ties": 0, "points_for": 0}
            owner_stats[owner]["wins"] += team.get("wins", 0)
            owner_stats[owner]["losses"] += team.get("losses", 0)
            owner_stats[owner]["ties"] += team.get("ties", 0)
            owner_stats[owner]["points_for"] += team.get("points_for", 0)
    return owner_stats

def calculate_overall_records(owner_stats=None):
    """Returns total wins, losses, and ties per owner across all years."""
    if owner_stats is None:
        owner_stats = aggregate_owner_stats()
    return {
        owner: {k: v for k, v in stats.items() if k != "points_for"}
        for owner, stats in owner_stats.items()
    }

def calculate_historical_head_to_head():
    """Calculates historical head-to-head records across all years."""
    all_owners = set()
    for year_data in league_data.values():
        for team in year_data.get("teams", []):
            all_owners.add(team.get("owner", "Unknown"))

    head_to_head = {
        owner: {opp: {"wins": 0, "losses": 0, "ties": 0} for opp in all_owners if opp != owner}
        for owner in all_owners
    }

    for year_data in league_data.values():
        for owner, matchups in year_data.get("head_to_head", {}).items():
            for opponent, record in matchups.items():
                if owner in head_to_head and opponent in head_to_head[owner]:
                    head_to_head[owner][opponent]["wins"] += record.get("wins", 0)
                    head_to_head[owner][opponent]["losses"] += record.get("losses", 0)
                    head_to_head[owner][opponent]["ties"] += record.get("ties", 0)
    return head_to_head

def season_is_complete(year_data):
    """Historical (ESPN) seasons have no status key and are always complete;
    Sleeper seasons carry status ('in_season', 'complete', ...)."""
    return year_data.get("status", "complete") == "complete"


def get_champions():
    """Gets the champion for each completed year (rank 1 team)."""
    champions = []
    for year in sorted(league_data.keys(), reverse=True):
        if not season_is_complete(league_data[year]):
            continue
        for team in league_data[year]['teams']:
            if team['rank'] == 1:
                champions.append({'year': year, 'team': team['name'], 'owner': team['owner']})
                break
    return champions

def get_power_rankings(owner_stats=None):
    """Calculates power rankings based on win percentage and total points scored."""
    if owner_stats is None:
        owner_stats = aggregate_owner_stats()

    # Calculate win percentage for each owner
    for stats in owner_stats.values():
        total_games = stats["wins"] + stats["losses"] + stats["ties"]
        stats["win_percentage"] = stats["wins"] / total_games if total_games > 0 else 0

    # Define active owners (owners with teams in the most recent year)
    latest_year = max(league_data.keys(), key=int)
    active_owners = {team["owner"] for team in league_data[latest_year].get("teams", [])}

    # Calculate championship counts (completed seasons only)
    championship_counts = {}
    for year_data in league_data.values():
        if not season_is_complete(year_data):
            continue
        for team in year_data.get("teams", []):
            if team["rank"] == 1:
                owner = team["owner"]
                championship_counts[owner] = championship_counts.get(owner, 0) + 1

    def build_rankings(owners_subset):
        ranked = sorted(
            [(owner, stats) for owner, stats in owner_stats.items() if owner in owners_subset],
            key=lambda x: x[1]["win_percentage"],
            reverse=True
        )
        return [
            {
                "rank": i + 1,
                "owner": owner,
                "win_percentage": round(stats["win_percentage"] * 100, 2),
                "championships": championship_counts.get(owner, 0)
            }
            for i, (owner, stats) in enumerate(ranked)
        ]

    # Per-season scoring is only meaningful over *finished* seasons. Counting a
    # season that is two weeks old as a whole one divides a career total by one
    # season too many, which quietly penalises every owner still playing while
    # leaving retired owners' averages correct — the exact opposite of what this
    # column is for. So the divisor, and the points it divides, are both taken
    # from completed seasons only.
    season_counts = {}
    completed_points = {}
    for year, year_data in league_data.items():
        if not season_is_complete(year_data):
            continue
        for team in year_data.get("teams", []):
            owner = team.get("owner", "Unknown")
            season_counts[owner] = season_counts.get(owner, 0) + 1
            completed_points[owner] = (completed_points.get(owner, 0.0)
                                       + team.get("points_for", 0))

    def per_season(owner):
        n = season_counts.get(owner, 0)
        return round(completed_points.get(owner, 0.0) / n, 1) if n else None

    points_scored_data = [
        {
            "rank": i + 1,
            "owner": owner,
            "points_for": round(stats["points_for"], 1),
            "seasons": season_counts.get(owner, 0),
            "pts_per_season": per_season(owner),
        }
        for i, (owner, stats) in enumerate(
            sorted(owner_stats.items(), key=lambda x: x[1]["points_for"], reverse=True)
        )
    ]

    return {
        "active": build_rankings(active_owners),
        "retired": build_rankings(set(owner_stats) - active_owners),
        "points_scored": points_scored_data
    }

def calculate_scoring_records():
    """Calculate highest score, lowest score, and biggest blowout across all weeks."""
    highest_score = {"team": None, "score": 0, "year": None, "week": None}
    lowest_score = {"team": None, "score": float('inf'), "year": None, "week": None}
    biggest_blowout = {"teams": None, "margin": 0, "year": None, "week": None}

    for year, year_data in league_data.items():
        for week, matchups in year_data.get("weekly_scores", {}).items():
            for matchup in matchups:
                home_team = matchup.get("home_team")
                away_team = matchup.get("away_team")
                home_score = matchup.get("home_score", 0)
                away_score = matchup.get("away_score", 0)

                if home_score > highest_score["score"]:
                    highest_score = {"team": home_team, "score": home_score, "year": year, "week": week}
                if away_score > highest_score["score"]:
                    highest_score = {"team": away_team, "score": away_score, "year": year, "week": week}

                if home_score > 0 and home_score < lowest_score["score"]:
                    lowest_score = {"team": home_team, "score": home_score, "year": year, "week": week}
                if away_score > 0 and away_score < lowest_score["score"]:
                    lowest_score = {"team": away_team, "score": away_score, "year": year, "week": week}

                margin = abs(home_score - away_score)
                if margin > biggest_blowout["margin"]:
                    biggest_blowout = {"teams": f"{home_team} vs {away_team}", "margin": margin, "year": year, "week": week}

    return {
        "highest_score": highest_score,
        "lowest_score": lowest_score,
        "biggest_blowout": biggest_blowout
    }

def calculate_playoff_stats():
    """
    Playoff W/L record, appearances, byes, and points for active owners.

    Two eras:
      2017 and earlier (4-team, no byes):
        ESPN stores 2 complete rounds — round 0 = semifinals, round 1 = championship + 3rd place.
        All games count. No pairing needed; ESPN already returns full-round results.
      2018 and later (6-team, seeds 1-2 have byes):
        Single-week rounds. 5/6 consolation (round-0 losers) excluded.

    Appearances are counted from team rankings for ALL years (even where bracket
    data is missing), since ESPN playoff data is unavailable for 2018-2024.
    W/L and points only reflect years with stored bracket data.
    """
    latest_year   = max(league_data.keys(), key=int)
    active_owners = {t["owner"] for t in league_data[latest_year].get("teams", [])}

    champ_counts = {}
    for year_data in league_data.values():
        if not season_is_complete(year_data):
            continue
        for team in year_data.get("teams", []):
            if team.get("rank") == 1:
                owner = team["owner"]
                champ_counts[owner] = champ_counts.get(owner, 0) + 1

    stats          = {}   # owner -> {wins, losses, points_for}
    years_appeared = {}   # owner -> set of years with a playoff appearance
    bye_counts     = {}   # owner -> number of byes (6-team era only)

    # ── Count appearances from team rankings for every completed year ────────
    # (bracket data is missing for ESPN 2018-2024 so we can't rely on games alone;
    # in-progress seasons are skipped — a top-6 record in week 3 isn't an appearance)
    for year, year_data in league_data.items():
        if not season_is_complete(year_data):
            continue
        playoff_size = 4 if int(year) <= 2017 else 6
        for team in year_data.get("teams", []):
            if team.get("rank", 999) <= playoff_size:
                owner = team["owner"]
                years_appeared.setdefault(owner, set()).add(year)
                stats.setdefault(owner, {"wins": 0, "losses": 0, "points_for": 0.0})

    # ── Process bracket data where available ─────────────────────────────────
    for year, year_data in league_data.items():
        playoffs = year_data.get("playoffs", [])
        if not playoffs:
            continue

        team_to_owner = {t["name"]: t["owner"] for t in year_data.get("teams", [])}
        team_to_rank  = {t["name"]: t["rank"]  for t in year_data.get("teams", [])}
        is_4team_era  = int(year) <= 2017

        def record_game(o1, o2, s1, s2):
            # Seed on demand. The appearance loop above only seeds owners from
            # *completed* seasons, but bracket data exists as soon as a playoff
            # game is scored — so during a live postseason these owners have no
            # entry yet and every += below would raise KeyError.
            for o in (o1, o2):
                stats.setdefault(o, {"wins": 0, "losses": 0, "points_for": 0.0})
            if s1 > s2:
                stats[o1]["wins"]   += 1
                stats[o2]["losses"] += 1
            elif s2 > s1:
                stats[o2]["wins"]   += 1
                stats[o1]["losses"] += 1
            stats[o1]["points_for"] += s1
            stats[o2]["points_for"] += s2

        if is_4team_era:
            # 2 stored rounds: round 0 = semis, round 1 = championship + 3rd place.
            # Each stored matchup is already a complete result — no week pairing needed.
            for rnd in playoffs:
                for m in rnd.get("matchups", []):
                    t1, t2 = m.get("team1"), m.get("team2")
                    s1, s2 = m.get("score1"), m.get("score2")
                    if not t1 or not t2 or s1 is None or s2 is None:
                        continue
                    o1 = team_to_owner.get(t1)
                    o2 = team_to_owner.get(t2)
                    if o1 and o2:
                        record_game(o1, o2, float(s1), float(s2))

        else:
            # 6-team era: round 0 = first round (seeds 3-6 play, seeds 1-2 bye),
            # subsequent rounds include semis + consolation mixed in.
            # Skip any game where either owner already lost in round 0 (consolation).
            playoff_teams     = {name for name, rank in team_to_rank.items() if rank <= 6}
            eliminated_owners = set()
            round0_players    = set()

            for rnd_idx, rnd in enumerate(playoffs):
                for m in rnd.get("matchups", []):
                    t1, t2 = m.get("team1"), m.get("team2")
                    if not t1 or not t2:
                        continue
                    if t1 not in playoff_teams or t2 not in playoff_teams:
                        continue
                    o1 = team_to_owner.get(t1)
                    o2 = team_to_owner.get(t2)
                    if not o1 or not o2:
                        continue
                    if o1 in eliminated_owners or o2 in eliminated_owners:
                        continue
                    s1, s2 = m.get("score1"), m.get("score2")
                    if s1 is None or s2 is None:
                        continue
                    s1, s2 = float(s1), float(s2)

                    record_game(o1, o2, s1, s2)

                    if rnd_idx == 0:
                        round0_players.update([o1, o2])
                        if s1 > s2:
                            eliminated_owners.add(o2)
                        elif s2 > s1:
                            eliminated_owners.add(o1)

            # Playoff teams that didn't play in round 0 had a bye (seeds 1 and 2)
            for team, rank in team_to_rank.items():
                if rank <= 6:
                    owner = team_to_owner.get(team)
                    if owner and owner not in round0_players:
                        bye_counts[owner] = bye_counts.get(owner, 0) + 1

    result = []
    for owner, s in stats.items():
        if owner not in active_owners:
            continue
        total = s["wins"] + s["losses"]
        result.append({
            "owner":         owner,
            "appearances":   len(years_appeared.get(owner, set())),
            "wins":          s["wins"],
            "losses":        s["losses"],
            "win_pct":       round(s["wins"] / total * 100, 1) if total > 0 else 0.0,
            "championships": champ_counts.get(owner, 0),
            "byes":          bye_counts.get(owner, 0),
            "points_for":    round(s["points_for"], 1),
        })
    return sorted(result, key=lambda x: (x["championships"], x["wins"], x["win_pct"]), reverse=True)


def calculate_season_scoring_extremes():
    """Highest and lowest full-season point totals of all time.

    Completed seasons only. A season in progress has a partial total by
    definition, so including it would let week 1 of the current year take the
    all-time *lowest season* record away from a team that really did score
    that little over a full year.
    """
    highest = {"team": None, "score": 0, "year": None}
    lowest = {"team": None, "score": float('inf'), "year": None}

    for year, year_data in league_data.items():
        if not season_is_complete(year_data):
            continue
        for team in year_data.get("teams", []):
            pts = team.get("points_for", 0)
            if pts > highest["score"]:
                highest = {"team": team["name"], "score": pts, "year": year}
            if pts > 0 and pts < lowest["score"]:
                lowest = {"team": team["name"], "score": pts, "year": year}

    return highest, lowest




def calculate_top_bottom_seasons(n=10):
    """Top N and bottom N full-season point totals across all completed years.

    In-progress seasons are excluded for the same reason as in
    calculate_season_scoring_extremes: a partial total isn't a season total,
    and every team in the current year would otherwise flood the bottom list
    until about midseason.
    """
    seasons = []
    for year, year_data in league_data.items():
        if not season_is_complete(year_data):
            continue
        for team in year_data.get("teams", []):
            pts = team.get("points_for", 0)
            if not pts:
                continue
            seasons.append({
                "team":       team["name"],
                "owner":      team["owner"],
                "year":       year,
                "points_for": round(pts, 2),
                "wins":       team.get("wins", 0),
                "losses":     team.get("losses", 0),
                "ties":       team.get("ties", 0),
                "rank":       team.get("rank"),
                "total_teams": len(year_data.get("teams", [])),
            })
    seasons.sort(key=lambda x: x["points_for"], reverse=True)
    return {"top": seasons[:n], "bottom": list(reversed(seasons[-n:]))}


def week_luck_verdicts(matchups):
    """Yield (team_name, verdict) for every team that actually played this week.

    verdict is "lucky" (won from the bottom half of the week's scores),
    "unlucky" (lost from the top half) or None.

    The all-time luck index and the per-season owner page both need this, and
    they used to carry separate copies of it. The copies drifted: one skipped
    scheduled-but-unplayed 0-0 games and the other scored them as a lucky win
    for the away team. One definition now, so they can't disagree again.
    """
    all_scores = []
    for m in matchups:
        hs = float(m.get("home_score") or 0)
        as_ = float(m.get("away_score") or 0)
        if hs or as_:
            all_scores.append((m["home_team"], hs))
            all_scores.append((m["away_team"], as_))

    n = len(all_scores)
    if n < 2:
        return

    sorted_desc = sorted(all_scores, key=lambda x: x[1], reverse=True)
    top_half_teams = {t for t, _ in sorted_desc[: n // 2]}

    for m in matchups:
        hs = float(m.get("home_score") or 0)
        as_ = float(m.get("away_score") or 0)
        if not hs and not as_:
            continue          # scheduled but unplayed — nobody got lucky
        # A tie counts as an away win, which is what both copies always did.
        home_won = hs > as_
        for team, won in ((m["home_team"], home_won), (m["away_team"], not home_won)):
            if team in top_half_teams:
                yield team, (None if won else "unlucky")
            else:
                yield team, ("lucky" if won else None)


def calculate_luck_index():
    """
    Lucky win: scored in bottom half of league that week, but won.
    Unlucky loss: scored in top half of league that week, but lost.
    Returns dict of owner -> {lucky_wins, unlucky_losses, net_luck}.
    """
    luck = {}
    for year, year_data in league_data.items():
        weekly_scores = year_data.get("weekly_scores", {})
        reg_weeks = get_regular_season_weeks(year)
        team_to_owner = {t["name"]: t["owner"] for t in year_data.get("teams", [])}

        for week_str, matchups in weekly_scores.items():
            if int(week_str) > reg_weeks or not matchups:
                continue

            for team, verdict in week_luck_verdicts(matchups):
                owner = team_to_owner.get(team, team)
                rec = luck.setdefault(owner, {"lucky_wins": 0, "unlucky_losses": 0})
                if verdict == "lucky":
                    rec["lucky_wins"] += 1
                elif verdict == "unlucky":
                    rec["unlucky_losses"] += 1

    for o in luck:
        luck[o]["net_luck"] = luck[o]["lucky_wins"] - luck[o]["unlucky_losses"]

    return luck


def calculate_weekly_high_scores(n=25):
    """Top N single-week scores across all regular seasons."""
    scores = []
    for year, year_data in league_data.items():
        reg_weeks = get_regular_season_weeks(year)
        team_to_owner = {t["name"]: t["owner"] for t in year_data.get("teams", [])}
        for week_str, matchups in year_data.get("weekly_scores", {}).items():
            if int(week_str) > reg_weeks:
                continue
            for m in matchups:
                for team, score in [(m["home_team"], m.get("home_score")), (m["away_team"], m.get("away_score"))]:
                    s = float(score or 0)
                    if s > 0:
                        scores.append({
                            "team": team,
                            "owner": team_to_owner.get(team, team),
                            "score": s,
                            "week": week_str,
                            "year": year,
                        })
    return sorted(scores, key=lambda x: x["score"], reverse=True)[:n]


def calculate_closest_games(n=25):
    """Top N closest margin games across all regular seasons."""
    games = []
    for year, year_data in league_data.items():
        reg_weeks = get_regular_season_weeks(year)
        for week_str, matchups in year_data.get("weekly_scores", {}).items():
            if int(week_str) > reg_weeks:
                continue
            for m in matchups:
                hs = float(m.get("home_score") or 0)
                as_ = float(m.get("away_score") or 0)
                if not hs and not as_:
                    continue
                games.append({
                    "home_team": m["home_team"],
                    "away_team": m["away_team"],
                    "home_score": hs,
                    "away_score": as_,
                    "margin": round(abs(hs - as_), 2),
                    "week": week_str,
                    "year": year,
                })
    return sorted(games, key=lambda x: x["margin"])[:n]


def calculate_most_points_in_loss(n=25):
    """Top N highest scores that still resulted in a loss."""
    losses = []
    for year, year_data in league_data.items():
        reg_weeks = get_regular_season_weeks(year)
        team_to_owner = {t["name"]: t["owner"] for t in year_data.get("teams", [])}
        for week_str, matchups in year_data.get("weekly_scores", {}).items():
            if int(week_str) > reg_weeks:
                continue
            for m in matchups:
                hs = float(m.get("home_score") or 0)
                as_ = float(m.get("away_score") or 0)
                if not hs and not as_:
                    continue
                if hs < as_:
                    losses.append({
                        "team": m["home_team"],
                        "owner": team_to_owner.get(m["home_team"], m["home_team"]),
                        "score": hs,
                        "opponent": m["away_team"],
                        "opponent_score": as_,
                        "week": week_str,
                        "year": year,
                    })
                elif as_ < hs:
                    losses.append({
                        "team": m["away_team"],
                        "owner": team_to_owner.get(m["away_team"], m["away_team"]),
                        "score": as_,
                        "opponent": m["home_team"],
                        "opponent_score": hs,
                        "week": week_str,
                        "year": year,
                    })
    return sorted(losses, key=lambda x: x["score"], reverse=True)[:n]


def calculate_streaks():
    """Longest all-time win and loss streaks per owner (chronological across all seasons)."""
    owner_results = {}
    for year in sorted(league_data.keys()):
        year_data = league_data[year]
        weekly_scores = year_data.get("weekly_scores", {})
        team_to_owner = {t["name"]: t["owner"] for t in year_data.get("teams", [])}
        reg_weeks = get_regular_season_weeks(year)
        for week in range(1, reg_weeks + 1):
            for m in weekly_scores.get(str(week), []):
                hs = float(m.get("home_score") or 0)
                as_ = float(m.get("away_score") or 0)
                if not hs and not as_:
                    continue
                ho = team_to_owner.get(m["home_team"], m["home_team"])
                ao = team_to_owner.get(m["away_team"], m["away_team"])
                for o in (ho, ao):
                    owner_results.setdefault(o, [])
                if hs > as_:
                    owner_results[ho].append("W")
                    owner_results[ao].append("L")
                elif as_ > hs:
                    owner_results[ao].append("W")
                    owner_results[ho].append("L")
                else:
                    owner_results[ho].append("T")
                    owner_results[ao].append("T")

    streaks = {}
    for owner, results in owner_results.items():
        max_w = max_l = max_t = cur_w = cur_l = cur_t = 0
        for r in results:
            if r == "W":
                cur_w += 1; cur_l = 0; cur_t = 0
                max_w = max(max_w, cur_w)
            elif r == "L":
                cur_l += 1; cur_w = 0; cur_t = 0
                max_l = max(max_l, cur_l)
            else:
                cur_t += 1; cur_w = 0; cur_l = 0
                max_t = max(max_t, cur_t)
        streaks[owner] = {"win_streak": max_w, "loss_streak": max_l, "tie_streak": max_t}
    return streaks


def _regular_season_games():
    """Every regular-season matchup ever played, flattened.

    Ten years of history live in two different shapes — ESPN seasons carry only
    scores, Sleeper seasons carry rosters too — but `weekly_scores` is common to
    both, so every all-time record below is built from this one pass.

    Yields dicts with both sides resolved to owners, so callers never have to
    redo the team-name lookup.
    """
    for year in sorted(league_data.keys()):
        year_data = league_data[year]
        reg_weeks = get_regular_season_weeks(year)
        team_to_owner = {t["name"]: t["owner"] for t in year_data.get("teams", [])}
        for week_str, matchups in year_data.get("weekly_scores", {}).items():
            if int(week_str) > reg_weeks:
                continue
            for m in matchups:
                hs = float(m.get("home_score") or 0)
                as_ = float(m.get("away_score") or 0)
                if not hs and not as_:
                    continue
                yield {
                    "year": year, "week": week_str,
                    "home_team": m["home_team"], "away_team": m["away_team"],
                    "home_owner": team_to_owner.get(m["home_team"], m["home_team"]),
                    "away_owner": team_to_owner.get(m["away_team"], m["away_team"]),
                    "home_score": hs, "away_score": as_,
                    "margin": round(abs(hs - as_), 2),
                    "combined": round(hs + as_, 2),
                }


def latest_played_year():
    """The most recent season with a regular-season game actually played.

    Not the same as max(league_data): Sleeper publishes next season's teams and
    schedule weeks before week 1, so for a stretch every summer the newest year
    exists with an empty record. Headers that describe the range of games on a
    page have to use this, or they name a season nobody has played yet.
    """
    played = {g["year"] for g in _regular_season_games()}
    return max(played, key=int) if played else max(league_data, key=int)


def calculate_biggest_blowouts(n=25):
    """Largest margins of victory in a regular-season game."""
    games = sorted(_regular_season_games(), key=lambda g: -g["margin"])[:n]
    for g in games:
        home_won = g["home_score"] > g["away_score"]
        g["winner"], g["winner_score"] = ((g["home_owner"], g["home_score"]) if home_won
                                          else (g["away_owner"], g["away_score"]))
        g["loser"], g["loser_score"] = ((g["away_owner"], g["away_score"]) if home_won
                                        else (g["home_owner"], g["home_score"]))
    return games


def calculate_highest_combined(n=25):
    """Shootouts — the most total points ever put up in one matchup."""
    return sorted(_regular_season_games(), key=lambda g: -g["combined"])[:n]


def calculate_weekly_low_scores(n=25):
    """The other end of the leaderboard: worst single-week scores.

    Zeroes are skipped by _regular_season_games only when *both* sides are zero,
    so a real team that genuinely scored nothing would still show up here — which
    is correct, that's a record.
    """
    scores = []
    for g in _regular_season_games():
        for side in ("home", "away"):
            s = g[side + "_score"]
            if s > 0:
                scores.append({"team": g[side + "_team"], "owner": g[side + "_owner"],
                               "score": s, "week": g["week"], "year": g["year"]})
    return sorted(scores, key=lambda x: x["score"])[:n]


def calculate_season_roll():
    """One row per completed season: who won it, who led scoring, who finished last.

    Finishing places come from the stored `rank`, which is the final standing
    after playoffs. The regular-season leader is computed separately from wins
    and points, because the best record and the trophy are often not the same
    person — which is the whole point of showing both.
    """
    roll = []
    for year in sorted(league_data.keys(), reverse=True):
        year_data = league_data[year]
        if not season_is_complete(year_data):
            continue
        teams = [t for t in year_data.get("teams", []) if t.get("owner")]
        if not teams:
            continue

        by_rank = {t.get("rank"): t for t in teams}
        best_record = max(teams, key=lambda t: (t.get("wins", 0), t.get("points_for", 0)))
        points_leader = max(teams, key=lambda t: t.get("points_for", 0))
        last = max(teams, key=lambda t: t.get("rank") or 0)

        roll.append({
            "year": year,
            "teams": len(teams),
            "champion":  by_rank.get(1),
            "runner_up": by_rank.get(2),
            "third":     by_rank.get(3),
            "best_record": best_record,
            "points_leader": points_leader,
            "last": last,
            "total_points": round(sum(t.get("points_for", 0) for t in teams), 1),
        })
    return roll


def calculate_owner_ledger():
    """All-time career line for every owner who has ever been in the league.

    Retired owners are included on purpose — this is the history page, and the
    league's story is incomplete without the people who left.
    """
    ledger = {}

    def entry(owner):
        return ledger.setdefault(owner, {
            "owner": owner, "seasons": set(), "games": 0,
            "wins": 0, "losses": 0, "ties": 0, "points_for": 0.0,
            "best_week": None, "worst_week": None,
            "titles": [], "playoffs": 0, "last_place": 0,
        })

    # ── Game-by-game: record, points, and career-best/worst single weeks ──────
    for g in _regular_season_games():
        for side, other in (("home", "away"), ("away", "home")):
            e = entry(g[side + "_owner"])
            mine, theirs = g[side + "_score"], g[other + "_score"]
            e["games"] += 1
            e["points_for"] += mine
            if mine > theirs:
                e["wins"] += 1
            elif theirs > mine:
                e["losses"] += 1
            else:
                e["ties"] += 1
            week = {"score": mine, "year": g["year"], "week": g["week"]}
            if not e["best_week"] or mine > e["best_week"]["score"]:
                e["best_week"] = week
            if not e["worst_week"] or mine < e["worst_week"]["score"]:
                e["worst_week"] = week

    # ── Season-by-season: finishes. Only completed years count, so an owner
    #    sitting in 12th place in week 2 isn't credited with a last-place finish.
    for year, year_data in league_data.items():
        teams = [t for t in year_data.get("teams", []) if t.get("owner")]
        # A season counts toward "years managed" the moment you have a team, but
        # its finishes only count once it's over — otherwise week 2 of a live
        # season would hand out titles and last-place finishes.
        for t in teams:
            entry(t["owner"])["seasons"].add(year)
        if not season_is_complete(year_data):
            continue
        playoff_size = 4 if int(year) <= 2017 else 6
        for t in teams:
            e = entry(t["owner"])
            rank = t.get("rank") or 999
            if rank == 1:
                e["titles"].append(year)
            if rank <= playoff_size:
                e["playoffs"] += 1
            if rank == len(teams):
                e["last_place"] += 1

    rows = []
    for e in ledger.values():
        decided = e["wins"] + e["losses"] + e["ties"]
        years = sorted(e["seasons"])
        rows.append({**e,
                     "seasons": len(years),
                     "first_year": years[0] if years else None,
                     "last_year": years[-1] if years else None,
                     "points_for": round(e["points_for"], 1),
                     "ppg": round(e["points_for"] / e["games"], 2) if e["games"] else 0,
                     "win_pct": round((e["wins"] + e["ties"] * 0.5) / decided * 100, 1)
                                if decided else 0})
    rows.sort(key=lambda r: (-len(r["titles"]), -r["win_pct"]))

    active = {t["owner"] for t in
              league_data[max(league_data.keys(), key=int)].get("teams", [])}
    for r in rows:
        r["active"] = r["owner"] in active
    return rows


def get_owner_transaction_stats(owner_name):
    """Aggregate transaction activity (trades, waiver/FA adds) per owner."""
    stats_by_year = {}
    for year, year_data in league_data.items():
        txns = year_data.get("transactions", [])
        if not txns:
            continue
        ys = {"trades": 0, "waiver_adds": 0, "fa_adds": 0}
        for t in txns:
            ttype = t.get("type", "")
            adds  = t.get("adds",  {})
            drops = t.get("drops", {})
            in_adds  = owner_name in adds
            in_drops = owner_name in drops
            if ttype == "trade" and (in_adds or in_drops):
                ys["trades"] += 1
            elif ttype == "waiver" and in_adds:
                ys["waiver_adds"] += len(adds[owner_name])
            elif ttype == "free_agent" and in_adds:
                ys["fa_adds"] += len(adds[owner_name])
        if any(ys.values()):
            stats_by_year[year] = ys

    if not stats_by_year:
        return None

    n = len(stats_by_year)
    tot_trades = sum(s["trades"]      for s in stats_by_year.values())
    tot_waiver = sum(s["waiver_adds"] for s in stats_by_year.values())
    tot_fa     = sum(s["fa_adds"]     for s in stats_by_year.values())
    return {
        "by_year":             stats_by_year,
        "total_trades":        tot_trades,
        "total_waiver_adds":   tot_waiver,
        "total_fa_adds":       tot_fa,
        "avg_trades_per_year": round(tot_trades / n, 1),
        "avg_waiver_per_year": round(tot_waiver / n, 1),
        "avg_fa_per_year":     round(tot_fa     / n, 1),
        "seasons_with_data":   n,
    }


def get_owner_profile(owner_name):
    """Compile per-season and career stats for a single owner."""
    seasons = []
    for year in sorted(league_data.keys()):
        year_data = league_data[year]
        for team in year_data.get("teams", []):
            if team["owner"] == owner_name:
                seasons.append({
                    "year": year,
                    "team_name": team["name"],
                    "wins": team["wins"],
                    "losses": team["losses"],
                    "ties": team["ties"],
                    "points_for": team["points_for"],
                    "points_against": team["points_against"],
                    "rank": team["rank"],
                    "total_teams": len(year_data.get("teams", [])),
                    "in_progress": not season_is_complete(year_data),
                })
                break

    if not seasons:
        return None

    total_wins = sum(s["wins"] for s in seasons)
    total_losses = sum(s["losses"] for s in seasons)
    total_ties = sum(s["ties"] for s in seasons)
    total_games = total_wins + total_losses + total_ties
    win_pct = round(total_wins / total_games * 100, 2) if total_games > 0 else 0

    # Championships and best/worst finishes only count completed seasons —
    # an in-progress season's rank is just the current standings
    completed = [s for s in seasons if not s["in_progress"]] or seasons
    championships = [s["year"] for s in completed if s["rank"] == 1 and not s["in_progress"]]
    team_names = list(dict.fromkeys(s["team_name"] for s in seasons))

    best_season = min(completed, key=lambda s: s["rank"])
    worst_season = max(completed, key=lambda s: s["rank"])

    h2h_all = calculate_historical_head_to_head()
    h2h_sorted = sorted(
        (
            (opp, rec)
            for opp, rec in h2h_all.get(owner_name, {}).items()
            if rec["wins"] + rec["losses"] + rec["ties"] > 0
        ),
        key=lambda x: x[1]["wins"] + x[1]["losses"] + x[1]["ties"],
        reverse=True,
    )

    return {
        "owner": owner_name,
        "seasons": seasons,
        "championships": championships,
        "team_names": team_names,
        "best_season": best_season,
        "worst_season": worst_season,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_ties": total_ties,
        "win_pct": win_pct,
        "h2h": h2h_sorted,
    }


# ── Live Sleeper data layer ───────────────────────────────────────────────────
# Small in-memory cache with stale-on-error: if Sleeper is unreachable we keep
# serving the last good copy rather than breaking the page.

_live_cache = {}
_cache_locks: dict = {}
_cache_locks_guard = Lock()


def _lock_for(key):
    with _cache_locks_guard:
        return _cache_locks.setdefault(key, Lock())


def _cached(key, ttl, fetch_fn):
    entry = _live_cache.get(key)
    if entry and time.time() - entry["ts"] < ttl:
        return entry["data"]

    # One fetch per key at a time. Without this every request arriving during a
    # slow miss starts its own copy of the work, and compute_playoff_odds() is a
    # 3,000-simulation run that takes seconds.
    #
    # The lock is per key and never global on purpose: that odds simulation
    # calls _cached() again for the identity map, league settings and schedule
    # while it holds its own key, so one shared lock would deadlock.
    with _lock_for(key):
        entry = _live_cache.get(key)      # may have been filled while we waited
        if entry and time.time() - entry["ts"] < ttl:
            return entry["data"]
        try:
            data = fetch_fn()
        except Exception:
            if entry:
                return entry["data"]      # stale beats broken
            raise
        _live_cache[key] = {"ts": time.time(), "data": data}
        return data


def _league_identity():
    """roster_id -> owner/team-name maps for the current Sleeper league."""
    users = safe_get(f"https://api.sleeper.app/v1/league/{SLEEPER_LEAGUE_ID}/users") or []
    rosters = safe_get(f"https://api.sleeper.app/v1/league/{SLEEPER_LEAGUE_ID}/rosters") or []
    users_by_id = {u["user_id"]: u for u in users}
    rid_owner, rid_team = {}, {}
    for r in rosters:
        rid = r["roster_id"]
        user = users_by_id.get(r.get("owner_id"), {})
        rid_owner[rid] = resolve_owner(user)
        rid_team[rid] = ((user.get("metadata") or {}).get("team_name")
                         or OWNER_TEAM_NAMES.get(rid_owner[rid])
                         or user.get("display_name")
                         or rid_owner[rid])
    return {"rid_owner": rid_owner, "rid_team": rid_team}


def get_live_scoreboard():
    """Current-week matchups with live points and lifetime owner-vs-owner
    records. Refreshes from Sleeper at most once a minute."""
    state = _cached("nfl_state", 300,
                    lambda: safe_get("https://api.sleeper.app/v1/state/nfl"))
    week = int(state.get("week") or 1)
    season_type = state.get("season_type", "regular")
    if season_type != "regular":
        return {"week": week, "season_type": season_type, "games": []}

    ident = _cached("identity", 3600, _league_identity)
    matchups = _cached(f"live_matchups_w{week}", 60,
                       lambda: safe_get(f"https://api.sleeper.app/v1/league/{SLEEPER_LEAGUE_ID}/matchups/{week}") or [])

    groups = {}
    for entry in matchups:
        mid = entry.get("matchup_id")
        if mid is not None:
            groups.setdefault(mid, []).append(entry)

    h2h = calculate_historical_head_to_head()
    games = []
    for mid in sorted(groups):
        pair = groups[mid]
        if len(pair) != 2:
            continue
        a, b = pair
        ao = ident["rid_owner"].get(a["roster_id"], "?")
        bo = ident["rid_owner"].get(b["roster_id"], "?")

        rec = h2h.get(ao, {}).get(bo, {"wins": 0, "losses": 0, "ties": 0})
        total = rec["wins"] + rec["losses"] + rec["ties"]
        if total == 0:
            h2h_line = "First ever meeting"
        elif rec["wins"] > rec["losses"]:
            h2h_line = f"{ao} leads {rec['wins']}–{rec['losses']}" + (f"–{rec['ties']}" if rec["ties"] else "")
        elif rec["losses"] > rec["wins"]:
            h2h_line = f"{bo} leads {rec['losses']}–{rec['wins']}" + (f"–{rec['ties']}" if rec["ties"] else "")
        else:
            h2h_line = f"All square at {rec['wins']}–{rec['losses']}"

        games.append({
            "team_a":  ident["rid_team"].get(a["roster_id"], "?"),
            "owner_a": ao,
            "pts_a":   round(float(a.get("points") or 0), 2),
            "team_b":  ident["rid_team"].get(b["roster_id"], "?"),
            "owner_b": bo,
            "pts_b":   round(float(b.get("points") or 0), 2),
            "h2h":     h2h_line,
        })
    return {"week": week, "season_type": season_type, "games": games}


# Long enough that filtering to a single position still leaves a useful list.
TREND_LIMIT = 100


def get_trends():
    """Sleeper-wide trending adds/drops (24h) tagged with availability in
    THIS league. Cached 15 minutes."""
    def fetch():
        # Tolerate a stale cache here: the background updater refreshes it
        # daily, and a web request should never trigger the 15MB download.
        players = get_player_names(max_age_hours=24 * 365)
        latest = max(league_data.keys(), key=int)
        owner_by_player = {}
        for t in league_data[latest].get("teams", []):
            for p in t.get("roster", []):
                owner_by_player[p["name"]] = t["owner"]

        out = {}
        for kind in ("add", "drop"):
            raw = safe_get(f"https://api.sleeper.app/v1/players/nfl/trending/{kind}"
                           f"?lookback_hours=24&limit={TREND_LIMIT}") or []
            rows = []
            for e in raw:
                pid = str(e.get("player_id"))
                info = players.get(pid) or {}
                name = info.get("name", pid)
                rows.append({
                    "name":      name,
                    "position":  info.get("position", "?"),
                    "team":      info.get("team"),
                    "count":     e.get("count", 0),
                    "owner":     owner_by_player.get(name),
                    "player_id": pid if info else None,
                })
            out[kind] = rows
        return out
    return _cached("trends", 900, fetch)


def _remaining_schedule(year_str, reg_weeks):
    """Future (unplayed) owner-vs-owner pairings, fetched from Sleeper and
    compared against the games already recorded in league_history.

    Only valid for the CURRENT season: the schedule comes from
    SLEEPER_LEAGUE_ID, so passing an older year compares this year's fixtures
    against that year's results and reports nonsense. Every caller runs on the
    latest season; keep it that way or pass the right league id through.
    """
    def fetch():
        ident = _cached("identity", 3600, _league_identity)
        schedule = {}
        for wk in range(1, reg_weeks + 1):
            raw = safe_get(f"https://api.sleeper.app/v1/league/{SLEEPER_LEAGUE_ID}/matchups/{wk}") or []
            groups = {}
            for e in raw:
                mid = e.get("matchup_id")
                if mid is not None:
                    groups.setdefault(mid, []).append(e)
            schedule[wk] = [
                (ident["rid_owner"].get(p[0]["roster_id"]), ident["rid_owner"].get(p[1]["roster_id"]))
                for p in groups.values() if len(p) == 2
            ]
        return schedule
    schedule = _cached("full_schedule", 21600, fetch)

    year_data = league_data[year_str]
    team_to_owner = {t["name"]: t["owner"] for t in year_data.get("teams", [])}
    played = set()
    for wk_str, ms in year_data.get("weekly_scores", {}).items():
        for m in ms:
            pair = frozenset((team_to_owner.get(m["home_team"], m["home_team"]),
                              team_to_owner.get(m["away_team"], m["away_team"])))
            played.add((int(wk_str), pair))

    remaining = []
    for wk, pairs in schedule.items():
        for a, b in pairs:
            if a and b and (wk, frozenset((a, b))) not in played:
                remaining.append((a, b))
    return remaining


def _league_settings():
    """Playoff shape straight from Sleeper, so nothing here hardcodes 'top 6'."""
    lg = safe_get(f"https://api.sleeper.app/v1/league/{SLEEPER_LEAGUE_ID}") or {}
    s = lg.get("settings") or {}
    return {"playoff_teams": int(s.get("playoff_teams") or 6),
            "playoff_week_start": int(s.get("playoff_week_start") or 15)}


def played_regular_weeks(year_str, reg_weeks):
    """How many regular-season weeks actually have scores on the board."""
    ws = league_data.get(year_str, {}).get("weekly_scores", {})
    n = 0
    for wk in range(1, reg_weeks + 1):
        games = ws.get(str(wk)) or []
        if any((m.get("home_score") or m.get("away_score")) for m in games):
            n += 1
    return n


def season_phase(year_str):
    """'preseason' | 'regular' | 'playoffs' | 'complete'.

    The playoff phase starts once every regular-season game is on the board —
    that's the moment the field stops being a question and the interesting
    number becomes where everyone finishes rather than who gets in.
    """
    year_data = league_data.get(year_str, {})
    if season_is_complete(year_data):
        return "complete"
    reg_weeks = get_regular_season_weeks(year_str)
    played = played_regular_weeks(year_str, reg_weeks)
    if played == 0:
        return "preseason"
    return "playoffs" if played >= reg_weeks else "regular"


# The league's 6-team bracket, expressed in seeds and keyed by Sleeper's own
# matchup numbers. Verified against the real 2025 bracket: round one is 3v6 and
# 4v5, seeds 1-2 have byes and each meets the winner of the *other* first-round
# game, and p=1/3/5 are the 1st, 3rd and 5th place games. The consolation
# bracket has the identical shape over seeds 7-12, six places lower.
_BRACKET = [
    # (matchup, side A, side B, place awarded to winner or None)
    (1, ("seed", 4), ("seed", 5), None),
    (2, ("seed", 3), ("seed", 6), None),
    (3, ("seed", 1), ("win", 1),  None),
    (4, ("seed", 2), ("win", 2),  None),
    (5, ("lose", 1), ("lose", 2), 5),
    (6, ("win", 3),  ("win", 4),  1),
    (7, ("lose", 3), ("lose", 4), 3),
]


def _simulate_bracket(seeded, mu, sigma, decided=None):
    """Play out one bracket. `seeded` is six owners in seed order.

    `decided` maps matchup number -> winning owner for games already played, so
    a half-finished playoff isn't re-rolled from scratch.

    Returns {owner: place 1..6}.
    """
    decided = decided or {}
    win, lose, places = {}, {}, {}

    def side(ref):
        kind, n = ref
        if kind == "seed":
            return seeded[n - 1]
        return win[n] if kind == "win" else lose[n]

    for m, a_ref, b_ref, place in _BRACKET:
        a, b = side(a_ref), side(b_ref)
        if a is None or b is None:
            continue
        winner = decided.get(m)
        if winner not in (a, b):
            winner = a if random.gauss(mu[a], sigma) > random.gauss(mu[b], sigma) else b
        loser = b if winner == a else a
        win[m], lose[m] = winner, loser
        if place:
            places[winner] = place
            places[loser] = place + 1
    return places


def _decided_bracket_games(kind):
    """{matchup number: winning owner} for playoff games Sleeper has settled."""
    def fetch():
        ident = _cached("identity", 3600, _league_identity)
        raw = safe_get(f"https://api.sleeper.app/v1/league/{SLEEPER_LEAGUE_ID}/{kind}") or []
        return {m["m"]: ident["rid_owner"].get(m["w"])
                for m in raw if m.get("m") and m.get("w")}
    try:
        return _cached(f"bracket:{kind}", 900, fetch)
    except Exception:
        return {}


def _league_scoring_baseline(latest):
    """Average team score per game, preferring the most recent finished season
    so week-zero odds aren't anchored to a hardcoded guess."""
    for y in sorted((k for k in league_data if k.isdigit()), key=int, reverse=True):
        scores = []
        for ms in league_data[y].get("weekly_scores", {}).values():
            for m in ms:
                for pts in (m.get("home_score"), m.get("away_score")):
                    if pts:
                        scores.append(float(pts))
        if len(scores) >= 24:          # at least a full week of real games
            return sum(scores) / len(scores)
    return 110.0


def compute_playoff_odds(n_sims=3000):
    """Monte Carlo the rest of the regular season.

    Before anyone has played, every team's odds should be the same 50% unless
    we know something about the rosters — so the prior is Rotowire's projected
    weekly output for each team's best lineup (see projections.py) rather than
    a flat league average. Actual scores then take over as they accumulate.
    Cached 30 minutes."""
    def sim():
        latest = max(league_data.keys(), key=int)
        year_data = league_data[latest]
        reg_weeks = get_regular_season_weeks(latest)
        teams = year_data.get("teams", [])
        owners = [t["owner"] for t in teams]
        base_wins = {t["owner"]: t["wins"] for t in teams}
        base_pf = {t["owner"]: t["points_for"] for t in teams}

        team_to_owner = {t["name"]: t["owner"] for t in teams}
        scores = {o: [] for o in owners}
        for ms in year_data.get("weekly_scores", {}).values():
            for m in ms:
                for tname, pts in ((m["home_team"], m["home_score"]),
                                   (m["away_team"], m["away_score"])):
                    o = team_to_owner.get(tname)
                    if o is not None and pts:
                        scores[o].append(float(pts))

        all_scores = [s for lst in scores.values() for s in lst]
        league_mean = (sum(all_scores) / len(all_scores) if all_scores
                       else _league_scoring_baseline(latest))

        # Roster ratings are centred on 100, so scaling by the league's real
        # scoring average turns them into points-per-week priors on the same
        # footing as actual results.
        ratings = projections.roster_ratings(teams, latest, _all_players())
        prior = {o: league_mean * ratings[o]["rating"] / 100
                    if o in ratings else league_mean
                 for o in owners}

        K, SIGMA = 4, 25.0
        mu = {o: (sum(scores[o]) + K * prior[o]) / (len(scores[o]) + K) for o in owners}

        phase = season_phase(latest)
        settings = _cached("league_settings", 3600, _league_settings)
        n_playoff = settings["playoff_teams"]
        remaining = _remaining_schedule(latest, reg_weeks) if phase != "playoffs" else []

        # Once the field is set, honour the playoff games Sleeper has already
        # settled instead of re-rolling them every simulation.
        decided_w = _decided_bracket_games("winners_bracket") if phase == "playoffs" else {}
        decided_l = _decided_bracket_games("losers_bracket") if phase == "playoffs" else {}

        playoff_n = {o: 0 for o in owners}
        last_n = {o: 0 for o in owners}
        finish_sum = {o: 0 for o in owners}
        title_n = {o: 0 for o in owners}
        top3_n = {o: 0 for o in owners}
        for _ in range(n_sims):
            w = dict(base_wins)
            pf = dict(base_pf)
            for a, b in remaining:
                sa = random.gauss(mu[a], SIGMA)
                sb = random.gauss(mu[b], SIGMA)
                if sa > sb:
                    w[a] += 1
                else:
                    w[b] += 1
                pf[a] += sa
                pf[b] += sb
            seeds = sorted(owners, key=lambda o: (w[o], pf[o]), reverse=True)

            # Play the bracket out: the top seeds for the title, everyone else
            # in the consolation bracket six places below.
            places = {}
            if len(seeds) >= 2 * n_playoff:
                places.update(_simulate_bracket(seeds[:n_playoff], mu, SIGMA, decided_w))
                for o, p in _simulate_bracket(seeds[n_playoff:2 * n_playoff],
                                              mu, SIGMA, decided_l).items():
                    places[o] = p + n_playoff
            # Anyone the bracket doesn't place keeps their seeding order.
            ranked = sorted(seeds, key=lambda o: places.get(o, seeds.index(o) + 1))

            for i, o in enumerate(ranked):
                finish_sum[o] += places.get(o, i + 1)
            for o in seeds[:n_playoff]:
                playoff_n[o] += 1
            champ = next((o for o in owners if places.get(o) == 1), None)
            if champ:
                title_n[champ] += 1
            for o in owners:
                if places.get(o, 99) <= 3:
                    top3_n[o] += 1
            last_n[ranked[-1]] += 1

        played = played_regular_weeks(latest, reg_weeks)
        rows = [{
            "owner":      o,
            "wins":       base_wins[o],
            "losses":     next(t["losses"] for t in teams if t["owner"] == o),
            "playoff_pct": round(playoff_n[o] / n_sims * 100, 1),
            "title_pct":   round(title_n[o] / n_sims * 100, 1),
            "top3_pct":    round(top3_n[o] / n_sims * 100, 1),
            "last_pct":    round(last_n[o] / n_sims * 100, 1),
            "avg_finish":  round(finish_sum[o] / n_sims, 1),
            "rating":      ratings.get(o, {}).get("rating"),
            "proj_week":   ratings.get(o, {}).get("raw"),
            "starters":    ratings.get(o, {}).get("starters"),
            # A team that makes it in every single simulation has clinched, and
            # one that misses in all of them is out. Only meaningful once games
            # remain to be played — before week 1 everything is still possible.
            "clinched":    played > 0 and playoff_n[o] == n_sims,
            "eliminated":  played > 0 and playoff_n[o] == 0,
        } for o in owners]
        if phase == "playoffs":
            rows.sort(key=lambda r: (-r["title_pct"], r["avg_finish"]))
        else:
            rows.sort(key=lambda r: (-r["playoff_pct"], r["avg_finish"]))
        sources = consensus.active_sources(_consensus_board(latest))

        # How much of each team's mean is still the preseason prior rather than
        # its own results — this is what the blurb quotes, so it can't drift.
        prior_weight = round(K / (K + played) * 100) if played is not None else 100

        return {"year": latest, "rows": rows,
                "phase": phase,
                "weeks_played": played, "reg_weeks": reg_weeks,
                "weeks_left": max(0, reg_weeks - played),
                "games_left": len(remaining),
                "playoff_teams": n_playoff,
                "prior_weight": prior_weight,
                "playoffs_started": bool(decided_w or decided_l),
                "n_sims": n_sims, "has_ratings": bool(ratings),
                "sources": sources}
    return _cached("playoff_odds", 1800, sim)


def get_current_keepers(year_data):
    """owner -> set of player names that were KEPT in this season's draft and
    are STILL on that owner's roster. A traded/dropped keeper stays flagged in
    the draft history but loses the badge on live roster views."""
    rosters = {t["owner"]: {p["name"] for p in t.get("roster", [])}
               for t in year_data.get("teams", [])}
    kept = {}
    for p in year_data.get("draft", []):
        if p.get("keeper") and p["player"] in rosters.get(p["owner"], set()):
            kept.setdefault(p["owner"], set()).add(p["player"])
    return kept


@app.route('/')
def home():
    champions = {c["year"]: c for c in get_champions()}
    scoring_records = calculate_scoring_records()
    highest_scoring_team_all_time, lowest_scoring_team_all_time = calculate_season_scoring_extremes()

    # The newest season has no champion until it's over, and rendering it as a
    # row of em-dashes made the top of the home page look broken. Mark it so the
    # template can show where the season actually stands instead.
    live_year = next((y for y in league_data
                      if not season_is_complete(league_data[y])), None)
    live_status = None
    if live_year:
        reg = get_regular_season_weeks(live_year)
        played = played_regular_weeks(live_year, reg)
        leader = max(league_data[live_year].get("teams", []),
                     key=lambda t: (t.get("wins", 0), t.get("points_for", 0)),
                     default=None)
        live_status = {"year": live_year, "played": played, "reg_weeks": reg,
                       "leader": leader}

    return render_template("index.html",
                           years=sorted(league_data.keys(), reverse=True),
                           champions=champions,
                           live_year=live_year, live_status=live_status,
                           scoring_records=scoring_records,
                           highest_scoring_team_all_time=highest_scoring_team_all_time,
                           lowest_scoring_team_all_time=lowest_scoring_team_all_time)

@app.route('/year/<int:year>')
def year_view(year):
    year_data = league_data.get(str(year), {})
    teams = sorted(year_data.get("teams", []), key=lambda x: x.get("rank", float('inf')))
    weekly_scores = year_data.get("weekly_scores", {})

    # Weekly game log per team — only weeks that were actually played
    # (an in-progress season would otherwise show a wall of N/A rows)
    team_weekly_summaries = {}
    for team in teams:
        team_name = team["name"]
        weekly_summary = []

        for week in range(1, get_regular_season_weeks(year) + 1):
            for matchup in weekly_scores.get(str(week), []):
                if team_name == matchup["home_team"]:
                    team_score, opponent, opponent_score = matchup["home_score"], matchup["away_team"], matchup["away_score"]
                elif team_name == matchup["away_team"]:
                    team_score, opponent, opponent_score = matchup["away_score"], matchup["home_team"], matchup["home_score"]
                else:
                    continue

                if team_score > opponent_score:
                    outcome = "Win"
                elif team_score < opponent_score:
                    outcome = "Loss"
                else:
                    outcome = "Tie"

                weekly_summary.append({
                    "week": week,
                    "score": team_score,
                    "opponent": opponent,
                    "opponent_score": opponent_score,
                    "outcome": outcome
                })
                break

        team_weekly_summaries[team_name] = weekly_summary

    # Transaction counts per owner for the teams tab
    team_txn_counts = {t["owner"]: {"trades": 0, "waiver_adds": 0, "fa_adds": 0} for t in teams}
    for t in year_data.get("transactions", []):
        ttype    = t.get("type", "")
        adds     = t.get("adds",  {})
        drops    = t.get("drops", {})
        involved = set(list(adds.keys()) + list(drops.keys()))
        for owner in involved:
            if owner not in team_txn_counts:
                continue
            if ttype == "trade":
                team_txn_counts[owner]["trades"] += 1
            elif ttype == "waiver":
                team_txn_counts[owner]["waiver_adds"] += len(adds.get(owner, []))
            elif ttype == "free_agent":
                team_txn_counts[owner]["fa_adds"] += len(adds.get(owner, []))

    return render_template("year.html", year=year, teams=teams,
                           team_weekly_summaries=team_weekly_summaries,
                           playoffs=year_data.get("playoffs", []),
                           draft=year_data.get("draft", []),
                           transactions=year_data.get("transactions", []),
                           team_txn_counts=team_txn_counts,
                           season_complete=season_is_complete(year_data),
                           analyzer=_analyzer_context(year),
                           trade_history=_trade_history(year),
                           efficiency=get_efficiency(year))

def _all_players():
    """Sleeper player metadata. Tolerates a stale cache — a page view should
    never trigger the 15MB download; the background updater keeps it fresh."""
    return get_player_names(max_age_hours=24 * 365)


def _consensus_board(season):
    """Multi-source player valuations. Never let a slow or down third party
    take a page with it — an empty board just hides the extra panels."""
    try:
        # ADP only counts until a real game has been played. Our own weekly
        # scores know that more precisely than the NFL week number, which flips
        # a few days before week 1 actually kicks off.
        preseason = season_phase(str(season)) == "preseason"
        return consensus.build_board(season, _all_players(), use_adp=preseason)
    except Exception as e:
        print(f"Consensus board unavailable: {e}")
        return {}


def _name_to_id():
    """Fallback lookup for roster/draft entries saved before player_id was
    stored. Ambiguous duplicate names are dropped rather than guessed."""
    def build():
        index, dupes = {}, set()
        for pid, info in _all_players().items():
            key = info["name"].lower()
            if key in index and index[key] != pid:
                dupes.add(key)
            index[key] = pid
        for k in dupes:
            index.pop(k, None)
        return index
    return _cached("name_to_id", 3600, build)


def resolve_player_id(entry):
    """player_id from a roster/draft entry, falling back to a name lookup.

    Also accepts a bare name — transactions record players that way.
    """
    if isinstance(entry, str):
        return _name_to_id().get(entry.lower())
    pid = entry.get("player_id")
    if pid:
        return str(pid)
    name = entry.get("name") or entry.get("player") or ""
    return _name_to_id().get(name.lower())


@app.template_filter("stat")
def stat_filter(value, blank="—"):
    """Stat numbers the way a box score prints them: whole numbers lose the
    trailing .0, fractions keep one decimal."""
    if value is None:
        return blank
    try:
        n = float(value)
    except (TypeError, ValueError):
        return value
    return str(int(n)) if n == int(n) else f"{n:.1f}"


@app.template_global()
def player_id_for(entry):
    """Usable directly in templates to decide whether to link a player."""
    try:
        return resolve_player_id(entry)
    except Exception:
        return None


def get_player_league_history(player_name, player_id):
    """Everything this league knows about a player: which owners have rostered
    him, what he actually scored for each of them, draft history and every
    add/drop. Points come from Sleeper-era seasons (2025+); earlier ESPN
    seasons contribute roster/draft history only."""
    pid = str(player_id)
    owners = {}

    def owner_rec(name):
        return owners.setdefault(name, {
            "owner": name, "seasons": set(), "points": 0.0, "weeks": 0,
            "starts": 0, "best": 0.0, "best_week": None, "best_season": None,
        })

    drafts, moves = [], []
    seasons_rostered = set()

    for year in sorted(league_data, key=int):
        yd = league_data[year]

        # Roster membership (end-of-season snapshot for ESPN years, live for now)
        for t in yd.get("teams", []):
            if any(p.get("name") == player_name for p in t.get("roster", [])):
                owner_rec(t["owner"])["seasons"].add(year)
                seasons_rostered.add(year)

        # Actual scoring for this league, per owner
        for owner, s in (yd.get("player_scoring", {}).get(pid) or {}).items():
            rec = owner_rec(owner)
            rec["seasons"].add(year)
            seasons_rostered.add(year)
            rec["points"] += s.get("points", 0)
            rec["weeks"] += s.get("weeks", 0)
            rec["starts"] += s.get("starts", 0)
            if s.get("best", 0) > rec["best"]:
                rec["best"] = s["best"]
                rec["best_week"] = s.get("best_week")
                rec["best_season"] = year

        for p in yd.get("draft", []):
            if p.get("player") == player_name:
                drafts.append({**p, "year": year})

        for t in yd.get("transactions", []):
            for kind in ("adds", "drops"):
                for owner, players in (t.get(kind) or {}).items():
                    if player_name in players:
                        moves.append({"year": year, "owner": owner,
                                      "kind": kind[:-1], "type": t.get("type"),
                                      "week": t.get("week"), "date": t.get("date")})

    rows = sorted(owners.values(), key=lambda r: (-r["points"], -len(r["seasons"])))
    for r in rows:
        r["points"] = round(r["points"], 2)
        r["season_list"] = sorted(r["seasons"])
        r["ppg"] = round(r["points"] / r["starts"], 2) if r["starts"] else None

    moves.sort(key=lambda m: (m["year"], m.get("week") or 0))
    # Seasons where per-player scoring exists at all (Sleeper era), so the page
    # can say plainly which years the points cover.
    scoring_years = sorted(y for y in league_data if league_data[y].get("player_scoring"))
    return {
        "owners":       rows,
        "scoring_from": scoring_years[0] if scoring_years else None,
        "drafts":       sorted(drafts, key=lambda d: d["year"]),
        "moves":        moves,
        "total_points": round(sum(r["points"] for r in rows), 2),
        "seasons":      sorted(seasons_rostered),
        "n_owners":     len(rows),
        "times_drafted": len(drafts),
        "times_kept":   sum(1 for d in drafts if d.get("keeper")),
    }


@app.route('/api/player/<player_id>/log/<season>')
def api_player_log(player_id, season):
    """Game log for one season — loaded on demand when a past season is
    expanded, so the page itself stays fast."""
    players = _all_players()
    info = players.get(str(player_id))
    if not info or not season.isdigit():
        return jsonify({"error": "not found"}), 404
    try:
        log = player_stats.get_player_game_log(player_id, season,
                                               info.get("position", "?"))
        return jsonify({"columns": log["columns"], "rows": log["rows"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route('/player/<player_id>')
def player_view(player_id):
    players = _all_players()
    info = players.get(str(player_id))
    if not info:
        return "Player not found", 404

    season = max(league_data.keys(), key=int)
    position = info.get("position", "?")

    try:
        log = player_stats.get_player_game_log(player_id, season, position)
        season_totals = player_stats.get_season_totals(player_id, season)
        summary = player_stats.season_summary(season_totals, position)
        error = None
    except Exception as e:
        print(f"Player stats fetch failed for {player_id}: {e}")
        log = {"rows": [], "totals": {}, "columns": []}
        season_totals, summary = {}, None
        error = "Couldn't reach Sleeper for this player's stats right now."

    # Prior seasons: the player's whole career, summary only up front (cheap);
    # the game log for each is fetched on demand when the row is expanded.
    # years_exp counts completed seasons, so it points at the rookie year — pad
    # it by one in case the count is off, and let empty seasons drop out.
    try:
        rookie_year = int(season) - int(info.get("years_exp") or 0) - 1
    except (TypeError, ValueError):
        rookie_year = int(season) - 4
    years = [str(y) for y in range(int(season) - 1,
                                   max(rookie_year, SLEEPER_STATS_EARLIEST) - 1, -1)]

    def _summary_for(year):
        try:
            totals = player_stats.get_season_totals(player_id, year)
        except Exception:
            return None
        s = player_stats.season_summary(totals, position)
        if not (s and s.get("games")):
            return None
        return {"season": year, "summary": s,
                "avg": round(s["points"] / s["games"], 2)
                       if s.get("points") and s.get("games") else None}

    # One request per season, so fetch them side by side — a 15-year veteran
    # would otherwise stall the page for several seconds.
    past = []
    if years:
        with ThreadPoolExecutor(max_workers=min(8, len(years))) as pool:
            past = [r for r in pool.map(_summary_for, years) if r]

    # Where the outside world has this player right now: expert projection,
    # draft market and trade market, plus how far apart they are.
    market = _consensus_board(season).get(str(player_id))

    league = get_player_league_history(info["name"], player_id)

    # Who rosters him in this league right now, and was he a keeper?
    owner = keeper = None
    year_data = league_data[season]
    for t in year_data.get("teams", []):
        if any(p["name"] == info["name"] for p in t.get("roster", [])):
            owner = t["owner"]
            break
    if owner:
        keeper = info["name"] in get_current_keepers(year_data).get(owner, set())

    drafted = next((p for p in year_data.get("draft", [])
                    if p["player"] == info["name"]), None)

    return render_template("player.html", info=info, player_id=str(player_id),
                           season=season, log=log, season_totals=season_totals,
                           summary=summary, past=past, league=league,
                           owner=owner, keeper=keeper, drafted=drafted,
                           height=player_stats.format_height(info.get("height")),
                           market=market, scoring="Half-PPR", error=error)


@app.context_processor
def inject_nav_context():
    """The season the nav menu links to, when the last note was posted, and
    which layout tab-able pages should use."""
    # Pages that can open inside a season-page tab extend `layout`, which
    # drops the nav and footer when the tab asks for ?fragment=1.
    ctx = {"current_season": "", "latest_note_ts": _latest_note_ts(),
           "layout": "fragment.html" if request.args.get("fragment") else "base.html"}
    try:
        ctx["current_season"] = max(league_data.keys(), key=int)
    except ValueError:
        pass
    return ctx


def _latest_note_ts():
    """Timestamp of the newest league note, for the nav's "new" dot. Each
    device remembers the newest one it has seen, so nothing is stored here."""
    notes = _load_json(NOTES_FILE, {})
    stamps = [n.get("timestamp", "") for year in notes.values() for n in year]
    return max(stamps, default="")


@app.route('/recaps')
@app.route('/recaps/<int:week>')
def recaps_view(week=None):
    """Weekly awards + results for any played week of the current season."""
    latest = max(league_data.keys(), key=int)
    year_data = league_data[latest]
    weeks = played_weeks(year_data)
    if not weeks:
        return render_template("recaps.html", year=latest, weeks=[],
                               week=None, awards=None)
    if week is None:
        week = weeks[-1]
    elif week not in weeks:
        return "No games played that week", 404
    return render_template("recaps.html", year=latest, weeks=weeks, week=week,
                           awards=compute_week_awards(year_data, week),
                           week_eff=get_week_efficiency(latest, week))


@app.route('/scoreboard')
def scoreboard_view():
    try:
        board = get_live_scoreboard()
        error = None
    except Exception as e:
        print(f"Scoreboard fetch failed: {e}")
        board = {"week": None, "season_type": "regular", "games": []}
        error = "Couldn't reach Sleeper right now. Try again in a minute."
    return render_template("scoreboard.html", board=board, error=error)


@app.route('/api/scoreboard')
def api_scoreboard():
    try:
        return jsonify(get_live_scoreboard())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


def get_market_movers(limit=10):
    """Who the wider fantasy market is buying and selling.

    Two independent clocks: FantasyCalc's 30-day value change (slow, trade
    driven) and KeepTradeCut's 7-day crowd swing (fast, news driven). Both are
    reported in their own value units, so they're ranked separately rather than
    mashed into one number.
    """
    season = max(league_data.keys(), key=int)
    board = _consensus_board(season)
    if not board:
        return {"movers30": [], "movers7": [], "most_cut": [], "market_error": True}

    players = _all_players()
    owner_of = {}
    for t in league_data[season].get("teams", []):
        for p in t.get("roster", []):
            owner_of[str(p.get("player_id"))] = t["owner"]

    def row(pid, v):
        return {"id": pid,
                "name": (players.get(pid) or {}).get("name", pid),
                "position": v["position"],
                "owner": owner_of.get(pid),
                "trend30": v.get("trend30"),
                "trend7": (v.get("ktc") or {}).get("trend7"),
                "keep_pct": (v.get("ktc") or {}).get("keep_pct"),
                "cut_pct": (v.get("ktc") or {}).get("cut_pct"),
                "votes": (v.get("ktc") or {}).get("votes"),
                "rank": v.get("rank")}

    rows = [row(pid, v) for pid, v in board.items()]

    with30 = [r for r in rows if r["trend30"]]
    with30.sort(key=lambda r: -r["trend30"])
    movers30 = {"up": with30[:limit], "down": with30[::-1][:limit]}

    with7 = [r for r in rows if r["trend7"]]
    with7.sort(key=lambda r: -r["trend7"])
    movers7 = {"up": with7[:limit], "down": with7[::-1][:limit]}

    # Highest cut rate among players enough people voted on to mean something.
    cut = [r for r in rows if r["cut_pct"] is not None and (r["votes"] or 0) >= 200]
    cut.sort(key=lambda r: -r["cut_pct"])

    return {"movers30": movers30, "movers7": movers7,
            "most_cut": cut[:limit], "market_error": False}


def get_efficiency(year):
    """Lineup efficiency for one season, ready to render."""
    year_str = str(year)
    year_data = league_data.get(year_str) or {}
    if not year_data.get("weekly_lineups"):
        return None

    players = _all_players()
    positions = {pid: (v.get("position") or "") for pid, v in players.items()}
    reg = get_regular_season_weeks(year_str)
    eff = lineups.season_efficiency(year_data, positions, through_week=reg)
    if not eff:
        return None

    def pname(pid):
        return (players.get(pid) or {}).get("name", pid)

    rows = []
    for owner, v in eff.items():
        worst = v["worst"] or {}
        rows.append({
            "owner": owner,
            "efficiency": v["efficiency"],
            "actual": v["actual"], "optimal": v["optimal"],
            "left": v["left"], "left_per_week": v["left_per_week"],
            "weeks": v["weeks"], "perfect": v["perfect"],
            "worst_week": worst.get("week"),
            "worst_left": worst.get("left"),
            "worst_sit": pname(worst["worst_sit"]) if worst.get("worst_sit") else None,
            "worst_sit_pts": worst.get("worst_sit_pts"),
            "worst_sit_id": worst.get("worst_sit"),
        })
    rows.sort(key=lambda r: -(r["efficiency"] or 0))

    # The single worst benching in the league this season, whoever did it.
    sits = []
    for owner, v in eff.items():
        for w in v["by_week"]:
            if w.get("worst_sit"):
                sits.append({"owner": owner, "week": w["week"],
                             "player": pname(w["worst_sit"]),
                             "player_id": w["worst_sit"],
                             "points": w["worst_sit_pts"],
                             "left": w["left"]})
    sits.sort(key=lambda s: -s["points"])

    return {"year": year_str, "rows": rows, "worst_sits": sits[:15],
            "weeks": reg,
            "league_efficiency": round(
                sum(r["actual"] for r in rows) / sum(r["optimal"] for r in rows) * 100, 1)
            if sum(r["optimal"] for r in rows) else None}


def get_week_efficiency(year, week):
    """One week's lineup efficiency, for the recap page.

    Season-long efficiency lives on that season's own page; this is the same maths
    over a single week, which is what you actually want to read on a Tuesday.
    """
    year_data = league_data.get(str(year)) or {}
    entries = (year_data.get("weekly_lineups") or {}).get(str(week))
    if not entries:
        return None

    players = _all_players()
    positions = {pid: (v.get("position") or "") for pid, v in players.items()}

    rows = []
    for owner, entry in entries.items():
        eff = lineups.week_efficiency(entry, positions)
        if not eff:
            continue
        sit = eff.get("worst_sit")
        rows.append({
            "owner": owner,
            "efficiency": eff["efficiency"],
            "actual": eff["actual"], "optimal": eff["optimal"], "left": eff["left"],
            "worst_sit": (players.get(sit) or {}).get("name", sit) if sit else None,
            "worst_sit_id": sit,
            "worst_sit_pts": eff.get("worst_sit_pts"),
        })
    if not rows:
        return None
    rows.sort(key=lambda r: -(r["efficiency"] or 0))

    optimal = sum(r["optimal"] for r in rows)
    return {
        "rows": rows,
        "week": int(week),
        "league_efficiency": round(sum(r["actual"] for r in rows) / optimal * 100, 1)
                             if optimal else None,
        "left": round(sum(r["left"] for r in rows), 2),
        "perfect": [r["owner"] for r in rows if r["left"] < 0.01],
    }


@app.route('/history')
@app.route('/history/<int:year>')
@app.route('/efficiency')
@app.route('/efficiency/<int:year>')
def efficiency_view(year=None):
    """Old standalone lineup-efficiency page.

    Efficiency now lives on the season it describes, as a tab on that year's
    page, so these routes only survive to redirect the bookmarks and the links
    already sitting in the Notes archive.
    """
    years = sorted((y for y, d in league_data.items()
                    if y.isdigit() and d.get("weekly_lineups")), key=int, reverse=True)
    if not years:
        return redirect(url_for('home'))
    year_str = str(year) if year and str(year) in years else years[0]
    return redirect(url_for('year_view', year=int(year_str)) + '#tab-efficiency')


def _analyzer_pool(season=None):
    """Every rostered player with the numbers the analyzer needs.

    Current season only. The analyzer runs on forward-looking consensus
    projections and live KeepTradeCut values, neither of which means anything
    applied to a roster from a season that is already over.

    Returns (season, board, pool) or (season, {}, []) if that season isn't the
    current one.
    """
    latest = max(league_data.keys(), key=int)
    season = str(season or latest)
    if season != latest:
        return season, {}, []
    year_data = league_data.get(season, {})
    board = _consensus_board(season)

    pool = []
    for t in year_data.get("teams", []):
        for p in t.get("roster", []):
            pid = str(p.get("player_id") or "")
            v = board.get(pid) or {}
            pool.append({
                "id": pid, "name": p["name"], "position": p.get("position", "?"),
                "owner": t["owner"],
                "value": v.get("ktc_value") or 0,
                "points": v.get("points") or 0,
                "rank": v.get("rank"),
                "tier": v.get("tier"),
            })
    pool.sort(key=lambda p: (-p["value"], p["name"]))
    return season, board, pool


def _analyzer_context(season=None):
    """Everything analyzer.html needs, or None if the season can't support it."""
    season, board, pool = _analyzer_pool(season)
    if not pool:
        return None
    teams = league_data[season].get("teams", [])

    # Baseline lineup strength, so the page can show what a team starts with.
    strength = {}
    for t in teams:
        pts, pos, _ = trade_machine.team_state(t.get("roster") or [], board)
        strength[t["owner"]] = round(trade_machine.lineup_strength(pts, pos), 1)

    rosters = {t["owner"]: [p for p in pool if p["owner"] == t["owner"]]
               for t in teams}
    return {"pool": pool, "rosters": rosters, "owners": sorted(rosters),
            "strength": strength, "year": season}


@app.route('/analyzer')
@app.route('/analyzer/<int:season>')
def analyzer_view(season=None):
    ctx = _analyzer_context(season)
    if not ctx:
        abort(404)
    return render_template("analyzer.html", analyzer=ctx)


def _season_resolver(year_data):
    """Name -> player id for one season's transactions.

    Transactions only record names, and some names belong to two NFL players
    (there's more than one Michael Carter). When the global lookup can't
    decide, pick the namesake who actually shows up on a roster that season.
    """
    on_rosters = set()
    for owners in (year_data.get("weekly_lineups") or {}).values():
        for entry in (owners or {}).values():
            on_rosters.update((entry or {}).get("points") or {})

    def resolve(name):
        pid = resolve_player_id(name)
        if pid:
            return pid
        key = (name or "").lower()
        matches = [p for p, info in _all_players().items()
                   if info.get("name", "").lower() == key and p in on_rosters]
        return matches[0] if len(matches) == 1 else None
    return resolve


def _trade_history(season):
    """Graded trades for one season, or None if that season has none to grade.

    Needs Sleeper's week-by-week rosters to know what a player did after he
    changed hands, so the ESPN years can't be scored at all.
    """
    year_data = league_data.get(str(season)) or {}
    if not year_data.get("weekly_lineups"):
        return None
    graded = trades.grade_season_trades(year_data, _season_resolver(year_data))
    if not graded:
        return None
    # Sorted here rather than in Jinja — dictsort can't order by a nested key.
    records = sorted(trades.owner_trade_record(graded).items(),
                     key=lambda kv: -kv[1]["net"])
    return {"graded": graded, "records": records, "year": str(season)}


def _latest_season():
    return max(league_data.keys(), key=int)


@app.route('/trades')
@app.route('/trades/<int:year>')
def trades_view(year=None):
    """Every graded trade, one season at a time."""
    years = [y for y in sorted(league_data, key=int, reverse=True)
             if _trade_history(y)]
    if not years:
        return render_template("trades.html", graded=[], records=[],
                               years=[], year=None)
    year_str = str(year) if year and str(year) in years else years[0]
    th = _trade_history(year_str)
    return render_template("trades.html", graded=th["graded"],
                           records=th["records"], years=years, year=year_str)


def _trade_finder_deals(season):
    """Every candidate deal for the current rosters. Cached for an hour: the
    search is a second or two of lineup maths across 66 pairs of teams."""
    def build():
        board = _consensus_board(season)
        if not board:
            return {"deals": [], "has_board": False, "ts": time.time()}
        teams = league_data.get(season, {}).get("teams", [])
        return {"deals": trade_machine.find_trades(teams, board),
                "has_board": True, "ts": time.time()}
    return _cached(f"trade_finder:{season}", 3600, build)


@app.route('/trade_finder')
def trade_finder_view():
    season = _latest_season()
    result = _trade_finder_deals(season)
    owners = sorted(t["owner"] for t in league_data[season].get("teams", []))
    owner = request.args.get("owner", "")
    if owner not in owners:
        owner = ""
    deals = result["deals"]
    if owner:
        found = trade_machine.pick_ideas(deals, owner=owner, limit=12,
                                         per_player=3, per_pair=3)
        pitches = trade_machine.pick_ideas(deals, owner=owner, limit=8,
                                           per_player=2, per_pair=2, mutual=False)
    else:
        found, pitches = trade_machine.pick_ideas(deals, limit=20), []
    age = int((time.time() - result["ts"]) / 60)
    return render_template("trade_finder.html", found=found, pitches=pitches,
                           owners=owners, owner=owner, season=season,
                           has_board=result["has_board"], age_minutes=age,
                           min_gain=trade_machine.MIN_WEEKLY_GAIN)


@app.route('/keepers')
def keepers_view():
    season = _latest_season()
    year_data = league_data[season]
    board = _consensus_board(season)
    preseason = season_phase(season) == "preseason"
    slots = keepers.draft_slots(board, use_adp=preseason) if board else {}
    teams_n = len(year_data.get("teams", [])) or 12
    options = keepers.keeper_options(year_data, board, resolve_player_id, slots)

    rows, steals = [], []
    for owner in sorted(options):
        opts = options[owner]
        for o in opts:
            o["verdict"], o["surplus"] = keepers.value_verdict(o, teams_n)
            o["cost_pick"] = round(keepers.cost_pick(o["cost_round"], teams_n))
            if o["verdict"] == "steal":
                steals.append(dict(o, owner=owner))
        # Best value first — the question on this page is "who do I keep?"
        opts.sort(key=lambda o: (o["surplus"] is None, -(o["surplus"] or 0)))
        if opts and opts[0]["surplus"] is not None:
            opts[0]["best"] = True
        rows.append((owner, opts))
    steals.sort(key=lambda o: -o["surplus"])
    return render_template("keepers.html", rows=rows, steals=steals[:12],
                           year=season, next_year=int(season) + 1,
                           has_values=bool(slots), preseason=preseason,
                           in_season=not season_is_complete(year_data))


@app.route('/draft_review')
@app.route('/draft_review/<int:year>')
def draft_review_view(year=None):
    years = [y for y in sorted(league_data, key=int, reverse=True)
             if league_data[y].get("player_scoring") and league_data[y].get("draft")]
    if not years:
        return render_template("draft_review.html", review=None, years=[])
    # A draft a couple of weeks old hasn't told us much yet, so default to the
    # newest finished season and let the buttons reach the live one.
    finished = [y for y in years if season_is_complete(league_data[y])]
    year_str = (str(year) if year and str(year) in years
                else (finished or years)[0])
    review = draft_review.review_draft(league_data[year_str], resolve_player_id)
    if review:
        review["year"] = year_str
        review["complete"] = season_is_complete(league_data[year_str])
    return render_template("draft_review.html", review=review, years=years)


# ── Home-screen app ──────────────────────────────────────────────────────────
# The manifest and service worker make "Add to Home Screen" install the site as
# an app: its own icon, full screen, and an offline page when there's no signal.

@app.route('/manifest.webmanifest')
def web_manifest():
    manifest = {
        "name": "Peyton Woods League",
        "short_name": "PW League",
        "description": "Scores, trades, keepers and league history.",
        "start_url": "/?source=app",
        "scope": "/",
        "display": "standalone",
        "background_color": "#add8e6",
        "theme_color": "#1565c0",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "This Week", "url": "/scoreboard"},
            {"name": "Trade Finder", "url": "/trade_finder"},
            {"name": "Chat", "url": "/chat"},
        ],
    }
    resp = jsonify(manifest)
    resp.mimetype = "application/manifest+json"
    return resp


@app.route('/sw.js')
def service_worker():
    # Served from the root, not /static/, so it can look after every page.
    resp = send_from_directory(os.path.join(app.root_path, "static"), "sw.js",
                               mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route('/offline')
def offline_page():
    return render_template("offline.html")


@app.route('/api/push/key')
def push_key():
    if not push.AVAILABLE:
        return jsonify({"available": False})
    return jsonify({"available": True, "key": push.public_key()})


@app.route('/api/push/subscribe', methods=['POST'])
def push_subscribe():
    if not push.AVAILABLE:
        return jsonify({"error": "Alerts aren't available right now."}), 503
    sub = request.get_json(silent=True) or {}
    if not push.subscribe(sub):
        return jsonify({"error": "That subscription didn't look right."}), 400
    push.notify_one(sub, "🔔 Alerts are on",
                    "You'll get a notification whenever a new league note is posted.",
                    "/notes")
    return jsonify({"ok": True})


@app.route('/api/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    endpoint = (request.get_json(silent=True) or {}).get("endpoint")
    if isinstance(endpoint, str):
        push.unsubscribe(endpoint)
    return jsonify({"ok": True})


@app.route('/trends')
def trends_view():
    try:
        trends = get_trends()
        error = None
    except Exception as e:
        print(f"Trends fetch failed: {e}")
        trends = {"add": [], "drop": []}
        error = "Couldn't reach Sleeper right now. Try again in a minute."
    return render_template("trends.html", trends=trends, error=error,
                           **get_market_movers())


@app.route('/odds')
def odds_view():
    try:
        odds = compute_playoff_odds()
        error = None
    except Exception as e:
        print(f"Odds computation failed: {e}")
        odds = None
        error = "Couldn't build the odds right now. Try again in a minute."
    return render_template("odds.html", odds=odds, error=error)


@app.route('/head_to_head')
def head_to_head_view():
    head_to_head = calculate_historical_head_to_head()
    # Current managers only — same definition the power rankings use. Retired
    # owners' head-to-head records are still on their own profile pages.
    active = {t["owner"] for t in
              league_data[max(league_data.keys(), key=int)].get("teams", [])}
    head_to_head = {o: {opp: r for opp, r in opps.items() if opp in active}
                    for o, opps in head_to_head.items() if o in active}
    owners = sorted(head_to_head.keys())
    return render_template("head_to_head.html", head_to_head=head_to_head, owners=owners,
                           min_year=min(league_data), max_year=max(league_data))

@app.route("/power_rankings")
def power_rankings():
    owner_stats = aggregate_owner_stats()
    rankings = get_power_rankings(owner_stats)
    overall_records = calculate_overall_records(owner_stats)

    # Completed seasons only. Mid-season `rank` is just the current standings —
    # after one week it is barely more than a points tiebreak, and averaging it
    # in moves a career figure by a third of a place on the strength of a single
    # Sunday. Owners with no finished season are left out, and the template
    # already renders "N/A" for anyone missing here.
    average_finish = {}
    for year, year_data in league_data.items():
        if not season_is_complete(year_data):
            continue
        for team in year_data.get("teams", []):
            owner = team.get("owner", "Unknown")
            average_finish.setdefault(owner, []).append(team.get("rank", float('inf')))

    for owner in average_finish:
        average_finish[owner] = sum(average_finish[owner]) / len(average_finish[owner])

    luck_index = calculate_luck_index()

    return render_template("power_rankings.html",
                           active_rankings=rankings["active"],
                           retired_rankings=rankings["retired"],
                           points_scored=rankings["points_scored"],
                           overall_records=overall_records,
                           average_finish=average_finish,
                           luck_index=luck_index,
                           playoff_stats=calculate_playoff_stats())


# PINs are short and one SHA-256 is fast, so unlimited guesses is a real
# weakness even for a 12-person league. Track recent failures per username and
# make the attacker wait. In-memory on purpose: a restart clearing it is fine,
# and it keeps the hot path off the disk.
_pin_failures: dict = {}
PIN_MAX_FAILURES = 5
PIN_LOCKOUT_SECONDS = 60


def _pin_lockout_remaining(username):
    """Seconds the caller must wait, or 0 if they may try now."""
    rec = _pin_failures.get(username)
    if not rec or rec["count"] < PIN_MAX_FAILURES:
        return 0
    elapsed = time.time() - rec["last"]
    if elapsed >= PIN_LOCKOUT_SECONDS:
        _pin_failures.pop(username, None)
        return 0
    return int(PIN_LOCKOUT_SECONDS - elapsed) + 1


def verify_pin(username, pin):
    """Return True if pin is valid for username; register if first time seen.
    PINs are stored as salted hashes in chat_users.json and survive restarts.
    Caller must hold _file_lock."""
    if not username or not pin:
        return False
    pin_hash = hashlib.sha256(f"{username}:{pin}".encode()).hexdigest()
    users = _load_json(CHAT_USERS_FILE, {})
    if username in users:
        # hmac.compare_digest: constant-time, so a wrong PIN can't be narrowed
        # down by timing how long the comparison took.
        ok = hmac.compare_digest(users[username], pin_hash)
        if ok:
            _pin_failures.pop(username, None)
        else:
            rec = _pin_failures.setdefault(username, {"count": 0, "last": 0.0})
            rec["count"] += 1
            rec["last"] = time.time()
        return ok
    users[username] = pin_hash
    _save_json_atomic(CHAT_USERS_FILE, users)
    return True


# ── Chat routes ───────────────────────────────────────────────────────────────

# The whole log is read, appended to and rewritten under a lock on every post,
# so it can't be allowed to grow without bound: keep the most recent
# CHAT_KEEP_MESSAGES and let older ones fall off. CHAT_WINDOW is how many the
# client is sent per poll.
CHAT_MAX_MESSAGE_CHARS = 4000
CHAT_KEEP_MESSAGES     = 1000
CHAT_WINDOW            = 200


@app.route('/chat', methods=['GET', 'POST'])
def chat():
    if request.method == 'POST':
        data     = request.json or {}
        username = (data.get('username') or '').strip()
        pin      = (data.get('pin')      or '').strip()
        message  = (data.get('message')  or '').strip()

        if not message:
            return jsonify({"error": "Message cannot be empty."}), 400
        if len(message) > CHAT_MAX_MESSAGE_CHARS:
            return jsonify({"error": f"Message is too long "
                                     f"({CHAT_MAX_MESSAGE_CHARS} character max)."}), 400

        timestamp  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_entry = {"username": username, "message": message, "timestamp": timestamp}

        with _file_lock:
            wait = _pin_lockout_remaining(username)
            if wait:
                return jsonify({"error": f"Too many wrong PINs. Try again in {wait}s."}), 429
            if not verify_pin(username, pin):
                return jsonify({"error": "Wrong PIN for that username."}), 403
            chat_log = _load_json(CHAT_LOG_FILE, [])
            chat_log.append(chat_entry)
            del chat_log[:-CHAT_KEEP_MESSAGES]
            _save_json_atomic(CHAT_LOG_FILE, chat_log)

        return jsonify({"success": True})

    return render_template("chat.html", gif_enabled=bool(KLIPY_API_KEY))


@app.route('/get_messages')
def get_messages():
    """The most recent CHAT_WINDOW messages, plus where that window starts.

    `first_index` is what makes the client's cursor work. Sending a bare list
    means the client can only count what it received, and once the log passes
    CHAT_WINDOW that count stops changing — the array is always the same length,
    so new messages never render and the chat appears frozen.
    """
    with _file_lock:
        messages = _load_json(CHAT_LOG_FILE, [])
    window = messages[-CHAT_WINDOW:] if CHAT_WINDOW else messages
    return jsonify({"messages": window,
                    "first_index": len(messages) - len(window)})


@app.route('/api/gifs')
def gif_search():
    """Server-side proxy to the Klipy GIF API (keeps the API key off the client).
    Returns trending GIFs when no query is given, search results otherwise."""
    if not KLIPY_API_KEY:
        return jsonify({"enabled": False, "gifs": []})

    q = (request.args.get('q') or '').strip()
    endpoint = "search" if q else "trending"
    params = {"per_page": 24, "content_filter": "medium", "format_filter": "gif"}
    if q:
        params["q"] = q

    try:
        resp = requests.get(
            f"https://api.klipy.com/api/v1/{KLIPY_API_KEY}/gifs/{endpoint}",
            params=params, timeout=10)
        resp.raise_for_status()
        items = ((resp.json().get("data") or {}).get("data")) or []
    except Exception as e:
        print(f"Klipy request failed: {e}")
        return jsonify({"enabled": True, "gifs": [], "error": "GIF service unavailable"}), 502

    def best_gif_url(file_variants, size_order):
        for size in size_order:
            fmt = (file_variants.get(size) or {}).get("gif") or {}
            if fmt.get("url"):
                return fmt["url"]
        return None

    gifs = []
    for item in items:
        file_variants = item.get("file") or {}
        full    = best_gif_url(file_variants, ("md", "hd", "sm"))
        preview = best_gif_url(file_variants, ("xs", "sm", "md")) or full
        if full:
            gifs.append({"url": full, "preview": preview, "title": item.get("title", "")})
    return jsonify({"enabled": True, "gifs": gifs})

@app.route('/records')
def records():
    return render_template("records.html",
                           weekly_high_scores=calculate_weekly_high_scores(),
                           weekly_low_scores=calculate_weekly_low_scores(),
                           closest_games=calculate_closest_games(),
                           blowouts=calculate_biggest_blowouts(),
                           shootouts=calculate_highest_combined(),
                           most_points_in_loss=calculate_most_points_in_loss(),
                           streaks=calculate_streaks(),
                           season_scoring=calculate_top_bottom_seasons(),
                           roll=calculate_season_roll(),
                           ledger=calculate_owner_ledger(),
                           min_year=min(league_data),
                           max_year=latest_played_year())


@app.route('/draft')
def draft_view():
    draft_seasons = {}
    for year, year_data in sorted(league_data.items(), reverse=True):
        picks = year_data.get("draft")
        if picks:
            draft_seasons[year] = {
                "picks": picks,
                "has_positions": any(p.get("position", "?") != "?" for p in picks),
            }
    return render_template("draft.html", draft_seasons=draft_seasons)


# ── League notes ──────────────────────────────────────────────────────────────
# Long-term notes organized by season year, stored in league_notes.json.
# Posting/deleting uses the same username+PIN identity as chat; you can only
# delete your own notes.

@app.route('/notes')
def notes_view():
    notes = _load_json(NOTES_FILE, {})
    # Years for the "post a note" dropdown: every season plus the current year
    year_options = sorted(set(league_data.keys()) | {str(datetime.now().year)}, reverse=True)
    # Years that actually have notes, newest first
    note_years = sorted((y for y in notes if notes[y]), reverse=True)
    return render_template("notes.html", notes=notes, note_years=note_years,
                           year_options=year_options)


@app.route('/api/notes', methods=['POST'])
def add_note():
    data     = request.get_json() or {}
    username = (data.get('username') or '').strip()
    pin      = (data.get('pin')      or '').strip()
    year     = str(data.get('year')  or '').strip()
    text     = (data.get('text')     or '').strip()

    if not text:
        return jsonify({"error": "Note cannot be empty."}), 400
    if len(text) > 20000:
        return jsonify({"error": "Note is too long (20000 character max)."}), 400
    if not (year.isdigit() and 2000 <= int(year) <= 2100):
        return jsonify({"error": "Invalid year."}), 400

    with _file_lock:
        wait = _pin_lockout_remaining(username)
        if wait:
            return jsonify({"error": f"Too many wrong PINs. Try again in {wait}s."}), 429
        if not verify_pin(username, pin):
            return jsonify({"error": "Wrong PIN for that username."}), 403
        notes = _load_json(NOTES_FILE, {})
        note_id = uuid.uuid4().hex
        notes.setdefault(year, []).insert(0, {
            "id":        note_id,
            "username":  username,
            "text":      text,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        _save_json_atomic(NOTES_FILE, notes)
    preview = " ".join(text.split())
    push.notify_all(f"📝 New league note from {username}",
                    preview[:140] + ("…" if len(preview) > 140 else ""),
                    f"/notes#note-{note_id}")
    return jsonify({"ok": True})


@app.route('/api/notes/delete', methods=['POST'])
def delete_note():
    data     = request.get_json() or {}
    username = (data.get('username') or '').strip()
    pin      = (data.get('pin')      or '').strip()
    note_id  = (data.get('id')       or '').strip()

    with _file_lock:
        wait = _pin_lockout_remaining(username)
        if wait:
            return jsonify({"error": f"Too many wrong PINs. Try again in {wait}s."}), 429
        if not verify_pin(username, pin):
            return jsonify({"error": "Wrong PIN for that username."}), 403
        notes = _load_json(NOTES_FILE, {})

        # Locate first, mutate after. Deleting a key while iterating the same
        # dict only worked before because the very next statement returned.
        found = next(((year, n) for year, year_notes in notes.items()
                      for n in year_notes if n["id"] == note_id), None)
        if not found:
            return jsonify({"error": "Note not found."}), 404
        year, note = found
        if note["username"] != username:
            return jsonify({"error": "You can only delete your own notes."}), 403
        notes[year].remove(note)
        if not notes[year]:
            del notes[year]
        _save_json_atomic(NOTES_FILE, notes)
        return jsonify({"ok": True})


POSITION_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "DEF": 5, "D/ST": 5}


@app.route('/rosters')
@app.route('/rosters/<int:year>')
def rosters_view(year=None):
    """Team rosters for a season. Latest season = active rosters (as of the
    last data refresh); earlier seasons = end-of-season rosters."""
    roster_years = [y for y in sorted(league_data.keys(), reverse=True)
                    if any(t.get("roster") for t in league_data[y].get("teams", []))]
    if not roster_years:
        return render_template("rosters.html", year=None, teams=[],
                               roster_years=[], is_latest=False)

    year_str = str(year) if year is not None else roster_years[0]
    if year_str not in roster_years:
        return "No roster data for that season", 404

    year_data = league_data[year_str]
    is_latest = (year_str == roster_years[0])
    keepers = get_current_keepers(year_data) if is_latest else {}

    teams = []
    for t in sorted(year_data.get("teams", []), key=lambda x: x.get("rank", 99)):
        roster = sorted(t.get("roster", []),
                        key=lambda p: (POSITION_ORDER.get(p.get("position"), 8), p.get("name", "")))
        teams.append({**t, "roster": roster})

    return render_template("rosters.html", year=year_str, teams=teams,
                           roster_years=roster_years, is_latest=is_latest,
                           keepers=keepers,
                           season_complete=season_is_complete(year_data))


def get_owner_season_detail(owner_name, year):
    """Full per-season breakdown for a single owner."""
    year_str  = str(year)
    year_data = league_data.get(year_str)
    if not year_data:
        return None

    team = next((t for t in year_data.get("teams", []) if t["owner"] == owner_name), None)
    if not team:
        return None

    team_name    = team["name"]
    all_teams    = year_data.get("teams", [])
    reg_weeks    = get_regular_season_weeks(year)
    weekly_scores = year_data.get("weekly_scores", {})
    team_to_owner = {t["name"]: t["owner"] for t in all_teams}

    # ── Weekly game log ────────────────────────────────────────────────────────
    # Pre-compute league average per week
    league_week_avg = {}
    for wk in range(1, reg_weeks + 1):
        scores = []
        for m in weekly_scores.get(str(wk), []):
            if m.get("home_score"): scores.append(float(m["home_score"]))
            if m.get("away_score"): scores.append(float(m["away_score"]))
        if scores:
            league_week_avg[wk] = round(sum(scores) / len(scores), 2)

    games = []
    for wk in range(1, reg_weeks + 1):
        for m in weekly_scores.get(str(wk), []):
            if m["home_team"] == team_name:
                score, opp, opp_score = m["home_score"], m["away_team"], m["away_score"]
            elif m["away_team"] == team_name:
                score, opp, opp_score = m["away_score"], m["home_team"], m["home_score"]
            else:
                continue
            score     = float(score     or 0)
            opp_score = float(opp_score or 0)
            if score > opp_score:   outcome = "W"
            elif score < opp_score: outcome = "L"
            else:                   outcome = "T"
            avg = league_week_avg.get(wk)
            games.append({
                "week": wk, "score": score,
                "opponent": team_to_owner.get(opp, opp), "opponent_score": opp_score,
                "outcome": outcome, "margin": round(score - opp_score, 2),
                "league_avg": avg, "beat_avg": (score > avg) if avg else None,
            })
            break

    valid = [g["score"] for g in games if g["score"]]
    avg_score       = round(sum(valid) / len(valid), 2) if valid else 0
    best_week       = max(games, key=lambda g: g["score"]) if games else None
    worst_week      = min(games, key=lambda g: g["score"]) if games else None
    biggest_win     = max(games, key=lambda g: g["margin"]) if games else None
    closest_loss    = min((g for g in games if g["outcome"] == "L"), key=lambda g: abs(g["margin"]), default=None)
    weeks_beat_avg  = sum(1 for g in games if g.get("beat_avg"))
    league_overall_avg = round(
        sum(league_week_avg.values()) / len(league_week_avg), 2
    ) if league_week_avg else 0

    # ── Luck this season ──────────────────────────────────────────────────────
    # Same definition the all-time luck index uses — see week_luck_verdicts().
    lucky_wins = unlucky_losses = 0
    for wk_str, matchups in weekly_scores.items():
        if int(wk_str) > reg_weeks:
            continue
        for scored_team, verdict in week_luck_verdicts(matchups):
            if scored_team != team_name:
                continue
            if verdict == "lucky":
                lucky_wins += 1
            elif verdict == "unlucky":
                unlucky_losses += 1

    # ── H2H this season ───────────────────────────────────────────────────────
    h2h_year = {}
    for wk_str, matchups in weekly_scores.items():
        if int(wk_str) > reg_weeks:
            continue
        for m in matchups:
            hs = float(m.get("home_score") or 0)
            as_ = float(m.get("away_score") or 0)
            if m["home_team"] == team_name:
                opp = team_to_owner.get(m["away_team"], m["away_team"])
                won, tied = hs > as_, hs == as_
            elif m["away_team"] == team_name:
                opp = team_to_owner.get(m["home_team"], m["home_team"])
                won, tied = as_ > hs, hs == as_
            else:
                continue
            h2h_year.setdefault(opp, {"wins": 0, "losses": 0, "ties": 0})
            if tied:       h2h_year[opp]["ties"]   += 1
            elif won:      h2h_year[opp]["wins"]    += 1
            else:          h2h_year[opp]["losses"]  += 1
    h2h_sorted = sorted(h2h_year.items(),
                        key=lambda x: x[1]["wins"] + x[1]["losses"] + x[1]["ties"],
                        reverse=True)

    # ── Transactions this season ──────────────────────────────────────────────
    owner_txns = [t for t in year_data.get("transactions", [])
                  if owner_name in t.get("adds", {}) or owner_name in t.get("drops", {})]
    txn_counts = {"trades": 0, "waiver_adds": 0, "fa_adds": 0}
    for t in owner_txns:
        tt = t.get("type", "")
        if tt == "trade":                         txn_counts["trades"]      += 1
        elif tt == "waiver":                      txn_counts["waiver_adds"] += len(t.get("adds", {}).get(owner_name, []))
        elif tt == "free_agent":                  txn_counts["fa_adds"]     += len(t.get("adds", {}).get(owner_name, []))

    # ── Standings context ─────────────────────────────────────────────────────
    standings = sorted(all_teams, key=lambda t: t.get("rank", 99))

    # ── Roster ────────────────────────────────────────────────────────────────
    # For a finished season this is the end-of-year roster; for the season in
    # progress it's whoever is on the team right now. Sleeper years also carry
    # per-player scoring, so we can show what each guy actually did here.
    scoring = year_data.get("player_scoring") or {}
    kept = get_current_keepers(year_data).get(owner_name, set())
    roster = []
    for p in team.get("roster", []):
        mine = (scoring.get(str(p.get("player_id"))) or {}).get(owner_name) or {}
        roster.append({
            **p,
            "keeper":  p["name"] in kept,
            "points":  round(mine["points"], 1) if mine.get("points") else None,
            "starts":  mine.get("starts"),
            "best":    mine.get("best"),
        })
    roster.sort(key=lambda p: (POSITION_ORDER.get(p.get("position"), 99),
                               -(p.get("points") or 0), p["name"]))
    roster_points = round(sum(p["points"] or 0 for p in roster), 1)

    return {
        "team": team, "year": year_str,
        "roster": roster,
        "roster_has_scoring": bool(scoring),
        "roster_points": roster_points,
        "season_complete": season_is_complete(year_data),
        "games": games,
        "avg_score": avg_score, "best_week": best_week, "worst_week": worst_week,
        "biggest_win": biggest_win, "closest_loss": closest_loss,
        "weeks_beat_avg": weeks_beat_avg, "total_weeks": len(games),
        "league_overall_avg": league_overall_avg,
        "lucky_wins": lucky_wins, "unlucky_losses": unlucky_losses,
        "net_luck": lucky_wins - unlucky_losses,
        "h2h": h2h_sorted,
        "transactions": owner_txns, "txn_counts": txn_counts,
        "standings": standings,
    }


@app.route('/owner/<string:owner_name>/<int:year>')
def owner_season(owner_name, year):
    detail = get_owner_season_detail(owner_name, year)
    if not detail:
        return "Season not found", 404
    return render_template("owner_season.html", owner_name=owner_name, detail=detail)


@app.route('/owner/<path:owner_name>')
def owner_profile(owner_name):
    if owner_name.lower() == "james mitchell hynes":
        return render_template("owner_hynes.html")
    profile = get_owner_profile(owner_name)
    if profile is None:
        return "Owner not found", 404
    luck = calculate_luck_index()
    owner_luck = luck.get(owner_name, {"lucky_wins": 0, "unlucky_losses": 0, "net_luck": 0})
    txn_stats = get_owner_transaction_stats(owner_name)

    # Latest-season roster with keeper badges (badge only while the kept
    # player is still on this owner's roster — draft history keeps its flags)
    current = None
    latest = max(league_data.keys(), key=int)
    year_data = league_data[latest]
    team = next((t for t in year_data.get("teams", []) if t["owner"] == owner_name), None)
    if team:
        keepers = get_current_keepers(year_data).get(owner_name, set())
        roster = sorted(team.get("roster", []),
                        key=lambda p: (POSITION_ORDER.get(p.get("position"), 8), p.get("name", "")))
        current = {
            "year": latest,
            "team": team,
            "roster": roster,
            "keepers": keepers,
            "in_progress": not season_is_complete(year_data),
        }

    return render_template("owner.html", profile=profile, luck=owner_luck,
                           txn_stats=txn_stats, current=current)


if __name__ == "__main__":
    # host="0.0.0.0" makes the site reachable from other devices on your
    # home network (e.g. your phone) at http://<this-PC's-IP>:5000
    #
    # Jinja caches every template it has rendered, and with debug off it never
    # checks the file again — so editing a template and refreshing showed the
    # old markup until the process was restarted. Only affects this local
    # runner; PythonAnywhere imports `app` directly and never reaches here.
    app.jinja_env.auto_reload = True
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
