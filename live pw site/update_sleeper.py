"""
update_sleeper.py — weekly Sleeper data refresh for league_history.json.

- Skips already-complete seasons (no wasted API calls)
- Automatically discovers new seasons (2026, 2027, ...) via user league lookup
- ESPN seasons in the JSON are never touched
- Safe to run as often as you like; idempotent
- Refuses to overwrite a season with an obviously degraded rebuild, and keeps
  dated backups (see validate_season and write_daily_backup below)

Shared Sleeper logic (owner maps, season builder) lives in sleeper_common.py.
"""

import glob
import json
import os
import shutil
import sys
from datetime import datetime

from sleeper_common import (
    SLEEPER_LEAGUE_ID, KNOWN_SLEEPER_USERNAME, USERNAME_TO_OWNER,
    safe_get, get_player_names, walk_league_chain, build_season,
)

# The canonical people. Anything outside this set is either a typo, a changed
# username, or a genuinely new member who needs adding to USERNAME_TO_OWNER.
KNOWN_OWNERS = set(USERNAME_TO_OWNER.values())

BASE        = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(BASE, "league_history.json")
BACKUP_DIR  = os.path.join(BASE, "backups")
BACKUP_KEEP = 14        # ~2MB each, so about 30MB of history


# ── Guarding the data ─────────────────────────────────────────────────────────
# league_history.json is the only copy of this league's past. The Sleeper years
# can be rebuilt from the API, but the ESPN years (2014-2024) came from a league
# that no longer exists in any accessible form — if they are lost, they are lost.
#
# This file gets rewritten every few minutes by live_loop.py, so a single bad
# rebuild would be written straight over the good data. The write itself is
# atomic, so a crash can't corrupt the file; the risk is a *successful* write of
# bad content — Sleeper returning a partial roster payload, or an outage making
# every owner resolve to a fallback name.

def played_week_count(season_data):
    return sum(1 for games in (season_data.get("weekly_scores") or {}).values()
               if any((g.get("home_score") or g.get("away_score")) for g in games))


def validate_season(new_data, old_data, season):
    """Reasons the freshly built `season` looks worse than what we already have.

    Returns a list of human-readable problems; empty means the rebuild is good.
    Every check is one-directional — data is expected to grow over a season, so
    anything that *shrinks* is the signal. A brand-new season has no `old_data`
    to compare against and is always accepted.
    """
    problems = []

    unknown = [t for t in new_data.get("teams", [])
               if t.get("owner") in ("Unknown", "", None)]
    if unknown:
        problems.append(f"{len(unknown)} team(s) have no resolved owner")

    # An owner who changes their Sleeper username falls through resolve_owner's
    # mapping and comes back as their raw display name. Nothing errors — they
    # just quietly become a *new person*, splitting their career in two and
    # taking their head-to-head record with them. Catching it here forces the
    # username into USERNAME_TO_OWNER before the split reaches the file.
    unmapped = sorted({t["owner"] for t in new_data.get("teams", [])
                       if t.get("owner") and t["owner"] not in KNOWN_OWNERS})
    if unmapped:
        problems.append(f"owner name(s) not in USERNAME_TO_OWNER: {', '.join(unmapped)}"
                        f" — add the new username there, or this splits their history")

    if not new_data.get("teams"):
        problems.append("no teams at all")

    if not old_data:
        return problems          # nothing to compare against yet

    old_teams, new_teams = len(old_data.get("teams", [])), len(new_data.get("teams", []))
    if new_teams < old_teams:
        problems.append(f"team count dropped {old_teams} -> {new_teams}")

    old_weeks, new_weeks = played_week_count(old_data), played_week_count(new_data)
    if new_weeks < old_weeks:
        problems.append(f"played weeks dropped {old_weeks} -> {new_weeks}")

    for label, key in (("draft picks", "draft"), ("transactions", "transactions")):
        old_n, new_n = len(old_data.get(key) or []), len(new_data.get(key) or [])
        if old_n and not new_n:
            problems.append(f"{label} vanished ({old_n} -> 0)")

    old_pf = sum(t.get("points_for", 0) for t in old_data.get("teams", []))
    new_pf = sum(t.get("points_for", 0) for t in new_data.get("teams", []))
    if old_pf and new_pf < old_pf * 0.9:
        problems.append(f"total points fell {old_pf:.0f} -> {new_pf:.0f}")

    return problems


def write_daily_backup():
    """Copy the current file aside once a day, keeping the last BACKUP_KEEP.

    Taken *before* this run writes anything, so each backup is a state the site
    was actually serving. Restoring is a plain file copy — no tooling needed,
    which matters when you are restoring under stress.
    """
    if not os.path.exists(OUTPUT_FILE):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dated = os.path.join(BACKUP_DIR,
                         f"league_history.{datetime.now():%Y-%m-%d}.json")
    if not os.path.exists(dated):
        shutil.copy2(OUTPUT_FILE, dated)
        print(f"  Backed up to {os.path.basename(dated)}")

    stale = sorted(glob.glob(os.path.join(BACKUP_DIR, "league_history.*.json")))[:-BACKUP_KEEP]
    for path in stale:
        try:
            os.remove(path)
        except OSError:
            pass


# ── Forward-discovery: find leagues created AFTER our last known one ──────────

def discover_next_league(last_known_id):
    """
    Ask Sleeper for KNOWN_SLEEPER_USERNAME's leagues in the current and next
    calendar year. If any league's previous_league_id == last_known_id, that
    is the newly created successor season — return its league_id.
    Returns last_known_id unchanged if nothing new is found.
    """
    try:
        user    = safe_get(f"https://api.sleeper.app/v1/user/{KNOWN_SLEEPER_USERNAME}")
        user_id = user["user_id"]
        year    = datetime.now().year
        for check_year in [year, year + 1]:
            leagues = safe_get(
                f"https://api.sleeper.app/v1/user/{user_id}/leagues/nfl/{check_year}"
            ) or []
            for league in leagues:
                if league.get("previous_league_id") == last_known_id:
                    new_id = league["league_id"]
                    print(f"  *** New league discovered for {check_year}: {new_id} ***")
                    return new_id
    except Exception as e:
        print(f"  (forward-discovery skipped: {e})")
    return last_known_id


# ── Main ──────────────────────────────────────────────────────────────────────

def update(force=False):
    """Refresh Sleeper seasons in league_history.json.

    `force` accepts a rebuild even when validate_season() objects. Use it after
    checking by hand that the change is real — a league genuinely shrinking from
    12 teams to 10, say.
    """
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Starting Sleeper update...")

    # Load existing data (ESPN seasons stay untouched)
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            history = json.load(f)
    else:
        history = {}

    write_daily_backup()

    # Get the most recent known Sleeper league ID
    # (stored in the JSON after first discovery so we don't hardcode it forever)
    latest_known_id = history.get("_latest_sleeper_id", SLEEPER_LEAGUE_ID)

    # Try to discover a brand-new season (2026, 2027, ...)
    latest_id = discover_next_league(latest_known_id)
    history["_latest_sleeper_id"] = latest_id

    # Walk the previous_league_id chain from latest backward to build full list
    print("  Building league chain...")
    chain = walk_league_chain(latest_id)

    # Process oldest → newest, skipping seasons that are complete + already saved
    player_names = None   # lazy-load only if we actually need to process something
    processed = skipped = rejected = 0

    for league_id, season, status in reversed(chain):
        already_saved = season in history and bool(history[season].get("teams"))

        if status == "complete" and already_saved:
            print(f"  Skipping {season} — complete and already saved.")
            skipped += 1
            continue

        # Don't add a season to the site until its draft has started —
        # otherwise an empty 0-0 season shows up on every page.
        if status == "pre_draft":
            print(f"  Skipping {season} — draft hasn't started yet.")
            skipped += 1
            continue

        # Load player names on first season that needs processing
        if player_names is None:
            player_names = get_player_names()

        # Previous season's data (drafts, rosters) powers keeper auto-detection
        prev_data = history.get(str(int(season) - 1)) if season.isdigit() else None
        season_key, _, data = build_season(league_id, player_names, prev_data)

        problems = validate_season(data, history.get(season_key), season_key)
        if problems and not force:
            # Keep what we already have. The next run tries again, so a
            # transient bad fetch costs nothing but a log line.
            print(f"  !! REJECTED {season_key} — keeping the saved copy:")
            for p in problems:
                print(f"       - {p}")
            print(f"     (re-run with --force if this change is real)")
            rejected += 1
            continue
        if problems:
            print(f"  (--force: accepting {season_key} despite {len(problems)} warning(s))")

        history[season_key] = data
        processed += 1

    if player_names is None:
        print("  Nothing to update — all seasons already complete.")

    # Save atomically so a web request never reads a half-written file
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=4, ensure_ascii=False)
    os.replace(tmp, OUTPUT_FILE)

    all_seasons = sorted(k for k in history if k.isdigit())
    print(f"Done. Processed {processed}, skipped {skipped}, rejected {rejected}. "
          f"All seasons: {', '.join(all_seasons)}")


if __name__ == "__main__":
    update(force="--force" in sys.argv)
