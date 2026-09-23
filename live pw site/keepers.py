"""
keepers.py — what each owner can keep next year, and what it costs.

League rules:
  * You can only keep a player you drafted (or kept, which counts as drafting
    him again at the price you paid).
  * The first time you keep someone, he costs the round you drafted him in.
  * Every additional consecutive year costs four rounds earlier.
  * The cost is capped at round 1 — you can't go above it, and a player already
    kept at round 1 can be kept again at round 1 indefinitely.

Nothing here calls an API: it's the stored draft plus the current roster.
"""

ROUND_STEP = 4          # each extra keeper year costs this many rounds earlier
TOP_ROUND = 1
# Kickers and defenses are picked up off waivers every week; keeping one is
# never the right use of a keeper slot, whatever the draft market says.
STREAMED = ("K", "DEF", "D/ST")


def next_cost(round_paid, was_keeper):
    """What this player costs to keep NEXT season."""
    if not was_keeper:
        return round_paid                     # first keep — same round
    return max(TOP_ROUND, round_paid - ROUND_STEP)


def draft_slots(board, use_adp=True):
    """{player_id: the overall pick he'd go at in a draft held today}.

    Preseason that's simply ADP. Once games are played ADP freezes, so each
    player's *current* consensus rank at his position is mapped onto where
    that position rank went in the draft market: if he's now the 10th-best
    QB, he'd go where the 10th QB went. That keeps each position's real draft
    habits (QBs go late in 1QB leagues) while the player's value stays current.
    Ranking straight off trade values instead makes every backup quarterback
    look like a steal.
    """
    if use_adp:
        return {pid: v["adp"] for pid, v in board.items() if v.get("adp")}

    curves = {}
    for v in board.values():
        if v.get("adp") and v.get("position"):
            curves.setdefault(v["position"], []).append(v["adp"])
    for c in curves.values():
        c.sort()

    out = {}
    for pid, v in board.items():
        curve, rank = curves.get(v.get("position")), v.get("rank")
        if not curve or not rank:
            continue
        i = rank - 1
        lo = min(int(i), len(curve) - 1)
        hi = min(lo + 1, len(curve) - 1)
        out[pid] = curve[lo] + (curve[hi] - curve[lo]) * (i - int(i))
    return out


def keeper_options(year_data, board=None, resolve_id=None, slots=None):
    """{owner: [option, ...]} — everyone currently rostered who was drafted by
    that owner this year, priced for next season.

    `board` is the consensus board and `slots` the draft_slots() estimate,
    used only to show what the market thinks each player is worth against the
    round he'd cost.
    """
    board = board or {}
    slots = slots or {}
    picks = year_data.get("draft") or []
    teams = year_data.get("teams") or []

    def key(entry, name_field):
        pid = entry.get("player_id")
        if not pid and resolve_id:
            pid = resolve_id(entry.get(name_field) or "")
        return str(pid) if pid else "name:" + (entry.get(name_field) or "").lower()

    # Who each owner drafted this year, and at what price. Matched on player id
    # where there is one: names drift ("DJ Moore" / "D.J. Moore") and collide.
    drafted = {}
    for p in picks:
        drafted.setdefault(p["owner"], {})[key(p, "player")] = p

    team_count = len(teams) or 12
    out = {}
    for t in teams:
        owner = t["owner"]
        mine = drafted.get(owner, {})
        options = []
        for player in t.get("roster", []):
            k = key(player, "name")
            pick = mine.get(k)
            if not pick:
                continue          # traded for or picked up — not keepable
            pid = "" if k.startswith("name:") else k
            v = board.get(pid) or {}
            cost = next_cost(pick["round"], bool(pick.get("keeper")))
            options.append({
                "name": player["name"],
                "player_id": pid or None,
                "position": player.get("position", "?"),
                "drafted_round": pick["round"],
                # Sleeper stores the overall pick, ESPN the pick in the round.
                "drafted_pick": ((pick.get("pick") or 1) - 1) % team_count + 1,
                "was_keeper": bool(pick.get("keeper")),
                "cost_round": cost,
                # At round 1 the discount has run out; the price stops moving.
                "maxed": cost == TOP_ROUND,
                "value": v.get("ktc_value") or v.get("mkt_value"),
                "adp": slots.get(pid) or v.get("adp"),
                "consensus_rank": v.get("rank"),
                "proj_points": v.get("points"),
                "tier": v.get("tier"),
            })
        # Cheapest rounds first, best players within a round.
        options.sort(key=lambda o: (o["cost_round"], o["adp"] or 999))
        out[owner] = options
    return out


def cost_pick(round_number, teams=12):
    """Middle-of-round pick number a keeper round roughly equates to."""
    return (round_number - 1) * teams + (teams + 1) / 2


def value_verdict(option, teams=12):
    """Is this keeper a bargain? Measured in draft picks of surplus.

    Compares where the player is actually being drafted (ADP) against the pick
    he would cost you. Keeping a player with an ADP of 15 for a 6th-rounder is
    about 50 picks of surplus, which is the number people actually reason with.

    Raw trade values don't work here: they're compressed at the top and QB-
    skewed in 1QB formats, so almost nothing looks like a bargain on that scale.
    """
    adp, cost = option.get("adp"), option.get("cost_round")
    if not adp or not cost or option.get("position") in STREAMED:
        return None, None
    surplus = cost_pick(cost, teams) - adp
    if surplus >= 2 * teams:        # two full rounds of value or better
        return "steal", surplus
    if surplus >= teams / 2:
        return "good", surplus
    if surplus >= -teams / 2:
        return "fair", surplus
    return "overpay", surplus
