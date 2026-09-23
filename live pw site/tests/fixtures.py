"""
A small synthetic league, built by hand so the expected answers are known.

The real league_history.json changes every time the updater runs, so it can't
be used to pin exact numbers. Everything here is deliberately tiny — four
teams, a couple of weeks — and every score is chosen to make one specific rule
observable (a tie, an unplayed game, a top-half loss).

Shape matches what sleeper_common.build_season() produces; see that function
if you need to add a field.
"""

# Four owners is the smallest league where "top half of the week" is
# meaningful: four scores split cleanly into two and two.
OWNERS = ["Ann", "Bob", "Cat", "Dan"]
TEAM_OF = {"Ann": "Aces", "Bob": "Bears", "Cat": "Cubs", "Dan": "Ducks"}


def _team(owner, wins, losses, points_for, rank, roster=None, ties=0):
    return {
        "name": TEAM_OF[owner], "team_id": OWNERS.index(owner) + 1,
        "owner": owner, "wins": wins, "losses": losses, "ties": ties,
        "points_for": points_for, "points_against": 0.0,
        "avg_points_for": 0.0, "avg_points_against": 0.0,
        "point_differential": 0.0, "rank": rank,
        "roster": roster or [],
    }


def _game(home, away, hs, as_):
    return {"home_team": TEAM_OF[home], "away_team": TEAM_OF[away],
            "home_score": hs, "away_score": as_}


# ── Week 1 ────────────────────────────────────────────────────────────────────
# Scores 100 / 90 / 80 / 70, so the top half is Ann and Bob.
#   Ann 100 beats Bob 90   -> Ann top+won (nothing), Bob top+lost  -> UNLUCKY
#   Cat  80 beats Dan 70   -> Cat bottom+won -> LUCKY, Dan bottom+lost (nothing)
WEEK1 = [_game("Ann", "Bob", 100.0, 90.0), _game("Cat", "Dan", 80.0, 70.0)]

# ── Week 2 ────────────────────────────────────────────────────────────────────
# A tie, and a game that was scheduled but never played (0-0). The 0-0 game must
# contribute nothing at all — it used to be scored as a lucky win for the away
# team on the owner-season page.
WEEK2 = [_game("Ann", "Bob", 95.0, 95.0), _game("Cat", "Dan", 0.0, 0.0)]


def season(status="complete", with_playoffs=False):
    """One synthetic season. `status` of 'in_season' marks it incomplete."""
    data = {
        "status": status,
        "teams": [
            _team("Ann", 1, 0, 195.0, 1, roster=[
                {"name": "Quarterback One", "position": "QB", "player_id": "qb1"},
                {"name": "Tight End One",   "position": "TE", "player_id": "te1"},
            ]),
            _team("Bob", 0, 1, 185.0, 2),
            _team("Cat", 1, 0, 80.0, 3),
            _team("Dan", 0, 1, 70.0, 4),
        ],
        "weekly_scores": {"1": WEEK1, "2": WEEK2},
        "head_to_head": {
            "Ann": {"Bob": {"wins": 1, "losses": 0, "ties": 1}},
            "Bob": {"Ann": {"wins": 0, "losses": 1, "ties": 1}},
            "Cat": {"Dan": {"wins": 1, "losses": 0, "ties": 0}},
            "Dan": {"Cat": {"wins": 0, "losses": 1, "ties": 0}},
        },
        "playoffs": [],
        "draft": [
            {"round": 1, "pick": 1, "player": "Quarterback One", "position": "QB",
             "owner": "Ann", "keeper": False, "player_id": "qb1"},
            {"round": 2, "pick": 5, "player": "Tight End One", "position": "TE",
             "owner": "Ann", "keeper": False, "player_id": "te1"},
        ],
        "transactions": [
            {"week": 1, "type": "free_agent", "adds": {"Bob": ["Somebody"]}, "drops": {}},
            {"week": 2, "type": "trade",
             "adds": {"Ann": ["Tight End One"], "Bob": ["Quarterback One"]},
             "drops": {"Bob": ["Tight End One"], "Ann": ["Quarterback One"]}},
        ],
        "player_scoring": {},
        "weekly_lineups": {},
    }
    if with_playoffs:
        # A single played bracket game. When the season is still 'in_season'
        # this is the exact shape that used to crash calculate_playoff_stats().
        data["playoffs"] = [{"round": 1, "matchups": [
            {"team1": TEAM_OF["Ann"], "score1": 120.0,
             "team2": TEAM_OF["Bob"], "score2": 98.0, "winner": TEAM_OF["Ann"]},
        ]}]
    return data


def league(**kwargs):
    """A whole league_data dict: one season, keyed by year like the real thing."""
    return {"2025": season(**kwargs)}


# ── Lineup fixtures ───────────────────────────────────────────────────────────
# The tight end is the highest scorer on the roster. If FLEX were filled before
# the fixed slots it would swallow him and leave the TE slot empty, which is the
# whole reason lineups.py resolves FLEX last.
LINEUP_POINTS = {
    "qb1": 20.0, "rb1": 15.0, "rb2": 12.0, "rb3": 10.0,
    "wr1": 14.0, "wr2": 11.0, "wr3": 9.0,
    "te1": 25.0, "k1": 8.0, "def1": 7.0,
}
LINEUP_POSITIONS = {
    "qb1": "QB", "rb1": "RB", "rb2": "RB", "rb3": "RB",
    "wr1": "WR", "wr2": "WR", "wr3": "WR",
    "te1": "TE", "k1": "K", "def1": "DEF",
}
# QB20 + RB15 + RB12 + WR14 + WR11 + TE25 + K8 + DEF7 + FLEX(rb3 10) + FLEX(wr3 9)
OPTIMAL_TOTAL = 131.0
