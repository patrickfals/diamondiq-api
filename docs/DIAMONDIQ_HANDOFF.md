# DiamondIQ — Development Handoff

**Date:** 2026-11-19
**Branch:** `main` (both repos, no commits made during Stage F)
**Workspace root:** `c:\Users\default.DESKTOP-L77NB48\projects`
**Repos:** `diamondiq-api/` · `diamondiq-mobile/`

---

## 1. Architecture Overview

DiamondIQ is a baseball replay app with AI commentary.

```
iPhone (Expo Go)
  └─ diamondiq-mobile (React Native / Expo)
       └─ fetches at-bat data from Render
       └─ replays pitches at 3-second intervals
       └─ shows CommentaryPanel with cached commentary

diamondiq-api (Python FastAPI on Render)
  ├─ GET /games/{game_id}/at-bats/{at_bat_index}   ← mobile data fetch
  ├─ POST /snapshot/redeem                          ← IBM Pitch Scout tool (HMAC-gated)
  ├─ POST /analyze-at-bat                           ← legacy test endpoint (key-gated)
  └─ GET /snapshot/redemptions/recent               ← diagnostic (key-gated, NEW)

IBM watsonx Orchestrate
  ├─ DiamondIQ Analyst        (WO_AGENT_ID)
  │    └─ delegates to Pitch Scout → calls POST /snapshot/redeem
  ├─ DiamondIQ Pitch Scout    (internal to Orchestrate)
  │    └─ registered tool: POST /snapshot/redeem
  └─ DiamondIQ Fan Experience (WO_FAN_EXPERIENCE_AGENT_ID)
       └─ called directly (no tool) with Stage A analysis + fact sheet
```

### Agent responsibilities
| Agent | Called by | Purpose |
|---|---|---|
| Analyst | precompute script via `generate_stage_a()` | Factual analysis of revealed pitch sequence |
| Pitch Scout | Analyst (internal IBM delegation) | Redeems snapshot token → returns pitch data |
| Fan Experience | precompute script via `generate_fan_commentary()` | Writes 4 knowledge-level commentaries |

**The `/analyze-at-bat` endpoint** uses a legacy `analyze_at_bat()` function that calls the Analyst without snapshot scoping. It is protected by `X-DiamondIQ-Test-Key` and is **not used by the mobile app or the precompute pipeline**.

---

## 2. Completed Stages

### Stage A — Snapshot-scoped Analyst invocation
- `generate_stage_a(game_id, at_bat_index, revealed_count)` in `orchestrate_client.py`
- Creates a signed HMAC token, sends it to the Analyst
- Analyst delegates to Pitch Scout which calls `POST /snapshot/redeem`
- Validates: JSON format, non-empty analysis, failure-message rejection, evidence_pitches bounds, future-pitch spoiler detection (single mentions only — see §8 Known Issues)
- Returns `{"analysis": str, "evidence_pitches": list[int]}`

### Stage B — Fan Experience commentary generation
- `generate_fan_commentary(stage_a_input)` in `orchestrate_client.py`
- Calls Fan Experience directly with Stage A analysis text
- Generates 4 knowledge levels: novice, learning, regular, expert
- Validates: all 4 levels present and non-empty, evidence_pitches passed through unchanged

### Stage C — Precompute pipeline
- `precompute_commentary.py` — offline-safe script with `--run-ibm` / `--position N`
- Resumable: skips positions already marked `complete: true`
- Atomic saves via temp-file rename
- Writes to `commentary_cache.json` (10 positions, all complete)
- Never stores tokens, secrets, or auth headers

### Commentary cache (complete)
- `diamondiq-api/commentary_cache.json` — 10 positions, all `complete: true`
- Position 3: `edited: true`, `edit_note` present
- Do **not** overwrite this file

### Mobile app (Stage D — complete)
- `diamondiq-mobile/src/data/commentary_cache.json` — bundled copy of above
- `diamondiq-mobile/src/components/CommentaryPanel.tsx` — 4-tab knowledge selector
- Wired into `AtBatScreen.tsx`, replaces the old AI placeholder section
- Commentary synchronized to `replay.state.revealedCount` (0 = intro screen)
- Default level: `learning`; `complete !== true` entries treated as unavailable
- `npx tsc --noEmit` passes with zero errors

### Replay engine (untouched)
- `diamondiq-mobile/src/hooks/useReplayEngine.ts` — 3-second interval, unchanged
- `revealedCount` drives both StrikeZone and CommentaryPanel

---

## 3. Stage F — Current Implementation Status

Stage F is **partially complete**. The following work was designed and reviewed in this session but several files were **not saved** (write attempts were cancelled before landing).

### ✅ Files successfully written/modified

| File | Status | Description |
|---|---|---|
| `diamondiq-api/mlb_data.py` | **WRITTEN** | Shared spoiler-safe MLB data module (new) |
| `diamondiq-api/test_mlb_data.py` | **WRITTEN** | 44 tests for mlb_data.py (35 pass, 11 skip) |
| `diamondiq-api/main.py` | **WRITTEN** | Updated: uses mlb_data.py, run_id unpack, redemption history, diagnostic endpoint |
| `diamondiq-api/snapshot.py` | **WRITTEN** | Updated: optional run_id (16-char hex), backward-compat verify, 4-tuple return |
| `diamondiq-api/stage_c_test.py` | **WRITTEN** | 1-line fix: `g, a, r = ...` → `g, a, r, rid = ...` + assertIsNone(rid) |

### ❌ Files NOT yet written (cancelled writes)

| File | Required action |
|---|---|
| `diamondiq-api/orchestrate_client.py` | Needs: `build_fact_sheet()`, updated `_FAN_EXPERIENCE_PROMPT_TEMPLATE` with `{fact_sheet}` and per-level guidance, `_check_future_pitch_refs()` helper, range-aware `_FUTURE_PITCH_RE`, `_validate_stage_b_output()`, updated `generate_fan_commentary(stage_a_input, fact_sheet, revealed_count)` |
| `diamondiq-api/precompute_commentary.py` | Needs: `--v2` flag, `run_id` generation, fact-sheet build, `generate_fan_commentary(fact_sheet=..., revealed_count=...)`, write to `commentary_cache_v2.json` and `trace_cache.json`, allowlisted IBM response metadata capture, redemption poll |
| `diamondiq-api/stage_f_test.py` | Does not exist yet; full spec in §8 |

### ⚠️ orchestrate_client.py current state
The file on disk is the **original pre-Stage-F version**. It does NOT contain:
- `build_fact_sheet()`
- Range-aware `_FUTURE_PITCH_RE` (currently only captures single pitch numbers, not ranges like "pitches 2-6")
- Updated Fan Experience prompt with `{fact_sheet}` interpolation
- Post-generation `_validate_stage_b_output()` scan
- Updated `generate_fan_commentary()` signature

---

## 4. Snapshot Authorization & Spoiler Prevention

### Token format
```
base64url( "game_id:at_bat_index:revealed_count[:{run_id}]" + "." + hex_hmac_sha256 )
```

**Legacy (3-field):** `game_id:at_bat_index:revealed_count`
**New (4-field):** `game_id:at_bat_index:revealed_count:run_id` (run_id = 16 lowercase hex chars)

Both formats verify correctly. `verify_snapshot_token()` now returns a 4-tuple:
```python
game_id, at_bat_index, revealed_count, run_id = verify_snapshot_token(token)
# run_id is None for legacy tokens
```

### Spoiler gates (enforced in `mlb_data.build_authorized_snapshot()`)
1. `revealed_count` must be a non-negative integer (not bool, float, str)
2. Only pitches 1…revealed_count returned
3. `outcome` is `None` unless `is_complete` is `True`
4. `is_complete = (total_pitches > 0 and revealed_count >= total_pitches)` — zero-pitch at-bats never auto-complete
5. `total_pitches` omitted from response when `is_complete` is `False`
6. Out-of-range error messages do not reveal `total_pitches`

### Verified at-bats
Only `(777245, 8)` is in `VERIFIED_STATS_AT_BATS`. Entering-game stats (AVG/ERA) are only computed for this pair. All other at-bats return `batter_stats: null, pitcher_stats: null`.

**Verified values for (777245, 8):**
- `batter_stats.hits = 79` (entering-game)
- `batter_stats.at_bats = 324` → `avg = 0.244`
- `pitcher_stats.earned_runs = 31` (entering-game)
- `pitcher_stats.outs_recorded = 178` → `era = 4.70`

---

## 5. Backend Status (Render)

**Deployed URL:** `https://diamondiq-api.onrender.com`
**Deployed code:** Original pre-Stage-F `main.py` is deployed
**Stage F `main.py`:** Written locally but NOT deployed (no Render deployment has been made during Stage F)

The local `main.py` is the reviewed Stage F version. It requires Render deployment before the IBM pilot can proceed.

**Render Python version:** UNCONFIRMED. No `runtime.txt` or `render.yaml` exists in the repo. Local Python is 3.14.2. The new files use `str | None` syntax requiring Python ≥ 3.10. Recommend adding `runtime.txt` with `python-3.11.9` before deployment.

---

## 6. Test Results (Actual)

### `python test_mlb_data.py`
**35 passed, 11 skipped, 0 failures**
Skipped: `TestFactSheetSpoilerRegression` (11 tests) — skipped because `build_fact_sheet` is not yet importable from `orchestrate_client`; they activate automatically once it exists.

Covers: revealed_count type/sign guards, out-of-range error messages, zero-pitch at-bat behavior, schema compatibility, spoiler regression (pos 0/3/9/10), stats extraction with verified values (hits=79 avg=0.244, er=31 era=4.7).

### `python stage_c_test.py`
**50 passed, 0 failures**
Covers: Stage A JSON validation, spoiler detection, evidence_pitches bounds, failure-message rejection, precompute resume/stop-on-failure/atomic-save, dry-run, Fan Experience prompt guards, position-3 count regression, snapshot token round-trip (now with 4-tuple unpack, assertIsNone(rid)).

### `python stage1_test.py` (local server, `http://127.0.0.1:8001`)
**67 passed, 1 skipped (expected), 0 failures — `ALL PASS`, exit code 0**
Run against the **original** `main.py` with `SNAPSHOT_SECRET=stage1-test-secret-do-not-use-in-production`.
Covers: health, pos 0/3/9/10, pos 11 → 404, tampered → 403, malformed → 400, GET at-bat, /analyze-at-bat guard, full spoiler sweep (pos 0/3/9: no "Single", no "In play", hits=79, earned_runs=31).

**Stage 1 has NOT been run against the new `main.py`** — it needs to be re-run after the new main.py is applied.

### `stage_f_test.py`
**Does not exist.** Must be created.

---

## 7. Environment Variables

Never commit secret values. All are set in Render environment and locally in `.env` (gitignored).

| Variable | Purpose |
|---|---|
| `SNAPSHOT_SECRET` | HMAC signing key for snapshot tokens. Generate: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `WO_API_KEY` | IBM Cloud API key for IAM token exchange |
| `WO_INSTANCE_URL` | watsonx Orchestrate instance base URL (no trailing slash) |
| `WO_AGENT_ID` | Agent ID of DiamondIQ Analyst |
| `WO_FAN_EXPERIENCE_AGENT_ID` | Agent ID of DiamondIQ Fan Experience |
| `DIAMONDIQ_TEST_KEY` | Key for `/analyze-at-bat` and `/snapshot/redemptions/recent` |

---

## 8. Known Issues & Unfinished Tasks

### Critical blockers for IBM pilot

1. **`orchestrate_client.py` not updated.** Must add:
   - `_check_future_pitch_refs(text, revealed_count) -> list[int]` — shared helper used by Stage A and Stage B validation
   - Updated `_FUTURE_PITCH_RE` regex to also capture range endpoints: `r"\bpitch(?:es)?\s*#?\s*(\d+)(?:\s*[-\u2013]\s*(\d+))?"` — currently misses the upper bound in "pitches 2-6"
   - `build_fact_sheet(game_data, game_id, at_bat_index, revealed_count) -> str` — fact sheet for Stage B; uses `build_authorized_snapshot()`, reads velocities from MLB data at runtime (the 91.9/93.1/93.6 mph values in `stage_b_test.py` came from IBM output, not independently verified MLB data)
   - Updated `_FAN_EXPERIENCE_PROMPT_TEMPLATE` with `{fact_sheet}` interpolation point and stronger per-level guidance (novice 40-55w / learning 45-60w / regular 50-65w / expert 55-70w)
   - `_validate_stage_b_output(commentary, revealed_count) -> list[str]` — post-generation regex scan for future-pitch refs and forbidden reset-count phrases
   - Updated `generate_fan_commentary(stage_a_input, fact_sheet="", revealed_count=0)` — passes fact sheet to prompt, runs post-gen validation when `revealed_count > 0`

2. **`precompute_commentary.py` not updated.** Must add:
   - `--v2` flag: writes to `commentary_cache_v2.json` (never `commentary_cache.json`)
   - `run_id = secrets.token_hex(8)` per position (16-char hex)
   - `generate_stage_a(..., run_id=run_id)` call
   - `fetch_game_data()` call + `build_fact_sheet()` before Stage B
   - `generate_fan_commentary(stage_a, fact_sheet=fs, revealed_count=pos)` call
   - Allowlisted IBM metadata capture: `result["id"]`, `result["created"]`, `result["model"]`, `result["usage"]`, `result["choices"][0]["finish_reason"]`, `result["choices"][0]["message"].get("tool_calls")` — only fields that actually exist
   - Write to `trace_cache.json` with event types: `run_started`, `snapshot_token_created`, `analyst_request_started`, `snapshot_redeemed` (from redemption poll), `analyst_response_received`, `fan_experience_request_started`, `fan_experience_response_received`, `run_completed`
   - Poll `GET /snapshot/redemptions/recent` after Stage A completes; match by `run_id`
   - Mark trace `trace_complete: false` if redemption event not found or IBM delegation unconfirmed

3. **`stage_f_test.py` does not exist.** Must cover:
   - Legacy 3-field token round-trip (run_id=None)
   - 4-field run_id token round-trip (run_id="abcd1234efgh5678")
   - Tampered run_id rejected (PermissionError)
   - Empty run_id rejected at create time
   - Invalid run_id characters (uppercase, non-hex) rejected
   - `_check_future_pitch_refs` with ranges: "pitches 2-6" at revealed=3 → [6]
   - `_check_future_pitch_refs` with en-dash: "pitches 2–5" → [5] at revealed=3
   - `_validate_stage_b_output` with "reset the count" → violation
   - `_validate_stage_b_output` with future pitch ref in commentary → violation
   - `build_fact_sheet` at pos 0: contains batter/pitcher names, no "Single", no pitch data
   - `build_fact_sheet` at pos 3: contains pitches 1-3 only, no pitch 4+, no "Single"
   - `build_fact_sheet` at pos 9: no pitch 10, no "Single"
   - `build_fact_sheet` verified stats: hits=79 and earned_runs=31 present
   - `build_fact_sheet` format: "Pitches revealed so far: N", no "of M"
   - Diagnostic endpoint returns 403 with no key
   - Diagnostic endpoint returns 403 with wrong key

4. **Render deployment required** before IBM pilot. The new `main.py` and `snapshot.py` must be deployed first so the HMAC verifier handles 4-field tokens.

5. **`runtime.txt` missing.** Add `python-3.11.9` before deployment to pin Render Python version.

### Non-blocking known limitations

- **Stage A spoiler detection:** `_FUTURE_PITCH_RE` currently does not capture the upper bound in ranges like "pitches 2-6". Fix is designed and tested but `orchestrate_client.py` write was cancelled.
- **Stage B post-gen validation:** Not implemented yet. Relies on prompt instructions alone for commentary accuracy. Regex cannot detect all factual errors; it catches only explicit future-pitch refs and forbidden phrases.
- **Redemption history:** In-memory only. Lost on Render restart or if Render uses multiple worker processes. Acceptable for single-process pilot.
- **IBM Observability:** Internal Analyst→Pitch Scout delegation steps cannot be verified from Python. `trace_cache.json` entries will be marked `trace_complete: false` with `ibm_delegation_note` explaining the limitation.
- **91.9/93.1/93.6 mph values** in `stage_b_test.py` came from IBM Analyst output, not independently verified. `build_fact_sheet()` reads velocities from MLB Stats API at runtime; those runtime values supersede the fixture values.
- **`/analyze-at-bat` spoiler risk:** This legacy endpoint does NOT use the snapshot spoiler gate. It is protected by `DIAMONDIQ_TEST_KEY` and is not reachable from the mobile app. The precompute pipeline never calls it. This is a known and documented limitation.

---

## 9. Exact Next Steps

**Step 1 — Write `orchestrate_client.py`** (highest priority)
Apply the full reviewed implementation: `_check_future_pitch_refs`, updated `_FUTURE_PITCH_RE`, `build_fact_sheet`, updated `_FAN_EXPERIENCE_PROMPT_TEMPLATE`, `_validate_stage_b_output`, updated `generate_fan_commentary`.
Run: `python stage_c_test.py` — must still pass 50/50.
Run: `python test_mlb_data.py` — the 11 skipped `TestFactSheetSpoilerRegression` tests must now execute and pass.

**Step 2 — Write `stage_f_test.py`** (see §8 for full spec)
Run: `python stage_f_test.py` — all tests must pass.

**Step 3 — Update `precompute_commentary.py`** with `--v2`, run_id, fact-sheet, trace output.
Dry-run: `python precompute_commentary.py --position 3` (no `--run-ibm`) must exit 0.

**Step 4 — Add `runtime.txt`** containing `python-3.11.9`.

**Step 5 — Get Render deployment approval**, then deploy. After deploy, run:
```bash
python stage1_test.py        # BASE=http://127.0.0.1:8001 by default; point to Render for production check
```

**Step 6 — Get IBM pilot approval**, then run:
```bash
python precompute_commentary.py --run-ibm --position 3 --v2
```
Review `commentary_cache_v2.json` and `trace_cache.json`.
Retrieve redemption event: `curl -H "X-DiamondIQ-Test-Key: $DIAMONDIQ_TEST_KEY" https://diamondiq-api.onrender.com/snapshot/redemptions/recent`

**Step 7 — Human review** of position-3 commentary at all 4 levels.
If approved, run remaining positions: `python precompute_commentary.py --run-ibm --v2`

**Step 8 — Mobile integration** (requires separate approval)
Copy `commentary_cache_v2.json` to `diamondiq-mobile/src/data/commentary_cache.json` only after human review of all 10 positions.

---

## 10. Run Commands

### Backend (local dev)
```bash
cd diamondiq-api

# Install dependencies
pip install -r requirements.txt

# Start local server (port 8001 matches stage1_test.py)
SNAPSHOT_SECRET=<secret> DIAMONDIQ_TEST_KEY=<key> python -m uvicorn main:app --host 127.0.0.1 --port 8001

# Offline tests (no network, no IBM)
python test_mlb_data.py
python stage_c_test.py
python stage_f_test.py      # once written

# Stage 1 regression (requires local server running)
python stage1_test.py

# Precompute dry-run (no IBM)
python precompute_commentary.py --position 3

# Precompute pilot (requires --run-ibm approval)
python precompute_commentary.py --run-ibm --position 3 --v2
```

### Mobile app (Expo Go on iPhone)
```bash
cd diamondiq-mobile
npx expo start       # scan QR with Expo Go, same Wi-Fi required
npx expo start --lan # explicit LAN mode if needed

# TypeScript check
npx tsc --noEmit
```

### Useful diagnostics
```bash
# Redemption history (requires Render deployment of new main.py)
curl -H "X-DiamondIQ-Test-Key: $DIAMONDIQ_TEST_KEY" \
  https://diamondiq-api.onrender.com/snapshot/redemptions/recent

# Health check
curl https://diamondiq-api.onrender.com/health
```

---

## 11. File Index

```
diamondiq-api/
  main.py                    ← Stage F version (new, written; needs deployment)
  snapshot.py                ← Stage F version (run_id support, 4-tuple return)
  orchestrate_client.py      ← ORIGINAL (Stage F changes NOT yet written)
  mlb_data.py                ← NEW — shared spoiler-safe MLB data module
  precompute_commentary.py   ← ORIGINAL (Stage F --v2/trace changes NOT yet written)
  commentary_cache.json      ← COMPLETE, do not overwrite
  test_mlb_data.py           ← NEW — 35 pass, 11 skip (activate after build_fact_sheet)
  stage_c_test.py            ← Updated (4-tuple unpack fix)
  stage_b_test.py            ← Unchanged
  stage1_test.py             ← Unchanged (BASE=127.0.0.1:8001)
  stage_f_test.py            ← DOES NOT EXIST YET
  docs/DIAMONDIQ_HANDOFF.md  ← This file

diamondiq-mobile/
  src/
    components/CommentaryPanel.tsx   ← Stage D (complete)
    screens/AtBatScreen.tsx          ← Stage D wiring (complete)
    data/commentary_cache.json       ← Bundled copy of API cache
    hooks/useReplayEngine.ts         ← Unchanged (3-second interval)
    types/atbat.ts                   ← Unchanged
    theme/colors.ts                  ← Unchanged
```
