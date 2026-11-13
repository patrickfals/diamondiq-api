"""
snapshot.py — HMAC-signed snapshot tokens for spoiler-safe pitch-prefix delivery.

Design
------
A snapshot token encodes three non-secret baseball identifiers:

    game_id : at_bat_index : revealed_count

and an HMAC-SHA256 signature that guarantees the backend authored the token.
Agents receive a token and present it to POST /snapshot/redeem.  They cannot
modify the revealed_count or any other field without invalidating the signature.

Token format (URL-safe base64, no padding):
    base64url( "<payload>" + "." + "<hex_signature>" )

where <payload> = "<game_id>:<at_bat_index>:<revealed_count>"

The separator "." inside the base64 payload is chosen to be unambiguous: the
three integer fields contain only digits, and the separator "." cannot appear
in decimal integers, so parsing is unambiguous without quoting.

Security properties
-------------------
- Signing secret is read from SNAPSHOT_SECRET environment variable only.
  No default secret.  Missing secret → callers must handle the ValueError.
- HMAC comparison uses hmac.compare_digest (timing-safe).
- Tokens contain no credentials, secrets, or future-game data.
  Logging a token is harmless from a secrecy perspective, but the module
  never logs tokens itself; callers decide what to log.

Usage
-----
Internal backend use only.  No HTTP endpoint issues tokens.

    from snapshot import create_snapshot_token, verify_snapshot_token

    token = create_snapshot_token(777245, 8, 3)
    game_id, at_bat_index, revealed_count = verify_snapshot_token(token)
    # raises ValueError on tamper, malformed input, or missing secret
"""

import base64
import hashlib
import hmac
import os


def _get_secret() -> bytes:
    """
    Read SNAPSHOT_SECRET from the environment.

    Raises ValueError — not KeyError — so callers can produce a 503 response
    without matching on exception type.  The ValueError message does not
    include the secret value.
    """
    secret = os.environ.get("SNAPSHOT_SECRET")
    if not secret:
        raise ValueError("SNAPSHOT_SECRET environment variable is not set")
    return secret.encode("utf-8")


def _build_payload(game_id: int, at_bat_index: int, revealed_count: int) -> str:
    """
    Build the canonical payload string from three integers.

    The colon-separated format is unambiguous because the values are
    non-negative integers (no colons, no sign characters).
    """
    return f"{game_id}:{at_bat_index}:{revealed_count}"


def _sign_payload(payload: str, secret: bytes) -> str:
    """Return the lowercase hex HMAC-SHA256 digest of a UTF-8 payload."""
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_snapshot_token(
    game_id: int,
    at_bat_index: int,
    revealed_count: int,
) -> str:
    """
    Create a signed snapshot token for the given baseball identifiers.

    Parameters
    ----------
    game_id        : MLB game identifier (must be > 0)
    at_bat_index   : zero-based at-bat index (must be >= 0)
    revealed_count : number of pitches authorized (must be >= 0)

    Returns
    -------
    A URL-safe base64-encoded token (no padding) that encodes the payload
    and its HMAC-SHA256 signature.

    Raises
    ------
    ValueError : if any input is out of range, or SNAPSHOT_SECRET is unset.
    """
    if game_id <= 0:
        raise ValueError(f"game_id must be > 0, got {game_id}")
    if at_bat_index < 0:
        raise ValueError(f"at_bat_index must be >= 0, got {at_bat_index}")
    if revealed_count < 0:
        raise ValueError(f"revealed_count must be >= 0, got {revealed_count}")

    secret = _get_secret()
    payload = _build_payload(game_id, at_bat_index, revealed_count)
    signature = _sign_payload(payload, secret)
    raw = f"{payload}.{signature}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def verify_snapshot_token(
    token: str,
) -> tuple[int, int, int]:
    """
    Verify a snapshot token and return the encoded identifiers.

    Parameters
    ----------
    token : the opaque string returned by create_snapshot_token

    Returns
    -------
    (game_id, at_bat_index, revealed_count) as integers

    Raises
    ------
    ValueError : token is malformed (wrong structure, non-integer fields,
                 or values out of range)
    PermissionError : HMAC signature does not match (tampered token)
    ValueError : SNAPSHOT_SECRET is not set (message indicates config error)
    """
    # --- Decode base64 (re-add stripped padding) ---
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        raise ValueError("Malformed snapshot token: base64 decode failed")

    # --- Split into payload and signature ---
    # The payload contains exactly two colons (a:b:c) and no dots,
    # so splitting on "." gives exactly ["payload", "signature"].
    parts = raw.split(".")
    if len(parts) != 2:
        raise ValueError("Malformed snapshot token: expected payload.signature")

    payload_str, received_sig = parts[0], parts[1]

    # --- Verify HMAC before parsing payload (fail-fast on tamper) ---
    secret = _get_secret()
    expected_sig = _sign_payload(payload_str, secret)
    if not hmac.compare_digest(expected_sig, received_sig):
        raise PermissionError("Invalid snapshot token signature")

    # --- Parse integer fields from verified payload ---
    fields = payload_str.split(":")
    if len(fields) != 3:
        raise ValueError("Malformed snapshot token: payload must have three fields")

    try:
        game_id        = int(fields[0])
        at_bat_index   = int(fields[1])
        revealed_count = int(fields[2])
    except ValueError:
        raise ValueError("Malformed snapshot token: payload fields must be integers")

    if game_id <= 0 or at_bat_index < 0 or revealed_count < 0:
        raise ValueError("Malformed snapshot token: field values out of range")

    return game_id, at_bat_index, revealed_count
