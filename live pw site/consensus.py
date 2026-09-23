"""
consensus.py — blends several independent public opinions on player value.

FantasyPros' consensus API needs a paid key, so this builds the same idea from
sources that are genuinely open:

  1. Expert projection — Rotowire's season-long numbers, served through Sleeper
     already scored in half-PPR (our exact scoring). Covers everyone, kickers
     and defenses included.
  2. Draft market — Sleeper's own ADP, i.e. where millions of real drafters
     actually take a player. Also covers everyone.
  3. Trade market — FantasyCalc's current values, derived from real trades in
     real leagues and configurable to 1QB / 0.5 PPR redraft, which is us.
     Only skill positions; nobody trades kickers.
  4. Crowd vote — KeepTradeCut's redraft start/sit values, from millions of
     "which of these three would you keep?" votes. Their robots.txt allows the
     rankings page (only /histories is disallowed). Caveat worth knowing: KTC's
     redraft board is *format-agnostic* — passing ppr=0.5 changes nothing, they
     publish one set of values for all scoring. It is 1QB, which does match us.

They disagree often enough to be worth averaging: preseason 2026 had Jaxon
Smith-Njigba 4th by trade value but 8th by ADP.

The blend works in *rank* space within each position, then maps the consensus
rank back onto the projection points curve — so the output stays in half-PPR
points and can be summed into a lineup, while still reflecting all three
opinions. Sources that are missing for a player simply drop out of that
player's average.
"""

import html
import json
import re
import urllib.request

import time
from threading import Lock

from sleeper_common import safe_get

_UA = {"User-Agent": "PeytonWoodsLeague/1.0 (private fantasy league history site)"}

# How much each opinion counts. The projection leads because it is the only one
# denominated in the thing we actually care about (points); the two markets are
# corroboration, and the trade market is the narrowest so it counts least.
# The two crowd-value feeds measure much the same thing, so they split what was
# previously one market share rather than doubling the market's say.
#
# ADP is deliberately preseason-only. It's a record of where players *were*
# drafted, and it stops updating the moment the season starts — by week 6 it's
# still insisting on a guy who has been hurt since week 2. The other three keep
# moving, so once real games exist ADP drops out and the remaining weights
# renormalise on their own (the blend divides by whatever sources it has).
WEIGHTS = {"proj": 0.45, "adp": 0.25, "market": 0.15, "ktc": 0.15}

SOURCE_NAMES = {"proj": "Rotowire projections", "adp": "Sleeper ADP",
                "market": "FantasyCalc trade values", "ktc": "KeepTradeCut"}

UNRANKED = 999          # Sleeper's "no ADP" sentinel
_ALL_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

_cache = {}
_TTL = 6 * 3600

_locks: dict = {}
_locks_guard = Lock()


def _lock_for(key):
    with _locks_guard:
        return _locks.setdefault(key, Lock())


def _cached(key, fetch_fn, ttl=_TTL):
    hit = _cache.get(key)
    if hit and time.time() - hit["ts"] < ttl:
        return hit["data"]
    # One fetch per key. A miss here means a 1.8MB scrape of the KTC rankings
    # page plus two API calls; letting every concurrent page view start its own
    # copy is how a slow third party turns into a slow site.
    #
    # Per key, never global: build_board's fetch calls _cached() again for the
    # projections, FantasyCalc and KTC feeds while holding its own key.
    with _lock_for(key):
        hit = _cache.get(key)
        if hit and time.time() - hit["ts"] < ttl:
            return hit["data"]
        try:
            data = fetch_fn()
        except Exception:
            if hit:
                return hit["data"]
            raise
        _cache[key] = {"ts": time.time(), "data": data}
        return data


# ── Sources ────────────────────────────────────────────────────────────────────

def get_projections_and_adp(season):
    """One Sleeper call gives both the expert points and the draft-market ADP.

    Returns ({player_id: points}, {player_id: adp}).
    """
    def fetch():
        qs = "&".join(f"position[]={p}" for p in _ALL_POSITIONS)
        url = (f"https://api.sleeper.com/projections/nfl/{season}"
               f"?season_type=regular&{qs}&order_by=pts_half_ppr")
        pts, adp = {}, {}
        for row in safe_get(url, timeout=25) or []:
            pid = row.get("player_id")
            if pid is None:
                continue
            pid = str(pid)
            s = row.get("stats") or {}
            if s.get("pts_half_ppr"):
                pts[pid] = float(s["pts_half_ppr"])
            a = s.get("adp_half_ppr")
            if a and float(a) < UNRANKED:
                adp[pid] = float(a)
        return pts, adp
    return _cached(f"proj_adp:{season}", fetch)


def get_market_values():
    """FantasyCalc redraft values for 1QB / half-PPR — our league's format.

    Returns {sleeper_player_id: {...}} including tier and 30-day trend.
    """
    def fetch():
        url = ("https://api.fantasycalc.com/values/current"
               "?isDynasty=false&numQbs=1&ppr=0.5")
        out = {}
        for row in safe_get(url, timeout=25) or []:
            p = row.get("player") or {}
            pid = p.get("sleeperId")
            if not pid:
                continue
            out[str(pid)] = {
                "value":     row.get("value"),
                "rank":      row.get("overallRank"),
                "pos_rank":  row.get("positionRank"),
                "tier":      row.get("maybeTier"),
                "trend30":   row.get("trend30Day"),
                "rostered":  row.get("maybeRosterPercent"),
            }
        return out
    return _cached("fantasycalc", fetch)


def _fetch_ktc_raw():
    """KTC's board, dug out of the rankings page.

    One 1.8MB page per refresh, cached for hours — their robots.txt allows this
    path, but there's no reason to be greedy about it.

    They've moved the payload once already: it used to be an inline
    `var playersArray = [...]` and is now JSON parked in a
    `<script id="ktc-players">` element. Both shapes are handled, newest first,
    because this is page scraping and it will move again.
    """
    req = urllib.request.Request("https://keeptradecut.com/fantasy-rankings",
                                 headers=_UA)
    page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")

    # Current: JSON.parse(document.getElementById('ktc-players').textContent)
    m = re.search(r"getElementById\('([^']+)'\)", page)
    if m:
        el = re.search(r'id=["\']' + re.escape(m.group(1)) + r'["\'][^>]*>(.*?)</',
                       page, re.S)
        if el:
            try:
                return json.loads(html.unescape(el.group(1).strip()))
            except json.JSONDecodeError:
                pass

    # Older: the array written straight into the script.
    m = re.search(r"(?:var|let|const)\s+playersArray\s*=\s*(\[.*?\]);", page, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    print("consensus: KTC page layout changed — no player data found")
    return []


def get_ktc_values(names_by_id=None):
    """{sleeper_player_id: {...}} from KeepTradeCut's redraft board.

    KTC keys players by its own id plus an MFL id, so we bridge through
    FantasyCalc (which publishes both mflId and sleeperId) and fall back to an
    unambiguous name+position match for anyone FantasyCalc doesn't carry.
    """
    def fetch():
        rows = _fetch_ktc_raw()
        if not rows:
            return {}

        # Bridge: mflId -> sleeperId, taken from the FantasyCalc payload.
        bridge = {}
        try:
            url = ("https://api.fantasycalc.com/values/current"
                   "?isDynasty=false&numQbs=1&ppr=0.5")
            for row in safe_get(url, timeout=25) or []:
                p = row.get("player") or {}
                if p.get("mflId") and p.get("sleeperId"):
                    bridge[str(p["mflId"])] = str(p["sleeperId"])
        except Exception:
            pass

        by_name_pos = {}
        for pid, info in (names_by_id or {}).items():
            nm, pos = info.get("name"), info.get("position")
            if nm and pos:
                by_name_pos.setdefault((nm.lower(), pos), []).append(pid)

        out = {}
        for x in rows:
            sid = bridge.get(str(x.get("mflid")))
            if not sid:
                cands = by_name_pos.get((x.get("playerName", "").lower(),
                                         x.get("position")))
                # Only accept an unambiguous name match — never guess between
                # two players who share a name and position.
                sid = cands[0] if cands and len(cands) == 1 else None
            if not sid:
                continue
            v = x.get("oneQBValues") or {}
            keep = v.get("kept") or 0
            trade = v.get("traded") or 0
            cut = v.get("cut") or 0
            votes = keep + trade + cut
            out[sid] = {
                "value":    v.get("startSitValue"),
                "rank":     v.get("startSitOverallRank"),
                "pos_rank": v.get("startSitPositionalRank"),
                "tier":     v.get("startSitPositionalTier"),
                "trend7":   v.get("overall7DayTrend"),
                "votes":    votes,
                "keep_pct":  round(keep / votes * 100) if votes else None,
                "trade_pct": round(trade / votes * 100) if votes else None,
                "cut_pct":   round(cut / votes * 100) if votes else None,
                "slug":     x.get("slug"),
            }
        return out

    return _cached("ktc", fetch)


# ── Blend ──────────────────────────────────────────────────────────────────────

def _ranks(values, higher_is_better):
    """{key: 1-based rank} over the supplied {key: value}."""
    ordered = sorted(values.items(), key=lambda kv: kv[1],
                     reverse=higher_is_better)
    return {k: i + 1 for i, (k, _) in enumerate(ordered)}


def _curve_lookup(curve, rank):
    """Points for a (possibly fractional) rank on a descending points curve."""
    if not curve:
        return None
    i = max(0.0, rank - 1)
    lo = min(int(i), len(curve) - 1)
    hi = min(lo + 1, len(curve) - 1)
    frac = i - lo
    return curve[lo] + (curve[hi] - curve[lo]) * frac


def adp_is_live(season):
    """True only before the season's first game.

    Sleeper's ADP is frozen at draft time, so it's a good prior in August and
    misinformation in November.
    """
    try:
        state = safe_get("https://api.sleeper.app/v1/state/nfl", timeout=15) or {}
    except Exception:
        return False        # if we can't tell, prefer the signals that update
    if str(season) != str(state.get("season")):
        return False        # a past season is definitionally under way
    if state.get("season_type") != "regular":
        return True         # pre / off season — the draft market is all we have
    return int(state.get("week") or 0) < 1


def build_board(season, players, use_adp=None):
    """Consensus board keyed by player id.

    `players` is our Sleeper player cache ({id: {name, position, ...}}) — the
    position decides who a player is ranked against, and the name is the
    fallback when joining KeepTradeCut's board.

    Each entry: points (consensus, half-PPR season total), the raw inputs, the
    per-source position ranks, and `spread` — how far apart the sources are, in
    position-rank places. A big spread means the experts and the market
    genuinely disagree about the player.
    """
    # Callers who know whether a real game has been played should say so;
    # the NFL-week fallback flips a few days before week 1 actually kicks off.
    if use_adp is None:
        use_adp = adp_is_live(season)

    def fetch():
        try:
            proj, adp = get_projections_and_adp(season)
        except Exception:
            proj, adp = {}, {}
        try:
            market = get_market_values()
        except Exception:
            market = {}
        try:
            ktc = get_ktc_values(players)
        except Exception:
            ktc = {}
        if not proj:
            return {}

        # The ranking universe is players who have a projection. Ranks are only
        # comparable if all three signals are ranked over the same population —
        # otherwise "68th of 200 projected" gets averaged against "557th of 690
        # with an ADP" and the player looks wildly contested when he isn't.
        # Projections cover every rosterable player, so nothing real is lost.
        groups = {}
        for pid in proj:
            pos = (players.get(pid) or {}).get("position")
            if pos in _ALL_POSITIONS:
                groups.setdefault(pos, []).append(pid)

        board = {}
        for pos, pids in groups.items():
            p_vals = {p: proj[p] for p in pids if p in proj}
            a_vals = {p: adp[p] for p in pids if p in adp}
            m_vals = {p: market[p]["value"] for p in pids
                      if p in market and market[p].get("value")}
            k_vals = {p: ktc[p]["value"] for p in pids
                      if p in ktc and ktc[p].get("value")}

            r_proj = _ranks(p_vals, higher_is_better=True)
            r_adp = _ranks(a_vals, higher_is_better=False)
            r_mkt = _ranks(m_vals, higher_is_better=True)
            r_ktc = _ranks(k_vals, higher_is_better=True)
            curve = sorted(p_vals.values(), reverse=True)

            for pid in pids:
                parts = []
                if pid in r_proj:
                    parts.append(("proj", r_proj[pid]))
                if pid in r_adp and use_adp:
                    parts.append(("adp", r_adp[pid]))
                if pid in r_mkt:
                    parts.append(("market", r_mkt[pid]))
                if pid in r_ktc:
                    parts.append(("ktc", r_ktc[pid]))
                if not parts:
                    continue
                wsum = sum(WEIGHTS[s] for s, _ in parts)
                crank = sum(WEIGHTS[s] * r for s, r in parts) / wsum
                seen = [r for _, r in parts]
                board[pid] = {
                    "position":   pos,
                    "points":     round(_curve_lookup(curve, crank) or 0, 1),
                    "rank":       round(crank, 1),
                    "sources":    [s for s, _ in parts],
                    # Which feeds cover *this* player — nobody trades kickers,
                    # so a K must not be credited to the trade market.
                    "source_names": [SOURCE_NAMES[s] for s, _ in parts],
                    "spread":     max(seen) - min(seen) if len(seen) > 1 else 0,
                    "proj_pts":   p_vals.get(pid),
                    "proj_rank":  r_proj.get(pid),
                    "adp":        a_vals.get(pid),
                    # Only exposed as a contributing source while it counts;
                    # the raw pick number above stays visible either way.
                    "adp_rank":   r_adp.get(pid) if use_adp else None,
                    "mkt_value":  m_vals.get(pid),
                    "mkt_rank":   r_mkt.get(pid),
                    "tier":       (market.get(pid) or {}).get("tier"),
                    "trend30":    (market.get(pid) or {}).get("trend30"),
                    "rostered":   (market.get(pid) or {}).get("rostered"),
                    "ktc_value":  k_vals.get(pid),
                    "ktc_rank":   r_ktc.get(pid),
                    "ktc":        ktc.get(pid),
                }
        return board

    # The flag is part of the key: the board must not keep serving a
    # preseason blend after week 1 kicks off.
    return _cached(f"board:{season}:adp{int(use_adp)}", fetch)


def active_sources(board):
    """Which feeds actually contributed, for honest labelling in the UI."""
    live = set()
    for v in board.values():
        live.update(v["sources"])
    return [SOURCE_NAMES[s] for s in ("proj", "adp", "market", "ktc") if s in live]
