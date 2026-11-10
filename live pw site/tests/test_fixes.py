"""
Regression tests for bugs found in the November 2026 audit. Offline.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import player_stats
import sleeper_common
import update_sleeper


class TestRenamedPlayers(unittest.TestCase):

    def test_suffixes_and_punctuation_are_ignored(self):
        n = sleeper_common.norm_name
        self.assertEqual(n("James Cook III"), n("James Cook"))
        self.assertEqual(n("Deebo Samuel Sr."), n("Deebo Samuel"))
        self.assertEqual(n("Amon-Ra St. Brown"), n("AmonRa St Brown"))
        self.assertNotEqual(n("James Cook"), n("James Conner"))

    def test_keeper_survives_a_rename(self):
        prev = {"draft": [{"player": "James Cook", "owner": "Grant", "round": 3}],
                "teams": [{"owner": "Grant", "roster": [{"name": "James Cook"}]}]}
        picks = [{"player": "James Cook III", "owner": "Grant", "round": 3}]
        sleeper_common.detect_keepers(picks, prev, "2099")
        self.assertTrue(picks[0].get("keeper"))


class TestLiveWeek(unittest.TestCase):

    def _build(self, last_scored, status="in_season"):
        """Run build_season against a canned two-team league in week 2."""
        def fake(url, timeout=15):
            if url.endswith("/league/L"):
                return {"season": "2099", "status": status,
                        "settings": {"playoff_week_start": 15,
                                     "last_scored_leg": last_scored}}
            if url.endswith("/users"):
                return [{"user_id": "u1", "display_name": "simd"},
                        {"user_id": "u2", "display_name": "rickle8"}]
            if url.endswith("/rosters"):
                return [{"roster_id": 1, "owner_id": "u1", "settings": {"wins": 1}},
                        {"roster_id": 2, "owner_id": "u2", "settings": {"losses": 1}}]
            if "/matchups/" in url:
                wk = int(url.rsplit("/", 1)[1])
                pts = {1: (100.0, 90.0), 2: (12.5, 30.0)}.get(wk, (0, 0))
                return [{"roster_id": 1, "matchup_id": 1, "points": pts[0]},
                        {"roster_id": 2, "matchup_id": 1, "points": pts[1]}]
            return []
        real = sleeper_common.safe_get
        sleeper_common.safe_get = fake
        try:
            return sleeper_common.build_season("L", {})[2]
        finally:
            sleeper_common.safe_get = real

    def test_unfinished_week_is_kept_out_of_the_results(self):
        data = self._build(last_scored=1)
        self.assertEqual(len(data["weekly_scores"]["1"]), 1)
        self.assertEqual(data["weekly_scores"]["2"], [])
        self.assertEqual(data["live_week"]["week"], 2)
        h2h = data["head_to_head"]["Sam Dennis"]["Eric Kenney"]
        self.assertEqual((h2h["wins"], h2h["losses"]), (1, 0))

    def test_week_counts_once_it_is_final(self):
        data = self._build(last_scored=2)
        self.assertEqual(len(data["weekly_scores"]["2"]), 1)
        self.assertIsNone(data["live_week"])

    def test_moving_the_live_week_out_is_not_a_lost_week(self):
        old = {"weekly_scores": {"1": [{"home_score": 1, "away_score": 2}],
                                 "2": [{"home_score": 1, "away_score": 2}]}}
        new = self._build(last_scored=1)
        self.assertEqual(update_sleeper.played_week_count(old),
                         update_sleeper.played_week_count(new))


class TestPlayerLog(unittest.TestCase):

    def test_game_played_without_a_stat_line_scores_zero(self):
        real_fetch, real_last = player_stats._fetch_grouped, player_stats.last_week_played
        player_stats._fetch_grouped = lambda kind, pid, season: (
            {"1": {"stats": {"gp": 1}, "opponent": "BUF"}} if kind == "stats"
            else {"1": {"stats": {"pts_half_ppr": 4.2}}})
        player_stats.last_week_played = lambda season: 1
        player_stats._cache.clear()
        try:
            row = player_stats.get_player_game_log("x", "1999", "WR")["rows"][0]
        finally:
            player_stats._fetch_grouped, player_stats.last_week_played = real_fetch, real_last
            player_stats._cache.clear()
        self.assertEqual(row["points"], 0)
        self.assertEqual(row["projected"], 4.2)


if __name__ == "__main__":
    unittest.main()
