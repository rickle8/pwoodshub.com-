# Peyton Woods League site (www.pwoodshub.com)

Flask site for a 12-team fantasy football league that runs on Sleeper. Hosted
on PythonAnywhere (account `rickle8`, app dir `/home/rickle8/pwoods site`, Python 3.13).
All code lives in `live pw site/`.

## Workflow the owner wants

The owner usually works from their phone and isn't a developer, so:

1. Make the change on the session's branch, restarted from the latest `main`
   (`git fetch origin main && git checkout -B <branch> origin/main`).
2. Run the tests (see below). Check page changes at phone width (~390–500px).
3. **Deploy to the live site** with `deploy.py` (needs `PA_TOKEN`, which the
   owner pastes into chat; never commit it).
4. Commit, push, open a PR, and **merge it yourself**. Don't ask first.
5. Explain what changed in plain language, without jargon.

## Deploying

```
cd "live pw site"
PA_TOKEN=... python3 deploy.py
```

- Uploads code, every template and `static/`, reloads the web app, restarts the
  always-on task, then checks the live pages. It retries on PythonAnywhere's
  API rate limit (about 40 calls a minute), so a full deploy takes about 2 minutes.
- **Never upload** `league_history.json` (the always-on task rewrites it on the
  server every few minutes), `local_config.py` (server secrets),
  `vapid_private.pem` / `push_subscriptions.json` (push alerts), or the chat,
  notes and prefs JSON files (live user data).
- Before overwriting a file, download the live copy and compare it with the repo,
  in case someone edited it on the server.
- The session's network access must allow `www.pythonanywhere.com`.
- To install a Python package on the server, use a one-off scheduled task through
  the PythonAnywhere API (`pip3.13 install --user ...`), then delete the task.
  PythonAnywhere's pyOpenSSL needs `cryptography<45`, so pin to fit
  (see `requirements.txt`).

## Tests

```
cd "live pw site" && python3 -m unittest discover -s tests
```

The push-delivery tests are skipped when `pywebpush` isn't installed. They run
in a venv with the packages from `requirements.txt`. Tests must stay offline;
pages that call Sleeper, FantasyCalc, KTC, ESPN or GitHub aren't in the route
smoke test.

## Map

- `pwoods_site.py`: all routes. `league_data` = `league_history.json` keyed by
  season.
- `consensus.py`: player values blended from Rotowire (via Sleeper), ESPN,
  FantasyPros ECR (via DynastyProcess), FantasyCalc, KeepTradeCut, and
  preseason-only ADP. In season, projections are rest-of-season, and board
  entries carry `weeks`.
- `trade_machine.py`: trade analyzer and Trade Finder (1-for-1 and 2-for-1).
- `keepers.py`, `draft_review.py`, `trades.py`, `lineups.py`, `projections.py`:
  the pages of the same names.
- `live_week.py`: the This Week page (live projections from ESPN's NFL
  scoreboard clock, win odds, weekly positional rankings). Inputs are fetched
  in `pwoods_site.get_live_scoreboard`.
- The current season's `/year/<season>` page is the hub: its lazy tabs load
  feature pages via `?fragment=1` (templates that `{% extends layout %}`).
- `push.py`: web push alerts for new league notes. `static/sw.js` is the
  service worker for the home-screen app. Bump its `VERSION` when you change it.
- Sleeper seasons store the overall pick number and ESPN seasons store the pick
  within the round. Normalize with `(pick - 1) % teams + 1`.
