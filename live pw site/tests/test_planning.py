"""
Tests for the planning pages: trade finder, keeper planner and draft review.

All offline — boards and seasons are built by hand, so these pin down the
rules themselves rather than whatever the markets say this week.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import draft_review
import keepers
import trade_machine


def _player(pid, pos, points, value):
    return pid, {"points": points, "position": pos, "ktc_value": value,
                 "mkt_value": value}


def _team(owner, players):
    return {"owner": owner,
            "roster": [{"player_id": pid, "position": b["position"], "name": pid}
                       for pid, b in players]}


class TestTradeFinder(unittest.TestCase):

    def setUp(self):
        # Ann: five good RBs (two RB slots plus two FLEX leaves one spare), a
        # weak TE. Bob: three good TEs, weak RBs.
        # Both have one of everything else so their lineups are legal.
        ann = [_player("a_qb", "QB", 300, 5000),
               _player("a_rb1", "RB", 220, 6000), _player("a_rb2", "RB", 210, 5800),
               _player("a_rb3", "RB", 200, 5600), _player("a_rb4", "RB", 190, 5400),
               _player("a_rb5", "RB", 180, 5000),
               _player("a_wr1", "WR", 150, 4000), _player("a_wr2", "WR", 140, 3800),
               _player("a_te", "TE", 60, 1000)]
        bob = [_player("b_qb", "QB", 300, 5000),
               _player("b_rb1", "RB", 90, 1500), _player("b_rb2", "RB", 80, 1200),
               _player("b_wr1", "WR", 150, 4000), _player("b_wr2", "WR", 140, 3800),
               _player("b_te1", "TE", 190, 5600), _player("b_te2", "TE", 180, 5400),
               _player("b_te3", "TE", 170, 5200)]
        self.board = dict(ann + bob)
        self.teams = [_team("Ann", ann), _team("Bob", bob)]

    def test_finds_a_swap_that_helps_both(self):
        deals = trade_machine.find_trades(self.teams, self.board)
        mutual = [d for d in deals if d["mutual"]]
        self.assertTrue(mutual)
        for d in mutual:
            self.assertGreaterEqual(d["gain_a"], trade_machine.MIN_WEEKLY_GAIN)
            self.assertGreaterEqual(d["gain_b"], trade_machine.MIN_WEEKLY_GAIN)
            self.assertGreaterEqual(d["fairness"], trade_machine.MIN_FAIRNESS)

    def test_rb_for_te_is_the_idea(self):
        best = trade_machine.pick_ideas(trade_machine.find_trades(self.teams, self.board))[0]
        sent = {p["position"] for p in best["a_sends"]} | {p["position"] for p in best["b_sends"]}
        self.assertEqual(sent, {"RB", "TE"})

    def test_two_for_one_must_beat_its_one_for_ones(self):
        deals = trade_machine.find_trades(self.teams, self.board)
        singles = {}
        for d in deals:
            if d["kind"] == "1-for-1" and d["mutual"]:
                singles[(d["a_sends"][0]["id"], d["b_sends"][0]["id"])] = d["score"]
        for d in deals:
            if d["kind"] != "2-for-1":
                continue
            two, one = ((d["a_sends"], d["b_sends"][0]) if len(d["a_sends"]) == 2
                        else (d["b_sends"], d["a_sends"][0]))
            for p in two:
                key = ((p["id"], one["id"]) if len(d["a_sends"]) == 2
                       else (one["id"], p["id"]))
                self.assertGreater(d["score"], singles.get(key, 0))

    def test_consolidation_premium(self):
        """Two decent players are worth less than their raw sum against a star."""
        self.assertLess(trade_machine.package_value([5000, 5000]), 10000)
        self.assertEqual(trade_machine.package_value([9000]), 9000)

    def test_owner_view_is_from_their_side(self):
        deals = trade_machine.find_trades(self.teams, self.board)
        for d in trade_machine.pick_ideas(deals, owner="Bob"):
            self.assertEqual(d["a"], "Bob")

    def test_kickers_and_defenses_are_never_offered(self):
        board = dict(self.board)
        board["a_k"] = {"points": 150, "position": "K", "ktc_value": 9000}
        self.teams[0]["roster"].append({"player_id": "a_k", "position": "K", "name": "K"})
        for d in trade_machine.find_trades(self.teams, board):
            self.assertNotIn("a_k", [p["id"] for p in d["a_sends"] + d["b_sends"]])


class TestKeepers(unittest.TestCase):

    def test_cost_moves_four_rounds_and_caps(self):
        self.assertEqual(keepers.next_cost(9, False), 9)
        self.assertEqual(keepers.next_cost(9, True), 5)
        self.assertEqual(keepers.next_cost(3, True), 1)
        self.assertEqual(keepers.next_cost(1, True), 1)

    def test_only_players_you_drafted_are_keepable(self):
        year = {"draft": [{"round": 3, "pick": 30, "player": "Mine", "owner": "Ann",
                           "player_id": "1"},
                          {"round": 2, "pick": 20, "player": "Theirs", "owner": "Bob",
                           "player_id": "2"}],
                "teams": [{"owner": "Ann", "roster": [
                    {"name": "Mine", "player_id": "1", "position": "RB"},
                    {"name": "Theirs", "player_id": "2", "position": "WR"}]}]}
        opts = keepers.keeper_options(year)
        self.assertEqual([o["name"] for o in opts["Ann"]], ["Mine"])

    def test_overall_pick_is_shown_within_its_round(self):
        """Sleeper stores the overall pick; the page shows 3.06, not 3.30."""
        roster = [{"name": f"P{i}", "player_id": str(i), "position": "RB"}
                  for i in range(12)]
        year = {"draft": [{"round": 3, "pick": 30, "player": "P0", "owner": "T0",
                           "player_id": "0"}],
                "teams": [{"owner": f"T{i}", "roster": roster if i == 0 else []}
                          for i in range(12)]}
        self.assertEqual(keepers.keeper_options(year)["T0"][0]["drafted_pick"], 6)

    def test_matches_on_id_when_names_differ(self):
        year = {"draft": [{"round": 5, "pick": 50, "player": "D.J. Moore",
                           "owner": "Ann", "player_id": "7"}],
                "teams": [{"owner": "Ann", "roster": [
                    {"name": "DJ Moore", "player_id": "7", "position": "WR"}]}]}
        self.assertEqual(len(keepers.keeper_options(year)["Ann"]), 1)

    def test_in_season_slots_follow_position_draft_habits(self):
        """A player now ranked QB2 goes where the 2nd QB went, not where his
        trade value alone would put him."""
        board = {"q1": {"position": "QB", "adp": 40, "rank": 1},
                 "q2": {"position": "QB", "adp": 60, "rank": 2},
                 "q3": {"position": "QB", "adp": 90, "rank": 1.9}}
        slots = keepers.draft_slots(board, use_adp=False)
        self.assertAlmostEqual(slots["q1"], 40)
        self.assertAlmostEqual(slots["q2"], 60)
        self.assertLess(slots["q3"], 60)

    def test_streamed_positions_get_no_verdict(self):
        verdict, _ = keepers.value_verdict({"adp": 20, "cost_round": 18, "position": "DEF"})
        self.assertIsNone(verdict)
        verdict, _ = keepers.value_verdict({"adp": 20, "cost_round": 18, "position": "RB"})
        self.assertEqual(verdict, "steal")


class TestDraftReview(unittest.TestCase):

    def _season(self, pick_style):
        picks, scoring = [], {}
        for rnd in (1, 2):
            for slot in range(1, 5):
                pid = f"{rnd}-{slot}"
                overall = (rnd - 1) * 4 + slot
                picks.append({"round": rnd,
                              "pick": overall if pick_style == "overall" else slot,
                              "player": pid, "player_id": pid, "position": "RB",
                              "owner": f"O{slot}"})
                scoring[pid] = {f"O{slot}": {"points": 100.0 + slot * 10 - rnd * 20}}
        picks.append({"round": 2, "pick": 8, "player": "k", "player_id": "k",
                      "position": "K", "owner": "O4"})
        scoring["k"] = {"O4": {"points": 500.0}}
        return {"draft": picks, "player_scoring": scoring}

    def test_pick_in_round_is_the_same_for_both_formats(self):
        a = draft_review.review_draft(self._season("overall"), lambda n: None)
        b = draft_review.review_draft(self._season("in_round"), lambda n: None)
        self.assertEqual([(r["round"], r["pick"]) for r in a["rows"]],
                         [(r["round"], r["pick"]) for r in b["rows"]])
        self.assertTrue(all(1 <= r["pick"] <= 4 for r in a["rows"]))

    def test_kickers_are_not_graded(self):
        r = draft_review.review_draft(self._season("overall"), lambda n: None)
        self.assertNotIn("k", [s["player_id"] for s in r["steals"]])
        self.assertTrue(all(br["position"] != "K" for br in r["best_by_round"].values()))

    def test_every_owner_gets_a_grade(self):
        r = draft_review.review_draft(self._season("overall"), lambda n: None)
        self.assertEqual(len(r["grades"]), 4)
        self.assertEqual(r["grades"][0]["grade"], "A+")

    def test_no_scoring_means_no_review(self):
        self.assertIsNone(draft_review.review_draft({"draft": [{"round": 1}]}, lambda n: None))


if __name__ == "__main__":
    unittest.main()
