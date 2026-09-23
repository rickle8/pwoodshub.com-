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


# ── Trade finder ──────────────────────────────────────────────────────────────
# Searches every 1-for-1 and 2-for-1 between every pair of teams for deals
# where BOTH lineups get better. Kickers and defenses are left out: nobody
# trades them and their projections are noise.

TRADE_POSITIONS = ("QB", "RB", "WR", "TE")
SEASON_WEEKS = 17          # fallback; boards say how many weeks they cover
CANDIDATES_PER_TEAM = 12   # most valuable players per roster worth shopping
MIN_WEEKLY_GAIN = 0.3      # below this a "gain" is projection noise
PITCH_MAX_LOSS = 0.3       # "worth a pitch": the other side loses at most this
MIN_FAIRNESS = 75          # adjusted trade value, smaller side vs bigger
SECOND_PIECE = 0.7         # a package's second player counts for less —
                           # two decent players are not worth one star


def trade_value(entry):
    """One trade value from the two crowd markets.

    FantasyCalc is built from real 1QB redraft trades; KeepTradeCut is QB-heavy
    in 1QB leagues. Averaging the two keeps quarterbacks from dominating.
    """
    vals = [v for v in ((entry or {}).get("mkt_value"),
                        (entry or {}).get("ktc_value")) if v]
    return round(sum(vals) / len(vals)) if vals else 0


def package_value(values):
    """Value of a group of players, with the consolidation premium built in."""
    vals = sorted(values, reverse=True)
    return sum(v * (1 if i == 0 else SECOND_PIECE) for i, v in enumerate(vals))


def _reason(chosen, receiving, names):
    """Plain-English note on why a side gets better: who they'd now start."""
    starting = [(val, slot, pid) for slot, pid, val in chosen if pid in receiving]
    if not starting:
        return None
    _, slot, pid = max(starting)
    return f"{names.get(pid, pid)} starts at {slot}"


class _Team:
    """Everything the search needs about one roster, computed once."""

    def __init__(self, owner, roster, board):
        self.owner = owner
        self.points, self.positions, self.names = team_state(roster, board)
        self.base_total, _ = best_lineup(self.points, self.positions)
        self.values = {pid: trade_value(board.get(pid)) for pid in self.points}
        shop = [pid for pid in self.points
                if self.positions.get(pid) in TRADE_POSITIONS
                and self.points[pid] > 0 and self.values[pid] > 0]
        shop.sort(key=lambda p: -self.values[p])
        self.candidates = shop[:CANDIDATES_PER_TEAM]

    def after(self, sending, receiving, other):
        """(gain in season points, reason, player to cut) for one side."""
        pts = {k: v for k, v in self.points.items() if k not in sending}
        pos = {k: v for k, v in self.positions.items() if k not in sending}
        for pid in receiving:
            pts[pid] = other.points[pid]
            pos[pid] = other.positions[pid]
        total, chosen = best_lineup(pts, pos)
        cut = None
        if len(receiving) > len(sending):
            # Taking more players than you send means cutting someone. The
            # cheapest player who isn't in the best lineup is the obvious one.
            starting = {pid for _, pid, _ in chosen}
            bench = [p for p in pts if p not in starting]
            if bench:
                cut = min(bench, key=lambda p: (pts[p], self.values.get(p, 0)))
        why = _reason(chosen, set(receiving), other.names)
        return total - self.base_total, why, cut

    def card(self, pid):
        return {"id": pid, "name": self.names.get(pid, pid),
                "position": self.positions.get(pid, "?"),
                "value": self.values.get(pid, 0),
                "points": round(self.points.get(pid, 0.0), 1)}


def board_weeks(board):
    """How many weeks the board's points cover (rest of season in-season)."""
    for v in board.values():
        if v.get("weeks"):
            return max(1, v["weeks"])
    return SEASON_WEEKS


def _score_deal(a, b, a_send, b_send, weeks=SEASON_WEEKS):
    """A mutually improving deal as a dict, or None."""
    a_val = package_value(a.values[p] for p in a_send)
    b_val = package_value(b.values[p] for p in b_send)
    if not a_val or not b_val:
        return None
    fairness = 100 * min(a_val, b_val) / max(a_val, b_val)
    if fairness < MIN_FAIRNESS:
        return None       # cheap check first: most pairs stop here

    # A deal is kept if both sides gain, or if one side gains and the other
    # barely notices — the second kind is only shown to the side that gains.
    min_gain = MIN_WEEKLY_GAIN * weeks
    max_loss = -PITCH_MAX_LOSS * weeks
    gain_a, why_a, cut_a = a.after(a_send, b_send, b)
    if gain_a < max_loss:
        return None
    gain_b, why_b, cut_b = b.after(b_send, a_send, a)
    if gain_b < max_loss or max(gain_a, gain_b) < min_gain:
        return None
    mutual = min(gain_a, gain_b) >= min_gain
    if not mutual and min(gain_a, gain_b) >= 0 and max(gain_a, gain_b) < 2 * min_gain:
        return None       # a small gain for one side and nothing for the other

    return {
        "a": a.owner, "b": b.owner,
        "a_sends": [a.card(p) for p in a_send],
        "b_sends": [b.card(p) for p in b_send],
        "kind": f"{max(len(a_send), len(b_send))}-for-{min(len(a_send), len(b_send))}",
        "gain_a": round(gain_a / weeks, 1),
        "gain_b": round(gain_b / weeks, 1),
        "why_a": why_a, "why_b": why_b,
        "cut_a": a.names.get(cut_a) if cut_a else None,
        "cut_b": b.names.get(cut_b) if cut_b else None,
        "fairness": round(fairness),
        "mutual": mutual,
        # Ranked on the smaller gain: a deal is only as good as it is for the
        # side that gets less out of it, because that side has to say yes.
        "score": min(gain_a, gain_b) / weeks,
    }


def find_trades(teams, board):
    """Every mutually improving 1-for-1 and 2-for-1 in the league, best first.

    `teams` is the season's team list (each with "owner" and "roster").
    A 2-for-1 is only kept when it beats both of the 1-for-1s inside it —
    otherwise the extra player is just a throw-in.
    """
    weeks = board_weeks(board)
    squads = [_Team(t["owner"], t.get("roster") or [], board) for t in teams]
    deals = []
    for i, a in enumerate(squads):
        for b in squads[i + 1:]:
            singles = {}
            for pa in a.candidates:
                for pb in b.candidates:
                    d = _score_deal(a, b, (pa,), (pb,), weeks)
                    singles[(pa, pb)] = d["score"] if d and d["mutual"] else 0.0
                    if d:
                        deals.append(d)
            # Both directions of 2-for-1: a sends two, then b sends two.
            for two, one, flip in ((a, b, False), (b, a, True)):
                cands = two.candidates
                for x in range(len(cands)):
                    for y in range(x + 1, len(cands)):
                        pair = (cands[x], cands[y])
                        for po in one.candidates:
                            d = _score_deal(two, one, pair, (po,), weeks)
                            if not d:
                                continue
                            best_single = max(
                                singles[(p, po) if not flip else (po, p)]
                                for p in pair)
                            if not d["mutual"] or d["score"] <= best_single + 0.2:
                                continue
                            deals.append(d)
    deals.sort(key=lambda d: (-d["score"], -(d["gain_a"] + d["gain_b"])))
    return deals


def pick_ideas(deals, owner=None, limit=25, per_player=2, per_pair=2,
               per_owner=5, mutual=True):
    """A varied shortlist from find_trades().

    Without a cap the top of the list is the same star offered to five teams.
    Each player appears at most `per_player` times and each pair of owners at
    most `per_pair` times, and on the league-wide list each owner at most
    `per_owner` times, so one team with a glaring hole doesn't fill the page.
    With `owner`, only deals involving that owner, shown from their side.
    `mutual=False` gives the other list instead: deals that help `owner` while
    costing the other team almost nothing (needs `owner`).
    """
    seen_player, seen_pair, seen_owner, out = {}, {}, {}, []
    if not mutual:
        # Pitches are ranked by the total they add, not the smaller side's gain.
        deals = sorted(deals, key=lambda d: -(d["gain_a"] + d["gain_b"]))
    for d in deals:
        if owner and owner not in (d["a"], d["b"]):
            continue
        if d["mutual"] != mutual:
            continue
        if owner and d["b"] == owner:
            d = flip_deal(d)
        if not mutual and d["gain_a"] < MIN_WEEKLY_GAIN:
            continue      # a pitch is only worth showing to the side it helps
        pids = [p["id"] for p in d["a_sends"] + d["b_sends"]]
        pair = frozenset((d["a"], d["b"]))
        if any(seen_player.get(p, 0) >= per_player for p in pids):
            continue
        if seen_pair.get(pair, 0) >= per_pair:
            continue
        if not owner and any(seen_owner.get(o, 0) >= per_owner for o in pair):
            continue
        for o in pair:
            seen_owner[o] = seen_owner.get(o, 0) + 1
        for p in pids:
            seen_player[p] = seen_player.get(p, 0) + 1
        seen_pair[pair] = seen_pair.get(pair, 0) + 1
        out.append(d)
        if len(out) >= limit:
            break
    return out


def flip_deal(d):
    """The same deal with the sides swapped, so `a` is the viewer."""
    f = dict(d)
    for x, y in (("a", "b"), ("a_sends", "b_sends"), ("gain_a", "gain_b"),
                 ("why_a", "why_b"), ("cut_a", "cut_b")):
        f[x], f[y] = d[y], d[x]
    return f
