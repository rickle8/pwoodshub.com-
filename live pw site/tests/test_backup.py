"""
Tests for the data-safety layer in update_sleeper.py.

This is the code standing between a bad Sleeper response and twelve years of
league history, so it gets tested from both directions: it has to reject the
degraded rebuilds, and — just as importantly — it must not reject the ordinary
ones. A gate that cries wolf gets turned off.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import update_sleeper as U


# Real owner names, so a default season passes the gate cleanly and every test
# below is isolating exactly one problem rather than tripping over the
# unmapped-owner check as well.
REAL_OWNERS = sorted(U.KNOWN_OWNERS)


def season(teams=12, weeks=2, picks=10, txns=5, points=100.0):
    """A plausible season, with knobs for each thing the gate checks."""
    return {
        "status": "in_season",
        "teams": [{"name": f"Team {i}", "owner": REAL_OWNERS[i % len(REAL_OWNERS)],
                   "wins": 1, "losses": 1, "ties": 0,
                   "points_for": points, "points_against": 100.0, "rank": i + 1}
                  for i in range(teams)],
        "weekly_scores": {str(w): [{"home_team": "Team 0", "away_team": "Team 1",
                                    "home_score": 100.0, "away_score": 90.0}]
                          for w in range(1, weeks + 1)},
        "draft": [{"round": 1, "pick": i, "player": f"P{i}"} for i in range(picks)],
        "transactions": [{"week": 1, "type": "free_agent"} for _ in range(txns)],
    }


class TestValidateSeason(unittest.TestCase):

    def test_normal_refresh_is_accepted(self):
        """The common case: another week has been played, more transactions."""
        old, new = season(weeks=2, txns=5), season(weeks=3, txns=8)
        self.assertEqual(U.validate_season(new, old, "2026"), [])

    def test_brand_new_season_is_accepted(self):
        """No prior data to compare against — an empty season is normal in
        August and must not be blocked."""
        self.assertEqual(U.validate_season(season(weeks=0, picks=0, txns=0),
                                           None, "2027"), [])

    def test_unresolved_owners_are_rejected(self):
        """resolve_owner() only falls back to 'Unknown' when the users endpoint
        gave us nothing — a strong signal the fetch was broken."""
        bad = season()
        bad["teams"][3]["owner"] = "Unknown"
        self.assertTrue(any("owner" in p for p in U.validate_season(bad, season(), "2026")))

    def test_unmapped_owner_name_is_rejected(self):
        """An owner changing their Sleeper username resolves to their raw
        display name instead. Nothing errors — they silently become a second
        person and their career splits in two."""
        bad = season()
        bad["teams"][0]["owner"] = "somenewhandle99"
        problems = U.validate_season(bad, season(), "2026")
        self.assertTrue(any("USERNAME_TO_OWNER" in p for p in problems))

    def test_real_owner_names_pass(self):
        """Sanity check against the actual mapping, so the gate can't be
        strict in a way that blocks every ordinary update."""
        real = season(teams=len(U.KNOWN_OWNERS))
        for team, owner in zip(real["teams"], sorted(U.KNOWN_OWNERS)):
            team["owner"] = owner
        self.assertEqual(U.validate_season(real, None, "2026"), [])

    def test_unknown_owner_message_counts_teams_not_names(self):
        bad = season(teams=12)
        for team in bad["teams"]:
            team["owner"] = "Unknown"
        problems = U.validate_season(bad, season(), "2026")
        self.assertTrue(any("12 team(s)" in p for p in problems))

    def test_shrinking_team_count_is_rejected(self):
        problems = U.validate_season(season(teams=8), season(teams=12), "2026")
        self.assertTrue(any("team count" in p for p in problems))

    def test_losing_played_weeks_is_rejected(self):
        problems = U.validate_season(season(weeks=1), season(weeks=5), "2026")
        self.assertTrue(any("played weeks" in p for p in problems))

    def test_vanished_draft_or_transactions_are_rejected(self):
        problems = U.validate_season(season(picks=0, txns=0), season(), "2026")
        self.assertTrue(any("draft picks" in p for p in problems))
        self.assertTrue(any("transactions" in p for p in problems))

    def test_collapsed_scoring_is_rejected(self):
        """Points only ever go up during a season."""
        problems = U.validate_season(season(points=10.0), season(points=100.0), "2026")
        self.assertTrue(any("total points" in p for p in problems))

    def test_small_scoring_wobble_is_tolerated(self):
        """Sleeper does re-score games after stat corrections, so the threshold
        has slack. A 5% dip must not trip the gate."""
        self.assertEqual(U.validate_season(season(points=95.0), season(points=100.0),
                                           "2026"), [])

    def test_empty_rebuild_is_rejected(self):
        problems = U.validate_season({"teams": []}, season(), "2026")
        self.assertTrue(any("no teams" in p for p in problems))

    def test_played_week_count_ignores_scheduled_but_unplayed_weeks(self):
        data = {"weekly_scores": {
            "1": [{"home_score": 100.0, "away_score": 90.0}],
            "2": [{"home_score": 0, "away_score": 0}],
            "3": [],
        }}
        self.assertEqual(U.played_week_count(data), 1)


class TestDailyBackup(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pwbackup-")
        self._saved = (U.OUTPUT_FILE, U.BACKUP_DIR, U.BACKUP_KEEP)
        U.OUTPUT_FILE = os.path.join(self.tmp, "league_history.json")
        U.BACKUP_DIR = os.path.join(self.tmp, "backups")
        with open(U.OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump({"2025": {"teams": []}}, f)

    def tearDown(self):
        U.OUTPUT_FILE, U.BACKUP_DIR, U.BACKUP_KEEP = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def backups(self):
        return sorted(os.listdir(U.BACKUP_DIR)) if os.path.isdir(U.BACKUP_DIR) else []

    @staticmethod
    def backup():
        """write_daily_backup() logs to stdout; keep the test output clean."""
        with contextlib.redirect_stdout(io.StringIO()):
            U.write_daily_backup()

    def test_creates_a_dated_copy(self):
        self.backup()
        self.assertEqual(self.backups(),
                         [f"league_history.{datetime.now():%Y-%m-%d}.json"])

    def test_backup_matches_the_source(self):
        self.backup()
        with open(os.path.join(U.BACKUP_DIR, self.backups()[0]), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"2025": {"teams": []}})

    def test_running_again_the_same_day_does_not_overwrite(self):
        """live_loop calls update() every few minutes. The backup must stay the
        state from the first run of the day, not get refreshed to match whatever
        was just written."""
        self.backup()
        with open(U.OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump({"CHANGED": True}, f)
        self.backup()

        self.assertEqual(len(self.backups()), 1)
        with open(os.path.join(U.BACKUP_DIR, self.backups()[0]), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"2025": {"teams": []}})

    def test_old_backups_are_pruned(self):
        os.makedirs(U.BACKUP_DIR, exist_ok=True)
        for day in range(1, 21):
            open(os.path.join(U.BACKUP_DIR,
                              f"league_history.2026-01-{day:02d}.json"), "w").close()
        U.BACKUP_KEEP = 5
        self.backup()
        self.assertEqual(len(self.backups()), 5)
        # Dated filenames sort chronologically, so the survivors are the newest.
        self.assertIn(f"league_history.{datetime.now():%Y-%m-%d}.json", self.backups())

    def test_missing_source_file_is_not_an_error(self):
        os.remove(U.OUTPUT_FILE)
        self.backup()          # must not raise
        self.assertEqual(self.backups(), [])


if __name__ == "__main__":
    unittest.main()
