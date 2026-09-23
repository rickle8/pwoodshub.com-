"""
live_week.py — the maths behind the This Week page.

Everything is pure: pwoods_site.py fetches the inputs (Sleeper matchups, the
two weekly projections, ESPN's NFL scoreboard) and hands them in, so this can
be tested without the network.

Live projection
    A player's live projection is what he has already scored plus his
    pre-game projection scaled by how much of his NFL game is left. Before
    kickoff that's just the projection; at the final whistle it's his score.

Win probability
    Each team's final score is treated as normally distributed around its
    live projection. The spread shrinks as games finish: a full week of
    fantasy football has a standard deviation of roughly 26 points per team,
    and what's left of that scales with the square root of the projected
    points still to come.
"""

import math
from datetime import datetime, timezone

GAME_SECONDS = 60 * 60
TEAM_SD = 26.0            # typical spread of a team's weekly score
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
RANK_DEPTH = {"QB": 32, "RB": 60, "WR": 72, "TE": 32, "K": 24, "DEF": 24}
# ESPN and Sleeper disagree on one team code.
ESPN_TEAM_FIX = {"WSH": "WAS"}
OUT_STATUSES = {"Out", "IR", "PUP", "Suspended", "NFI", "Doubtful"}


# ── NFL games ────────────────────────────────────────────────────────────────

def parse_scoreboard(espn):
    """{team: game} from ESPN's NFL scoreboard JSON.

    Each game: state (pre/in/post), left (fraction of the game remaining),
    label (kickoff time, clock, or Final), opponent and kickoff (ISO time).
    """
    games = {}
    for ev in (espn or {}).get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        status = comp.get("status") or ev.get("status") or {}
        state = (status.get("type") or {}).get("state", "pre")
        teams = [ESPN_TEAM_FIX.get(c["team"]["abbreviation"], c["team"]["abbreviation"])
                 for c in comp.get("competitors") or [] if c.get("team")]
        scores = {ESPN_TEAM_FIX.get(c["team"]["abbreviation"], c["team"]["abbreviation"]):
                  c.get("score") for c in comp.get("competitors") or [] if c.get("team")}
        kickoff = ev.get("date") or ""
        if state == "post":
            left, label = 0.0, "Final"
        elif state == "in":
            left = _fraction_left(status.get("period"), status.get("displayClock"))
            period = status.get("period") or 1
            label = (f"Q{period} {status.get('displayClock', '')}" if period <= 4
                     else "OT")
            if (status.get("type") or {}).get("name") == "STATUS_HALFTIME":
                label = "Half"
        else:
            left, label = 1.0, _kickoff_label(kickoff)
        for t in teams:
            opp = next((o for o in teams if o != t), "")
            games[t] = {"state": state, "left": round(left, 3), "label": label,
                        "opponent": opp, "kickoff": kickoff,
                        "score": f"{scores.get(t, '')}-{scores.get(opp, '')}"
                                 if state != "pre" else ""}
    return games


def _fraction_left(period, clock):
    try:
        period = int(period or 1)
        m, s = (clock or "15:00").split(":")
        secs = int(m) * 60 + int(float(s))
    except (ValueError, AttributeError):
        return 0.5
    if period > 4:
        return 0.02                      # overtime: nearly done
    return max(0.0, min(1.0, ((4 - period) * 900 + secs) / GAME_SECONDS))


def _kickoff_label(iso):
    """'Sun 1:00 PM' in US Eastern — the time the league actually thinks in."""
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    # Eastern is UTC-4 for the whole regular season except its last weeks.
    offset = 5 if (t.month, t.day) >= (11, 2) else 4
    local = datetime.fromtimestamp(t.timestamp() - offset * 3600, timezone.utc)
    return local.strftime("%a %-I:%M %p")


# ── Projections ──────────────────────────────────────────────────────────────

def blend(sources):
    """{pid: {"proj": average, "by": {source: pts}}} from {source: {pid: pts}}."""
    out = {}
    for name, proj in sources.items():
        for pid, pts in (proj or {}).items():
            if pts is None:
                continue
            out.setdefault(pid, {})[name] = pts
    return {pid: {"proj": round(sum(by.values()) / len(by), 2), "by": by}
            for pid, by in out.items()}


def player_line(pid, points, proj, info, games):
    """One player this week: score, projection, live projection, game."""
    team = (info or {}).get("team") or (pid if not pid.isdigit() else "")
    game = games.get(team)
    injury = (info or {}).get("injury_status")
    if game is None:
        # No game this week for his team: a bye (or a free agent with no team).
        left, label, state = 0.0, "BYE" if team else "—", "bye"
    else:
        left, label, state = game["left"], game["label"], game["state"]
    base = proj if proj is not None else 0.0
    if state == "pre" and injury in OUT_STATUSES:
        base = 0.0                       # won't play: count on nothing
    live = (points or 0.0) + base * left
    return {
        "id": pid, "name": (info or {}).get("name", pid),
        "pos": (info or {}).get("position", "?"), "team": team,
        "points": round(points or 0.0, 2), "proj": round(base, 1),
        "live": round(live, 1), "left": left, "state": state, "game": label,
        "opp": game["opponent"] if game else "", "injury": injury,
    }


def win_probability(live_a, live_b, rem_a, rem_b):
    """Chance team A wins, from live projections and projected points still to come."""
    sd_a = TEAM_SD * math.sqrt(max(rem_a, 0) / 110.0)
    sd_b = TEAM_SD * math.sqrt(max(rem_b, 0) / 110.0)
    sd = math.sqrt(sd_a ** 2 + sd_b ** 2)
    diff = live_a - live_b
    if sd < 0.5:
        return 1.0 if diff > 0 else 0.0 if diff < 0 else 0.5
    return 0.5 * (1 + math.erf(diff / (sd * math.sqrt(2))))


# ── The page ─────────────────────────────────────────────────────────────────

def build_week(matchups, owners, team_names, projections, players, games, h2h_line):
    """Everything the This Week page shows.

    matchups     Sleeper's /matchups/{week} list
    owners       roster_id -> owner, team_names roster_id -> team name
    projections  {pid: {"proj", "by"}} from blend()
    players      Sleeper player cache {pid: {name, position, team, injury_status}}
    games        parse_scoreboard() output
    h2h_line     f(owner_a, owner_b) -> lifetime record text
    """
    by_mid, owner_of = {}, {}
    for m in matchups or []:
        if m.get("matchup_id") is not None:
            by_mid.setdefault(m["matchup_id"], []).append(m)
        for pid in m.get("players") or []:
            owner_of[str(pid)] = owners.get(m.get("roster_id"), "?")

    # This week's rank at his position ("WR1"), over everyone projected, so a
    # lineup can show how each starter stacks up league-wide.
    week_rank = {}
    by_pos = {}
    for pid, p in projections.items():
        pos = (players.get(pid) or {}).get("position")
        if pos in POSITIONS and p["proj"] > 0:        # ruled out: no rank
            by_pos.setdefault(pos, []).append((p["proj"], pid))
    for pos, rows in by_pos.items():
        for i, (_, pid) in enumerate(sorted(rows, reverse=True), 1):
            week_rank[pid] = f"{pos}{i}"

    def line(pid, pts_map):
        pid = str(pid)
        p = projections.get(pid) or {}
        out = player_line(pid, pts_map.get(pid), p.get("proj"), players.get(pid), games)
        out["by"] = p.get("by") or {}
        out["week_rank"] = week_rank.get(pid)
        return out

    def team(m):
        pts_map = {str(k): v for k, v in (m.get("players_points") or {}).items()}
        slots = m.get("starters") or []
        starters, alerts = [], []
        for pid in slots:
            if not pid or str(pid) == "0":
                starters.append({"id": None, "name": "Empty slot", "pos": "", "team": "",
                                 "points": 0, "proj": 0, "live": 0, "left": 0,
                                 "state": "empty", "game": "", "opp": "", "injury": None,
                                 "by": {}})
                alerts.append("Empty lineup slot")
                continue
            ln = line(pid, pts_map)
            starters.append(ln)
            if ln["state"] == "bye":
                alerts.append(f"{ln['name']} is on bye")
            elif ln["state"] == "pre" and ln["injury"] in OUT_STATUSES:
                alerts.append(f"{ln['name']} is {ln['injury']}")
        start_ids = {str(p) for p in slots}
        bench = [line(pid, pts_map) for pid in (m.get("players") or [])
                 if str(pid) not in start_ids]
        bench.sort(key=lambda b: -(b["points"] or b["proj"]))
        points = round(float(m.get("points") or 0), 2)
        live = round(sum(s["live"] for s in starters), 1)
        remaining = sum(s["proj"] * s["left"] for s in starters)
        return {"roster_id": m.get("roster_id"),
                "owner": owners.get(m.get("roster_id"), "?"),
                "team": team_names.get(m.get("roster_id"), "?"),
                "points": points, "live": live, "remaining": round(remaining, 1),
                "proj": round(sum(s["proj"] for s in starters), 1),
                "starters": starters, "bench": bench, "alerts": alerts,
                "bench_points": round(sum(b["points"] for b in bench), 2),
                "yet_to_play": sum(1 for s in starters if s["state"] == "pre"),
                "playing": sum(1 for s in starters if s["state"] == "in")}

    matches = []
    for mid in sorted(by_mid):
        pair = by_mid[mid]
        if len(pair) != 2:
            continue
        a, b = team(pair[0]), team(pair[1])
        pa = win_probability(a["live"], b["live"], a["remaining"], b["remaining"])
        matches.append({"a": a, "b": b, "win_a": round(pa * 100),
                        "win_b": 100 - round(pa * 100),
                        "h2h": h2h_line(a["owner"], b["owner"]),
                        "margin": round(abs(a["live"] - b["live"]), 1)})

    # Rankings: every player with a projection, by position, with who has him.
    rankings = {pos: [] for pos in POSITIONS}
    for pid, p in projections.items():
        info = players.get(pid) or {}
        pos = info.get("position")
        if pos not in rankings:
            continue
        ln = player_line(pid, None, p["proj"], info, games)
        ln["by"] = p["by"]
        ln["owner"] = owner_of.get(pid)
        rankings[pos].append(ln)
    live_points = {}
    for m in matchups or []:
        for pid, pts in (m.get("players_points") or {}).items():
            live_points[str(pid)] = pts
    for pos, rows in rankings.items():
        rows.sort(key=lambda r: -r["proj"])
        del rows[RANK_DEPTH[pos]:]
        for i, r in enumerate(rows, 1):
            r["rank"] = i
            if r["id"] in live_points:
                r["points"] = round(live_points[r["id"]], 2)

    # Leaders: this week's best performances on league rosters.
    everyone = [dict(s, owner=t["owner"], started=True)
                for mt in matches for t in (mt["a"], mt["b"]) for s in t["starters"] if s["id"]]
    everyone += [dict(bn, owner=t["owner"], started=False)
                 for mt in matches for t in (mt["a"], mt["b"]) for bn in t["bench"]]
    scored = [p for p in everyone if p["points"]]
    top = sorted(scored, key=lambda p: -p["points"])[:10]
    beat = sorted((p for p in scored if p["state"] == "post" and p["proj"]),
                  key=lambda p: -(p["points"] - p["proj"]))[:5]
    bench = sorted(({"owner": t["owner"], "team": t["team"], "points": t["bench_points"]}
                    for mt in matches for t in (mt["a"], mt["b"])),
                   key=lambda r: -r["points"])

    # Each game is listed once per team, so halve the count.
    live_games = sum(1 for g in games.values() if g["state"] == "in") // 2
    return {"matchups": matches, "rankings": rankings,
            "leaders": {"top": top, "beat": beat, "bench": bench},
            "live_games": live_games}
