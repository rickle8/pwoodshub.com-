"""
trade_machine.py — evaluating and discovering trades.

A trade is worth making if it improves the lineup you can actually start. Raw
trade value doesn't capture that: a team with four good running backs and no
tight end can hand over an RB it never starts and get better, even in a deal
that looks lopsided on paper.

So every trade here is scored two ways:
  lineup — the change in each team's projected season-long starting points,
           from consensus projections in the league's real slots. This is
           what the trade actually does for you.
  value  — the change in total trade value (KeepTradeCut). This is what the
           trade looks like to everyone else, and it's the check on whether
           the other owner would ever say yes.

Both numbers can rise for both teams at once. That's not alchemy: it's two
rosters with opposite positional surpluses.
"""

from lineups import DEFAULT_LINEUP, best_lineup


def team_state(roster, board):
    """Points and positions for one roster, keyed by player id."""
    points, positions, names = {}, {}, {}
    for p in roster:
        pid = str(p.get("player_id") or "")
        if not pid:
            continue
        entry = board.get(pid) or {}
        points[pid] = entry.get("points") or 0.0
        positions[pid] = p.get("position") or entry.get("position") or ""
        names[pid] = p.get("name", pid)
    return points, positions, names


def lineup_strength(points, positions, lineup=None):
    """Projected FULL-SEASON points from the best legal lineup.

    Consensus points are season totals, so this is too — the number that
    matters is the delta between two versions of a roster, which is on the
    same footing either way.
    """
    total, _ = best_lineup(points, positions, lineup or DEFAULT_LINEUP)
    return total


def evaluate(side_a, side_b, board, lineup=None):
    """Score a proposed trade.

    `side_a`/`side_b` are {"owner", "roster", "sending": [player_id, ...]}.
    Returns per-team lineup and value deltas.
    """
    lineup = lineup or DEFAULT_LINEUP
    out = {}
    rosters = {}
    for side in (side_a, side_b):
        rosters[side["owner"]] = team_state(side["roster"], board)

    a_send = set(side_a.get("sending") or [])
    b_send = set(side_b.get("sending") or [])

    for side, sending, incoming_from in ((side_a, a_send, side_b),
                                         (side_b, b_send, side_a)):
        points, positions, names = rosters[side["owner"]]
        o_points, o_positions, o_names = rosters[incoming_from["owner"]]
        incoming = set(incoming_from.get("sending") or [])

        before = lineup_strength(points, positions, lineup)

        after_points = {k: v for k, v in points.items() if k not in sending}
        after_positions = {k: v for k, v in positions.items() if k not in sending}
        for pid in incoming:
            after_points[pid] = o_points.get(pid, 0.0)
            after_positions[pid] = o_positions.get(pid, "")
        after = lineup_strength(after_points, after_positions, lineup)

        val_out = sum((board.get(p) or {}).get("ktc_value") or 0 for p in sending)
        val_in = sum((board.get(p) or {}).get("ktc_value") or 0 for p in incoming)

        out[side["owner"]] = {
            "before": round(before, 1),
            "after": round(after, 1),
            "lineup_delta": round(after - before, 1),
            "value_out": val_out,
            "value_in": val_in,
            "value_delta": val_in - val_out,
            "sending": [names.get(p, p) for p in sending],
            "receiving": [o_names.get(p, p) for p in incoming],
        }
    return out
