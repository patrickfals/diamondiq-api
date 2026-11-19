import collections
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from mlb_data import (
    DataError,
    NetworkError,
    build_authorized_snapshot,
    count_pitches_in_at_bat,
    fetch_game_data,
)
from orchestrate_client import analyze_at_bat
from snapshot import verify_snapshot_token

logger = logging.getLogger(__name__)

app = FastAPI(
    title="DiamondIQ Baseball API",
    servers=[{"url": "https://diamondiq-api.onrender.com"}]
)

# ---------------------------------------------------------------------------
# Redemption history
# ---------------------------------------------------------------------------
# Bounded in-memory ring buffer of recent successful /snapshot/redeem events.
# Records run_id (from token when present) for correlation with the precompute
# script.  No tokens, secrets, pitch data, or authorization headers are stored.
#
# Limitation: in-memory only.  Lost on Render process restart or if Render
# runs multiple worker processes.  Intended for single-process pilot use only.
# ---------------------------------------------------------------------------
_MAX_REDEMPTION_HISTORY = 50
_redemption_history: collections.deque = collections.deque(
    maxlen=_MAX_REDEMPTION_HISTORY
)


def _record_redemption(
    game_id: int,
    at_bat_index: int,
    revealed_count: int,
    run_id: str | None,
    user_agent: str | None,
) -> None:
    """
    Append a safe redemption record to the in-memory history.

    Never stores tokens, secrets, pitch data, or authorization headers.
    user_agent is truncated to 120 characters; it is not authenticated proof
    of the caller and is stored only as supporting context.
    """
    safe_ua: str | None = user_agent[:120] if user_agent else None
    _redemption_history.append({
        "ts_utc":         datetime.now(timezone.utc).isoformat(),
        "game_id":        game_id,
        "at_bat_index":   at_bat_index,
        "revealed_count": revealed_count,
        "run_id":         run_id,
        "user_agent":     safe_ua,
        "status":         "ok",
    })


def _check_test_key(x_diamondiq_test_key: str | None) -> None:
    """Shared guard for test-key-protected endpoints."""
    expected_key = os.environ.get("DIAMONDIQ_TEST_KEY")
    if (
        not expected_key
        or not x_diamondiq_test_key
        or not secrets.compare_digest(x_diamondiq_test_key, expected_key)
    ):
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "DiamondIQ Baseball API"}


@app.get("/games/{game_id}/at-bats/{at_bat_index}")
def get_at_bat(game_id: int, at_bat_index: int):
    """
    Return full at-bat data for the requested game and at-bat index.

    Not registered as an IBM agent tool.
    Always returns the complete at-bat (all pitches, final outcome).

    Response keys — exactly 10, identical to previous implementation:
        game_id, at_bat_index, batter, pitcher, inning, half_inning,
        outcome, pitches, batter_stats, pitcher_stats

    Single MLB network fetch per request.

    Zero-pitch at-bat note
    ----------------------
    build_authorized_snapshot() with revealed_count=0 returns outcome=None
    (Guard 3: spoiler-safe builder never reveals outcome when is_complete=False).
    For this full-GET endpoint we always want the recorded outcome regardless
    of pitch count, so for the total==0 case we read the outcome directly from
    game_data after building the snapshot.  This preserves the existing API
    contract without weakening the spoiler-safe builder.
    """
    if game_id <= 0 or at_bat_index < 0:
        raise HTTPException(status_code=400, detail="Invalid ID")

    # --- Single MLB fetch ---
    try:
        game_data = fetch_game_data(game_id)
    except NetworkError:
        raise HTTPException(
            status_code=502,
            detail="Unable to retrieve MLB game data",
        )

    # --- Determine actual pitch count ---
    total = count_pitches_in_at_bat(game_data, at_bat_index)
    if total is None:
        raise HTTPException(status_code=404, detail="At-bat not found")

    # --- Build full at-bat via shared builder (revealed_count == total) ---
    # For total > 0: is_complete=True, outcome included, correct pitch list.
    # For total == 0: is_complete=False (Guard 3), outcome=None — corrected below.
    try:
        snapshot = build_authorized_snapshot(
            game_data,
            game_id=game_id,
            at_bat_index=at_bat_index,
            revealed_count=total,
        )
    except DataError:
        # Should not occur: total came from count_pitches_in_at_bat on the
        # same game_data, so the at-bat exists and count is valid.
        raise HTTPException(status_code=404, detail="At-bat not found")

    # --- Resolve outcome (zero-pitch preservation) ---
    # For normal at-bats (total > 0), snapshot["outcome"] is correct.
    # For zero-pitch at-bats, is_complete=False so builder returns None;
    # read the raw outcome to preserve the previous API contract.
    if total == 0:
        plays = game_data.get("liveData", {}).get("plays", {}).get("allPlays", [])
        at_bat_raw = next(
            (p for p in plays
             if p.get("about", {}).get("atBatIndex") == at_bat_index),
            None,
        )
        outcome = at_bat_raw.get("result", {}).get("event") if at_bat_raw else None
    else:
        outcome = snapshot["outcome"]

    # --- Return exactly the original 10 public response keys ---
    # revealed_count, is_complete, and total_pitches are internal builder
    # fields; they are intentionally excluded from this public endpoint.
    return {
        "game_id":       snapshot["game_id"],
        "at_bat_index":  snapshot["at_bat_index"],
        "batter":        snapshot["batter"],
        "pitcher":       snapshot["pitcher"],
        "inning":        snapshot["inning"],
        "half_inning":   snapshot["half_inning"],
        "outcome":       outcome,
        "pitches":       snapshot["pitches"],
        "batter_stats":  snapshot["batter_stats"],
        "pitcher_stats": snapshot["pitcher_stats"],
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
    x_diamondiq_test_key: str | None = Header(default=None),
):
    _check_test_key(x_diamondiq_test_key)

    try:
        story = analyze_at_bat(
            request.game_id,
            request.at_bat_index,
            request.knowledge_level,
        )
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Unable to generate IBM analysis",
        )

    return {"story": story}


# ---------------------------------------------------------------------------
# Snapshot redemption
# ---------------------------------------------------------------------------
# POST /snapshot/redeem is the ONLY endpoint registered as a Pitch Scout tool
# in IBM Orchestrate.  It accepts a signed token and returns a spoiler-safe
# pitch prefix.
#
# Token creation is strictly internal (snapshot.create_snapshot_token).
# No HTTP endpoint issues tokens.
#
# revealed_count=0 is a valid token value (Stage 1 tests and OpenAPI spec
# allow it; it returns an empty pitch list with no spoiler data).
# ---------------------------------------------------------------------------

class RedeemSnapshotRequest(BaseModel):
    token: str


@app.post("/snapshot/redeem")
def redeem_snapshot(
    request: RedeemSnapshotRequest,
    user_agent: str | None = Header(default=None),
):
    """
    Redeem a signed snapshot token and return a spoiler-safe pitch prefix.

    Spoiler-safety guarantees (enforced by mlb_data.build_authorized_snapshot)
    --------------------------------------------------------------------------
    - Only pitches 1 … revealed_count are returned.
    - outcome is null unless revealed_count >= total_pitches (is_complete).
    - total_pitches is omitted when is_complete is false.
    - Entering-game AVG / ERA are included (pre-game, non-spoiler).

    Redemption history
    ------------------
    Each successful redemption is appended to the in-memory ring buffer
    with run_id for correlation.  Tokens and secrets are never stored or logged.
    """
    # --- Verify token (HMAC checked before any payload values are used) ---
    try:
        game_id, at_bat_index, revealed_count, run_id = verify_snapshot_token(
            request.token
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Invalid snapshot token")
    except ValueError as exc:
        if "SNAPSHOT_SECRET" in str(exc):
            raise HTTPException(
                status_code=503, detail="Snapshot signing not configured"
            )
        # Generic 400 — do not expose internal token structure details.
        raise HTTPException(status_code=400, detail="Malformed snapshot token")

    # --- Single MLB fetch ---
    try:
        game_data = fetch_game_data(game_id)
    except NetworkError:
        logger.warning(
            "snapshot_redeem game=%d at_bat=%d count=%d status=mlb_error",
            game_id, at_bat_index, revealed_count,
        )
        raise HTTPException(
            status_code=502, detail="Unable to retrieve MLB game data"
        )

    # --- Build spoiler-safe snapshot ---
    try:
        snapshot = build_authorized_snapshot(
            game_data,
            game_id=game_id,
            at_bat_index=at_bat_index,
            revealed_count=revealed_count,
        )
    except DataError as exc:
        msg = str(exc)
        if "not found" in msg:
            logger.warning(
                "snapshot_redeem game=%d at_bat=%d count=%d status=not_found",
                game_id, at_bat_index, revealed_count,
            )
            raise HTTPException(status_code=404, detail="At-bat not found")
        logger.warning(
            "snapshot_redeem game=%d at_bat=%d count=%d status=count_exceeds_total",
            game_id, at_bat_index, revealed_count,
        )
        raise HTTPException(
            status_code=404,
            detail="revealed_count exceeds available pitches",
        )

    # --- Record redemption (run_id used for correlation; no token stored) ---
    _record_redemption(game_id, at_bat_index, revealed_count, run_id, user_agent)

    logger.info(
        "snapshot_redeem ts=%s game=%d at_bat=%d authorized_count=%d "
        "run_id=%s is_complete=%s status=ok",
        datetime.now(timezone.utc).isoformat(),
        game_id,
        at_bat_index,
        revealed_count,
        run_id,
        snapshot["is_complete"],
    )

    return snapshot


# ---------------------------------------------------------------------------
# Diagnostic: recent redemption history
# ---------------------------------------------------------------------------
# Protected by X-DiamondIQ-Test-Key (same guard as /analyze-at-bat).
# Not registered as an IBM agent tool.  Not accessible to the mobile app.
# Returns no pitch data, no tokens, no secrets.
# ---------------------------------------------------------------------------

@app.get("/snapshot/redemptions/recent")
def recent_redemptions(
    x_diamondiq_test_key: str | None = Header(default=None),
):
    """
    Return the recent in-memory redemption history (diagnostic only).

    Each record: ts_utc, game_id, at_bat_index, revealed_count,
    run_id (or null), user_agent (truncated, or null), status.

    No tokens, secrets, pitch data, or authorization headers are included.
    Protected by X-DiamondIQ-Test-Key.
    """
    _check_test_key(x_diamondiq_test_key)
    return {"redemptions": list(_redemption_history)}
