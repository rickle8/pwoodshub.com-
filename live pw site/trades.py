"""
trades.py — who actually won each trade.

Trades are recorded with the week they happened, so combining that with the
week-by-week lineups tells us what each side genuinely got: the points every
acquired player went on to score for his new owner, and what the players sent
away scored for the other guy.

Two numbers per side, because they answer different questions:
  started  — points the player actually put in that owner's starting lineup.
             This is real, banked value.
  rostered — everything he scored while on the roster, bench included. Higher,
             and the fairer measure of the asset rather than the lineup calls.
"""


def _accumulate(weekly, owner, pid, from_week):
    """(started, rostered, weeks) for one player on one roster from a week on."""
    started = rostered = weeks = 0.0
    for wk_str, owners in weekly.items():
        if int(wk_str) < from_week:
            continue
        entry = (owners or {}).get(owner)
        if not entry:
            continue
        pts = (entry.get("points") or {}).get(pid)
        if pts is None:
            continue          # wasn't on this roster that week
        rostered += pts
        weeks += 1
        if pid in (entry.get("starters") or []):
            started += pts
    return round(started, 2), round(rostered, 2), int(weeks)


def grade_season_trades(year_data, resolve_id, name_of=None):
    """Grade every trade in a season.

    `resolve_id` maps a player name to a Sleeper id; `name_of` is the inverse
    used only for display. Returns a list of trades, newest first.
    """
    weekly = year_data.get("weekly_lineups") or {}
    if not weekly:
        return []

    graded = []
    for t in year_data.get("transactions", []):
        if t.get("type") != "trade":
            continue
        week = t.get("week")
        adds = t.get("adds") or {}
        if not week or len(adds) < 2:
            continue

        sides = []
        for owner, names in adds.items():
            got = []
            for nm in names:
                pid = resolve_id(nm)
                if not pid:
                    got.append({"name": nm, "player_id": None, "started": None,
                                "rostered": None, "weeks": 0})
                    continue
                s, r, w = _accumulate(weekly, owner, str(pid), int(week))
                got.append({"name": nm, "player_id": str(pid),
                            "started": s, "rostered": r, "weeks": w})
            sides.append({
                "owner": owner,
                "got": got,
                "started": round(sum(g["started"] or 0 for g in got), 2),
                "rostered": round(sum(g["rostered"] or 0 for g in got), 2),
            })

        # With two sides the margin is meaningful; with three it isn't, so we
        # just rank them and skip the head-to-head verdict.
        sides.sort(key=lambda s: -s["started"])
        margin = (round(sides[0]["started"] - sides[1]["started"], 2)
                  if len(sides) == 2 else None)
        graded.append({
            "week": int(week),
            "sides": sides,
            "winner": sides[0]["owner"] if margin and margin > 0 else None,
            "margin": margin,
            "even": margin is not None and abs(margin) < 10,
        })

    graded.sort(key=lambda g: -g["week"])
    return graded


def owner_trade_record(graded):
    """{owner: {trades, won, lost, net}} from a season's graded trades."""
    rec = {}
    for g in graded:
        if len(g["sides"]) != 2:
            continue
        a, b = g["sides"]
        for side, other in ((a, b), (b, a)):
            r = rec.setdefault(side["owner"],
                               {"trades": 0, "won": 0, "lost": 0, "net": 0.0})
            r["trades"] += 1
            r["net"] += side["started"] - other["started"]
            if g["even"]:
                continue
            if side["started"] > other["started"]:
                r["won"] += 1
            else:
                r["lost"] += 1
    for r in rec.values():
        r["net"] = round(r["net"], 2)
    return rec
