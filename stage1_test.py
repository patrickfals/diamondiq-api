"""
Stage 1 endpoint tests for POST /snapshot/redeem.
Tokens and secrets are never printed.
Run with: python stage1_test.py
"""
import base64
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

os.environ["SNAPSHOT_SECRET"] = "stage1-test-secret-do-not-use-in-production"
sys.path.insert(0, ".")
from snapshot import create_snapshot_token  # noqa: E402

BASE = "http://127.0.0.1:8001"

# Sentinel: distinguishes "do not check" from "assert must be None"
_SKIP = object()

failures: list[str] = []


def post_redeem(token: str) -> tuple[int, dict]:
    payload = json.dumps({"token": token}).encode()
    req = Request(
        f"{BASE}/snapshot/redeem",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        return e.code, json.loads(e.read().decode())


def expect(label: str, cond: bool, detail: str = "") -> None:
    suffix = f" {detail}" if detail else ""
    if cond:
        print(f"PASS [{label}]{suffix}")
    else:
        print(f"FAIL [{label}]{suffix}")
        failures.append(label)


def check(
    label: str,
    status: int,
    body: dict,
    exp_status: int,
    exp_pitches=_SKIP,
    exp_outcome=_SKIP,
    exp_complete=_SKIP,
    forbidden_keys: list[str] | None = None,
) -> None:
    expect(label + "_status", status == exp_status,
           f"status={status} expected={exp_status}")
    if status != exp_status:
        return
    if exp_pitches is not _SKIP:
        got = len(body.get("pitches", []))
        expect(label + "_pitches", got == exp_pitches,
               f"pitches={got} expected={exp_pitches}")
    if exp_outcome is not _SKIP:
        got = body.get("outcome")
        if exp_outcome is None:
            expect(label + "_outcome_null", got is None,
                   f"outcome={got!r} must be null")
        else:
            expect(label + "_outcome", got == exp_outcome,
                   f"outcome={got!r} expected={exp_outcome!r}")
    if exp_complete is not _SKIP:
        got = body.get("is_complete")
        expect(label + "_complete", got == exp_complete,
               f"is_complete={got} expected={exp_complete}")
    if forbidden_keys:
        for k in forbidden_keys:
            expect(label + f"_no_{k}", k not in body,
                   f"forbidden key {k!r} present={k in body}")


# ── Health ────────────────────────────────────────────────────────────────────
req = Request(f"{BASE}/health", method="GET")
with urlopen(req, timeout=10) as r:
    hdata = json.load(r)
expect("health", hdata.get("status") == "ok", f"status={hdata.get('status')}")

# ── Position 0: zero pitches, outcome null, incomplete, total_pitches absent ──
s, b = post_redeem(create_snapshot_token(777245, 8, 0))
check("pos_0", s, b, 200,
      exp_pitches=0, exp_outcome=None, exp_complete=False,
      forbidden_keys=["total_pitches"])

# ── Position 3: exactly pitches 1-3, outcome null, incomplete ────────────────
s, b = post_redeem(create_snapshot_token(777245, 8, 3))
check("pos_3", s, b, 200,
      exp_pitches=3, exp_outcome=None, exp_complete=False,
      forbidden_keys=["total_pitches"])
if s == 200:
    nums = [p["number"] for p in b.get("pitches", [])]
    expect("pos_3_numbers", nums == [1, 2, 3], f"pitch_numbers={nums}")
    expect("pos_3_no_future",
           all(p["number"] <= 3 for p in b.get("pitches", [])),
           "no pitch number > 3")

# ── Position 9: nine pitches, outcome null, incomplete ───────────────────────
s, b = post_redeem(create_snapshot_token(777245, 8, 9))
check("pos_9", s, b, 200,
      exp_pitches=9, exp_outcome=None, exp_complete=False,
      forbidden_keys=["total_pitches"])
if s == 200:
    nums = [p["number"] for p in b.get("pitches", [])]
    expect("pos_9_numbers", nums == list(range(1, 10)), f"pitch_numbers={nums}")
    expect("pos_9_no_future",
           all(p["number"] <= 9 for p in b.get("pitches", [])),
           "no pitch number > 9")

# ── Position 10: all 10 pitches, outcome Single, complete ────────────────────
s, b = post_redeem(create_snapshot_token(777245, 8, 10))
check("pos_10", s, b, 200,
      exp_pitches=10, exp_outcome="Single", exp_complete=True)
if s == 200:
    expect("pos_10_total_pitches", b.get("total_pitches") == 10,
           f"total_pitches={b.get('total_pitches')}")
    avg = b["batter_stats"].get("avg") if b.get("batter_stats") else None
    expect("pos_10_avg", avg == 0.244, f"avg={avg}")
    era = b["pitcher_stats"].get("era") if b.get("pitcher_stats") else None
    expect("pos_10_era", era == 4.7, f"era={era}")

# ── Position 11 (beyond total) → 404 ─────────────────────────────────────────
s, b = post_redeem(create_snapshot_token(777245, 8, 11))
check("pos_11_beyond", s, b, 404)

# ── Tampered token: flip a byte inside the HMAC hex signature → 403 ──────────
# Decode the token, mutate the first character of the hex signature (after the
# dot separator), re-encode.  This corrupts actual signed bytes, not padding.
good_tok = create_snapshot_token(777245, 8, 3)
padded = good_tok + "=" * (-len(good_tok) % 4)
raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
dot = raw.index(".")
sig_list = list(raw[dot + 1:])
sig_list[0] = "f" if sig_list[0] != "f" else "e"
tampered_raw = raw[: dot + 1] + "".join(sig_list)
tampered_tok = (
    base64.urlsafe_b64encode(tampered_raw.encode("utf-8"))
    .rstrip(b"=")
    .decode("ascii")
)
s, b = post_redeem(tampered_tok)
check("tampered", s, b, 403)

# ── Malformed token → 400 ────────────────────────────────────────────────────
s, b = post_redeem("this-is-not-a-real-token")
check("malformed", s, b, 400)

# ── Missing SNAPSHOT_SECRET on server → 503 ──────────────────────────────────
# We cannot unset the server's env var from here, so this test is skipped.
# It is covered by the token unit tests (create_snapshot_token raises ValueError
# when SNAPSHOT_SECRET is absent, and redeem_snapshot maps that to 503).
print("SKIP [missing_secret_503] requires server restart without SNAPSHOT_SECRET")

# ── Existing GET /games/777245/at-bats/8 unaffected ──────────────────────────
req = Request(f"{BASE}/games/777245/at-bats/8", method="GET")
with urlopen(req, timeout=30) as r:
    gdata = json.load(r)
expect("get_pitches", len(gdata.get("pitches", [])) == 10,
       f"pitches={len(gdata.get('pitches', []))}")
expect("get_outcome", gdata.get("outcome") == "Single",
       f"outcome={gdata.get('outcome')!r}")
avg = gdata["batter_stats"].get("avg") if gdata.get("batter_stats") else None
expect("get_avg", avg == 0.244, f"avg={avg}")
era = gdata["pitcher_stats"].get("era") if gdata.get("pitcher_stats") else None
expect("get_era", era == 4.7, f"era={era}")

# ── /analyze-at-bat still returns 403 without key (not 404) ──────────────────
req = Request(
    f"{BASE}/analyze-at-bat",
    data=json.dumps({
        "game_id": 777245,
        "at_bat_index": 8,
        "knowledge_level": "I watch a good amount",
    }).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urlopen(req, timeout=10) as r:
        expect("analyze_guard", False, "expected 403, got 200")
except HTTPError as e:
    expect("analyze_guard", e.code == 403,
           f"got {e.code} expected 403 (endpoint intact)")

# ── Spoiler sweep: positions 0, 3, 9 ─────────────────────────────────────────
# Checks that no future-pitch data, final outcome, or postgame stats leak.
# Known entering-game values for game 777245 / at-bat 8 (verified live):
#   batter_stats.hits = 79  (entering-game, pre-game subtraction method)
#   pitcher_stats.earned_runs = 31  (entering-game)
# These are the ONLY stats fields that must appear; postgame totals must not.

SPOILER_POSITIONS = [
    (0, 0),
    (3, 3),
    (9, 9),
]

# Top-level keys allowed in a partial snapshot response.
# total_pitches is intentionally absent (withheld until complete).
ALLOWED_PARTIAL_KEYS = {
    "game_id", "at_bat_index", "revealed_count", "is_complete",
    "batter", "pitcher", "inning", "half_inning",
    "outcome", "pitches", "batter_stats", "pitcher_stats",
}

# Strings that must never appear anywhere in a partial snapshot JSON body.
FORBIDDEN_STRINGS = ["Single", "In play"]

for pos_label, count in SPOILER_POSITIONS:
    s, b = post_redeem(create_snapshot_token(777245, 8, count))
    pfx = f"sweep_pos_{pos_label}"

    expect(pfx + "_status", s == 200, f"status={s}")
    if s != 200:
        continue

    # Exact authorized pitch count
    got_pitches = len(b.get("pitches", []))
    expect(pfx + "_pitch_count", got_pitches == count,
           f"pitches={got_pitches} expected={count}")

    # No future pitch numbers present
    future = [p["number"] for p in b.get("pitches", []) if p["number"] > count]
    expect(pfx + "_no_future_pitches", future == [],
           f"future pitch numbers={future}")

    # outcome must be null
    expect(pfx + "_outcome_null", b.get("outcome") is None,
           f"outcome={b.get('outcome')!r}")

    # No forbidden strings anywhere in the serialised body
    body_text = json.dumps(b)
    for fs in FORBIDDEN_STRINGS:
        expect(pfx + f"_no_{fs.replace(' ', '_').lower()}",
               fs not in body_text,
               f"forbidden string {fs!r} found in body")

    # No unexpected top-level keys
    extra_keys = set(b.keys()) - ALLOWED_PARTIAL_KEYS
    expect(pfx + "_no_extra_keys", extra_keys == set(),
           f"unexpected keys={extra_keys}")

    # Entering-game stats: hits=79, earned_runs=31 when present
    bs = b.get("batter_stats")
    if bs is not None:
        expect(pfx + "_hits_79", bs.get("hits") == 79,
               f"batter_stats.hits={bs.get('hits')} expected=79")
        expect(pfx + "_basis_entering", bs.get("basis") == "entering_game",
               f"batter_stats.basis={bs.get('basis')!r}")
    else:
        # batter_stats may be None for unverified at-bats; not a failure here
        # because VERIFIED_STATS_AT_BATS gates availability — but we know
        # (777245, 8) is verified, so None would indicate a regression.
        expect(pfx + "_batter_stats_present", False,
               "batter_stats is null for verified at-bat (777245, 8)")

    ps = b.get("pitcher_stats")
    if ps is not None:
        expect(pfx + "_earned_runs_31", ps.get("earned_runs") == 31,
               f"pitcher_stats.earned_runs={ps.get('earned_runs')} expected=31")
        expect(pfx + "_pitcher_basis_entering", ps.get("basis") == "entering_game",
               f"pitcher_stats.basis={ps.get('basis')!r}")
    else:
        expect(pfx + "_pitcher_stats_present", False,
               "pitcher_stats is null for verified at-bat (777245, 8)")

# ── Summary ───────────────────────────────────────────────────────────────────
print()
if failures:
    print(f"FAILED: {len(failures)} assertion(s): {failures}")
    sys.exit(1)
else:
    print("ALL PASS")
