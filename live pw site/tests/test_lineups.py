"""
Tests for lineup construction, efficiency and trade grading.

best_lineup() is the load-bearing function here: the efficiency pages, the
roster ratings behind the playoff odds, and the trade machine all call it, so a
mistake in slot filling quietly moves numbers on four different pages.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lineups
import trades
import trade_machine
from tests import fixtures as F


class TestBestLineup(unittest.TestCase):

    def test_flex_is_filled_last(self):
        """The reason FLEX resolves last: the tight end is the highest scorer
        on this roster, so a greedy FLEX would take him and leave the TE slot
        empty — costing more than the FLEX gained."""
        total, chosen = lineups.best_lineup(F.LINEUP_POINTS, F.LINEUP_POSITIONS)
        slots = {slot: pid for slot, pid, _ in chosen}
        self.assertEqual(slots["TE"], "te1")
        self.assertEqual(total, F.OPTIMAL_TOTAL)

    def test_flex_takes_the_best_remaining_eligible_player(self):
        flex = sorted(pid for slot, pid, _ in
                      lineups.best_lineup(F.LINEUP_POINTS, F.LINEUP_POSITIONS)[1]
                      if slot == "FLEX")
        self.assertEqual(flex, ["rb3", "wr3"])   # 10 and 9, the best left over

    def test_kicker_is_never_flexed(self):
        """FLEX is RB/WR/TE only. A high-scoring kicker must stay in the K slot
        and never displace a flex-eligible player."""
        points = dict(F.LINEUP_POINTS, k1=99.0)
        _, chosen = lineups.best_lineup(points, F.LINEUP_POSITIONS)
        self.assertNotIn("k1", [pid for slot, pid, _ in chosen if slot == "FLEX"])

    def test_short_roster_does_not_invent_players(self):
        """A roster that can't fill every slot returns only what it can."""
        total, chosen = lineups.best_lineup({"qb1": 20.0}, {"qb1": "QB"})
        self.assertEqual(total, 20.0)
        self.assertEqual(len(chosen), 1)

    def test_empty_roster(self):
        self.assertEqual(lineups.best_lineup({}, {}), (0, []))


class TestWeekEfficiency(unittest.TestCase):

    def _entry(self, starters):
        return {"starters": starters, "points": F.LINEUP_POINTS}

    def test_perfect_lineup_is_100_percent(self):
        optimal = [pid for _, pid, _ in
                   lineups.best_lineup(F.LINEUP_POINTS, F.LINEUP_POSITIONS)[1]]
        eff = lineups.week_efficiency(self._entry(optimal), F.LINEUP_POSITIONS)
        self.assertEqual(eff["efficiency"], 100.0)
        self.assertEqual(eff["left"], 0.0)
        self.assertIsNone(eff["worst_sit"])

    def test_benching_the_best_player_is_reported(self):
        """Start the low-scoring WR instead of the tight end."""
        starters = ["qb1", "rb1", "rb2", "wr1", "wr2", "wr3", "rb3", "k1", "def1"]
        eff = lineups.week_efficiency(self._entry(starters), F.LINEUP_POSITIONS)
        self.assertEqual(eff["worst_sit"], "te1")
        self.assertEqual(eff["worst_sit_pts"], 25.0)
        self.assertLess(eff["actual"], eff["optimal"])
        self.assertGreater(eff["left"], 0)

    def test_empty_starting_slots_are_ignored(self):
        """Sleeper writes "0" into a slot left empty; it isn't a player."""
        eff = lineups.week_efficiency(
            {"starters": ["qb1", "0", ""], "points": F.LINEUP_POINTS},
            F.LINEUP_POSITIONS)
        self.assertEqual(eff["starters"], ["qb1"])

    def test_week_with_no_points_returns_none(self):
        self.assertIsNone(lineups.week_efficiency({"starters": [], "points": {}},
                                                  F.LINEUP_POSITIONS))


class TestSeasonEfficiency(unittest.TestCase):

    def test_playoff_weeks_are_excluded_by_through_week(self):
        """Owners who missed the playoffs have no games in weeks 15+, so
        including those weeks would compare unequal sample sizes."""
        year_data = {"weekly_lineups": {
            "1":  {"Ann": {"starters": ["qb1"], "points": {"qb1": 20.0}}},
            "15": {"Ann": {"starters": ["te1"], "points": {"te1": 25.0}}},
        }}
        eff = lineups.season_efficiency(year_data, F.LINEUP_POSITIONS, through_week=14)
        self.assertEqual(eff["Ann"]["weeks"], 1)
        self.assertEqual(eff["Ann"]["actual"], 20.0)

    def test_perfect_weeks_are_counted(self):
        year_data = {"weekly_lineups": {
            "1": {"Ann": {"starters": ["qb1"], "points": {"qb1": 20.0}}},
        }}
        eff = lineups.season_efficiency(year_data, F.LINEUP_POSITIONS, through_week=14)
        self.assertEqual(eff["Ann"]["perfect"], 1)


class TestTradeGrading(unittest.TestCase):
    """Trades are graded on what each player did *after* the trade week, for
    the owner who received him."""

    def setUp(self):
        self.year_data = {
            "weekly_lineups": {
                # Week 1 is before the trade and must not count.
                "1": {"Ann": {"starters": ["p1"], "points": {"p1": 50.0}},
                      "Bob": {"starters": ["p2"], "points": {"p2": 50.0}}},
                "2": {"Ann": {"starters": ["p2"], "points": {"p2": 30.0}},
                      "Bob": {"starters": ["p1"], "points": {"p1": 10.0}}},
                "3": {"Ann": {"starters": [],     "points": {"p2": 20.0}},
                      "Bob": {"starters": ["p1"], "points": {"p1": 5.0}}},
            },
            "transactions": [{
                "week": 2, "type": "trade",
                "adds":  {"Ann": ["Player Two"], "Bob": ["Player One"]},
                "drops": {"Ann": ["Player One"], "Bob": ["Player Two"]},
            }],
        }
        self.resolve = {"Player One": "p1", "Player Two": "p2"}.get

    def test_points_before_the_trade_are_excluded(self):
        graded = trades.grade_season_trades(self.year_data, self.resolve)
        by_owner = {s["owner"]: s for s in graded[0]["sides"]}
        # Ann started p2 in week 2 (30) but only rostered him in week 3 (20).
        self.assertEqual(by_owner["Ann"]["started"], 30.0)
        self.assertEqual(by_owner["Ann"]["got"][0]["rostered"], 50.0)

    def test_winner_is_the_side_with_more_started_points(self):
        graded = trades.grade_season_trades(self.year_data, self.resolve)
        self.assertEqual(graded[0]["winner"], "Ann")     # 30 started vs Bob's 15
        self.assertEqual(graded[0]["margin"], 15.0)

    def test_close_trades_are_marked_even(self):
        """Under 10 points apart is a wash, not a win."""
        self.year_data["weekly_lineups"]["2"]["Bob"]["points"]["p1"] = 28.0
        graded = trades.grade_season_trades(self.year_data, self.resolve)
        self.assertTrue(graded[0]["even"])

    def test_owner_record_nets_out_across_both_sides(self):
        graded = trades.grade_season_trades(self.year_data, self.resolve)
        rec = trades.owner_trade_record(graded)
        self.assertEqual(rec["Ann"]["net"], -rec["Bob"]["net"])
        self.assertEqual(rec["Ann"]["won"] + rec["Bob"]["won"], 1)

    def test_season_without_lineups_grades_nothing(self):
        """ESPN seasons have no week-by-week rosters, so they can't be graded."""
        self.assertEqual(trades.grade_season_trades({"transactions": []}, self.resolve), [])


class TestTradeMachine(unittest.TestCase):

    def test_swapping_a_surplus_player_for_a_need_helps_both_sides(self):
        """Two rosters with opposite holes both improve — the premise of the
        whole analyzer page.

        A real surplus needs more players than startable slots. RB is eligible
        for 2 RB slots plus both FLEX, so it takes five running backs before the
        fifth is genuinely unstartable; TE reaches its ceiling at four.
        """
        board, ann_roster, bob_roster = {}, [], []
        for i in range(5):
            board[f"rb{i}"] = {"points": 200.0, "position": "RB", "ktc_value": 5000}
            board[f"te{i}"] = {"points": 200.0, "position": "TE", "ktc_value": 5000}
            ann_roster.append({"player_id": f"rb{i}", "position": "RB", "name": f"RB{i}"})
            bob_roster.append({"player_id": f"te{i}", "position": "TE", "name": f"TE{i}"})

        # Ann is all running backs and cannot fill TE; Bob is the mirror image.
        out = trade_machine.evaluate(
            {"owner": "Ann", "sending": ["rb0"], "roster": ann_roster},
            {"owner": "Bob", "sending": ["te0"], "roster": bob_roster},
            board)
        self.assertGreater(out["Ann"]["lineup_delta"], 0)
        self.assertGreater(out["Bob"]["lineup_delta"], 0)

    def test_trading_away_a_starter_for_nothing_useful_hurts(self):
        """The other direction, so the test above isn't just measuring that
        every trade looks good."""
        board = {"rb0": {"points": 200.0, "position": "RB", "ktc_value": 5000},
                 "k0":  {"points": 1.0,   "position": "K",  "ktc_value": 1}}
        out = trade_machine.evaluate(
            {"owner": "Ann", "sending": ["rb0"],
             "roster": [{"player_id": "rb0", "position": "RB", "name": "RB0"}]},
            {"owner": "Bob", "sending": ["k0"],
             "roster": [{"player_id": "k0", "position": "K", "name": "K0"}]},
            board)
        self.assertLess(out["Ann"]["lineup_delta"], 0)

    def test_value_delta_tracks_ktc_not_lineup(self):
        board = {"a": {"points": 10.0, "position": "RB", "ktc_value": 100},
                 "b": {"points": 10.0, "position": "RB", "ktc_value": 900}}
        out = trade_machine.evaluate(
            {"owner": "Ann", "sending": ["a"],
             "roster": [{"player_id": "a", "position": "RB", "name": "A"}]},
            {"owner": "Bob", "sending": ["b"],
             "roster": [{"player_id": "b", "position": "RB", "name": "B"}]},
            board)
        self.assertEqual(out["Ann"]["value_delta"], 800)
        self.assertEqual(out["Bob"]["value_delta"], -800)


if __name__ == "__main__":
    unittest.main()
