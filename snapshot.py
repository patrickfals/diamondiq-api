"""
snapshot.py — HMAC-signed snapshot tokens for spoiler-safe pitch-prefix delivery.

Design
------
A snapshot token encodes three non-secret baseball identifiers plus an optional
non-secret run correlation ID:

    game_id : at_bat_index : revealed_count [: run_id]

and an HMAC-SHA256 signature that guarantees the backend authored the token.
Agents receive a token and present it to POST /snapshot/redeem.  They cannot
modify any field without invalidating the signature.

Token format (URL-safe base64, no padding):
    base64url( "<payload>" + "." + "<hex_signature>" )

where <payload> is one of:
    "<game_id>:<at_bat_index>:<revealed_count>"           (3-field, legacy)
    "<game_id>:<at_bat_index>:<revealed_count>:<run_id>"  (4-field, with correlation)

Backward compatibility
----------------------
verify_snapshot_token() returns a 4-tuple in both cases:
    (game_id, at_bat_index, revealed_count, run_id)
run_id is None for legacy 3-field tokens.
All existing callers that previously unpacked 3 values must be updated to
unpack 4.  Attempting a 3-value unpack raises ValueError at the call site.

run_id rules
------------
- Must be a non-empty string of hex characters only (0-9, a-f).
- Recommended length: 8–16 characters (secrets.token_hex(8) produces 16 chars).
- It is NOT a secret: safe to log, store in diagnostic records, return in
  API responses.  It provides correlation, not authentication.
- A token with a forged run_id fails the HMAC check.
- An empty string run_id is rejected at both create and verify time.

Security properties
-------------------
- Signing secret is read from SNAPSHOT_SECRET environment variable only.
  No default secret.  Missing secret → callers must handle the ValueError.
- HMAC comparison uses hmac.compare_digest (timing-safe).
- Tokens contain no credentials, secrets, or future-game data.
  The module never logs tokens itself; callers decide what to log.

Usage
-----
Internal backend use only.  No HTTP endpoint issues tokens.

    from snapshot import create_snapshot_token, verify_snapshot_token

    # Legacy (no run_id) — run_id is None in the returned tuple
    token = create_snapshot_token(777245, 8, 3)
    game_id, at_bat_index, revealed_count, run_id = verify_snapshot_token(token)

    # With run_id for run correlation
    import secrets
    token = create_snapshot_token(777245, 8, 3, run_id=secrets.token_hex(8))
    game_id, at_bat_index, revealed_count, run_id = verify_snapshot_token(token)
"""

import base64
import hashlib
import hmac
import os
import re as _re

# run_id must be exactly 16 lowercase hex characters.
_RUN_ID_RE = _re.compile(r'[0-9a-f]{16}')


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


def _build_payload(
    game_id: int,
    at_bat_index: int,
    revealed_count: int,
    run_id: str | None = None,
) -> str:
    """
    Build the canonical payload string.

    With run_id=None  → "game_id:at_bat_index:revealed_count"           (3-field)
    With run_id=<str> → "game_id:at_bat_index:revealed_count:run_id"    (4-field)

    run_id must be a non-empty lowercase hex string.  Colons are unambiguous
    delimiters because the integer fields contain only digits and run_id
    contains only hex characters (0-9, a-f); none of these include ':' or '.'.
    """
    if run_id is not None:
        if not run_id:
            raise ValueError("run_id must not be empty")
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError(
                f"run_id must be exactly 16 lowercase hex characters, got: {run_id!r}"
            )
        return f"{game_id}:{at_bat_index}:{revealed_count}:{run_id}"
    return f"{game_id}:{at_bat_index}:{revealed_count}"


def _sign_payload(payload: str, secret: bytes) -> str:
    """Return the lowercase hex HMAC-SHA256 digest of a UTF-8 payload."""
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_snapshot_token(
    game_id: int,
    at_bat_index: int,
    revealed_count: int,
    run_id: str | None = None,
) -> str:
    """
    Create a signed snapshot token for the given baseball identifiers.

    Parameters
    ----------
    game_id        : MLB game identifier (must be > 0)
    at_bat_index   : zero-based at-bat index (must be >= 0)
    revealed_count : number of pitches authorized (must be >= 0)
    run_id         : optional non-secret run correlation ID.
                     Must be exactly 16 lowercase hex characters when provided
                     (secrets.token_hex(8) produces exactly 16 chars).
                     Embedded in the signed payload; not a secret.

    Returns
    -------
    A URL-safe base64-encoded token (no padding) that encodes the payload
    and its HMAC-SHA256 signature.

    Raises
    ------
    ValueError : if any input is out of range, run_id is invalid,
                 or SNAPSHOT_SECRET is unset.
    """
    if game_id <= 0:
        raise ValueError(f"game_id must be > 0, got {game_id}")
    if at_bat_index < 0:
        raise ValueError(f"at_bat_index must be >= 0, got {at_bat_index}")
    if revealed_count < 0:
        raise ValueError(f"revealed_count must be >= 0, got {revealed_count}")

    secret = _get_secret()
    payload = _build_payload(game_id, at_bat_index, revealed_count, run_id)
    signature = _sign_payload(payload, secret)
    raw = f"{payload}.{signature}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def verify_snapshot_token(
    token: str,
) -> tuple[int, int, int, str | None]:
    """
    Verify a snapshot token and return the encoded identifiers.

    Parameters
    ----------
    token : the opaque string returned by create_snapshot_token

    Returns
    -------
    (game_id, at_bat_index, revealed_count, run_id)

    run_id is None for legacy 3-field tokens.
    run_id is a non-empty lowercase hex string for 4-field tokens.

    Raises
    ------
    ValueError      : token is malformed (wrong structure, non-integer fields,
                      invalid run_id format, or values out of range)
    PermissionError : HMAC signature does not match (tampered token)
    ValueError      : SNAPSHOT_SECRET is not set (message indicates config error)
    """
    # --- Decode base64 (re-add stripped padding) ---
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        raise ValueError("Malformed snapshot token: base64 decode failed")

    # --- Split into payload and signature ---
    # The payload contains colons only in integer and run_id fields; no dots.
    # Splitting on "." gives exactly ["payload", "signature"].
    parts = raw.split(".")
    if len(parts) != 2:
        raise ValueError("Malformed snapshot token: expected payload.signature")

    payload_str, received_sig = parts[0], parts[1]

    # --- Verify HMAC before parsing payload (fail-fast on tamper) ---
    secret = _get_secret()
    expected_sig = _sign_payload(payload_str, secret)
    if not hmac.compare_digest(expected_sig, received_sig):
        raise PermissionError("Invalid snapshot token signature")

    # --- Parse fields from verified payload ---
    # Accept 3-field (legacy) and 4-field (with run_id) payloads.
    fields = payload_str.split(":")
    if len(fields) not in (3, 4):
        raise ValueError(
            "Malformed snapshot token: payload must have three or four fields"
        )

    try:
        game_id        = int(fields[0])
        at_bat_index   = int(fields[1])
        revealed_count = int(fields[2])
    except ValueError:
        raise ValueError("Malformed snapshot token: payload fields must be integers")

    if game_id <= 0 or at_bat_index < 0 or revealed_count < 0:
        raise ValueError("Malformed snapshot token: field values out of range")

    # --- Extract and validate run_id if present ---
    run_id: str | None = None
    if len(fields) == 4:
        run_id = fields[3]
        if not run_id:
            raise ValueError("Malformed snapshot token: run_id field must not be empty")
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError(
                f"Malformed snapshot token: run_id must be exactly 16 lowercase "
                f"hex characters, got: {run_id!r}"
            )

    return game_id, at_bat_index, revealed_count, run_id
