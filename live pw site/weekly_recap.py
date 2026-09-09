"""
weekly_recap.py — auto-posts a weekly awards note to the league Notes page.

Run after each week wraps (a Tuesday-morning scheduled task on the server).
Finds the most recent completed week of the latest season, computes the
awards, and appends a note to league_notes.json as "PW Bot". Idempotent:
skips weeks it has already posted about.

Usage:
    python weekly_recap.py                 post recap for latest complete week
    python weekly_recap.py --dry-run       print the note instead of posting
    python weekly_recap.py --season 2025 --week 14 --dry-run   preview any week
"""

import argparse
import hashlib
import json
import os
import secrets
import sys
import uuid
from datetime import datetime, timezone

from recaps import compute_week_awards, format_recap_text, latest_complete_week

# The recap uses emoji; Windows consoles default to cp1252 and would crash on
# them. Notes are always written to disk as UTF-8 regardless.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(__file__)
HISTORY_FILE    = os.path.join(BASE, "league_history.json")
NOTES_FILE      = os.path.join(BASE, "league_notes.json")
CHAT_USERS_FILE = os.path.join(BASE, "chat_users.json")

BOT_NAME = "PW Bot"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def reserve_bot_username():
    """Claim the bot's chat identity with a random PIN hash so nobody can
    register the name and delete bot notes."""
    users = load_json(CHAT_USERS_FILE, {})
    if BOT_NAME not in users:
        users[BOT_NAME] = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        save_json_atomic(CHAT_USERS_FILE, users)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", help="season year (default: latest)")
    ap.add_argument("--week", type=int, help="week number (default: latest complete)")
    ap.add_argument("--dry-run", action="store_true", help="print instead of posting")
    args = ap.parse_args()

    history = load_json(HISTORY_FILE, {})
    seasons = sorted((k for k in history if k.isdigit()), key=int)
    if not seasons:
        print("No league data.")
        return
    season = args.season or seasons[-1]
    if season not in history:
        print(f"No data for season {season}.")
        return

    week = args.week or latest_complete_week(history[season])
    if not week:
        print(f"No complete weeks in {season} yet — nothing to recap.")
        return

    notes = load_json(NOTES_FILE, {})
    marker = f"WEEK {week} RECAP — {season}"
    already = any(n["text"].startswith(marker) for n in notes.get(season, []))
    if already and not args.dry_run:
        print(f"Week {week} of {season} already recapped — skipping.")
        return

    awards = compute_week_awards(history[season], week)
    if not awards:
        print(f"No games found for {season} week {week}.")
        return
    text = format_recap_text(awards, history[season], season)

    if args.dry_run:
        print("=" * 60)
        print(text)
        print("=" * 60)
        print("(dry run — not posted)")
        return

    reserve_bot_username()
    notes.setdefault(season, []).insert(0, {
        "id":        uuid.uuid4().hex,
        "username":  BOT_NAME,
        "text":      text,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    save_json_atomic(NOTES_FILE, notes)
    print(f"Posted week {week} recap for {season}.")


if __name__ == "__main__":
    main()
