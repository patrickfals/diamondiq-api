import json
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

from fastapi import FastAPI, HTTPException

app = FastAPI(title="DiamondIQ Baseball API")


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "DiamondIQ Baseball API"
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

    return {
        "game_id": game_id,
        "at_bat_index": at_bat_index,
        "batter": matchup.get("batter", {}).get("fullName"),
        "pitcher": matchup.get("pitcher", {}).get("fullName"),
        "inning": about.get("inning"),
        "half_inning": about.get("halfInning"),
        "outcome": at_bat.get("result", {}).get("event"),
        "pitches": pitches
    }
