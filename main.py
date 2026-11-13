import json
import os
import secrets
from typing import Literal
from pydantic import BaseModel, Field
from fastapi import Header
from orchestrate_client import analyze_at_bat
from urllib.request import urlopen
from urllib.error import HTTPError, URLError
from fastapi import FastAPI, HTTPException

app = FastAPI(
    title="DiamondIQ Baseball API",
    servers=[{"url": "https://diamondiq-api.onrender.com"}]
)

# Games for which the postgame seasonStats subtraction method has been
# independently verified to produce accurate entering-game statistics.
# See Task 4 Phase 1 investigation for game 777245.
# Do not add a game ID here without establishing the statistical cutoff.
VERIFIED_STATS_AT_BATS = {(777245, 8)}


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "DiamondIQ Baseball API"
    }


def _extract_batter_stats(boxscore_teams: dict, batter_id) -> dict | None:
    """
    Compute entering-game batting statistics for the batter.

    Uses the postgame seasonStats subtraction method: entering value =
    season total - this game's contribution. Only valid when all four
    source values are present, the denominator is positive, and the
    intermediate totals are not negative (which would indicate a data
    inconsistency).

    Returns a stats dict on success, or None if any value is missing,
    zero-denominator, or produces an impossible result.
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

    # Reject impossible totals
    if entering_hits < 0 or entering_ab < 0:
        return None
    if entering_ab == 0:
        return None
    # Hits cannot exceed at-bats
    if entering_hits > entering_ab:
        return None

    avg = round(entering_hits / entering_ab, 3)
    return {
        "avg":     avg,
        "hits":    entering_hits,
        "at_bats": entering_ab,
        "basis":   "entering_game",
    }


def _extract_pitcher_stats(boxscore_teams: dict, pitcher_id) -> dict | None:
    """
    Compute entering-game pitching statistics for the pitcher.

    Uses the postgame seasonStats subtraction method: entering value =
    season total - this game's contribution. The MLB API outs field is
    used directly (already expressed as a whole-number out count, not
    innings-pitched decimal notation).

    Returns a stats dict on success, or None if any value is missing,
    zero-denominator, or produces an impossible result.
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

    # Reject impossible totals
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


@app.get("/games/{game_id}/at-bats/{at_bat_index}")
def get_at_bat(game_id: int, at_bat_index: int):

    # Check that the requested IDs are valid
    if game_id <= 0 or at_bat_index < 0:
        raise HTTPException(status_code=400, detail="Invalid ID")

    # Retrieve the game from MLB
    url = f"https://statsapi.mlb.com/api/v1.1/game/{game_id}/feed/live"

    try:
        with urlopen(url, timeout=15) as response:
            game_data = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError):
        raise HTTPException(
            status_code=502,
            detail="Unable to retrieve MLB game data"
        )

    # Find all plate appearances
    plays = game_data.get("liveData", {}).get("plays", {}).get("allPlays", [])

    # Find the requested at-bat
    at_bat = next(
        (play for play in plays
         if play.get("about", {}).get("atBatIndex") == at_bat_index),
        None
    )

    if at_bat is None:
        raise HTTPException(status_code=404, detail="At-bat not found")

    # Ignore non-pitch events
    pitch_events = [
        event for event in at_bat.get("playEvents", [])
        if event.get("isPitch")
    ]

    # Return only the information DiamondIQ needs
    pitches = []

    for event in pitch_events:
        details = event.get("details") or {}
        pitch_data = event.get("pitchData") or {}
        coordinates = pitch_data.get("coordinates") or {}

        pitches.append({
            "number": event.get("pitchNumber"),
            "type": (details.get("type") or {}).get("description"),
            "result": details.get("description"),
            "velocity_mph": pitch_data.get("startSpeed"),
            "plate_x": coordinates.get("pX"),
            "plate_z": coordinates.get("pZ"),
            "count_after_pitch": event.get("count")
        })

    matchup = at_bat.get("matchup", {})
    about = at_bat.get("about", {})

    # -- Historical entering-game statistics ----------------------------------
    # Only computed for games whose statistical cutoff has been independently
    # verified (see VERIFIED_STATS_GAME_IDS). All other games return null for
    # both fields without affecting pitch data or other response fields.
    #
    # Batter and pitcher statistics are extracted independently: a failure in
    # one does not suppress the other.
    batter_stats = None
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
            batter_stats = _extract_batter_stats(boxscore_teams, batter_id)
        except Exception:
            batter_stats = None

        try:
            pitcher_stats = _extract_pitcher_stats(boxscore_teams, pitcher_id)
        except Exception:
            pitcher_stats = None

    return {
        "game_id": game_id,
        "at_bat_index": at_bat_index,
        "batter": matchup.get("batter", {}).get("fullName"),
        "pitcher": matchup.get("pitcher", {}).get("fullName"),
        "inning": about.get("inning"),
        "half_inning": about.get("halfInning"),
        "outcome": at_bat.get("result", {}).get("event"),
        "pitches": pitches,
        "batter_stats":  batter_stats,
        "pitcher_stats": pitcher_stats,
    }


class AnalyzeAtBatRequest(BaseModel):
    game_id: int = Field(gt=0)
    at_bat_index: int = Field(ge=0)
    knowledge_level: Literal[
        "I don't know baseball",
        "I somewhat know the rules",
        "I watch a good amount",
        "I'm a scout / super fan"
    ]


@app.post("/analyze-at-bat")
def analyze_at_bat_endpoint(
    request: AnalyzeAtBatRequest,
    x_diamondiq_test_key: str | None = Header(default=None)
):
    # Restrict access while testing the IBM integration
    expected_key = os.environ.get("DIAMONDIQ_TEST_KEY")

    if (
        not expected_key
        or not x_diamondiq_test_key
        or not secrets.compare_digest(x_diamondiq_test_key, expected_key)
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        story = analyze_at_bat(
            request.game_id,
            request.at_bat_index,
            request.knowledge_level
        )
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Unable to generate IBM analysis"
        )

    return {"story": story}