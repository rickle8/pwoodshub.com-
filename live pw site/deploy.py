"""
deploy.py — push the site to PythonAnywhere.

Usage (PowerShell, from this folder):

    $env:PA_TOKEN = "your-api-token"
    python deploy.py

Get a token at pythonanywhere.com/user/rickle8/account/#api_token. The token is
read from the environment and never written to disk — don't hardcode it here.

Uploads the app files, reloads the web app, restarts the always-on task (it
imports sleeper_common, so a code change there needs a restart), then checks the
live site actually came back up.
"""
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

USER = "rickle8"
HOST = "https://www.pythonanywhere.com"
REMOTE_DIR = "/home/rickle8/pwoods site"        # the space is real — keep it quoted
DOMAIN = "www.pwoodshub.com"
ALWAYS_ON_ID = 270099
SRC = os.path.dirname(os.path.abspath(__file__))

# Everything the app needs. local_config.py is deliberately absent: the server
# has its own copy with the live secrets and must never be overwritten from here.
FILES = [
    "consensus.py", "projections.py", "player_stats.py", "recaps.py",
    "lineups.py", "trades.py", "trade_machine.py", "keepers.py", "draft_review.py",
    "pwoods_site.py", "sleeper_common.py", "scraper.py", "update_sleeper.py",
    "weekly_recap.py", "live_loop.py",
] + sorted(f"templates/{f}" for f in os.listdir(os.path.join(SRC, "templates"))
           if f.endswith(".html")) + ["static/sw.js"] + sorted(
    f"static/icons/{f}" for f in os.listdir(os.path.join(SRC, "static", "icons")))
# league_history.json is deliberately absent too: the always-on task rewrites
# it on the server every few minutes, so any local copy is already stale and
# uploading it would roll the live standings back.

TOKEN = os.environ.get("PA_TOKEN", "").strip()
if not TOKEN:
    sys.exit("PA_TOKEN is not set.  PowerShell:  $env:PA_TOKEN = \"...\"")
AUTH = {"Authorization": f"Token {TOKEN}"}


def api(method, path, data=None, headers=None, timeout=180):
    req = urllib.request.Request(HOST + path, data=data, method=method,
                                 headers={**AUTH, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.getcode(), r.read()


def upload(rel):
    with open(os.path.join(SRC, rel.replace("/", os.sep)), "rb") as f:
        body = f.read()
    remote = urllib.parse.quote(f"{REMOTE_DIR}/{rel}")
    boundary = "----pwdeploy" + str(int(time.time() * 1000))
    payload = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="content"; '
        f'filename="{os.path.basename(rel)}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + body + f"\r\n--{boundary}--\r\n".encode()
    code, _ = api("POST", f"/api/v0/user/{USER}/files/path{remote}", payload,
                  {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return code, len(body)


def main():
    try:
        code, _ = api("GET", f"/api/v0/user/{USER}/cpu/", timeout=30)
    except urllib.error.HTTPError as e:
        sys.exit(f"Auth check failed ({e.code}). Is PA_TOKEN current and not revoked?")
    print(f"Authenticated as {USER}.  Uploading {len(FILES)} files to '{REMOTE_DIR}'\n")

    missing = [f for f in FILES if not os.path.exists(os.path.join(SRC, f.replace("/", os.sep)))]
    if missing:
        sys.exit(f"Missing locally, aborting before any upload: {missing}")

    failed = []
    for rel in FILES:
        try:
            for attempt in range(4):
                try:
                    code, n = upload(rel)
                    break
                except urllib.error.HTTPError as e:
                    # PythonAnywhere allows ~40 API calls a minute; wait it out.
                    if e.code != 429 or attempt == 3:
                        raise
                    print(f"  ... rate limited, waiting 30s before {rel}")
                    time.sleep(30)
            ok = code in (200, 201)
            print(f"  {'ok ' if ok else 'ERR'} {code}  {rel:<32} {n:>10,} bytes")
            if not ok:
                failed.append(rel)
        except Exception as e:
            print(f"  ERR      {rel:<32} {type(e).__name__}: {str(e)[:60]}")
            failed.append(rel)

    if failed:
        # Half-uploaded code plus a reload is how you serve a broken site.
        sys.exit(f"\n{len(failed)} upload(s) failed — NOT reloading: {failed}")

    print("\nReloading web app…")
    print("  reload ->", api("POST", f"/api/v0/user/{USER}/webapps/{DOMAIN}/reload/", b"")[0])

    print("Restarting always-on task…")
    try:
        print("  restart ->",
              api("POST", f"/api/v0/user/{USER}/always_on/{ALWAYS_ON_ID}/restart/", b"")[0])
    except Exception as e:
        print(f"  restart failed ({type(e).__name__}) — restart task {ALWAYS_ON_ID} "
              f"by hand from the Tasks tab.")

    print("\nVerifying the live site…")
    # Each entry is (path, needle, want). want=False asserts the needle is GONE,
    # which is the only way to catch a removal that silently didn't ship — a
    # positive-only check passes happily against last week's code.
    checks = [("/", "Peyton Woods", True), ("/odds", "roster rating", True),
              ("/trends", "Market Movers", True),
              ("/player/9509", "Consensus Value", True),
              ("/records", "All-Time Owner Ledger", True),
              ("/year/2025", "tab-efficiency", True),
              ("/trade_finder", "Trade Finder", True),
              ("/trades", "Trade History", True),
              ("/keepers", "Keeper Planner", True),
              ("/draft_review", "Draft Grades", True),
              ("/manifest.webmanifest", "PW League", True),
              ("/sw.js", "pw-v", True),
              ("/", 'href="/history"', False)]
    time.sleep(5)
    bad = 0
    for path, needle, want in checks:
        for attempt in range(4):
            try:
                r = urllib.request.urlopen(f"https://{DOMAIN}{path}", timeout=60)
                html = r.read().decode("utf-8", "replace")
                hit = needle.lower() in html.lower()
                ok = hit is want
                label = ("contains" if hit else "MISSING") if want else \
                        ("STILL PRESENT" if hit else "gone")
                print(f"  {r.getcode()} {path:<16} {label} {needle!r}")
                bad += 0 if ok else 1
                break
            except Exception as e:
                if attempt == 3:
                    print(f"  ERR {path:<16} {type(e).__name__}: {str(e)[:60]}")
                    bad += 1
                else:
                    time.sleep(5)
    print("\nDeploy complete." if not bad else f"\nDeployed, but {bad} check(s) failed.")


if __name__ == "__main__":
    main()
