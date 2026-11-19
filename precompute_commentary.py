"""
precompute_commentary.py — Offline-safe Stage A + B precomputation script.

Processes game 777245, at-bat 8, positions 1-10 (or a single position with
--position N).  Results are saved to commentary_cache.json, keyed by
string position number.

Safety rules
------------
- Requires --run-ibm for any live IBM execution.  Default makes zero IBM calls.
- Stops on the first failure.  Never retries IBM calls automatically.
- Saves each successful position atomically (write-then-replace).
- Skips positions already marked complete in the cache when resuming.
- Never stores tokens, secrets, or Authorization headers.

Usage
-----
    python precompute_commentary.py                       # dry-run (no IBM)
    python precompute_commentary.py --run-ibm             # all 10 positions
    python precompute_commentary.py --run-ibm --position 3  # one position only
"""

import argparse
import json
import os
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GAME_ID = 777245
AT_BAT_INDEX = 8
POSITIONS = list(range(1, 11))          # 1 through 10 inclusive
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "commentary_cache.json")

# Keys that must never appear in a saved cache entry.
_FORBIDDEN_CACHE_KEYS = frozenset(
    {"token", "access_token", "authorization", "apikey", "api_key"}
)

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
# Core precompute loop (importable for tests)
# ---------------------------------------------------------------------------


def run_precompute(
    positions: list,
    cache: dict,
    cache_path: str,
    stage_a_fn,
    stage_b_fn,
    game_id: int = GAME_ID,
    at_bat_index: int = AT_BAT_INDEX,
) -> dict:
    """
    Precompute Stage A + B commentary for each position in *positions*.

    Parameters
    ----------
    positions    : list of int positions to process (e.g. [1, 2, 3])
    cache        : current cache dict (mutated in-place on success)
    cache_path   : path for atomic saves
    stage_a_fn   : callable(game_id, at_bat_index, position) -> dict
    stage_b_fn   : callable(stage_a_dict) -> dict
    game_id      : MLB game identifier
    at_bat_index : zero-based at-bat index

    Returns
    -------
    The (mutated) cache dict.

    Side effects
    ------------
    - Prints progress to stdout.
    - Calls save_cache_atomic() after each successful position.
    - Calls sys.exit(1) on the first failure.
    """
    for position in positions:
        key = str(position)

        # --- Resume: skip completed positions ---
        if cache.get(key, {}).get("complete") is True:
            print(f"[position {position:2d}] SKIP — already complete")
            continue

        print(f"[position {position:2d}] Starting ...")
        t_start = time.monotonic()

        # --- Stage A ---
        try:
            stage_a = stage_a_fn(game_id, at_bat_index, position)
        except Exception as exc:
            print(f"[position {position:2d}] FAIL Stage A: "
                  f"{type(exc).__name__}: {exc}")
            sys.exit(1)

        # --- Stage B ---
        try:
            stage_b = stage_b_fn(stage_a)
        except Exception as exc:
            print(f"[position {position:2d}] FAIL Stage B: "
                  f"{type(exc).__name__}: {exc}")
            sys.exit(1)

        elapsed = time.monotonic() - t_start

        # --- Build cache entry (no tokens, no secrets) ---
        entry = {
            "commentary_by_level": stage_b["commentary_by_level"],
            "evidence_pitches":    stage_b["evidence_pitches"],
            "complete":            True,
            "elapsed_seconds":     round(elapsed, 2),
        }
        assert_no_secrets(entry)
        cache[key] = entry

        # --- Atomic save after each successful position ---
        save_cache_atomic(cache, cache_path)

        print(f"[position {position:2d}] OK  ({elapsed:.1f}s)  "
              f"evidence={stage_b['evidence_pitches']}")

    return cache


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
    args = parser.parse_args()

    if args.position is not None and args.position not in POSITIONS:
        print(f"ERROR: --position must be 1-10, got {args.position}")
        sys.exit(2)

    positions_to_run = [args.position] if args.position is not None else POSITIONS

    if not args.run_ibm:
        print("Dry-run mode: no IBM calls will be made.")
        print(f"Would process positions: {positions_to_run}")
        print("Pass --run-ibm to execute live precomputation.")
        sys.exit(0)

    # IBM imports — only reached when --run-ibm is set
    from orchestrate_client import generate_stage_a, generate_fan_commentary

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
    )

    print()
    print("Precomputation complete.")


if __name__ == "__main__":
    _main()
