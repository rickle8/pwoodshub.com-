"""
sleeper_common.py — shared Sleeper config + season-building logic.

Used by both scraper.py (full rebuild) and update_sleeper.py (weekly refresh)
so the owner mappings and season logic live in exactly one place.
"""

import json
import os
import time

import requests

# ── Config ────────────────────────────────────────────────────────────────────
# Known Sleeper league IDs per season. The previous_league_id chain normally
# discovers these automatically, but the link can break (the 2026 league was
# recreated with no link back to 2025), so they're pinned here as a backstop.
KNOWN_SLEEPER_LEAGUES = {
    "2025": "1251040853791625216",
    "2026": "1381069574920740864",
}
SLEEPER_LEAGUE_ID      = KNOWN_SLEEPER_LEAGUES[max(KNOWN_SLEEPER_LEAGUES)]
KNOWN_SLEEPER_USERNAME = "simd"   # used only for forward-discovery of new seasons

USERNAME_TO_OWNER = {
    "simd":           "Sam Dennis",
    "slimelife":      "Malin Craig",
    "rcmoncada":      "Roldan Navarrete",
    "dabears":        "Niall oliver",
    "dabears1994":    "Niall oliver",
    "footballdude":   "Grant Lewis",
    "footballdude69": "Grant Lewis",
    "everyones dad":  "Hugh Ritter",
    "everyonesdad12": "Hugh Ritter",
    "chris nuggy":    "Chris Nguyen",
    "chrisnuggy":     "Chris Nguyen",
    # Logan Duffy renamed himself on Sleeper in 2026; the old handle stays so
    # earlier seasons keep resolving. Sleeper stopped returning `username` in
    # the league users payload, so display_name is now the only key that can
    # match — every rename lands here and has to be added, or the sanity check
    # in update_sleeper.py rejects the rebuild and the site stops updating.
    "doganluffy":     "Logan Duffy",
    "wojmelvina":     "Logan Duffy",
    "calmpalmtree":   "Chris Evans",
    "rickle8":        "Eric Kenney",
    "lebrookj":       "Brook Price",
    "datbeef":        "mitch Reuter",
}

# ── Keepers ───────────────────────────────────────────────────────────────────
# League rule: you can only keep a player you drafted; the 1st keep costs the
# same round as the year before, each later keep 4 rounds earlier, capped at
# round 1. Sleeper doesn't flag these (the commissioner sets the picks
# manually), so build_season auto-detects them: a pick is a keeper when the
# player was on that owner's previous-season roster, was drafted by them that
# year at round R, and the new pick costs exactly R (or R-4 for a repeat keep).
#
# KNOWN_KEEPERS pins confirmed keepers (belt and suspenders for history);
# KEEPER_EXCLUSIONS blocks coincidences the auto-detection would wrongly flag
# (e.g. a DST re-drafted in the same late round two years running).
KNOWN_KEEPERS = {
    "2026": {
        ("Ja'Marr Chase",     "Malin Craig"),
        ("Jahmyr Gibbs",      "Chris Evans"),
        ("Amon-Ra St. Brown", "Roldan Navarrete"),
        ("Bijan Robinson",    "Niall oliver"),
        ("Jonathan Taylor",   "Eric Kenney"),
        ("James Cook",        "Grant Lewis"),
        ("Omarion Hampton",   "Hugh Ritter"),
        ("George Pickens",    "Sam Dennis"),
        ("Brock Bowers",      "Brook Price"),
        ("Bucky Irving",      "Logan Duffy"),
    },
}

KEEPER_EXCLUSIONS = {
    "2026": {
        ("Minnesota Vikings", "mitch Reuter"),  # same-round DST re-draft, not a keep
    },
}


def detect_keepers(draft_picks, prev_season_data, season):
    """Flag keeper picks by applying the league's keeper-pricing rule against
    the previous season's draft and end-of-season rosters. Mutates draft_picks
    in place (sets pick["keeper"] = True). Safe no-op without prior data."""
    if not prev_season_data:
        return
    exclusions = KEEPER_EXCLUSIONS.get(season, set())
    prev_draft = {(p["player"], p["owner"]): (p["round"], bool(p.get("keeper")))
                  for p in prev_season_data.get("draft", [])}
    prev_roster = {t["owner"]: {pl["name"] for pl in t.get("roster", [])}
                   for t in prev_season_data.get("teams", [])}

    for p in draft_picks:
        if p.get("keeper"):
            continue
        key = (p["player"], p["owner"])
        if key in exclusions or key not in prev_draft:
            continue
        if p["player"] not in prev_roster.get(p["owner"], set()):
            continue
        prev_round, was_kept = prev_draft[key]
        if was_kept:
            allowed = {max(1, prev_round - 4)}
        else:
            # prev year's keeper flags may be missing (older data), so accept
            # both the first-keep price and the repeat-keep price
            allowed = {prev_round, max(1, prev_round - 4)}
        if p["round"] in allowed:
            p["keeper"] = True

# Fallback team names for Sleeper users with no team_name in their metadata
OWNER_TEAM_NAMES = {
    "Sam Dennis":       "XXL Orbs",
    "Malin Craig":      "Sad Tipper 🫃🏼",
    "Roldan Navarrete": "Ocean Dan",
    "Niall oliver":     "Current Champ",
    "Grant Lewis":      "Jared's FountainOfDreams",
    "Hugh Ritter":      "Auto Draft Kings",
    "Chris Nguyen":     "Edward Inc.",
    "Logan Duffy":      "Mid Slop",
    "Chris Evans":      "Not Washed Yet",
    "Eric Kenney":      "Emporer Zurg",
    "Brook Price":      "Cis Peyton",
    "mitch Reuter":     "Greasy Guys",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def reg_season_weeks(year):
    """14 weeks from 2021 onward (NFL added 17th game); 13 weeks before."""
    return 14 if int(year) >= 2021 else 13


def safe_get(url, timeout=15):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def resolve_owner(user):
    for key in (user.get("username", ""), user.get("display_name", "")):
        owner = USERNAME_TO_OWNER.get(key.lower().strip())
        if owner:
            return owner
    fallback = user.get("display_name") or user.get("username") or "Unknown"
    print(f"  WARNING: no mapping for '{user.get('username')}' — using '{fallback}'")
    return fallback


_PLAYERS_CACHE = os.path.join(os.path.dirname(__file__), "players_cache.json")
_CACHE_VERSION = 2   # bump to force a rebuild when the stored fields change

# Metadata kept per player — enough to render a full player profile page
# without another trip to the 15MB dump.
_PLAYER_FIELDS = ("team", "number", "age", "height", "weight", "years_exp",
                  "college", "status", "injury_status", "injury_body_part",
                  "depth_chart_position", "depth_chart_order", "search_rank")


def get_player_names(max_age_hours=24):
    """Player id -> {name, position, team, age, ...} map. The full Sleeper
    dump is ~15MB, so it's cached to disk and only re-fetched once a day —
    this is what makes frequent live updates cheap."""
    try:
        if time.time() - os.path.getmtime(_PLAYERS_CACHE) < max_age_hours * 3600:
            with open(_PLAYERS_CACHE, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("_v") == _CACHE_VERSION:
                return cached["players"]
    except (OSError, json.JSONDecodeError, AttributeError, KeyError):
        pass

    print("  Fetching NFL player data from Sleeper (may take a moment)...")
    data = safe_get("https://api.sleeper.app/v1/players/nfl", timeout=120)
    players = {}
    for pid, info in (data or {}).items():
        if not info:
            continue
        name = (info.get("full_name")
                or f"{info.get('first_name','')} {info.get('last_name','')}".strip()
                or pid)
        rec = {"name": name, "position": info.get("position", "?")}
        for f in _PLAYER_FIELDS:
            v = info.get(f)
            if v is not None:
                rec[f] = v
        players[pid] = rec
    try:
        tmp = _PLAYERS_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"_v": _CACHE_VERSION, "players": players}, f)
        os.replace(tmp, _PLAYERS_CACHE)
    except OSError:
        pass
    return players


def find_champion_roster_id(winners_bracket):
    if not winners_bracket:
        return None
    max_round = max((m.get("r", 0) for m in winners_bracket), default=0)
    finals = [m for m in winners_bracket if m.get("r") == max_round]
    if not finals:
        return None
    return min(finals, key=lambda m: m.get("m", 999)).get("w")


def walk_league_chain(latest_id):
    """Follow previous_league_id links from newest to oldest, then add any
    pinned KNOWN_SLEEPER_LEAGUES the chain missed (broken prev links).
    Returns a list of (league_id, season, status) tuples, newest first."""
    chain = []
    seen = set()
    current = latest_id
    while current and current != "0" and current not in seen:
        info = safe_get(f"https://api.sleeper.app/v1/league/{current}")
        chain.append((current, str(info.get("season", "?")), info.get("status", "unknown")))
        seen.add(current)
        prev = info.get("previous_league_id")
        current = prev if prev and prev != "0" else None
    for lid in KNOWN_SLEEPER_LEAGUES.values():
        if lid not in seen:
            info = safe_get(f"https://api.sleeper.app/v1/league/{lid}")
            chain.append((lid, str(info.get("season", "?")), info.get("status", "unknown")))
            seen.add(lid)
    chain.sort(key=lambda x: x[1], reverse=True)
    return chain


# ── Season builder ────────────────────────────────────────────────────────────

def build_season(league_id, player_names, prev_season_data=None):
    """Build the full data dict for one Sleeper season.
    Returns (season, status, data) where data matches the league_history.json
    per-year format (teams, weekly_scores, head_to_head, playoffs, draft,
    transactions). Pass the previous season's data dict to enable automatic
    keeper detection on the draft."""
    league_info        = safe_get(f"https://api.sleeper.app/v1/league/{league_id}")
    season             = str(league_info.get("season", "unknown"))
    status             = league_info.get("status", "unknown")
    settings           = league_info.get("settings") or {}
    playoff_week_start = int(settings.get("playoff_week_start", 15))
    regular_weeks      = min(playoff_week_start - 1, reg_season_weeks(season))
    last_scored        = int(settings.get("last_scored_leg") or 0)
    fetch_through      = max(last_scored, regular_weeks) if last_scored else regular_weeks

    print(f"  Processing {season} (status={status}, scored through wk {last_scored})...", end=" ", flush=True)

    users_list  = safe_get(f"https://api.sleeper.app/v1/league/{league_id}/users") or []
    users_by_id = {u["user_id"]: u for u in users_list}
    rosters     = safe_get(f"https://api.sleeper.app/v1/league/{league_id}/rosters") or []

    roster_owner     = {}
    roster_team_name = {}
    for r in rosters:
        rid  = r["roster_id"]
        user = users_by_id.get(r.get("owner_id"), {})
        roster_owner[rid] = resolve_owner(user)
        user_team_name = (user.get("metadata") or {}).get("team_name")
        roster_team_name[rid] = (
            user_team_name
            or OWNER_TEAM_NAMES.get(roster_owner[rid])
            or user.get("display_name")
            or roster_owner[rid]
        )

    # Fetch all scored/playoff weeks
    raw_matchups = {}
    for week in range(1, fetch_through + 1):
        try:
            raw_matchups[week] = safe_get(
                f"https://api.sleeper.app/v1/league/{league_id}/matchups/{week}") or []
        except Exception:
            raw_matchups[week] = []

    # Regular-season weekly scores + H2H
    weekly_scores = {}
    head_to_head  = {roster_owner[r["roster_id"]]: {} for r in rosters}

    for week in range(1, regular_weeks + 1):
        groups = {}
        for entry in raw_matchups.get(week, []):
            mid = entry.get("matchup_id")
            if mid is not None:
                groups.setdefault(mid, []).append(entry)

        week_results = []
        for pair in groups.values():
            if len(pair) != 2:
                continue
            a, b  = pair
            a_rid = a["roster_id"]
            b_rid = b["roster_id"]
            a_pts = float(a.get("points") or 0)
            b_pts = float(b.get("points") or 0)

            # Skip scheduled-but-unplayed games (both 0) — otherwise an
            # in-progress season records phantom 0-0 ties in the H2H data
            if not a_pts and not b_pts:
                continue

            week_results.append({
                "home_team":  roster_team_name[a_rid],
                "away_team":  roster_team_name[b_rid],
                "home_score": a_pts,
                "away_score": b_pts,
            })

            ao = roster_owner[a_rid]
            bo = roster_owner[b_rid]
            head_to_head[ao].setdefault(bo, {"wins": 0, "losses": 0, "ties": 0})
            head_to_head[bo].setdefault(ao, {"wins": 0, "losses": 0, "ties": 0})
            if a_pts > b_pts:
                head_to_head[ao][bo]["wins"]   += 1
                head_to_head[bo][ao]["losses"] += 1
            elif b_pts > a_pts:
                head_to_head[bo][ao]["wins"]   += 1
                head_to_head[ao][bo]["losses"] += 1
            else:
                head_to_head[ao][bo]["ties"] += 1
                head_to_head[bo][ao]["ties"] += 1

        weekly_scores[str(week)] = week_results

    # ── Per-player scoring for this league ───────────────────────────────────
    # Sleeper's matchup payloads carry each rostered player's weekly points, so
    # this costs no extra API calls. Powers the league-history panel on player
    # pages: who has owned a player and what he actually produced for them.
    # Only weeks Sleeper has actually scored — otherwise a not-yet-played week
    # counts as a 0-point start and wrecks per-start averages.
    #
    # The same loop also keeps the *unaggregated* week-by-week lineups. That
    # detail is what makes bench points, lineup efficiency and "who won this
    # trade" possible — none of it is recoverable once the totals below have
    # been summed. Costs no extra requests, only file size.
    player_scoring = {}
    weekly_lineups = {}
    for week in range(1, last_scored + 1):
        for entry in raw_matchups.get(week, []):
            owner = roster_owner.get(entry.get("roster_id"))
            if not owner:
                continue
            starters = set(entry.get("starters") or [])
            points = {str(pid): round(float(pts or 0), 2)
                      for pid, pts in (entry.get("players_points") or {}).items()}
            if points:
                weekly_lineups.setdefault(str(week), {})[owner] = {
                    # Order matters — Sleeper lists starters by lineup slot.
                    "starters": [str(p) for p in (entry.get("starters") or [])],
                    "points":   points,
                }
            for pid, pts in (entry.get("players_points") or {}).items():
                pts = float(pts or 0)
                rec = player_scoring.setdefault(str(pid), {}).setdefault(
                    owner, {"points": 0.0, "weeks": 0, "starts": 0,
                            "best": 0.0, "best_week": None})
                rec["points"] += pts
                rec["weeks"] += 1
                if pid in starters:
                    rec["starts"] += 1
                    if pts > rec["best"]:
                        rec["best"] = pts
                        rec["best_week"] = week
    for by_owner in player_scoring.values():
        for rec in by_owner.values():
            rec["points"] = round(rec["points"], 2)
            rec["best"] = round(rec["best"], 2)

    # Playoff bracket
    try:
        winners_bracket = safe_get(
            f"https://api.sleeper.app/v1/league/{league_id}/winners_bracket") or []
    except Exception:
        winners_bracket = []
    try:
        losers_bracket = safe_get(
            f"https://api.sleeper.app/v1/league/{league_id}/losers_bracket") or []
    except Exception:
        losers_bracket = []
    champion_rid = find_champion_roster_id(winners_bracket)

    bracket = []
    if winners_bracket:
        rounds = {}
        for m in winners_bracket:
            r = m.get("r")
            if r:
                rounds.setdefault(r, []).append(m)
        for r in sorted(rounds.keys()):
            week = playoff_week_start + r - 1
            w_scores = {e["roster_id"]: float(e.get("points") or 0)
                        for e in raw_matchups.get(week, [])}
            round_matchups = []
            for m in sorted(rounds[r], key=lambda x: x.get("m", 0)):
                t1, t2 = m.get("t1"), m.get("t2")
                if not t1 or not t2:
                    continue
                winner_rid = m.get("w")
                s1 = round(w_scores[t1], 2) if t1 in w_scores else None
                s2 = round(w_scores[t2], 2) if t2 in w_scores else None
                # Skip skeleton matchups Sleeper pre-seeds before the playoffs
                # actually start (no scores, no winner)
                if winner_rid is None and s1 is None and s2 is None:
                    continue
                round_matchups.append({
                    "team1":  roster_team_name.get(t1, f"Team {t1}"),
                    "score1": s1,
                    "team2":  roster_team_name.get(t2, f"Team {t2}"),
                    "score2": s2,
                    "winner": roster_team_name.get(winner_rid) if winner_rid else None,
                })
            if round_matchups:
                bracket.append({"round": r, "matchups": round_matchups})

    # Team records (wins/losses/points pulled from roster settings — always current)
    teams = []
    for r in rosters:
        rid    = r["roster_id"]
        s      = r.get("settings") or {}
        wins   = int(s.get("wins", 0))
        losses = int(s.get("losses", 0))
        ties   = int(s.get("ties", 0))
        pf     = float(s.get("fpts") or 0) + float(s.get("fpts_decimal") or 0) / 100
        pa     = float(s.get("fpts_against") or 0) + float(s.get("fpts_against_decimal") or 0) / 100
        gp     = wins + losses + ties

        roster_players = [
            {"name": player_names[pid]["name"],
             "position": player_names[pid]["position"],
             "player_id": pid}
            for pid in (r.get("players") or [])
            if pid in player_names
        ]

        teams.append({
            "name":               roster_team_name[rid],
            "team_id":            rid,
            "owner":              roster_owner[rid],
            "wins":               wins,
            "losses":             losses,
            "ties":               ties,
            "points_for":         round(pf, 2),
            "points_against":     round(pa, 2),
            "avg_points_for":     round(pf / gp, 1) if gp else 0,
            "avg_points_against": round(pa / gp, 1) if gp else 0,
            "point_differential": round((pf - pa) / gp, 1) if gp else 0,
            "rank":               0,
            "roster":             roster_players,
            "_rid":               rid,
        })

    # ── Final ranks ──────────────────────────────────────────────────────────
    # Bracket placement games define the true final standings: in the winners
    # bracket p=1 is the championship (winner 1st, loser 2nd), p=3 the third-
    # place game, p=5 the fifth-place game. The losers (consolation) bracket
    # continues below the playoff teams: its p=1 decides 7th/8th, and so on.
    final_ranks = {}
    playoff_rids = {m.get(k) for m in winners_bracket for k in ("t1", "t2")} - {None}
    for bracket_matchups, offset in ((winners_bracket, 0),
                                     (losers_bracket, len(playoff_rids))):
        for m in bracket_matchups:
            p, w, l = m.get("p"), m.get("w"), m.get("l")
            if p and w and l:
                final_ranks[w] = offset + p
                final_ranks[l] = offset + p + 1

    teams.sort(key=lambda t: (t["wins"], t["points_for"]), reverse=True)

    if final_ranks:
        # Teams without a placement game fill the remaining slots in record order
        used = set(final_ranks.values())
        open_slots = [i for i in range(1, len(teams) + 1) if i not in used]
        for t in teams:
            rank = final_ranks.get(t["_rid"])
            t["rank"] = rank if rank is not None else open_slots.pop(0)
    else:
        # No completed placement games (mid-season): rank by record,
        # promoting the champion if the bracket already names one.
        for i, t in enumerate(teams):
            t["rank"] = i + 1
        if champion_rid is not None:
            champ = next((t for t in teams if t["_rid"] == champion_rid), None)
            if champ and champ["rank"] != 1:
                r1 = next((t for t in teams if t["rank"] == 1), None)
                if r1:
                    r1["rank"] = champ["rank"]
                champ["rank"] = 1

    for t in teams:
        del t["_rid"]
    teams.sort(key=lambda t: t["rank"])

    # Draft picks
    draft_picks = []
    try:
        drafts = safe_get(f"https://api.sleeper.app/v1/league/{league_id}/drafts") or []
        if drafts:
            draft_id = drafts[0].get("draft_id")
            if draft_id:
                picks = safe_get(f"https://api.sleeper.app/v1/draft/{draft_id}/picks") or []
                season_keepers = KNOWN_KEEPERS.get(season, set())
                for pick in sorted(picks, key=lambda p: (p.get("round") or 0, p.get("pick_no") or 0)):
                    pid  = str(pick.get("player_id") or "")
                    info = player_names.get(pid, {"name": "Unknown", "position": "?"})
                    rid  = pick.get("roster_id")
                    owner = roster_owner.get(rid, "Unknown")
                    keeper = (bool(pick.get("is_keeper"))
                              or (info["name"], owner) in season_keepers)
                    draft_picks.append({
                        "round":     pick.get("round", 0),
                        "pick":      pick.get("pick_no", 0),
                        "player":    info["name"],
                        "position":  info["position"],
                        "owner":     owner,
                        "keeper":    keeper,
                        "player_id": pid or None,
                    })
    except Exception as e:
        print(f"\n    (draft unavailable: {e})", end=" ")

    detect_keepers(draft_picks, prev_season_data, season)

    # Transactions (adds, drops, trades — no FAAB)
    transactions = []
    for week in range(1, fetch_through + 4):
        try:
            txns = safe_get(
                f"https://api.sleeper.app/v1/league/{league_id}/transactions/{week}") or []
        except Exception:
            break
        for t in txns:
            if t.get("status") != "complete":
                continue
            adds_raw  = t.get("adds")  or {}
            drops_raw = t.get("drops") or {}
            adds, drops = {}, {}
            for pid, rid in adds_raw.items():
                owner = roster_owner.get(rid, "Unknown")
                adds.setdefault(owner, []).append(
                    (player_names.get(str(pid)) or {}).get("name", str(pid)))
            for pid, rid in drops_raw.items():
                owner = roster_owner.get(rid, "Unknown")
                drops.setdefault(owner, []).append(
                    (player_names.get(str(pid)) or {}).get("name", str(pid)))
            if not adds and not drops:
                continue
            transactions.append({
                "week":  week,
                "type":  t.get("type"),
                "adds":  adds,
                "drops": drops,
            })

    n_keepers = sum(1 for p in draft_picks if p.get("keeper"))
    print(f"done. ({len(teams)} teams, {len(draft_picks)} draft picks "
          f"[{n_keepers} keepers], {len(bracket)} playoff rounds, {len(transactions)} transactions)")
    return season, status, {
        "status":         status,   # "in_season" seasons don't count as titles yet
        "teams":          teams,
        "weekly_scores":  weekly_scores,
        "head_to_head":   head_to_head,
        "playoffs":       bracket,
        "draft":          draft_picks,
        "transactions":   transactions,
        "player_scoring": player_scoring,
        "weekly_lineups": weekly_lineups,
    }
