"""
Tests for the derived-statistics layer: luck, playoff records, owner aliases.

These are the calculations nobody eyeballs. A wrong luck index or a dropped
head-to-head record looks entirely plausible on the page, so the only way to
know it's right is to pin it against a fixture whose answers were worked out by
hand (see fixtures.py).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pwoods_site as P
from tests import fixtures as F


class LeagueDataTestCase(unittest.TestCase):
    """Swaps the module-global league_data for the fixture, and puts it back."""

    season_kwargs: dict = {}

    def setUp(self):
        self._real = P.league_data
        P.league_data = F.league(**self.season_kwargs)

    def tearDown(self):
        P.league_data = self._real


class TestWeekLuckVerdicts(unittest.TestCase):
    """The shared luck rule. Both the all-time index and the per-season owner
    page run through this, so its edge cases are worth stating explicitly."""

    def test_top_half_loss_is_unlucky_bottom_half_win_is_lucky(self):
        verdicts = dict(P.week_luck_verdicts(F.WEEK1))
        self.assertEqual(verdicts[F.TEAM_OF["Bob"]], "unlucky")  # 90, top half, lost
        self.assertEqual(verdicts[F.TEAM_OF["Cat"]], "lucky")    # 80, bottom half, won
        self.assertIsNone(verdicts[F.TEAM_OF["Ann"]])            # top half and won
        self.assertIsNone(verdicts[F.TEAM_OF["Dan"]])            # bottom half and lost

    def test_unplayed_game_yields_nothing(self):
        """A scheduled 0-0 must not hand anyone a lucky win.

        This is the bug that used to differ between the two copies of this
        logic: `home_won = hs > as_` is False for 0-0, which the owner page
        then read as an away win.
        """
        teams = [t for t, _ in P.week_luck_verdicts(F.WEEK2)]
        self.assertNotIn(F.TEAM_OF["Cat"], teams)
        self.assertNotIn(F.TEAM_OF["Dan"], teams)

    def test_tie_counts_as_an_away_win(self):
        """Pinned behaviour, not necessarily desirable behaviour.

        `home_won = hs > as_` makes a tie an away win, so a tied game can hand
        out a lucky/unlucky pair: the home side reads as a top-half loser and
        the away side as a bottom-half winner. There has never been a tie in
        this league (0 in 12 seasons, and half-PPR decimals make one almost
        impossible), so this is academic — but it is pinned here so that anyone
        who changes tie handling finds out from a test rather than from the
        luck column looking odd.
        """
        verdicts = dict(P.week_luck_verdicts(F.WEEK2))
        self.assertEqual(verdicts[F.TEAM_OF["Ann"]], "unlucky")  # home side of the tie
        self.assertEqual(verdicts[F.TEAM_OF["Bob"]], "lucky")    # away side of the tie

    def test_week_with_no_scores_is_skipped(self):
        self.assertEqual(list(P.week_luck_verdicts([F._game("Ann", "Bob", 0, 0)])), [])
        self.assertEqual(list(P.week_luck_verdicts([])), [])


class TestLuckIndex(LeagueDataTestCase):

    def test_totals_match_the_per_week_verdicts(self):
        luck = P.calculate_luck_index()
        self.assertEqual(luck["Bob"]["unlucky_losses"], 1)   # week 1: top half, lost
        self.assertEqual(luck["Cat"]["lucky_wins"], 1)       # week 1: bottom half, won
        self.assertEqual(luck["Ann"]["unlucky_losses"], 1)   # week 2: home side of the tie
        self.assertEqual(luck["Bob"]["lucky_wins"], 1)       # week 2: away side of the tie
        self.assertEqual(luck["Cat"]["net_luck"], 1)
        self.assertEqual(luck["Bob"]["net_luck"], 0)         # one of each cancels out

    def test_owner_page_agrees_with_the_all_time_index(self):
        """The two used to be separate copies of the same rule and drifted.
        With one season in the fixture the numbers must match exactly."""
        luck = P.calculate_luck_index()
        for owner in F.OWNERS:
            detail = P.get_owner_season_detail(owner, "2025")
            self.assertEqual(detail["lucky_wins"], luck[owner]["lucky_wins"], owner)
            self.assertEqual(detail["unlucky_losses"], luck[owner]["unlucky_losses"], owner)


class TestPlayoffStatsDuringLivePlayoffs(LeagueDataTestCase):
    """Regression: bracket data exists as soon as a playoff game is scored, but
    the appearance loop only seeds owners from *completed* seasons. A live
    postseason therefore hit an unseeded owner and raised KeyError, taking the
    whole power-rankings page down with it."""

    season_kwargs = {"status": "in_season", "with_playoffs": True}

    def test_does_not_crash(self):
        rows = P.calculate_playoff_stats()          # used to raise KeyError
        self.assertIsInstance(rows, list)

    def test_bracket_result_is_recorded(self):
        by_owner = {r["owner"]: r for r in P.calculate_playoff_stats()}
        self.assertEqual(by_owner["Ann"]["wins"], 1)
        self.assertEqual(by_owner["Bob"]["losses"], 1)

    def test_in_progress_season_awards_no_title(self):
        """A team sitting top of the table in week 2 has not won anything."""
        for row in P.calculate_playoff_stats():
            self.assertEqual(row["championships"], 0)


class TestPlayoffStatsCompletedSeason(LeagueDataTestCase):
    season_kwargs = {"status": "complete", "with_playoffs": True}

    def test_champion_is_credited(self):
        by_owner = {r["owner"]: r for r in P.calculate_playoff_stats()}
        self.assertEqual(by_owner["Ann"]["championships"], 1)


class TestOwnerAliasMerge(unittest.TestCase):
    """Aliasing a duplicated owner name has to rewrite both the owner's own row
    and every opponent's reference to them. Fixing only the outer key leaves the
    table asymmetric and silently drops half the record."""

    def test_alias_is_removed_from_outer_and_inner_keys(self):
        raw = {"2020": {
            "teams": [{"owner": "A  B"}, {"owner": "C"}],
            "head_to_head": {
                "A  B": {"C": {"wins": 2, "losses": 1, "ties": 0}},
                "C": {"A  B": {"wins": 1, "losses": 2, "ties": 0}},
            }}}
        merged = _apply_aliases(raw, {"A  B": "A B"})["2020"]["head_to_head"]

        self.assertNotIn("A  B", merged)
        self.assertNotIn("A  B", merged["C"])
        self.assertEqual(merged["A B"]["C"]["wins"], 2)
        self.assertEqual(merged["C"]["A B"]["wins"], 1)

    def test_records_are_summed_not_replaced(self):
        """When both spellings appear, their games add up rather than one
        overwriting the other."""
        raw = {"2020": {"teams": [], "head_to_head": {
            "C": {"A  B": {"wins": 1, "losses": 0, "ties": 0},
                  "A B": {"wins": 3, "losses": 2, "ties": 1}}}}}
        merged = _apply_aliases(raw, {"A  B": "A B"})["2020"]["head_to_head"]
        self.assertEqual(merged["C"]["A B"], {"wins": 4, "losses": 2, "ties": 1})


def _apply_aliases(raw, name_aliases):
    """Mirror of the normalisation block in load_league_data().

    That block runs inside a file read, so this exercises the same rules against
    an in-memory dict. Keep the two in step.
    """
    for year_data in raw.values():
        for team in year_data.get("teams", []):
            team["owner"] = name_aliases.get(team.get("owner"), team.get("owner"))
        h2h = year_data.get("head_to_head", {})
        for old, canonical in name_aliases.items():
            if old in h2h:
                h2h.setdefault(canonical, {})
                for opp, record in h2h[old].items():
                    h2h[canonical].setdefault(opp, {"wins": 0, "losses": 0, "ties": 0})
                    for k in ("wins", "losses", "ties"):
                        h2h[canonical][opp][k] += record.get(k, 0)
                del h2h[old]
            for opps in h2h.values():
                if old in opps:
                    record = opps.pop(old)
                    merged = opps.setdefault(canonical, {"wins": 0, "losses": 0, "ties": 0})
                    for k in ("wins", "losses", "ties"):
                        merged[k] += record.get(k, 0)
    return raw


class TestSeasonCompleteness(LeagueDataTestCase):
    season_kwargs = {"status": "in_season"}

    def test_in_progress_season_has_no_champion(self):
        self.assertEqual(P.get_champions(), [])

    def test_espn_seasons_without_a_status_count_as_complete(self):
        """Historical ESPN seasons carry no status key at all."""
        self.assertTrue(P.season_is_complete({}))
        self.assertFalse(P.season_is_complete({"status": "in_season"}))


class TestRegularSeasonLength(unittest.TestCase):
    def test_seventeen_game_era_adds_a_week(self):
        self.assertEqual(P.get_regular_season_weeks(2020), 13)
        self.assertEqual(P.get_regular_season_weeks(2021), 14)
        self.assertEqual(P.get_regular_season_weeks("2026"), 14)


if __name__ == "__main__":
    unittest.main()
