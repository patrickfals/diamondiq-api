"""
mlb_data.py -- Shared spoiler-safe MLB data retrieval for DiamondIQ.

This module is the single authoritative implementation of:

  1. fetch_game_data()          -- retrieves raw MLB Stats API feed for a game.
  2. extract_batter_stats()     -- entering-game batting stats (postgame subtraction).
  3. extract_pitcher_stats()    -- entering-game pitching stats (postgame subtraction).
  4. build_authorized_snapshot()-- spoiler-safe pitch prefix for a given position.

Both POST /snapshot/redeem (main.py) and build_fact_sheet() (orchestrate_client.py)
MUST import from here.  No spoiler-filtering or stats-extraction logic may be
duplicated elsewhere.

Spoiler-safety guarantees (enforced here, nowhere else)
--------------------------------------------------------
- revealed_count is validated as a non-negative integer before any slice.
  Negative values and non-integer types raise DataError immediately so Python
  negative-index slicing can never expose unauthorized pitch events.
- Only pitches 1 ... revealed_count are returned.
- outcome is None unless is_complete is True.
- is_complete is True only when total_pitches > 0 AND revealed_count >= total_pitches.
  A zero-pitch at-bat at position 0 is NOT complete; outcome remains None.
- total_pitches is omitted from the return dict when is_complete is False,
  preventing the caller from inferring the remaining pitch count.
- Out-of-range error messages do not reveal total_pitches.
- batter_stats / pitcher_stats are entering-game only (postgame subtraction).
- Plate coordinates (pX, pZ) are numeric values only; no location labels.

Error types
-----------
- NetworkError : MLB Stats API call failed (network, timeout, HTTP error).
- DataError    : invalid revealed_count type/range, at-bat not found, or
                 revealed_count exceeds available pitches.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


class NetworkError(RuntimeError):
    """Raised when the MLB Stats API cannot be reached or returns an error."""


class DataError(ValueError):
    """Raised when inputs are invalid or the at-bat cannot be found."""


# ---------------------------------------------------------------------------
# MLB Stats API fetch
# ---------------------------------------------------------------------------

def fetch_game_data(game_id: int, timeout: int = 15) -> dict:
    """
    Fetch the full live feed for a game from the MLB Stats API.

    Parameters
    ----------
    game_id : MLB game identifier (must be > 0)
    timeout : HTTP timeout in seconds

    Returns
    -------
    Parsed JSON dict (the full MLB Stats API response).

    Raises
    ------
    NetworkError : on HTTP error, URL error, timeout, or JSON decode failure.
    """
    url = f"https://statsapi.mlb.com/api/v1.1/game/{game_id}/feed/live"
    try:
        with urlopen(url, timeout=timeout) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise NetworkError(
            f"MLB Stats API unavailable for game {game_id}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Entering-game statistics (postgame subtraction method)
# ---------------------------------------------------------------------------

def extract_batter_stats(boxscore_teams: dict, batter_id) -> dict | None:
    """
    Compute entering-game batting statistics for the batter.

    Uses the postgame seasonStats subtraction method: entering value =
    season total - this game's contribution.  Returns a stats dict on
    success, or None if any value is missing, the denominator is zero,
    or the intermediate totals are impossible.
    """
    batter_player = None
    for side in ("home", "away"):
        players = boxscore_teams.get(side, {}).get("players", {})
        key = f"ID{batter_id}"
        if key in players:
            batter_player = players[key]
            break

    if batter_player is None:
        return None

    season_bat = batter_player.get("seasonStats", {}).get("batting", {})
    game_bat   = batter_player.get("stats", {}).get("batting", {})

    season_hits = season_bat.get("hits")
    season_ab   = season_bat.get("atBats")
    game_hits   = game_bat.get("hits")
    game_ab     = game_bat.get("atBats")

    if any(v is None for v in (season_hits, season_ab, game_hits, game_ab)):
        return None

    entering_hits = int(season_hits) - int(game_hits)
    entering_ab   = int(season_ab)   - int(game_ab)

    if entering_hits < 0 or entering_ab < 0:
        return None
    if entering_ab == 0:
        return None
    if entering_hits > entering_ab:
        return None

    avg = round(entering_hits / entering_ab, 3)
    return {
        "avg":     avg,
        "hits":    entering_hits,
        "at_bats": entering_ab,
        "basis":   "entering_game",
    }


def extract_pitcher_stats(boxscore_teams: dict, pitcher_id) -> dict | None:
    """
    Compute entering-game pitching statistics for the pitcher.

    Uses the postgame seasonStats subtraction method.  The MLB API outs field
    is used directly (whole-number out count, not innings-pitched decimal).

    Returns a stats dict on success, or None if any value is missing,
    the denominator is zero, or the totals are impossible.
    """
    pitcher_player = None
    for side in ("home", "away"):
        players = boxscore_teams.get(side, {}).get("players", {})
        key = f"ID{pitcher_id}"
        if key in players:
            pitcher_player = players[key]
            break

    if pitcher_player is None:
        return None

    season_pit = pitcher_player.get("seasonStats", {}).get("pitching", {})
    game_pit   = pitcher_player.get("stats", {}).get("pitching", {})

    season_er   = season_pit.get("earnedRuns")
    season_outs = season_pit.get("outs")
    game_er     = game_pit.get("earnedRuns")
    game_outs   = game_pit.get("outs")

    if any(v is None for v in (season_er, season_outs, game_er, game_outs)):
        return None

    entering_er   = int(season_er)   - int(game_er)
    entering_outs = int(season_outs) - int(game_outs)

    if entering_er < 0 or entering_outs < 0:
        return None
    if entering_outs == 0:
        return None

    era = round(27 * entering_er / entering_outs, 2)
    return {
        "era":           era,
        "earned_runs":   entering_er,
        "outs_recorded": entering_outs,
        "basis":         "entering_game",
    }


# ---------------------------------------------------------------------------
# Authorized snapshot builder  (THE single spoiler gate)
# ---------------------------------------------------------------------------

# At-bats for which the postgame subtraction method has been independently
# verified to produce accurate entering-game statistics.
VERIFIED_STATS_AT_BATS: frozenset[tuple[int, int]] = frozenset({(777245, 8)})


def count_pitches_in_at_bat(game_data: dict, at_bat_index: int) -> int | None:
    """
    Return the total number of pitch events in the specified at-bat, or None
    if the at-bat is not found.  Does not enforce any spoiler gate.

    Used by get_at_bat() to determine the actual pitch count so it can call
    build_authorized_snapshot() with the full count without duplicating pitch
    extraction logic.
    """
    plays = game_data.get("liveData", {}).get("plays", {}).get("allPlays", [])
    at_bat = next(
        (play for play in plays
         if play.get("about", {}).get("atBatIndex") == at_bat_index),
        None,
    )
    if at_bat is None:
        return None
    return sum(1 for e in at_bat.get("playEvents", []) if e.get("isPitch"))


def build_authorized_snapshot(
    game_data: dict,
    game_id: int,
    at_bat_index: int,
    revealed_count: int,
) -> dict:
    """
    Build a spoiler-safe snapshot from an already-fetched game feed.

    Parameters
    ----------
    game_data      : full MLB Stats API response from fetch_game_data()
    game_id        : MLB game identifier
    at_bat_index   : zero-based at-bat index
    revealed_count : number of pitches to authorize.  Must be a non-negative
                     integer.  Position 0 returns an empty pitch list (pregame).

    Returns
    -------
    dict with keys:
        game_id, at_bat_index, revealed_count, is_complete,
        batter, pitcher, inning, half_inning,
        outcome (None unless is_complete is True),
        pitches (list, length == revealed_count),
        batter_stats (dict or None),
        pitcher_stats (dict or None),
        total_pitches (int, only present when is_complete is True)

    Raises
    ------
    DataError : if revealed_count is not a non-negative integer, if the
                at-bat is not found, or if revealed_count exceeds the
                available pitches.  Error messages do not reveal
                total_pitches or any future-game data.
    """
    # --- Guard 1: validate revealed_count type and sign before any slice ---
    if not isinstance(revealed_count, int) or isinstance(revealed_count, bool):
        raise DataError(
            f"revealed_count must be a non-negative integer, "
            f"got {type(revealed_count).__name__}"
        )
    if revealed_count < 0:
        raise DataError(
            "revealed_count must be >= 0 (use 0 for a pregame fact sheet)"
        )

    # --- Locate the at-bat ---
    plays = game_data.get("liveData", {}).get("plays", {}).get("allPlays", [])
    at_bat = next(
        (play for play in plays
         if play.get("about", {}).get("atBatIndex") == at_bat_index),
        None,
    )
    if at_bat is None:
        raise DataError(
            f"At-bat index {at_bat_index} not found in game {game_id}"
        )

    all_pitch_events = [
        event for event in at_bat.get("playEvents", [])
        if event.get("isPitch")
    ]
    total_pitches = len(all_pitch_events)

    # --- Guard 2: out-of-range check WITHOUT revealing total_pitches ---
    if revealed_count > total_pitches:
        raise DataError(
            f"revealed_count {revealed_count} is not a valid position "
            f"for this at-bat"
        )

    # --- Guard 3: zero-pitch at-bats are never complete at position 0 ---
    # is_complete is True only when there are pitches AND all are revealed.
    is_complete = (total_pitches > 0 and revealed_count >= total_pitches)

    # --- Build pitch list: only pitches 1 ... revealed_count ---
    # revealed_count is a validated non-negative integer; slicing is safe.
    authorized_events = all_pitch_events[:revealed_count]
    pitches = []
    for event in authorized_events:
        details     = event.get("details") or {}
        pitch_data  = event.get("pitchData") or {}
        coordinates = pitch_data.get("coordinates") or {}
        pitches.append({
            "number":            event.get("pitchNumber"),
            "type":              (details.get("type") or {}).get("description"),
            "result":            details.get("description"),
            "velocity_mph":      pitch_data.get("startSpeed"),
            "plate_x":           coordinates.get("pX"),
            "plate_z":           coordinates.get("pZ"),
            "count_after_pitch": event.get("count"),
        })

    matchup = at_bat.get("matchup", {})
    about   = at_bat.get("about", {})

    # Outcome: only when is_complete is True (never at position 0 or partial)
    outcome = at_bat.get("result", {}).get("event") if is_complete else None

    # Entering-game stats: only for verified at-bats
    batter_stats  = None
    pitcher_stats = None
    if (game_id, at_bat_index) in VERIFIED_STATS_AT_BATS:
        batter_id  = matchup.get("batter", {}).get("id")
        pitcher_id = matchup.get("pitcher", {}).get("id")
        boxscore_teams = (
            game_data.get("liveData", {})
                     .get("boxscore", {})
                     .get("teams", {})
        )
        try:
            batter_stats = extract_batter_stats(boxscore_teams, batter_id)
        except Exception:
            batter_stats = None
        try:
            pitcher_stats = extract_pitcher_stats(boxscore_teams, pitcher_id)
        except Exception:
            pitcher_stats = None

    result: dict = {
        "game_id":        game_id,
        "at_bat_index":   at_bat_index,
        "revealed_count": revealed_count,
        "is_complete":    is_complete,
        "batter":         matchup.get("batter", {}).get("fullName"),
        "pitcher":        matchup.get("pitcher", {}).get("fullName"),
        "inning":         about.get("inning"),
        "half_inning":    about.get("halfInning"),
        "outcome":        outcome,
        "pitches":        pitches,
        "batter_stats":   batter_stats,
        "pitcher_stats":  pitcher_stats,
    }
    if is_complete:
        result["total_pitches"] = total_pitches

    return result
