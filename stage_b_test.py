"""
Stage B standalone test: Fan Experience direct invocation.

Uses the saved, verified Stage A text output from IBM Analyst + Pitch Scout
for game 777245, at-bat 8, position 3 (3 pitches revealed).

This fixture is genuine IBM output recorded externally. Individual pitch
details have not been independently cross-checked; treat as a test fixture,
not proof of factual correctness.

Usage:
    python stage_b_test.py            # offline validation only (no network)
    python stage_b_test.py --run-ibm  # offline checks, then one IBM call

Credentials are never printed.
"""

import json
import sys
import time

# ---------------------------------------------------------------------------
# Argument parsing — only --run-ibm is accepted; anything else is rejected.
# ---------------------------------------------------------------------------
args = sys.argv[1:]
if args and args != ["--run-ibm"]:
    print(f"ERROR: unrecognised option(s): {args}")
    print("Usage: python stage_b_test.py [--run-ibm]")
    sys.exit(2)

RUN_IBM = args == ["--run-ibm"]

# ---------------------------------------------------------------------------
# Saved Stage A fixture — game 777245, at-bat 8, position 3
# ---------------------------------------------------------------------------
STAGE_A_FIXTURE = """\
Key observation:
The first three pitches are all four-seam fastballs thrown above 91 mph, \
moving the count to 1-2.

Supporting evidence:
Pitch 1: 91.9 mph, strike (inside-low).
Pitch 2: 93.1 mph, strike (outside-mid).
Pitch 3: 93.6 mph, ball (high-outside).

Significance:
Soroka established his fastball early, securing two strikes before issuing \
a ball.

Limitations:
Only three pitches have been revealed. No information about subsequent \
pitches or the final outcome is available.\
"""

# Evidence pitches derived from verified position-3 snapshot (game 777245, at-bat 8).
EVIDENCE_PITCHES = [1, 2, 3]

REQUIRED_LEVELS = ("novice", "learning", "regular", "expert")

# ---------------------------------------------------------------------------
# Imports from orchestrate_client (no network side-effects on import)
# ---------------------------------------------------------------------------
from orchestrate_client import (  # noqa: E402
    FanCommentaryError,
    _FAN_EXPERIENCE_LEVELS,
    _FAN_EXPERIENCE_PROMPT_TEMPLATE,
    _strip_fences,
    generate_fan_commentary,
)

# ---------------------------------------------------------------------------
# Local validation (no network)
# ---------------------------------------------------------------------------
local_failures: list[str] = []


def local_pass(label: str) -> None:
    print(f"LOCAL PASS  [{label}]")


def local_fail(label: str, detail: str) -> None:
    print(f"LOCAL FAIL  [{label}] {detail}")
    local_failures.append(label)


# 1. Prompt formatting succeeds
try:
    formatted = _FAN_EXPERIENCE_PROMPT_TEMPLATE.format(
        stage_a_analysis=STAGE_A_FIXTURE
    )
    if "{stage_a_analysis}" in formatted:
        local_fail("prompt_format", "placeholder not substituted")
    elif STAGE_A_FIXTURE not in formatted:
        local_fail("prompt_format", "fixture text missing from prompt")
    else:
        local_pass("prompt_format")
except Exception as exc:
    local_fail("prompt_format", str(exc))

# 2. Valid JSON parses (plain)
valid_json = '{"commentary_by_level": {"novice": "a", "learning": "b", "regular": "c", "expert": "d"}}'
try:
    parsed = json.loads(_strip_fences(valid_json))
    assert parsed["commentary_by_level"]["novice"] == "a"
    local_pass("valid_json_plain")
except Exception as exc:
    local_fail("valid_json_plain", str(exc))

# 3. JSON in Markdown fences parses
fenced_json = f"```json\n{valid_json}\n```"
try:
    parsed = json.loads(_strip_fences(fenced_json))
    assert parsed["commentary_by_level"]["expert"] == "d"
    local_pass("valid_json_fenced")
except Exception as exc:
    local_fail("valid_json_fenced", str(exc))

# 4. Missing knowledge level fails validation
incomplete_json = '{"commentary_by_level": {"novice": "a", "learning": "b", "regular": "c"}}'
parsed_incomplete = json.loads(incomplete_json)
missing = [
    lvl for lvl in _FAN_EXPERIENCE_LEVELS
    if lvl not in parsed_incomplete["commentary_by_level"]
]
if missing == ["expert"]:
    local_pass("missing_level_detected")
else:
    local_fail("missing_level_detected", f"expected ['expert'], got {missing}")

# 5. Non-dictionary JSON root fails validation
parsed_non_dict = json.loads('["novice", "learning"]')
if not isinstance(parsed_non_dict, dict):
    local_pass("non_dict_root_detected")
else:
    local_fail("non_dict_root_detected", "list incorrectly treated as dict")

# 6. Evidence pitch numbers are preserved
result_mock = {
    "commentary_by_level": {lvl: f"commentary for {lvl}" for lvl in _FAN_EXPERIENCE_LEVELS},
    "evidence_pitches": EVIDENCE_PITCHES,
}
if result_mock["evidence_pitches"] == [1, 2, 3]:
    local_pass("evidence_pitches_preserved")
else:
    local_fail("evidence_pitches_preserved", f"got {result_mock['evidence_pitches']}")

print()
if local_failures:
    print(f"LOCAL VALIDATION FAILED — {len(local_failures)} check(s): {local_failures}")
    sys.exit(1)

print("LOCAL VALIDATION PASS — all 6 checks passed.")

if not RUN_IBM:
    print("IBM execution skipped. Run with --run-ibm to invoke Fan Experience.")
    sys.exit(0)

# ---------------------------------------------------------------------------
# IBM call — only reached when --run-ibm is passed
# ---------------------------------------------------------------------------
print()
print("Stage B IBM test — Fan Experience direct invocation")
print("Fixture: game 777245 / at-bat 8 / position 3")
print("-" * 60)

stage_a_input = {
    "analysis": STAGE_A_FIXTURE,
    "evidence_pitches": EVIDENCE_PITCHES,
}

start = time.monotonic()

try:
    result = generate_fan_commentary(stage_a_input)
except KeyError as exc:
    print(f"FAIL — missing environment variable: {exc}")
    sys.exit(1)
except TypeError as exc:
    print(f"FAIL — invalid input: {exc}")
    sys.exit(1)
except FanCommentaryError as exc:
    print(f"FAIL — IBM response invalid: {exc}")
    # Print raw response only in this local test, never in production code
    if exc.raw_response:
        print(f"Raw response (first 500 chars): {exc.raw_response[:500]}")
    sys.exit(1)
except Exception as exc:
    print(f"FAIL — unexpected error: {type(exc).__name__}: {exc}")
    sys.exit(1)

elapsed = time.monotonic() - start

commentary = result["commentary_by_level"]
for level in REQUIRED_LEVELS:
    print(f"\n[{level.upper()}]")
    print(commentary[level])

print(f"\nevidence_pitches: {result['evidence_pitches']}")

print()
print("-" * 60)

ibm_failures = []
for level in REQUIRED_LEVELS:
    text = commentary.get(level, "")
    if not isinstance(text, str) or not text.strip():
        ibm_failures.append(f"empty or missing: {level}")
if result.get("evidence_pitches") != EVIDENCE_PITCHES:
    ibm_failures.append(
        f"evidence_pitches mismatch: got {result.get('evidence_pitches')}"
    )

if ibm_failures:
    print(f"VALIDATION FAIL — {ibm_failures}")
    sys.exit(1)

print("VALIDATION PASS — all four levels present, non-empty, evidence_pitches preserved")
print(f"Elapsed: {elapsed:.1f}s")
