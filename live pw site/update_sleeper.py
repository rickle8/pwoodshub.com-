"""
update_sleeper.py — weekly Sleeper data refresh for league_history.json.

- Skips already-complete seasons (no wasted API calls)
- Automatically discovers new seasons (2026, 2027, ...) via user league lookup
- ESPN seasons in the JSON are never touched
- Safe to run as often as you like; idempotent

Shared Sleeper logic (owner maps, season builder) lives in sleeper_common.py.
"""

import json
import os
from datetime import datetime

from sleeper_common import (
    SLEEPER_LEAGUE_ID, KNOWN_SLEEPER_USERNAME,
    safe_get, get_player_names, walk_league_chain, build_season,
)

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "league_history.json")


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

def update():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Starting Sleeper update...")

    # Load existing data (ESPN seasons stay untouched)
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            history = json.load(f)
    else:
        history = {}

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
    processed = skipped = 0

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
    print(f"Done. Processed {processed}, skipped {skipped}. "
          f"All seasons: {', '.join(all_seasons)}")


if __name__ == "__main__":
    update()
