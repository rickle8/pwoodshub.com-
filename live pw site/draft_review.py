"""
draft_review.py — how every pick in a draft actually turned out.

Each pick is scored by what the player produced for the owner who drafted him,
bench weeks included, from the season's player_scoring. It's then judged
against "par": what players at the same position, taken around the same spot,
returned. A 14th-rounder isn't punished for not being a first-rounder, and
quarterbacks aren't all steals just because they out-score everyone.
"""

PAR_WINDOW = 18        # picks either side of this one that count as "around here"
PAR_MIN_COMPS = 4      # widen the window until at least this many comparables
BUST_ROUNDS = 8        # a late flier that misses costs nobody anything
LIST_LENGTH = 12
# Kickers and defenses are streamed week to week, so holding one all season
# says nothing about the draft pick and would swamp the late-round steals.
UNGRADED = ("K", "DEF", "D/ST")


def _in_round(p, teams):
    """Pick within its round. ESPN seasons store it that way already; Sleeper
    seasons store the overall pick, so fold that back into the round."""
    return ((p.get("pick") or 1) - 1) % teams + 1


def _pick_number(p, teams):
    return (p["round"] - 1) * teams + _in_round(p, teams)


def _par(target, same_pos, teams):
    """Average points of same-position picks near this one, itself excluded."""
    n = _pick_number(target, teams)
    others = [q for q in same_pos if q is not target]
    window = PAR_WINDOW
    while True:
        comps = [q["points"] for q in others
                 if abs(_pick_number(q, teams) - n) <= window]
        if len(comps) >= PAR_MIN_COMPS or window > 12 * teams:
            break
        window += teams
    return sum(comps) / len(comps) if comps else None


def review_draft(year_data, resolve_id):
    """Scored picks and summaries for one season, or None without the data."""
    picks = year_data.get("draft") or []
    scoring = year_data.get("player_scoring") or {}
    if not picks or not scoring:
        return None
    teams = max(1, len({p["owner"] for p in picks}))

    rows = []
    for p in picks:
        pid = str(p.get("player_id") or resolve_id(p.get("player") or "") or "")
        mine = (scoring.get(pid) or {}).get(p["owner"]) or {}
        everyone = sum(v.get("points") or 0 for v in (scoring.get(pid) or {}).values())
        rows.append({
            "round": p["round"], "pick": _in_round(p, teams),
            "player": p["player"], "player_id": pid or None,
            "position": p.get("position") or "?", "owner": p["owner"],
            "keeper": bool(p.get("keeper")),
            "points": round(mine.get("points") or 0, 1),
            "starts": mine.get("starts") or 0,
            # Points he went on to score for somebody else — a traded pick
            # isn't a bust just because the drafter cashed him in.
            "elsewhere": round(everyone - (mine.get("points") or 0), 1),
        })

    by_pos = {}
    for r in rows:
        by_pos.setdefault(r["position"], []).append(r)
    for r in rows:
        par = _par(r, by_pos[r["position"]], teams)
        r["par"] = round(par, 1) if par is not None else None
        r["over"] = round(r["points"] - par, 1) if par is not None else None

    graded = [r for r in rows
              if r["over"] is not None and r["position"] not in UNGRADED]
    steals = sorted((r for r in graded if r["over"] > 0),
                    key=lambda r: -r["over"])[:LIST_LENGTH]
    # A player traded away who kept producing wasn't a bust for the drafter —
    # the return shows up in the trade grades instead.
    busts = sorted((r for r in graded if r["over"] < 0
                    and r["round"] <= BUST_ROUNDS and r["elsewhere"] <= 0),
                   key=lambda r: r["over"])[:LIST_LENGTH]

    rounds = {}
    for r in rows:
        rounds.setdefault(r["round"], []).append(r)
    par = {rnd: round(sum(r["points"] for r in rs) / len(rs), 1)
           for rnd, rs in rounds.items()}
    best_by_round = {}
    for rnd, rs in rounds.items():
        skill = [r for r in rs if r["position"] not in UNGRADED]
        if skill:
            best_by_round[rnd] = max(skill, key=lambda r: r["points"])

    # Owner grades: total points over par across the whole draft.
    owners = {}
    for r in graded:
        o = owners.setdefault(r["owner"], {"owner": r["owner"], "over": 0.0,
                                           "points": 0.0, "hits": 0, "picks": 0})
        o["over"] += r["over"]
        o["points"] += r["points"]
        o["picks"] += 1
        o["hits"] += r["over"] > 0
    grades = sorted(owners.values(), key=lambda o: -o["over"])
    for i, o in enumerate(grades):
        o["over"] = round(o["over"], 1)
        o["points"] = round(o["points"], 1)
        o["grade"] = _letter(i, len(grades))

    return {"steals": steals, "busts": busts, "par": par,
            "best_by_round": best_by_round, "grades": grades, "rows": rows}


def _letter(i, n):
    """Curve the owners onto letter grades by rank."""
    scale = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "F"]
    return scale[min(len(scale) - 1, round(i * (len(scale) - 1) / max(1, n - 1)))]
