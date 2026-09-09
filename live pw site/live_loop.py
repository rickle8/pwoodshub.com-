"""
live_loop.py — always-on task for PythonAnywhere.

Continuously refreshes league_history.json from Sleeper:
  - every 5 minutes during NFL game windows (Thu/Sun/Mon nights US time)
  - every 30 minutes the rest of the week

Player data is cached daily (see sleeper_common.get_player_names) so each
run is only ~40 small API calls. Run with `python3 -u` so prints reach the
always-on task log immediately.
"""

import time
import traceback
from datetime import datetime, timezone

import update_sleeper


def in_game_window():
    """True during typical NFL game windows. PythonAnywhere runs on UTC:
    Thu night ET = Fri 00-05 UTC, Sunday slate = Sun 16 UTC through
    Mon 05 UTC, Mon night ET = Tue 00-05 UTC."""
    now = datetime.now(timezone.utc)
    wd, hr = now.weekday(), now.hour  # Monday=0
    if wd == 4 and hr < 5:
        return True            # Thursday night game
    if wd == 6 and hr >= 16:
        return True            # Sunday afternoon + night
    if wd == 0 and hr < 5:
        return True            # tail of Sunday night game
    if wd == 1 and hr < 5:
        return True            # Monday night game
    return False


def main():
    print("live_loop started", flush=True)
    while True:
        try:
            update_sleeper.update()
        except Exception:
            traceback.print_exc()
        delay = 300 if in_game_window() else 1800
        print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC] "
              f"sleeping {delay // 60} min...", flush=True)
        time.sleep(delay)


if __name__ == "__main__":
    main()
