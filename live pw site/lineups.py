"""
lineups.py — what a roster *could* have scored, versus what it did.

Sleeper records every rostered player's points each week plus who was actually
started, so the best legal lineup in hindsight is recoverable. The gap between
that and the lineup someone really set is the most quietly brutal stat in
fantasy: points you already had and left on the bench.

Used for the efficiency pages and shared with projections.py so the slot-filling
rules live in exactly one place.
"""

# The league's starting lineup. FLEX is resolved last, from whatever RB/WR/TE
# remain, because filling it early can steal the only startable player at a
# fixed position and understate the true optimum.
DEFAULT_LINEUP = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF"]
FLEX_POSITIONS = ("RB", "WR", "TE")


def best_lineup(values, positions, lineup=None):
    """Highest-scoring legal lineup.

    `values` is {player_id: number} (real points, or projections), `positions`
    is {player_id: "RB"}. Returns (total, [(slot, player_id, value), ...]).
    """
    lineup = lineup or DEFAULT_LINEUP
    pool = sorted(((float(v), pid) for pid, v in values.items() if v is not None),
                  reverse=True)

    used, chosen = set(), []
    for slot in [s for s in lineup if s != "FLEX"]:
        for i, (val, pid) in enumerate(pool):
            if i not in used and positions.get(pid) == slot:
                used.add(i)
                chosen.append((slot, pid, round(val, 2)))
                break
    for _ in [s for s in lineup if s == "FLEX"]:
        for i, (val, pid) in enumerate(pool):
            if i not in used and positions.get(pid) in FLEX_POSITIONS:
                used.add(i)
                chosen.append(("FLEX", pid, round(val, 2)))
                break

    return round(sum(c[2] for c in chosen), 2), chosen


def week_efficiency(entry, positions, lineup=None):
    """One owner's week: what they started vs what they could have.

    `entry` is a weekly_lineups record: {"starters": [...], "points": {...}}.
    Returns None when the week has no usable data.
    """
    points = entry.get("points") or {}
    if not points:
        return None
    # Sleeper writes "0" into a starting slot left empty.
    starters = [p for p in (entry.get("starters") or []) if p and p != "0"]
    actual = round(sum(points.get(p, 0.0) for p in starters), 2)
    optimal, chosen = best_lineup(points, positions, lineup)
    best_ids = {pid for _, pid, _ in chosen}

    # The single most painful omission: highest scorer who sat while a lineup
    # slot he was eligible for went to someone worse.
    benched = [(points.get(p, 0.0), p) for p in points
               if p not in starters and p in best_ids]
    benched.sort(reverse=True)

    return {
        "actual":     actual,
        "optimal":    optimal,
        "left":       round(max(0.0, optimal - actual), 2),
        "efficiency": round(actual / optimal * 100, 1) if optimal else None,
        "worst_sit":  benched[0][1] if benched else None,
        "worst_sit_pts": round(benched[0][0], 2) if benched else None,
        "starters":   starters,
        "optimal_ids": best_ids,
    }


def season_efficiency(year_data, positions, through_week=None, lineup=None):
    """{owner: {...}} across a season, plus every week's detail.

    `through_week` caps at the regular season when you don't want playoff weeks
    mixed in (owners who missed the playoffs simply have no games then, which
    would otherwise flatter or distort the comparison).
    """
    weekly = year_data.get("weekly_lineups") or {}
    out = {}
    for wk_str, owners in weekly.items():
        wk = int(wk_str)
        if through_week and wk > through_week:
            continue
        for owner, entry in owners.items():
            eff = week_efficiency(entry, positions, lineup)
            if not eff:
                continue
            rec = out.setdefault(owner, {
                "actual": 0.0, "optimal": 0.0, "left": 0.0,
                "weeks": 0, "perfect": 0, "worst": None, "by_week": [],
            })
            rec["actual"] += eff["actual"]
            rec["optimal"] += eff["optimal"]
            rec["left"] += eff["left"]
            rec["weeks"] += 1
            if eff["left"] < 0.01:
                rec["perfect"] += 1
            if rec["worst"] is None or eff["left"] > rec["worst"]["left"]:
                rec["worst"] = {**eff, "week": wk}
            rec["by_week"].append({"week": wk, **eff})

    for rec in out.values():
        rec["actual"] = round(rec["actual"], 2)
        rec["optimal"] = round(rec["optimal"], 2)
        rec["left"] = round(rec["left"], 2)
        rec["efficiency"] = (round(rec["actual"] / rec["optimal"] * 100, 1)
                             if rec["optimal"] else None)
        rec["left_per_week"] = round(rec["left"] / rec["weeks"], 1) if rec["weeks"] else 0
        rec["by_week"].sort(key=lambda w: w["week"])
    return out
