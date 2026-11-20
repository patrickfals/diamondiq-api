"""
precompute_commentary.py — Offline-safe Stage A + B precomputation script.

Processes game 777245, at-bat 8, positions 1-10 (or a single position with
--position N).  Results are saved to commentary_cache.json (legacy) or
commentary_cache_v2.json + trace_cache.json (--v2 mode).

Safety rules
------------
- Requires --run-ibm for any live IBM execution.  Default makes zero IBM calls.
- Stops on the first failure.  Never retries IBM calls automatically.
- Saves each successful position atomically (write-then-replace).
- Skips positions already marked complete in the cache when resuming.
- Never stores tokens, secrets, or Authorization headers.
- Never falls back to a placeholder fact sheet during a live IBM run.
  If fact_sheet_fn is provided and raises, the position fails immediately.
- --v2 never reads from, writes to, or modifies commentary_cache.json.
- Exception text is never persisted in traces; only safe error class names
  and fixed diagnostic descriptions are stored.
- Console diagnostics apply simple credential redaction before printing.

Usage
-----
    python precompute_commentary.py                        # dry-run (no IBM)
    python precompute_commentary.py --run-ibm              # all 10 (legacy)
    python precompute_commentary.py --run-ibm --position 3 # one (legacy)
    python precompute_commentary.py --run-ibm --v2         # all 10 (V2)
    python precompute_commentary.py --run-ibm --v2 --position 3  # one (V2)
"""

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GAME_ID = 777245
AT_BAT_INDEX = 8
POSITIONS = list(range(1, 11))          # 1 through 10 inclusive

_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH       = os.path.join(_DIR, "commentary_cache.json")
CACHE_V2_PATH    = os.path.join(_DIR, "commentary_cache_v2.json")
TRACE_CACHE_PATH = os.path.join(_DIR, "trace_cache.json")

# Default backend base URL for the redemption diagnostic endpoint.
_DEFAULT_API_BASE = "https://diamondiq-api.onrender.com"

# Keys that must never appear in a saved cache entry.
_FORBIDDEN_CACHE_KEYS = frozenset(
    {"token", "access_token", "authorization", "apikey", "api_key"}
)

# Allowlisted keys from a redemption record (server-side safe fields only).
# Any field NOT in this set is silently dropped at persistence time.
_REDEMPTION_ALLOWLIST = frozenset(
    {"ts_utc", "game_id", "at_bat_index", "revealed_count",
     "run_id", "user_agent", "status"}
)

# Observable V2 trace note — recorded in every V2 trace.
_TRACE_INCOMPLETE_NOTE = (
    "IBM-internal agent delegation, snapshot-token creation, Pitch Scout "
    "tool execution, and individual HTTP request/response timings cannot be "
    "directly observed from the precompute script. These are not recorded."
)

# Sentinel values for _poll_redemption_internal return outcomes.
_REDEMPTION_FOUND   = "found"    # endpoint responded, run_id matched
_REDEMPTION_MISSING = "missing"  # endpoint responded, no run_id match
_REDEMPTION_ERROR   = "error"    # network/HTTP/parse failure


# ---------------------------------------------------------------------------
# Cache helpers (importable for tests)
# ---------------------------------------------------------------------------


def load_cache(cache_path: str = CACHE_PATH) -> dict:
    """Load existing cache from disk, or return an empty dict."""
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: could not read cache ({exc}); starting fresh.")
        return {}


def save_cache_atomic(cache: dict, cache_path: str = CACHE_PATH) -> None:
    """Write cache to disk atomically using a temp file + rename."""
    cache_dir = os.path.dirname(os.path.abspath(cache_path))
    fd, tmp_path = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2)
        os.replace(tmp_path, cache_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def assert_no_secrets(entry: dict) -> None:
    """Raise RuntimeError if any forbidden key appears in a cache entry."""
    lowered = {k.lower() for k in entry}
    found = lowered & _FORBIDDEN_CACHE_KEYS
    if found:
        raise RuntimeError(f"BUG: attempted to cache secret key(s): {found}")


# ---------------------------------------------------------------------------
# V2 helpers (importable for tests)
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _safe_meta(meta: dict | None) -> dict | None:
    """
    Sanitised copy of response_metadata for trace storage.

    Retains: id, created, usage, finish_reason, tool_calls
    (function names only — arguments and message content excluded).
    Returns None when meta is None or empty.
    """
    if not meta:
        return None
    out: dict = {}
    for field in ("id", "created", "usage", "finish_reason"):
        if field in meta:
            out[field] = meta[field]
    if "tool_calls" in meta:
        safe_calls = []
        for tc in meta.get("tool_calls", []):
            safe_tc = {k: v for k, v in tc.items()
                       if k in ("id", "type", "function_name")}
            if safe_tc:
                safe_calls.append(safe_tc)
        if safe_calls:
            out["tool_calls"] = safe_calls
    return out or None


def _build_trace_entry(position: int, run_id: str,
                        events: list, capture_notes: list) -> dict:
    """Build a trace cache entry dict for one position."""
    return {
        "position":       position,
        "run_id":         run_id,
        "events":         events,
        "trace_complete": False,
        "capture_notes":  list(capture_notes),
    }


def _redact_for_console(text: str) -> str:
    """
    Apply simple credential redaction to *text* before printing to console.

    Replaces the actual values of SNAPSHOT_SECRET, WO_API_KEY, and
    DIAMONDIQ_TEST_KEY (when set and non-empty) with [REDACTED].
    Also redacts any JSON-like "token": "..." field value.

    This is a narrow, deliberate implementation.  It does NOT attempt to
    detect all possible secrets — it replaces only the specific values named
    above and simple "token" JSON patterns.

    Never called at persistence time.  Persisted traces use only safe error
    class names and fixed diagnostic strings.
    """
    for env_var in ("SNAPSHOT_SECRET", "WO_API_KEY", "DIAMONDIQ_TEST_KEY"):
        val = os.environ.get(env_var, "")
        if val:
            text = text.replace(val, "[REDACTED]")
    # Redact JSON-like token field values: "token": "somevalue"
    import re as _re
    text = _re.sub(r'"token"\s*:\s*"[^"]*"', '"token": "[REDACTED]"', text)
    return text


def _safe_console_msg(exc: Exception) -> str:
    """
    Build a safe console diagnostic string from an exception.

    Applies _redact_for_console and truncates to 300 characters.
    Never returns raw IBM response content.
    """
    raw = f"{type(exc).__name__}: {exc}"
    redacted = _redact_for_console(raw)
    return redacted[:300]


# ---------------------------------------------------------------------------
# Redemption polling (V2 only, importable for tests)
# ---------------------------------------------------------------------------

def _poll_redemption_internal(run_id: str, test_key: str,
                               api_base: str) -> tuple:
    """
    Poll GET /snapshot/redemptions/recent for a record matching run_id.

    Returns:
        (_REDEMPTION_FOUND,   allowlisted_record_dict)  — matched
        (_REDEMPTION_MISSING, None)                      — no match found
        (_REDEMPTION_ERROR,   None)                      — request failed

    Never persists raw HTTP responses or exception text.
    Never called when test_key is None or empty.
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request as _Req, urlopen as _urlopen

    url = f"{api_base.rstrip('/')}/snapshot/redemptions/recent"
    try:
        req = _Req(url, headers={"X-DiamondIQ-Test-Key": test_key},
                   method="GET")
        with _urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except (HTTPError, URLError, OSError, ValueError):
        return _REDEMPTION_ERROR, None

    for record in data.get("redemptions", []):
        if record.get("run_id") == run_id:
            safe = {k: v for k, v in record.items()
                    if k in _REDEMPTION_ALLOWLIST}
            return _REDEMPTION_FOUND, safe

    return _REDEMPTION_MISSING, None


def _apply_redemption_to_trace(run_id: str, redemption_fn,
                                trace_events: list,
                                capture_notes: list,
                                add_event_fn) -> None:
    """
    Poll for redemption evidence and add an event / capture_note as appropriate.

    Correction 1: independently re-verifies that the returned record's run_id
    matches the current run_id.  Applies _REDEMPTION_ALLOWLIST at persistence.
    Never fabricates evidence.

    Parameters
    ----------
    run_id        : the run_id for this position
    redemption_fn : callable(run_id) -> (outcome, record|None), or None
    trace_events  : list to append snapshot_redeemed event to
    capture_notes : list to append notes to
    add_event_fn  : callable(name, **kwargs) used to append events
    """
    if redemption_fn is None:
        capture_notes.append(
            "Redemption polling disabled: DIAMONDIQ_TEST_KEY not set."
        )
        return

    try:
        outcome, record = redemption_fn(run_id)
    except Exception:
        capture_notes.append("Redemption endpoint request failed.")
        return

    if outcome == _REDEMPTION_FOUND and record is not None:
        # Independently verify run_id before creating event (Correction 1).
        if record.get("run_id") != run_id:
            capture_notes.append(
                "Redemption record run_id did not match this run."
            )
            return
        # Allowlist-filter at persistence time (Correction 1).
        safe_record = {k: v for k, v in record.items()
                       if k in _REDEMPTION_ALLOWLIST}
        add_event_fn("snapshot_redeemed",
                     evidence_source="render_redemption_log",
                     server_ts_utc=safe_record.get("ts_utc"),
                     note="server-reported timestamp, not directly observed",
                     record=safe_record)

    elif outcome == _REDEMPTION_MISSING:
        capture_notes.append(
            "No matching redemption record found for this run_id."
        )
    else:  # _REDEMPTION_ERROR or unexpected
        capture_notes.append("Redemption endpoint request failed.")


# ---------------------------------------------------------------------------
# Core precompute loop — legacy (importable for tests)
# ---------------------------------------------------------------------------


def run_precompute(
    positions: list,
    cache: dict,
    cache_path: str,
    stage_a_fn,
    stage_b_fn,
    game_id: int = GAME_ID,
    at_bat_index: int = AT_BAT_INDEX,
    fact_sheet_fn=None,
) -> dict:
    """
    Precompute Stage A + B commentary for each position in *positions*.

    Parameters
    ----------
    positions      : list of int positions to process (e.g. [1, 2, 3])
    cache          : current cache dict (mutated in-place on success)
    cache_path     : path for atomic saves
    stage_a_fn     : callable(game_id, at_bat_index, position) -> dict
    stage_b_fn     : callable(stage_a_dict) -> dict
    game_id        : MLB game identifier
    at_bat_index   : zero-based at-bat index
    fact_sheet_fn  : optional callable(game_id, at_bat_index, position) -> str

    Returns
    -------
    The (mutated) cache dict.
    """
    for position in positions:
        key = str(position)

        if cache.get(key, {}).get("complete") is True:
            print(f"[position {position:2d}] SKIP — already complete")
            continue

        print(f"[position {position:2d}] Starting ...")
        t_start = time.monotonic()

        try:
            stage_a = stage_a_fn(game_id, at_bat_index, position)
        except Exception as exc:
            print(f"[position {position:2d}] FAIL Stage A: "
                  f"{type(exc).__name__}: {exc}")
            sys.exit(1)

        stage_a["revealed_count"] = position

        if fact_sheet_fn is not None:
            try:
                fact_sheet = fact_sheet_fn(game_id, at_bat_index, position)
            except Exception as exc:
                print(f"[position {position:2d}] FAIL Fact sheet: "
                      f"{type(exc).__name__}: {exc}")
                sys.exit(1)
            if not fact_sheet or not fact_sheet.strip():
                print(f"[position {position:2d}] FAIL Fact sheet: "
                      f"fact_sheet_fn returned empty text for position {position}")
                sys.exit(1)
            stage_a["fact_sheet"] = fact_sheet

        try:
            stage_b = stage_b_fn(stage_a)
        except Exception as exc:
            print(f"[position {position:2d}] FAIL Stage B: "
                  f"{type(exc).__name__}: {exc}")
            sys.exit(1)

        elapsed = time.monotonic() - t_start

        entry = {
            "commentary_by_level": stage_b["commentary_by_level"],
            "evidence_pitches":    stage_b["evidence_pitches"],
            "complete":            True,
            "elapsed_seconds":     round(elapsed, 2),
        }
        assert_no_secrets(entry)
        cache[key] = entry
        save_cache_atomic(cache, cache_path)

        print(f"[position {position:2d}] OK  ({elapsed:.1f}s)  "
              f"evidence={stage_b['evidence_pitches']}")

    return cache


# ---------------------------------------------------------------------------
# V2 precompute loop (importable for tests)
# ---------------------------------------------------------------------------


def run_v2_precompute(
    positions: list,
    v2_cache: dict,
    v2_cache_path: str,
    trace_cache: dict,
    trace_cache_path: str,
    stage_a_fn,
    stage_b_fn,
    game_id: int = GAME_ID,
    at_bat_index: int = AT_BAT_INDEX,
    fact_sheet_fn=None,
    redemption_fn=None,
) -> tuple:
    """
    V2 precompute: commentary_cache_v2.json + trace_cache.json.

    Parameters
    ----------
    positions        : list of int positions to process
    v2_cache         : V2 commentary cache dict (mutated in-place)
    v2_cache_path    : path for atomic V2 commentary writes
    trace_cache      : trace cache dict (mutated in-place)
    trace_cache_path : path for atomic trace writes
    stage_a_fn       : callable(game_id, at_bat_index, position, run_id=...) -> dict
    stage_b_fn       : callable(stage_a_dict) -> dict
    game_id          : MLB game identifier
    at_bat_index     : zero-based at-bat index
    fact_sheet_fn    : callable(game_id, at_bat_index, position) -> str
                       Required in V2.  Fails fast when None or empty.
    redemption_fn    : optional callable(run_id) -> (outcome, record|None)

    Returns
    -------
    (v2_cache, trace_cache) — both mutated dicts.

    Write order (Correction 2)
    --------------------------
    For each completed position:
      1. Construct v2_entry and trace_entry.
      2. save_cache_atomic(trace_cache, trace_cache_path)   ← first
      3. save_cache_atomic(v2_cache, v2_cache_path)         ← only on trace success
    A position is complete only when its v2_cache entry has been saved.
    If trace write fails, v2_cache is not marked complete.

    Console diagnostics (Correction 3)
    ------------------------------------
    _safe_console_msg() applies credential redaction before printing.
    Exception text is never stored in traces or v2 cache.
    """
    import secrets as _secrets

    for position in positions:
        key = str(position)

        # --- Resume: skip completed V2 positions ---
        if v2_cache.get(key, {}).get("complete") is True:
            print(f"[v2 position {position:2d}] SKIP — already complete")
            continue

        # --- V2 requires a fact_sheet_fn (Correction from prior task) ---
        if fact_sheet_fn is None:
            print(f"[v2 position {position:2d}] FAIL: "
                  "fact_sheet_fn is required in V2 mode but was not provided")
            sys.exit(1)

        # --- Fresh run_id for this position ---
        run_id = _secrets.token_hex(8)

        print(f"[v2 position {position:2d}] Starting ... run_id={run_id}")
        t_run_start = time.monotonic()

        trace_events: list = []
        capture_notes: list = [_TRACE_INCOMPLETE_NOTE]

        def _add_event(name: str, **extra):
            ev = {
                "event":           name,
                "ts_utc":          _now_utc(),
                "elapsed_seconds": round(time.monotonic() - t_run_start, 4),
                "run_id":          run_id,
            }
            ev.update(extra)
            trace_events.append(ev)

        def _fail(stage: str, error_class: str, diagnostic: str,
                  exc: Exception | None = None) -> None:
            """
            Record a run_failed event and persist trace; sys.exit(1).
            Correction 3: exception text printed to console (redacted),
            never stored in trace or v2_cache.
            """
            # Persist: safe class name + fixed diagnostic only
            _add_event("run_failed",
                       stage=stage,
                       error_class=error_class,
                       diagnostic=diagnostic)
            trace_cache[key] = _build_trace_entry(
                position, run_id, trace_events, capture_notes
            )
            try:
                save_cache_atomic(trace_cache, trace_cache_path)
            except Exception:
                pass  # best-effort trace save on failure path

            # Console: safe message with redaction (never persisted)
            if exc is not None:
                console_detail = _safe_console_msg(exc)
            else:
                console_detail = diagnostic
            print(f"[v2 position {position:2d}] FAIL {stage}: {console_detail}")
            sys.exit(1)

        _add_event("run_started",
                   position=position,
                   game_id=game_id,
                   at_bat_index=at_bat_index)

        # --- Stage A ---
        _add_event("stage_a_call_started")
        try:
            stage_a = stage_a_fn(game_id, at_bat_index, position, run_id=run_id)
        except Exception as exc:
            _fail("stage_a", type(exc).__name__,
                  "Stage A returned an error. See console output.", exc)

        _add_event("stage_a_call_returned",
                   meta=_safe_meta(stage_a.get("response_metadata")))

        # --- Authoritative revealed_count ---
        stage_a["revealed_count"] = position

        # --- Fact sheet ---
        try:
            fact_sheet = fact_sheet_fn(game_id, at_bat_index, position)
        except Exception as exc:
            _fail("fact_sheet", type(exc).__name__,
                  "Fact sheet generation raised an exception. See console output.",
                  exc)
        if not fact_sheet or not fact_sheet.strip():
            _fail("fact_sheet", "ValueError",
                  "Fact sheet function returned empty text.")
        stage_a["fact_sheet"] = fact_sheet

        # --- Redemption poll (Correction 1: verified + allowlisted) ---
        _apply_redemption_to_trace(
            run_id, redemption_fn, trace_events, capture_notes, _add_event
        )

        # --- Stage B ---
        _add_event("stage_b_call_started")
        try:
            stage_b = stage_b_fn(stage_a)
        except Exception as exc:
            _fail("stage_b", type(exc).__name__,
                  "Stage B returned an error. See console output.", exc)

        _add_event("stage_b_call_returned",
                   meta=_safe_meta(stage_b.get("response_metadata")))

        elapsed_total = round(time.monotonic() - t_run_start, 3)
        _add_event("run_completed", elapsed_total_seconds=elapsed_total)

        # --- Build entries ---
        v2_entry = {
            "commentary_by_level": stage_b["commentary_by_level"],
            "evidence_pitches":    stage_b["evidence_pitches"],
            "complete":            True,
            "elapsed_seconds":     elapsed_total,
            "run_id":              run_id,
        }
        assert_no_secrets(v2_entry)

        trace_entry = _build_trace_entry(
            position, run_id, trace_events, capture_notes
        )

        # --- Correction 2: write trace first, then commentary ---
        trace_cache[key] = trace_entry
        save_cache_atomic(trace_cache, trace_cache_path)   # ← FIRST
        v2_cache[key] = v2_entry
        save_cache_atomic(v2_cache, v2_cache_path)         # ← ONLY after trace

        print(f"[v2 position {position:2d}] OK  ({elapsed_total:.1f}s)  "
              f"run_id={run_id}  evidence={stage_b['evidence_pitches']}")

    return v2_cache, trace_cache


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Precompute Stage A+B commentary for game 777245, at-bat 8."
    )
    parser.add_argument(
        "--run-ibm",
        action="store_true",
        help="Authorise live IBM API calls.  Required for any network activity.",
    )
    parser.add_argument(
        "--position",
        type=int,
        metavar="N",
        help="Process only this position (1-10).  Omit to process all.",
    )
    parser.add_argument(
        "--v2",
        action="store_true",
        help=(
            "V2 mode: write to commentary_cache_v2.json and trace_cache.json. "
            "Never reads or writes commentary_cache.json."
        ),
    )
    args = parser.parse_args()

    if args.position is not None and args.position not in POSITIONS:
        print(f"ERROR: --position must be 1-10, got {args.position}")
        sys.exit(2)

    positions_to_run = [args.position] if args.position is not None else POSITIONS

    if not args.run_ibm:
        mode = "V2" if args.v2 else "legacy"
        print(f"Dry-run mode ({mode}): no IBM calls will be made.")
        print(f"Would process positions: {positions_to_run}")
        print("Pass --run-ibm to execute live precomputation.")
        sys.exit(0)

    # IBM imports — only reached when --run-ibm is set
    from mlb_data import fetch_game_data
    from orchestrate_client import (
        build_fact_sheet,
        generate_fan_commentary,
        generate_stage_a,
    )

    # Fetch game data once and reuse for all positions.
    print(f"Fetching MLB data for game {GAME_ID} ...")
    try:
        game_data = fetch_game_data(GAME_ID)
    except Exception as exc:
        print(f"FAIL: could not fetch game data: {type(exc).__name__}: {exc}")
        sys.exit(1)

    def _fact_sheet_fn(gid, ab_idx, position):
        return build_fact_sheet(
            game_data,
            game_id=gid,
            at_bat_index=ab_idx,
            revealed_count=position,
        )

    if args.v2:
        v2_cache    = load_cache(CACHE_V2_PATH)
        trace_cache = load_cache(TRACE_CACHE_PATH)
        print(f"V2 cache   : {CACHE_V2_PATH}")
        print(f"Trace cache: {TRACE_CACHE_PATH}")
        print(f"Positions  : {positions_to_run}")
        print()

        test_key = os.environ.get("DIAMONDIQ_TEST_KEY")
        api_base = os.environ.get("DIAMONDIQ_API_BASE", _DEFAULT_API_BASE)

        if test_key:
            def _redemption_fn(run_id):
                return _poll_redemption_internal(run_id, test_key, api_base)
        else:
            _redemption_fn = None
            print("NOTE: DIAMONDIQ_TEST_KEY not set; redemption polling disabled.")

        run_v2_precompute(
            positions=positions_to_run,
            v2_cache=v2_cache,
            v2_cache_path=CACHE_V2_PATH,
            trace_cache=trace_cache,
            trace_cache_path=TRACE_CACHE_PATH,
            stage_a_fn=generate_stage_a,
            stage_b_fn=generate_fan_commentary,
            fact_sheet_fn=_fact_sheet_fn,
            redemption_fn=_redemption_fn,
        )
    else:
        cache = load_cache(CACHE_PATH)
        print(f"Cache path : {CACHE_PATH}")
        print(f"Positions  : {positions_to_run}")
        print()

        run_precompute(
            positions=positions_to_run,
            cache=cache,
            cache_path=CACHE_PATH,
            stage_a_fn=generate_stage_a,
            stage_b_fn=generate_fan_commentary,
            fact_sheet_fn=_fact_sheet_fn,
        )

    print()
    print("Precomputation complete.")


if __name__ == "__main__":
    _main()
