"""
recaps.py — weekly awards, shared by the site (/recaps) and the auto-poster
(weekly_recap.py) so both tell exactly the same story.
"""


def _owner_of(team_name, teams):
    return next((t["owner"] for t in teams if t["name"] == team_name), team_name)


def played_weeks(year_data):
    """Weeks where at least one game has a score, oldest first."""
    weeks = []
    for wk, matchups in year_data.get("weekly_scores", {}).items():
        if any(float(m.get("home_score") or 0) or float(m.get("away_score") or 0)
               for m in matchups):
            weeks.append(int(wk))
    return sorted(weeks)


def latest_complete_week(year_data):
    """Most recent week where every scheduled matchup has been played."""
    teams_n = len(year_data.get("teams", []))
    expected = teams_n // 2 if teams_n else 0
    complete = [wk for wk in played_weeks(year_data)
                if len([m for m in year_data["weekly_scores"][str(wk)]
                        if float(m.get("home_score") or 0) or float(m.get("away_score") or 0)]) >= expected]
    return max(complete) if complete else None


def compute_week_awards(year_data, week):
    """Awards + full results for one week, or None if nothing was played."""
    teams = year_data.get("teams", [])
    raw = year_data.get("weekly_scores", {}).get(str(week), [])
    games = [m for m in raw
             if float(m.get("home_score") or 0) or float(m.get("away_score") or 0)]
    if not games:
        return None

    entries = []
    results = []
    for m in games:
        hs = float(m["home_score"])
        as_ = float(m["away_score"])
        ho = _owner_of(m["home_team"], teams)
        ao = _owner_of(m["away_team"], teams)
        entries.append({"team": m["home_team"], "owner": ho, "score": hs,
                        "opponent": m["away_team"], "opp_owner": ao,
                        "opp_score": as_, "won": hs > as_})
        entries.append({"team": m["away_team"], "owner": ao, "score": as_,
                        "opponent": m["home_team"], "opp_owner": ho,
                        "opp_score": hs, "won": as_ > hs})
        results.append({
            "home_team": m["home_team"], "home_owner": ho, "home_score": hs,
            "away_team": m["away_team"], "away_owner": ao, "away_score": as_,
            "margin": round(abs(hs - as_), 2),
        })

    ranked = sorted(entries, key=lambda e: e["score"], reverse=True)
    # Split the week's scores into halves by rank so the halves are exactly even
    top_half = {id(e) for e in ranked[: len(ranked) // 2]}

    results.sort(key=lambda r: max(r["home_score"], r["away_score"]), reverse=True)

    return {
        "week":     week,
        "high":     ranked[0],
        "low":      ranked[-1],
        "blowout":  max(results, key=lambda r: r["margin"]),
        "closest":  min(results, key=lambda r: r["margin"]),
        "lucky":    [e for e in entries if e["won"] and id(e) not in top_half],
        "unlucky":  [e for e in entries if not e["won"] and id(e) in top_half],
        "results":  results,
    }


def format_recap_text(awards, year_data, season):
    """The plain-text version posted to the Notes page by the bot."""
    a = awards
    lines = [f"WEEK {a['week']} RECAP — {season}", ""]
    lines.append(f"🏆 High score: {a['high']['owner']} ({a['high']['team']}) "
                 f"with {a['high']['score']:.2f}")
    lines.append(f"🗑️ Low score: {a['low']['owner']} ({a['low']['team']}) "
                 f"with {a['low']['score']:.2f}")
    b = a["blowout"]
    lines.append(f"🔨 Biggest beatdown: {b['home_team']} {b['home_score']:.2f} vs "
                 f"{b['away_team']} {b['away_score']:.2f} ({b['margin']:.2f} pt margin)")
    c = a["closest"]
    lines.append(f"😰 Nail-biter: {c['home_team']} {c['home_score']:.2f} vs "
                 f"{c['away_team']} {c['away_score']:.2f} (decided by {c['margin']:.2f})")
    for e in a["lucky"]:
        lines.append(f"🍀 Lucky win: {e['owner']} won with {e['score']:.2f} "
                     f"(bottom half of the league this week)")
    for e in a["unlucky"]:
        lines.append(f"💔 Robbed: {e['owner']} scored {e['score']:.2f} (top half) "
                     f"and still lost")

    standings = sorted(year_data.get("teams", []),
                       key=lambda t: (-t["wins"], -t["points_for"]))
    if standings:
        lines.append("")
        lines.append("STANDINGS CHECK")
        for i, t in enumerate(standings[:3], 1):
            lines.append(f"{i}. {t['owner']} ({t['wins']}-{t['losses']}, "
                         f"{t['points_for']:.1f})")
        cellar = standings[-1]
        lines.append(f"...and in the basement: {cellar['owner']} "
                     f"({cellar['wins']}-{cellar['losses']}). "
                     f"The loser bracket is watching.")

    lines.append("")
    lines.append("— PW Bot (automated, unbiased, unbribeable)")
    return "\n".join(lines)
