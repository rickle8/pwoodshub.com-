"""
Tests for the This Week page maths (live_week.py) and division standings.
Offline: games, projections and matchups are built by hand.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_week as L


def _espn(state, period=None, clock=None, home="BUF", away="MIA"):
    status = {"type": {"state": state}, "period": period, "displayClock": clock}
    return {"events": [{"date": "2026-09-27T17:00Z", "competitions": [{
        "status": status,
        "competitors": [{"team": {"abbreviation": home}, "score": "10"},
                        {"team": {"abbreviation": away}, "score": "7"}]}]}]}


class TestGames(unittest.TestCase):

    def test_fraction_of_game_left(self):
        self.assertEqual(L.parse_scoreboard(_espn("pre"))["BUF"]["left"], 1.0)
        self.assertEqual(L.parse_scoreboard(_espn("post"))["MIA"]["left"], 0.0)
        mid = L.parse_scoreboard(_espn("in", 3, "7:30"))["BUF"]
        self.assertAlmostEqual(mid["left"], (900 + 450) / 3600, places=3)
        self.assertEqual(mid["label"], "Q3 7:30")

    def test_espn_washington_code_matches_sleeper(self):
        self.assertIn("WAS", L.parse_scoreboard(_espn("pre", home="WSH")))

    def test_kickoff_shown_in_eastern(self):
        self.assertEqual(L.parse_scoreboard(_espn("pre"))["BUF"]["label"], "Sun 1:00 PM")


class TestProjections(unittest.TestCase):

    def test_blend_averages_what_each_source_has(self):
        b = L.blend({"A": {"1": 10.0, "2": 4.0}, "B": {"1": 20.0}})
        self.assertEqual(b["1"]["proj"], 15.0)
        self.assertEqual(b["2"]["proj"], 4.0)

    def test_live_projection_moves_from_projection_to_score(self):
        info = {"name": "P", "position": "WR", "team": "BUF"}
        pre = L.player_line("1", None, 12.0, info, L.parse_scoreboard(_espn("pre")))
        half = L.player_line("1", 8.0, 12.0, info, L.parse_scoreboard(_espn("in", 3, "15:00")))
        done = L.player_line("1", 20.0, 12.0, info, L.parse_scoreboard(_espn("post")))
        self.assertEqual(pre["live"], 12.0)
        self.assertEqual(half["live"], 14.0)          # 8 scored + half of 12 to come
        self.assertEqual(done["live"], 20.0)

    def test_ruled_out_players_project_to_nothing(self):
        info = {"name": "P", "position": "WR", "team": "BUF", "injury_status": "Out"}
        self.assertEqual(L.player_line("1", None, 12.0, info, L.parse_scoreboard(_espn("pre")))["live"], 0)

    def test_no_game_is_a_bye(self):
        line = L.player_line("1", None, 12.0, {"team": "KC"}, L.parse_scoreboard(_espn("pre")))
        self.assertEqual(line["state"], "bye")
        self.assertEqual(line["live"], 0)


class TestWinOdds(unittest.TestCase):

    def test_even_matchup_is_a_coin_flip(self):
        self.assertAlmostEqual(L.win_probability(100, 100, 100, 100), 0.5)

    def test_bigger_projection_is_favoured(self):
        self.assertGreater(L.win_probability(120, 100, 110, 110), 0.7)

    def test_finished_games_are_certain(self):
        self.assertEqual(L.win_probability(101, 100, 0, 0), 1.0)
        self.assertEqual(L.win_probability(99, 100, 0, 0), 0.0)

    def test_lead_matters_more_as_games_finish(self):
        early = L.win_probability(110, 100, 100, 100)
        late = L.win_probability(110, 100, 10, 10)
        self.assertGreater(late, early)


class TestBuildWeek(unittest.TestCase):

    def test_matchup_rankings_and_alerts(self):
        games = L.parse_scoreboard(_espn("pre"))
        players = {"1": {"name": "QB One", "position": "QB", "team": "BUF"},
                   "2": {"name": "QB Two", "position": "QB", "team": "MIA"},
                   "3": {"name": "Hurt", "position": "WR", "team": "BUF", "injury_status": "Out"},
                   "4": {"name": "Free", "position": "QB", "team": "MIA"}}
        proj = L.blend({"S": {"1": 20.0, "2": 15.0, "3": 10.0, "4": 18.0}})
        matchups = [{"matchup_id": 1, "roster_id": 1, "starters": ["1", "3"],
                     "players": ["1", "3"], "players_points": {}, "points": 0},
                    {"matchup_id": 1, "roster_id": 2, "starters": ["2", "0"],
                     "players": ["2"], "players_points": {}, "points": 0}]
        out = L.build_week(matchups, {1: "Ann", 2: "Bob"}, {1: "A", 2: "B"},
                           proj, players, games, lambda a, b: "")
        m = out["matchups"][0]
        self.assertGreater(m["win_a"], 50)
        self.assertEqual(m["win_a"] + m["win_b"], 100)
        self.assertIn("Hurt is Out", m["a"]["alerts"])
        self.assertIn("Empty lineup slot", m["b"]["alerts"])
        qbs = out["rankings"]["QB"]
        self.assertEqual([r["name"] for r in qbs], ["QB One", "Free", "QB Two"])
        self.assertEqual(qbs[0]["owner"], "Ann")
        self.assertIsNone(qbs[1]["owner"])          # nobody rosters him
        self.assertEqual(m["a"]["starters"][0]["week_rank"], "QB1")
        self.assertEqual(m["b"]["starters"][0]["week_rank"], "QB3")


class TestDivisions(unittest.TestCase):

    def test_grouped_and_ordered_by_record(self):
        import pwoods_site as P
        teams = [{"owner": "A", "wins": 1, "losses": 1, "points_for": 200},
                 {"owner": "B", "wins": 2, "losses": 0, "points_for": 150},
                 {"owner": "C", "wins": 1, "losses": 1, "points_for": 250}]
        tables = P.division_standings(teams, {"A": "East", "B": "East", "C": "East"})
        self.assertEqual([t["owner"] for t in tables[0][1]], ["B", "C", "A"])


if __name__ == "__main__":
    unittest.main()
