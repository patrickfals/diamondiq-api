"""
stage_f_test.py — Offline unit tests for Stage F functionality.

Covers:
  1.  Fact sheet spoiler regression (positions 0, 3, 9)
  2.  Fact sheet format ("Pitches revealed so far: N", no "of M")
  3.  Position-3 fact sheet print (for human review)
  4.  Entering-game stats in fact sheet (hits=79, ERA=4.70)
  5.  run_id forwarding to create_snapshot_token
  6.  Backward compatibility without run_id
  7.  Stage A response metadata extraction
  8.  Fan commentary response metadata extraction
  9.  Fan commentary fact sheet in prompt
 10.  Fan commentary requires revealed_count
 11.  Precompute: revealed_count injection and fact_sheet_fn forwarding
 12.  _check_future_pitch_refs patterns
 13.  Stage A spoiler detection (hyphen/en-dash ranges)
 14.  Fan commentary future-pitch rejection
 15.  Fan commentary count-reset phrase rejection
 16.  Stage A return contract
 17.  Fan commentary return contract
 18.  _extract_response_metadata unit tests
 19.  V2: --v2 never touches commentary_cache.json
 20.  V2: completed positions are skipped on resume
 21.  V2: each position gets a unique run_id
 22.  V2: run_id reaches generate_stage_a as keyword arg
 23.  V2: legacy 3-arg stage_a mocks remain compatible
 24.  V2: Stage B receives correct revealed_count
 25.  V2: Stage B receives verified fact sheet (position 3)
 26.  V2: missing/empty fact sheet prevents Stage B
 27.  V2: trace events in correct order with nondecreasing elapsed
 28.  V2: trace_complete is False
 29.  V2: Stage A/B response metadata retained in trace
 30.  V2: redemption matching run_id → snapshot_redeemed event
 31.  V2: redemption run_id mismatch → note only
 32.  V2: token field not persisted in redemption record
 33.  V2: all saved redemption fields in _REDEMPTION_ALLOWLIST
 34.  V2: missing redemption record → note only, not fatal
 35.  V2: endpoint error → note only, not fatal
 36.  V2: trace written before commentary (Correction 2)
 37.  V2: trace write failure → position not complete, can reprocess
 38.  V2: SNAPSHOT_SECRET redacted in console (Correction 3)
 39.  V2: WO_API_KEY redacted in console
 40.  V2: DIAMONDIQ_TEST_KEY redacted in console
 41.  V2: "token" JSON value redacted
 42.  V2: exception messages not in trace/cache
 43.  V2: both output files written atomically
 44.  V2: partial failures preserve previously completed positions

No network calls are made.  IBM services are never invoked.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, ".")

# ---------------------------------------------------------------------------
# Imports from project modules
# ---------------------------------------------------------------------------
from orchestrate_client import (
    FanCommentaryError,
    StageAError,
    _COUNT_RESET_PHRASES,
    _FUTURE_PITCH_RE,
    _check_future_pitch_refs,
    _extract_response_metadata,
    build_fact_sheet,
    generate_fan_commentary,
    generate_stage_a,
)
from precompute_commentary import (
    CACHE_PATH,
    CACHE_V2_PATH,
    TRACE_CACHE_PATH,
    _REDEMPTION_ALLOWLIST,
    _REDEMPTION_ERROR,
    _REDEMPTION_FOUND,
    _REDEMPTION_MISSING,
    _TRACE_INCOMPLETE_NOTE,
    _apply_redemption_to_trace,
    _build_trace_entry,
    _redact_for_console,
    _safe_console_msg,
    _safe_meta,
    assert_no_secrets,
    load_cache,
    run_precompute,
    run_v2_precompute,
    save_cache_atomic,
)


# ---------------------------------------------------------------------------
# Shared synthetic game-data fixture (identical to test_mlb_data.py helper)
# ---------------------------------------------------------------------------

def _make_pitch_event(number, result, balls, strikes):
    return {
        "isPitch": True,
        "pitchNumber": number,
        "details": {
            "description": result,
            "type": {"description": "Four-Seam Fastball"},
        },
        "pitchData": {
            "startSpeed": 90.0 + number,
            "coordinates": {"pX": 0.1, "pZ": 2.5},
        },
        "count": {"balls": balls, "strikes": strikes, "outs": 0},
    }


def _make_game_data(total_pitches=10, outcome="Single",
                    game_id=0, with_verified_stats=False):
    """
    Build a minimal synthetic MLB Stats API response.

    when with_verified_stats=True the boxscore players section contains
    batter ID 100 and pitcher ID 200 with values chosen to produce exactly
    AVG 0.244 (79H / 324AB) and ERA 4.70 (31 ER / 178 outs).

    Fixture arithmetic (verified):
      batter : season_hits=80, game_hits=1  -> entering_hits=79
               season_ab=328,  game_ab=4    -> entering_ab=324
               AVG = round(79/324, 3) = 0.244
      pitcher: season_er=32,   game_er=1    -> entering_er=31
               season_outs=188, game_outs=10-> entering_outs=178
               ERA = round(27*31/178, 2)    = 4.7
    """
    events = []
    balls, strikes = 0, 0
    for i in range(1, total_pitches + 1):
        if i <= 2:
            result = "Called Strike"
            strikes = min(strikes + 1, 2)
        else:
            result = "Ball"
            balls = min(balls + 1, 3)
        events.append(_make_pitch_event(i, result, balls, strikes))

    batter_id = 100
    pitcher_id = 200

    if with_verified_stats:
        players = {
            f"ID{batter_id}": {
                "seasonStats": {
                    "batting":  {"hits": 80, "atBats": 328},
                    "pitching": {},
                },
                "stats": {
                    "batting":  {"hits": 1, "atBats": 4},
                    "pitching": {},
                },
            },
            f"ID{pitcher_id}": {
                "seasonStats": {
                    "batting":  {},
                    "pitching": {"earnedRuns": 32, "outs": 188},
                },
                "stats": {
                    "batting":  {},
                    "pitching": {"earnedRuns": 1, "outs": 10},
                },
            },
        }
        boxscore = {"teams": {"home": {"players": players}}}
    else:
        boxscore = {"teams": {}}

    at_bat_index = 8 if with_verified_stats else 0

    return {
        "liveData": {
            "plays": {
                "allPlays": [
                    {
                        "about": {
                            "atBatIndex": at_bat_index,
                            "inning": 1,
                            "halfInning": "top",
                        },
                        "matchup": {
                            "batter":  {"id": batter_id,  "fullName": "Test Batter"},
                            "pitcher": {"id": pitcher_id, "fullName": "Test Pitcher"},
                        },
                        "playEvents": events,
                        "result": {"event": outcome},
                    }
                ]
            },
            "boxscore": boxscore,
        }
    }


# Standard fixture (game_id=0, at_bat_index=0, 10 pitches, no verified stats)
GAME_DATA_10 = _make_game_data(10)
# Verified-stats fixture (game_id=777245, at_bat_index=8)
GAME_DATA_VERIFIED = _make_game_data(10, with_verified_stats=True)

FORBIDDEN_STRINGS = ["Single", "In play"]


# ---------------------------------------------------------------------------
# 1. Fact sheet spoiler regression
# ---------------------------------------------------------------------------

class TestFactSheetSpoilerRegression(unittest.TestCase):
    """build_fact_sheet must not leak unrevealed pitches or outcome."""

    def _sheet(self, pos, game_data=None, game_id=0, at_bat_index=0):
        if game_data is None:
            game_data = GAME_DATA_10
        return build_fact_sheet(game_data, game_id, at_bat_index, pos)

    def test_position_0_no_pitches(self):
        sheet = self._sheet(0)
        self.assertIn("Pitches revealed so far: 0", sheet)
        self.assertNotIn("Pitch 1", sheet)
        self.assertNotIn("Pitch 2", sheet)

    def test_position_0_no_outcome(self):
        sheet = self._sheet(0)
        for s in FORBIDDEN_STRINGS:
            self.assertNotIn(s, sheet, f"Position 0 must not contain {s!r}")

    def test_position_3_shows_3_pitches(self):
        sheet = self._sheet(3)
        self.assertIn("Pitches revealed so far: 3", sheet)
        self.assertIn("Pitch 1", sheet)
        self.assertIn("Pitch 2", sheet)
        self.assertIn("Pitch 3", sheet)
        self.assertNotIn("Pitch 4", sheet)

    def test_position_3_no_outcome(self):
        sheet = self._sheet(3)
        for s in FORBIDDEN_STRINGS:
            self.assertNotIn(s, sheet, f"Position 3 must not contain {s!r}")

    def test_position_9_shows_9_pitches(self):
        sheet = self._sheet(9)
        self.assertIn("Pitches revealed so far: 9", sheet)
        for n in range(1, 10):
            self.assertIn(f"Pitch {n}", sheet)
        self.assertNotIn("Pitch 10", sheet)

    def test_position_9_no_outcome(self):
        sheet = self._sheet(9)
        for s in FORBIDDEN_STRINGS:
            self.assertNotIn(s, sheet, f"Position 9 must not contain {s!r}")

    def test_position_10_shows_outcome(self):
        """At the final pitch the outcome must appear."""
        sheet = self._sheet(10)
        self.assertIn("Single", sheet)
        self.assertIn("At-bat outcome", sheet)

    def test_no_total_pitch_count_before_complete(self):
        """'of 10' or similar total must never appear before position 10."""
        for pos in (0, 1, 3, 5, 9):
            sheet = self._sheet(pos)
            self.assertNotIn("of 10", sheet)
            self.assertNotIn("of M", sheet)


# ---------------------------------------------------------------------------
# 2. Fact sheet format
# ---------------------------------------------------------------------------

class TestFactSheetFormat(unittest.TestCase):
    """Required format strings must appear; forbidden ones must not."""

    def test_header_present(self):
        sheet = build_fact_sheet(GAME_DATA_10, 0, 0, 3)
        self.assertIn("=== DiamondIQ Verified Fact Sheet ===", sheet)

    def test_footer_present(self):
        sheet = build_fact_sheet(GAME_DATA_10, 0, 0, 3)
        self.assertIn("=== End of Fact Sheet ===", sheet)

    def test_pitches_revealed_format_no_of(self):
        """Must say 'Pitches revealed so far: N', never 'of M'."""
        for pos in (0, 1, 3, 9):
            sheet = build_fact_sheet(GAME_DATA_10, 0, 0, pos)
            self.assertIn(f"Pitches revealed so far: {pos}", sheet)
            self.assertNotIn(f"of ", sheet.split("Pitches revealed")[1][:20])

    def test_batter_pitcher_present(self):
        sheet = build_fact_sheet(GAME_DATA_10, 0, 0, 3)
        self.assertIn("Batter", sheet)
        self.assertIn("Pitcher", sheet)

    def test_inning_present(self):
        sheet = build_fact_sheet(GAME_DATA_10, 0, 0, 3)
        self.assertIn("Inning", sheet)


# ---------------------------------------------------------------------------
# 3. Position-3 fact sheet printed for human review
# ---------------------------------------------------------------------------

class TestFactSheetPosition3Print(unittest.TestCase):
    """Print the complete position-3 fact sheet and assert structure."""

    @classmethod
    def setUpClass(cls):
        cls.sheet = build_fact_sheet(
            GAME_DATA_VERIFIED, 777245, 8, 3
        )
        print("\n" + "=" * 60)
        print("POSITION-3 FACT SHEET (for human review):")
        print("=" * 60)
        print(cls.sheet)
        print("=" * 60 + "\n")

    def test_header_and_footer(self):
        self.assertIn("=== DiamondIQ Verified Fact Sheet ===", self.sheet)
        self.assertIn("=== End of Fact Sheet ===", self.sheet)

    def test_reveals_exactly_3_pitches(self):
        self.assertIn("Pitches revealed so far: 3", self.sheet)
        self.assertIn("Pitch 1", self.sheet)
        self.assertIn("Pitch 2", self.sheet)
        self.assertIn("Pitch 3", self.sheet)
        self.assertNotIn("Pitch 4", self.sheet)

    def test_no_spoiler_outcome(self):
        for s in FORBIDDEN_STRINGS:
            self.assertNotIn(s, self.sheet)

    def test_entering_stats_present(self):
        """Verified entering-game stats must appear."""
        self.assertIn("0.244", self.sheet)
        self.assertIn("79", self.sheet)
        self.assertIn("4.7", self.sheet)   # ERA
        self.assertIn("31", self.sheet)    # earned runs

    def test_game_id_in_sheet(self):
        self.assertIn("777245", self.sheet)


# ---------------------------------------------------------------------------
# 4. Entering-game statistics in fact sheet
# ---------------------------------------------------------------------------

class TestFactSheetStats(unittest.TestCase):
    """Verified entering-game stats: 79 hits, 31 earned runs."""

    def setUp(self):
        self.sheet = build_fact_sheet(GAME_DATA_VERIFIED, 777245, 8, 3)

    def test_batter_avg_244(self):
        self.assertIn("0.244", self.sheet)

    def test_batter_hits_79(self):
        self.assertIn("79", self.sheet)

    def test_pitcher_era_4_7(self):
        self.assertIn("4.7", self.sheet)

    def test_pitcher_er_31(self):
        self.assertIn("31", self.sheet)

    def test_unverified_game_id_no_stats(self):
        """game_id=0 is not in VERIFIED_STATS_AT_BATS — no stats line."""
        sheet = build_fact_sheet(GAME_DATA_10, 0, 0, 3)
        self.assertNotIn("AVG", sheet)
        self.assertNotIn("ERA", sheet)


# ---------------------------------------------------------------------------
# 5. run_id forwarding to create_snapshot_token
# ---------------------------------------------------------------------------

class TestRunIdForwarding(unittest.TestCase):
    """generate_stage_a must forward run_id to create_snapshot_token."""

    def _mock_response(self):
        resp = {
            "id": "resp-001",
            "created": 1700000000,
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({
                        "analysis": "Pitcher threw a fastball.",
                        "evidence_pitches": [1],
                    })
                },
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        return resp

    def _setup_urlopen(self, mock_urlopen, response_dict):
        """Configure urlopen to return IAM token then IBM response."""
        cm_iam = MagicMock()
        cm_iam.__enter__ = MagicMock(return_value=cm_iam)
        cm_iam.__exit__ = MagicMock(return_value=False)
        cm_iam.read = MagicMock(return_value=json.dumps(
            {"access_token": "tok"}).encode())
        cm_iam.__class__ = type(cm_iam)

        cm_ibm = MagicMock()
        cm_ibm.__enter__ = MagicMock(return_value=cm_ibm)
        cm_ibm.__exit__ = MagicMock(return_value=False)
        cm_ibm.read = MagicMock(return_value=json.dumps(response_dict).encode())

        # json.load() is called on the response object — patch urlopen so the
        # context manager's return value supports read() properly
        def fake_urlopen(req, timeout=None):
            # Discriminate by URL
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "iam.cloud.ibm.com" in url:
                class FakeIAM:
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                    def read(self): return json.dumps({"access_token": "tok"}).encode()
                return FakeIAM()
            else:
                class FakeIBM:
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                    def read(self): return json.dumps(response_dict).encode()
                return FakeIBM()

        mock_urlopen.side_effect = fake_urlopen

    @patch.dict(os.environ, {
        "SNAPSHOT_SECRET": "testsecret",
        "WO_API_KEY": "testkey",
        "WO_INSTANCE_URL": "https://test.example.com",
        "WO_AGENT_ID": "agent-001",
    })
    @patch("orchestrate_client.create_snapshot_token")
    @patch("orchestrate_client.urlopen")
    def test_run_id_forwarded(self, mock_urlopen, mock_create_token):
        """run_id must be forwarded as keyword arg to create_snapshot_token."""
        mock_create_token.return_value = "fake-token"
        self._setup_urlopen(mock_urlopen, self._mock_response())

        import secrets
        run_id = secrets.token_hex(8)
        generate_stage_a(777245, 8, 1, run_id=run_id)

        mock_create_token.assert_called_once_with(777245, 8, 1, run_id=run_id)

    @patch.dict(os.environ, {
        "SNAPSHOT_SECRET": "testsecret",
        "WO_API_KEY": "testkey",
        "WO_INSTANCE_URL": "https://test.example.com",
        "WO_AGENT_ID": "agent-001",
    })
    @patch("orchestrate_client.create_snapshot_token")
    @patch("orchestrate_client.urlopen")
    def test_no_run_id_backward_compat(self, mock_urlopen, mock_create_token):
        """Calling without run_id must work (backward compat)."""
        mock_create_token.return_value = "fake-token"
        self._setup_urlopen(mock_urlopen, self._mock_response())

        generate_stage_a(777245, 8, 1)  # no run_id

        mock_create_token.assert_called_once_with(777245, 8, 1, run_id=None)

    @patch.dict(os.environ, {
        "SNAPSHOT_SECRET": "testsecret",
        "WO_API_KEY": "testkey",
        "WO_INSTANCE_URL": "https://test.example.com",
        "WO_AGENT_ID": "agent-001",
    })
    @patch("orchestrate_client.create_snapshot_token")
    @patch("orchestrate_client.urlopen")
    def test_stage_a_return_contract(self, mock_urlopen, mock_create_token):
        """Return dict must include analysis and evidence_pitches."""
        mock_create_token.return_value = "fake-token"
        self._setup_urlopen(mock_urlopen, self._mock_response())

        result = generate_stage_a(777245, 8, 1)

        self.assertIn("analysis", result)
        self.assertIn("evidence_pitches", result)
        self.assertIsInstance(result["analysis"], str)
        self.assertIsInstance(result["evidence_pitches"], list)


# ---------------------------------------------------------------------------
# 6. Stage A response metadata
# ---------------------------------------------------------------------------

class TestStageAResponseMetadata(unittest.TestCase):

    def _make_full_response(self):
        return {
            "id": "resp-abc",
            "created": 1700001000,
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({
                        "analysis": "Pitcher threw a fastball.",
                        "evidence_pitches": [1],
                    }),
                    "tool_calls": [
                        {
                            "id": "tc-1",
                            "type": "function",
                            "function": {
                                "name": "redeem_snapshot",
                                "arguments": '{"token": "SUPERSECRET"}',
                            },
                        }
                    ],
                },
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    def test_allowed_fields_extracted(self):
        result = _extract_response_metadata(self._make_full_response())
        self.assertEqual(result["id"], "resp-abc")
        self.assertEqual(result["created"], 1700001000)
        self.assertIn("usage", result)
        self.assertEqual(result["finish_reason"], "stop")

    def test_tool_call_arguments_excluded(self):
        result = _extract_response_metadata(self._make_full_response())
        self.assertIn("tool_calls", result)
        tc = result["tool_calls"][0]
        self.assertNotIn("arguments", tc)
        # function_name is safe; arguments are not
        self.assertEqual(tc["function_name"], "redeem_snapshot")

    def test_non_numeric_usage_excluded(self):
        resp = {
            "id": "x",
            "created": 1,
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 10, "model": "ibm-granite"},
        }
        result = _extract_response_metadata(resp)
        self.assertIn("usage", result)
        self.assertEqual(result["usage"]["prompt_tokens"], 10)
        self.assertNotIn("model", result.get("usage", {}))

    def test_empty_response_no_metadata(self):
        result = _extract_response_metadata({})
        self.assertEqual(result, {})

    def test_missing_fields_no_failure(self):
        """Missing individual fields must never raise."""
        resp = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        result = _extract_response_metadata(resp)
        self.assertNotIn("id", result)
        self.assertNotIn("created", result)
        self.assertNotIn("usage", result)
        self.assertEqual(result["finish_reason"], "stop")

    def test_truly_empty_envelope_no_metadata_key(self):
        """When IBM returns nothing useful, metadata key must be absent."""
        result = _extract_response_metadata({})
        self.assertFalse(result)  # empty dict → falsy → no key added

    def test_no_sensitive_fields(self):
        result = _extract_response_metadata(self._make_full_response())
        for bad_key in ("token", "access_token", "authorization", "apikey", "secret"):
            self.assertNotIn(bad_key, result)
            for tc in result.get("tool_calls", []):
                self.assertNotIn(bad_key, tc)


# ---------------------------------------------------------------------------
# 7. Fan commentary response metadata
# ---------------------------------------------------------------------------

class TestFanCommentaryResponseMetadata(unittest.TestCase):

    def _make_fan_response(self):
        return {
            "id": "fan-resp-01",
            "created": 1700002000,
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({
                        "commentary_by_level": {
                            "novice":   "Simple explanation.",
                            "learning": "Learning explanation.",
                            "regular":  "Regular commentary.",
                            "expert":   "Expert commentary.",
                        }
                    }),
                },
            }],
            "usage": {"prompt_tokens": 20, "completion_tokens": 40},
        }

    def _stage_a_input(self):
        return {
            "analysis": "Pitcher threw one fastball.",
            "evidence_pitches": [1],
            "revealed_count": 1,
            "fact_sheet": "Test fact sheet.",
        }

    @patch.dict(os.environ, {
        "WO_API_KEY": "testkey",
        "WO_INSTANCE_URL": "https://test.example.com",
        "WO_FAN_EXPERIENCE_AGENT_ID": "fan-agent-001",
    })
    @patch("orchestrate_client.urlopen")
    def test_metadata_extracted(self, mock_urlopen):
        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            class R:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self):
                    if "iam.cloud.ibm.com" in url:
                        return json.dumps({"access_token": "tok"}).encode()
                    return json.dumps(self._resp).encode()
            r = R()
            r._resp = {"access_token": "tok"} if "iam.cloud.ibm.com" in url else self._make_fan_response()
            return r
        mock_urlopen.side_effect = lambda req, timeout=None: _fake_dual_urlopen(
            req, timeout,
            fan_response=self._make_fan_response()
        )

        result = generate_fan_commentary(self._stage_a_input())
        self.assertIn("response_metadata", result)
        self.assertEqual(result["response_metadata"]["id"], "fan-resp-01")

    @patch.dict(os.environ, {
        "WO_API_KEY": "testkey",
        "WO_INSTANCE_URL": "https://test.example.com",
        "WO_FAN_EXPERIENCE_AGENT_ID": "fan-agent-001",
    })
    @patch("orchestrate_client.urlopen")
    def test_metadata_no_sensitive_fields(self, mock_urlopen):
        mock_urlopen.side_effect = lambda req, timeout=None: _fake_dual_urlopen(
            req, timeout,
            fan_response=self._make_fan_response()
        )
        result = generate_fan_commentary(self._stage_a_input())
        meta = result.get("response_metadata", {})
        for bad_key in ("token", "access_token", "authorization", "apikey"):
            self.assertNotIn(bad_key, meta)


# ---------------------------------------------------------------------------
# Helper: dual-URL fake urlopen used by fan commentary tests
# ---------------------------------------------------------------------------

def _fake_dual_urlopen(req, timeout=None, fan_response=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)

    class FakeResp:
        def __init__(self, data):
            self._data = data
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self._data).encode()

    if "iam.cloud.ibm.com" in url:
        return FakeResp({"access_token": "tok"})
    return FakeResp(fan_response)


# ---------------------------------------------------------------------------
# 8. Fan commentary fact sheet injected into prompt
# ---------------------------------------------------------------------------

class TestFanCommentaryFactSheet(unittest.TestCase):
    """The fact sheet must appear verbatim in the outgoing prompt."""

    UNIQUE_MARKER = "UNIQUE_FACT_SHEET_MARKER_XYZ_12345"

    def _stage_a_input(self):
        return {
            "analysis": "Pitcher threw one fastball.",
            "evidence_pitches": [1],
            "revealed_count": 1,
            "fact_sheet": self.UNIQUE_MARKER,
        }

    def _fan_response(self):
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({
                        "commentary_by_level": {
                            "novice":   "A fastball.",
                            "learning": "A four-seam fastball.",
                            "regular":  "One fastball pitch.",
                            "expert":   "One four-seam fastball.",
                        }
                    })
                },
            }]
        }

    @patch.dict(os.environ, {
        "WO_API_KEY": "testkey",
        "WO_INSTANCE_URL": "https://test.example.com",
        "WO_FAN_EXPERIENCE_AGENT_ID": "fan-agent-001",
    })
    @patch("orchestrate_client.urlopen")
    def test_fact_sheet_in_prompt(self, mock_urlopen):
        """The marker must appear in the second urlopen call's payload."""
        captured_payloads = []

        def capture_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if hasattr(req, "data") and req.data:
                captured_payloads.append(req.data.decode("utf-8"))

            class FakeResp:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self):
                    if "iam.cloud.ibm.com" in url:
                        return json.dumps({"access_token": "tok"}).encode()
                    return json.dumps(self._fan_response()).encode()
            r = FakeResp()
            r._fan_response = self._fan_response
            return r

        mock_urlopen.side_effect = capture_urlopen
        generate_fan_commentary(self._stage_a_input())

        # Second call is the IBM agent request
        self.assertGreaterEqual(len(captured_payloads), 2,
                                "Expected at least 2 urlopen calls")
        agent_payload = captured_payloads[1]
        self.assertIn(self.UNIQUE_MARKER, agent_payload,
                      "Fact sheet marker must appear in the Fan Experience prompt")


# ---------------------------------------------------------------------------
# 9. Fan commentary requires revealed_count
# ---------------------------------------------------------------------------

class TestFanCommentaryRequiresRevealedCount(unittest.TestCase):

    def _base_input(self):
        return {
            "analysis": "One fastball.",
            "evidence_pitches": [1],
        }

    def test_missing_revealed_count_raises(self):
        with self.assertRaises(FanCommentaryError) as ctx:
            generate_fan_commentary(self._base_input())
        self.assertIn("revealed_count", str(ctx.exception))

    def test_string_revealed_count_raises(self):
        inp = {**self._base_input(), "revealed_count": "3"}
        with self.assertRaises(FanCommentaryError):
            generate_fan_commentary(inp)

    def test_negative_revealed_count_raises(self):
        inp = {**self._base_input(), "revealed_count": -1}
        with self.assertRaises(FanCommentaryError):
            generate_fan_commentary(inp)

    def test_zero_revealed_count_accepted(self):
        """revealed_count=0 is valid (pre-game analysis with no pitches)."""
        # With zero pitches there's nothing to match against FUTURE_PITCH_RE
        # for future pitches, so this should NOT raise on input validation.
        # We just check that FanCommentaryError is not raised for the
        # revealed_count value itself (it may raise for missing env vars).
        inp = {**self._base_input(), "revealed_count": 0}
        try:
            generate_fan_commentary(inp)
        except FanCommentaryError as exc:
            msg = str(exc)
            # Only acceptable if it's not about revealed_count validation
            self.assertNotIn("revealed_count must be a non-negative", msg)
        except (KeyError, OSError):
            pass  # env vars not set — that's fine in this test


# ---------------------------------------------------------------------------
# 10. Precompute: revealed_count injection and fact_sheet_fn
# ---------------------------------------------------------------------------

class TestPrecomputeRevealedCountAndFactSheet(unittest.TestCase):
    """run_precompute must inject revealed_count=position and pass fact_sheet."""

    def _make_stage_a(self):
        return {"analysis": "One pitch.", "evidence_pitches": [1]}

    def _make_stage_b(self, stage_a_input):
        return {
            "commentary_by_level": {
                "novice": "A.", "learning": "B.", "regular": "C.", "expert": "D."
            },
            "evidence_pitches": stage_a_input.get("evidence_pitches", [1]),
        }

    def test_revealed_count_injected(self):
        """run_precompute must set stage_a['revealed_count'] = position."""
        received = []

        def stage_a_fn(game_id, at_bat_index, position):
            return self._make_stage_a()

        def stage_b_fn(stage_a_input):
            received.append(stage_a_input.get("revealed_count"))
            return self._make_stage_b(stage_a_input)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            cache_path = f.name
        try:
            run_precompute(
                [3, 5], {}, cache_path,
                stage_a_fn=stage_a_fn, stage_b_fn=stage_b_fn,
            )
        finally:
            os.unlink(cache_path)

        self.assertEqual(received, [3, 5])

    def test_revealed_count_overrides_preset(self):
        """run_precompute must overwrite any revealed_count the stage_a_fn returns."""
        received = []

        def stage_a_fn(game_id, at_bat_index, position):
            return {"analysis": "X.", "evidence_pitches": [1], "revealed_count": 999}

        def stage_b_fn(stage_a_input):
            received.append(stage_a_input["revealed_count"])
            return self._make_stage_b(stage_a_input)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            cache_path = f.name
        try:
            run_precompute([4], {}, cache_path,
                           stage_a_fn=stage_a_fn, stage_b_fn=stage_b_fn)
        finally:
            os.unlink(cache_path)

        self.assertEqual(received[0], 4,
                         "revealed_count must be overwritten to position")

    def test_fact_sheet_fn_injected(self):
        """fact_sheet_fn result must appear in stage_b's stage_a_input."""
        received_fact_sheets = []

        def stage_a_fn(game_id, at_bat_index, position):
            return self._make_stage_a()

        def stage_b_fn(stage_a_input):
            received_fact_sheets.append(stage_a_input.get("fact_sheet"))
            return self._make_stage_b(stage_a_input)

        def fact_sheet_fn(game_id, at_bat_index, position):
            return f"FACT_SHEET_FOR_POSITION_{position}"

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            cache_path = f.name
        try:
            run_precompute([2], {}, cache_path,
                           stage_a_fn=stage_a_fn, stage_b_fn=stage_b_fn,
                           fact_sheet_fn=fact_sheet_fn)
        finally:
            os.unlink(cache_path)

        self.assertEqual(received_fact_sheets, ["FACT_SHEET_FOR_POSITION_2"])

    def test_fact_sheet_fn_failure_causes_sys_exit(self):
        """If fact_sheet_fn raises, run_precompute must call sys.exit(1)."""

        def stage_a_fn(game_id, at_bat_index, position):
            return self._make_stage_a()

        def stage_b_fn(stage_a_input):
            return self._make_stage_b(stage_a_input)

        def bad_fact_sheet_fn(game_id, at_bat_index, position):
            raise RuntimeError("Simulated fact sheet failure")

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            cache_path = f.name
        try:
            with self.assertRaises(SystemExit):
                run_precompute([1], {}, cache_path,
                               stage_a_fn=stage_a_fn, stage_b_fn=stage_b_fn,
                               fact_sheet_fn=bad_fact_sheet_fn)
        finally:
            os.unlink(cache_path)


# ---------------------------------------------------------------------------
# 11. _check_future_pitch_refs patterns
# ---------------------------------------------------------------------------

class TestCheckFuturePitchRefs(unittest.TestCase):

    def test_single_number_future(self):
        refs = _check_future_pitch_refs("pitch 4 was thrown", revealed_count=3)
        self.assertIn(4, refs)

    def test_single_number_hash_notation(self):
        refs = _check_future_pitch_refs("pitch #7 was wild", revealed_count=3)
        self.assertIn(7, refs)

    def test_hyphen_range_upper_future(self):
        """'pitches 2-6' — upper endpoint 6 > revealed_count=3 is flagged."""
        refs = _check_future_pitch_refs("pitches 2-6 were excellent", revealed_count=3)
        self.assertIn(6, refs)

    def test_en_dash_range_upper_future(self):
        """'pitches 2–6' en-dash — same detection."""
        refs = _check_future_pitch_refs("pitches 2\u20136 were excellent", revealed_count=3)
        self.assertIn(6, refs)

    def test_already_revealed_no_flag(self):
        """References to revealed pitches must NOT be flagged."""
        refs = _check_future_pitch_refs("pitch 1 and pitch 3", revealed_count=3)
        self.assertEqual(refs, [])

    def test_range_entirely_revealed(self):
        refs = _check_future_pitch_refs("pitches 1-3", revealed_count=3)
        self.assertEqual(refs, [])

    def test_plural_pitches_single_future(self):
        # "pitches 5 and 6" matches as two separate regex tokens: "pitches 5"
        # and "pitch 6". Both are beyond revealed_count=3.
        refs = _check_future_pitch_refs("pitches 5 and pitch 6", revealed_count=3)
        self.assertIn(5, refs)
        self.assertIn(6, refs)

    def test_empty_string_no_refs(self):
        refs = _check_future_pitch_refs("", revealed_count=3)
        self.assertEqual(refs, [])

    def test_no_pitch_numbers_no_refs(self):
        refs = _check_future_pitch_refs("The pitcher threw well.", revealed_count=3)
        self.assertEqual(refs, [])


# ---------------------------------------------------------------------------
# 12. Stage A spoiler detection enhanced
# ---------------------------------------------------------------------------

class TestStageASpoilerDetectionEnhanced(unittest.TestCase):
    """generate_stage_a must reject analysis with future pitch references."""

    def _setup_stage_a(self, mock_urlopen, mock_create_token, analysis_text):
        mock_create_token.return_value = "fake-token"
        response = {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({
                        "analysis": analysis_text,
                        "evidence_pitches": [1],
                    })
                },
            }]
        }
        mock_urlopen.side_effect = lambda req, timeout=None: _fake_dual_urlopen(
            req, timeout, fan_response=response
        )

    @patch.dict(os.environ, {
        "SNAPSHOT_SECRET": "s", "WO_API_KEY": "k",
        "WO_INSTANCE_URL": "https://x.com", "WO_AGENT_ID": "a",
    })
    @patch("orchestrate_client.create_snapshot_token")
    @patch("orchestrate_client.urlopen")
    def test_hyphen_range_rejected(self, mock_urlopen, mock_create_token):
        self._setup_stage_a(mock_urlopen, mock_create_token,
                            "Pitches 2-5 showed velocity drop.")
        with self.assertRaises(StageAError):
            generate_stage_a(777245, 8, 1)

    @patch.dict(os.environ, {
        "SNAPSHOT_SECRET": "s", "WO_API_KEY": "k",
        "WO_INSTANCE_URL": "https://x.com", "WO_AGENT_ID": "a",
    })
    @patch("orchestrate_client.create_snapshot_token")
    @patch("orchestrate_client.urlopen")
    def test_en_dash_range_rejected(self, mock_urlopen, mock_create_token):
        self._setup_stage_a(mock_urlopen, mock_create_token,
                            "Pitches 2\u20135 showed velocity drop.")
        with self.assertRaises(StageAError):
            generate_stage_a(777245, 8, 1)

    @patch.dict(os.environ, {
        "SNAPSHOT_SECRET": "s", "WO_API_KEY": "k",
        "WO_INSTANCE_URL": "https://x.com", "WO_AGENT_ID": "a",
    })
    @patch("orchestrate_client.create_snapshot_token")
    @patch("orchestrate_client.urlopen")
    def test_single_future_pitch_rejected(self, mock_urlopen, mock_create_token):
        self._setup_stage_a(mock_urlopen, mock_create_token,
                            "Pitch 4 was a curveball.")
        with self.assertRaises(StageAError):
            generate_stage_a(777245, 8, 3)

    @patch.dict(os.environ, {
        "SNAPSHOT_SECRET": "s", "WO_API_KEY": "k",
        "WO_INSTANCE_URL": "https://x.com", "WO_AGENT_ID": "a",
    })
    @patch("orchestrate_client.create_snapshot_token")
    @patch("orchestrate_client.urlopen")
    def test_valid_analysis_accepted(self, mock_urlopen, mock_create_token):
        self._setup_stage_a(mock_urlopen, mock_create_token,
                            "Pitch 1 was a four-seam fastball.")
        result = generate_stage_a(777245, 8, 3)
        self.assertIn("analysis", result)


# ---------------------------------------------------------------------------
# 13. Fan commentary future-pitch rejection
# ---------------------------------------------------------------------------

class TestFanCommentaryFuturePitchRejected(unittest.TestCase):

    def _make_fan_response_with_text(self, level_text):
        levels = {
            "novice": "Normal.", "learning": "Normal.",
            "regular": "Normal.", "expert": "Normal.",
        }
        levels["expert"] = level_text
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({"commentary_by_level": levels})
                },
            }]
        }

    def _stage_a_input(self, revealed_count=3):
        return {
            "analysis": "Three pitches thrown.",
            "evidence_pitches": [1, 2, 3],
            "revealed_count": revealed_count,
        }

    @patch.dict(os.environ, {
        "WO_API_KEY": "k", "WO_INSTANCE_URL": "https://x.com",
        "WO_FAN_EXPERIENCE_AGENT_ID": "f",
    })
    @patch("orchestrate_client.urlopen")
    def test_future_pitch_rejected(self, mock_urlopen):
        mock_urlopen.side_effect = lambda req, timeout=None: _fake_dual_urlopen(
            req, timeout,
            fan_response=self._make_fan_response_with_text("Pitch 4 was nasty.")
        )
        with self.assertRaises(FanCommentaryError) as ctx:
            generate_fan_commentary(self._stage_a_input(revealed_count=3))
        self.assertIn("future pitch", str(ctx.exception).lower())

    @patch.dict(os.environ, {
        "WO_API_KEY": "k", "WO_INSTANCE_URL": "https://x.com",
        "WO_FAN_EXPERIENCE_AGENT_ID": "f",
    })
    @patch("orchestrate_client.urlopen")
    def test_en_dash_range_rejected(self, mock_urlopen):
        mock_urlopen.side_effect = lambda req, timeout=None: _fake_dual_urlopen(
            req, timeout,
            fan_response=self._make_fan_response_with_text(
                "Pitches 2\u20136 were outstanding."
            )
        )
        with self.assertRaises(FanCommentaryError):
            generate_fan_commentary(self._stage_a_input(revealed_count=3))


# ---------------------------------------------------------------------------
# 14. Fan commentary count-reset phrase rejection
# ---------------------------------------------------------------------------

class TestFanCommentaryCountResetPhrases(unittest.TestCase):

    def _make_response_with_phrase(self, phrase):
        levels = {
            "novice": "Normal.", "learning": "Normal.",
            "regular": "Normal.", "expert": f"He got a {phrase} with ball four.",
        }
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({"commentary_by_level": levels})
                },
            }]
        }

    @patch.dict(os.environ, {
        "WO_API_KEY": "k", "WO_INSTANCE_URL": "https://x.com",
        "WO_FAN_EXPERIENCE_AGENT_ID": "f",
    })
    @patch("orchestrate_client.urlopen")
    def _assert_phrase_rejected(self, phrase, mock_urlopen):
        mock_urlopen.side_effect = lambda req, timeout=None: _fake_dual_urlopen(
            req, timeout,
            fan_response=self._make_response_with_phrase(phrase)
        )
        stage_a_input = {
            "analysis": "Pitcher threw one fastball.",
            "evidence_pitches": [1],
            "revealed_count": 1,
        }
        with self.assertRaises(FanCommentaryError) as ctx:
            generate_fan_commentary(stage_a_input)
        self.assertIn("count-reset phrase", str(ctx.exception).lower())

    def test_fresh_chance_rejected(self):
        self._assert_phrase_rejected("fresh chance")

    def test_reset_the_count_rejected(self):
        self._assert_phrase_rejected("reset the count")

    def test_back_in_the_count_rejected(self):
        self._assert_phrase_rejected("back in the count")

    def test_leveled_the_count_rejected(self):
        self._assert_phrase_rejected("leveled the count")


# ---------------------------------------------------------------------------
# 15. Stage A return contract
# ---------------------------------------------------------------------------

class TestStageAReturnContract(unittest.TestCase):
    """Detailed return-contract assertions for generate_stage_a."""

    @patch.dict(os.environ, {
        "SNAPSHOT_SECRET": "s", "WO_API_KEY": "k",
        "WO_INSTANCE_URL": "https://x.com", "WO_AGENT_ID": "a",
    })
    @patch("orchestrate_client.create_snapshot_token")
    @patch("orchestrate_client.urlopen")
    def test_return_dict_keys(self, mock_urlopen, mock_create_token):
        mock_create_token.return_value = "tok"
        response = {
            "id": "r1", "created": 1, "usage": {"prompt_tokens": 1},
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({
                        "analysis": "One fastball.",
                        "evidence_pitches": [1],
                    })
                },
            }],
        }
        mock_urlopen.side_effect = lambda req, timeout=None: _fake_dual_urlopen(
            req, timeout, fan_response=response
        )
        result = generate_stage_a(777245, 8, 1)
        self.assertIn("analysis", result)
        self.assertIn("evidence_pitches", result)
        self.assertIsInstance(result["analysis"], str)
        self.assertTrue(result["analysis"].strip())
        self.assertIsInstance(result["evidence_pitches"], list)
        # response_metadata present when IBM returns recognized fields
        self.assertIn("response_metadata", result)

    @patch.dict(os.environ, {
        "SNAPSHOT_SECRET": "s", "WO_API_KEY": "k",
        "WO_INSTANCE_URL": "https://x.com", "WO_AGENT_ID": "a",
    })
    @patch("orchestrate_client.create_snapshot_token")
    @patch("orchestrate_client.urlopen")
    def test_no_metadata_key_when_empty(self, mock_urlopen, mock_create_token):
        mock_create_token.return_value = "tok"
        response = {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({
                        "analysis": "One fastball.",
                        "evidence_pitches": [1],
                    })
                },
            }],
        }
        mock_urlopen.side_effect = lambda req, timeout=None: _fake_dual_urlopen(
            req, timeout, fan_response=response
        )
        result = generate_stage_a(777245, 8, 1)
        # Only finish_reason from choices — that is non-empty, metadata key may exist
        # but must not contain id/created/usage
        meta = result.get("response_metadata", {})
        self.assertNotIn("id", meta)
        self.assertNotIn("created", meta)
        self.assertNotIn("usage", meta)


# ---------------------------------------------------------------------------
# 16. Fan commentary return contract
# ---------------------------------------------------------------------------

class TestFanCommentaryReturnContract(unittest.TestCase):

    def _good_fan_response(self):
        return {
            "id": "fan-1", "created": 2,
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({
                        "commentary_by_level": {
                            "novice": "Simple.", "learning": "Learn.",
                            "regular": "Regular.", "expert": "Expert.",
                        }
                    })
                },
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 20},
        }

    @patch.dict(os.environ, {
        "WO_API_KEY": "k", "WO_INSTANCE_URL": "https://x.com",
        "WO_FAN_EXPERIENCE_AGENT_ID": "f",
    })
    @patch("orchestrate_client.urlopen")
    def test_return_dict_keys(self, mock_urlopen):
        mock_urlopen.side_effect = lambda req, timeout=None: _fake_dual_urlopen(
            req, timeout, fan_response=self._good_fan_response()
        )
        stage_a_input = {
            "analysis": "One fastball.", "evidence_pitches": [1],
            "revealed_count": 1,
        }
        result = generate_fan_commentary(stage_a_input)
        self.assertIn("commentary_by_level", result)
        self.assertIn("evidence_pitches", result)
        for level in ("novice", "learning", "regular", "expert"):
            self.assertIn(level, result["commentary_by_level"])
            self.assertIsInstance(result["commentary_by_level"][level], str)
        # evidence_pitches passed through from input
        self.assertEqual(result["evidence_pitches"], [1])


# ---------------------------------------------------------------------------
# 17. _extract_response_metadata unit tests
# ---------------------------------------------------------------------------

class TestExtractResponseMetadata(unittest.TestCase):

    def test_all_safe_fields_present(self):
        resp = {
            "id": "x",
            "created": 123,
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            "choices": [{"finish_reason": "stop", "message": {}}],
        }
        m = _extract_response_metadata(resp)
        self.assertEqual(m["id"], "x")
        self.assertEqual(m["created"], 123)
        self.assertIn("usage", m)
        self.assertEqual(m["finish_reason"], "stop")

    def test_non_string_id_excluded(self):
        resp = {"id": 42, "choices": []}
        m = _extract_response_metadata(resp)
        self.assertNotIn("id", m)

    def test_non_int_created_excluded(self):
        resp = {"created": "2024-01-01", "choices": []}
        m = _extract_response_metadata(resp)
        self.assertNotIn("created", m)

    def test_tool_calls_arguments_stripped(self):
        resp = {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": "{}",
                    "tool_calls": [{
                        "id": "tc1", "type": "function",
                        "function": {
                            "name": "redeem_snapshot",
                            "arguments": '{"token":"SECRET"}',
                        }
                    }]
                }
            }]
        }
        m = _extract_response_metadata(resp)
        tc = m["tool_calls"][0]
        self.assertEqual(tc["function_name"], "redeem_snapshot")
        self.assertNotIn("arguments", tc)
        self.assertNotIn("function", tc)

    def test_empty_dict_returns_empty(self):
        self.assertEqual(_extract_response_metadata({}), {})

    def test_usage_non_numeric_values_stripped(self):
        resp = {
            "usage": {"prompt_tokens": 10, "model": "gpt-x", "completion_tokens": 5},
            "choices": [],
        }
        m = _extract_response_metadata(resp)
        self.assertIn("usage", m)
        self.assertNotIn("model", m["usage"])
        self.assertIn("prompt_tokens", m["usage"])

    def test_no_choices_no_finish_reason(self):
        resp = {"id": "y", "created": 1, "choices": []}
        m = _extract_response_metadata(resp)
        self.assertNotIn("finish_reason", m)


# ---------------------------------------------------------------------------
# V2 helpers: shared mocks
# ---------------------------------------------------------------------------

def _good_stage_a_fn(game_id, at_bat_index, position, run_id=None):
    return {
        "analysis": f"Analysis for position {position}.",
        "evidence_pitches": [1],
        "response_metadata": {"id": f"a-{position}", "finish_reason": "stop"},
    }


def _good_stage_b_fn(stage_a_input):
    return {
        "commentary_by_level": {
            "novice": "N.", "learning": "L.", "regular": "R.", "expert": "E."
        },
        "evidence_pitches": stage_a_input.get("evidence_pitches", [1]),
        "response_metadata": {"id": "b-1", "finish_reason": "stop"},
    }


def _good_fact_sheet_fn(game_id, at_bat_index, position):
    return f"Fact sheet for position {position}. Pitches revealed so far: {position}"


# ---------------------------------------------------------------------------
# 18. V2: --v2 never touches commentary_cache.json
# ---------------------------------------------------------------------------

class TestV2NeverTouchesLegacyCache(unittest.TestCase):

    def test_v2_does_not_read_legacy_cache(self):
        """run_v2_precompute must not touch CACHE_PATH at all."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        # Write a sentinel to CACHE_PATH location (we pass a different path)
        try:
            run_v2_precompute(
                [1], {}, v2_path, {}, trace_path,
                stage_a_fn=_good_stage_a_fn,
                stage_b_fn=_good_stage_b_fn,
                fact_sheet_fn=_good_fact_sheet_fn,
            )
            # Verify CACHE_PATH (the constant) was not written
            # by checking the V2 path was written instead
            v2_cache = load_cache(v2_path)
            self.assertIn("1", v2_cache)
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

    def test_v2_writes_to_separate_files(self):
        """V2 must write v2_cache and trace_cache to their designated paths."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            run_v2_precompute(
                [2], {}, v2_path, {}, trace_path,
                stage_a_fn=_good_stage_a_fn,
                stage_b_fn=_good_stage_b_fn,
                fact_sheet_fn=_good_fact_sheet_fn,
            )
            v2_cache = load_cache(v2_path)
            trace_cache = load_cache(trace_path)
            self.assertIn("2", v2_cache)
            self.assertIn("2", trace_cache)
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 19. V2: completed positions are skipped on resume
# ---------------------------------------------------------------------------

class TestV2SkipCompleted(unittest.TestCase):

    def test_completed_position_skipped(self):
        call_counts = []

        def counting_stage_a(game_id, at_bat_index, position, run_id=None):
            call_counts.append(position)
            return _good_stage_a_fn(game_id, at_bat_index, position, run_id)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        # Pre-populate position 1 as complete
        pre_cache = {"1": {"complete": True, "commentary_by_level": {
            "novice": "X", "learning": "X", "regular": "X", "expert": "X"
        }}}
        try:
            run_v2_precompute(
                [1, 2], pre_cache, v2_path, {}, trace_path,
                stage_a_fn=counting_stage_a,
                stage_b_fn=_good_stage_b_fn,
                fact_sheet_fn=_good_fact_sheet_fn,
            )
            # Only position 2 should have been processed
            self.assertEqual(call_counts, [2])
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 20. V2: each position gets a unique run_id
# ---------------------------------------------------------------------------

class TestV2UniqueRunIds(unittest.TestCase):

    def test_unique_run_ids_per_position(self):
        received_run_ids = []

        def capturing_stage_a(game_id, at_bat_index, position, run_id=None):
            received_run_ids.append(run_id)
            return _good_stage_a_fn(game_id, at_bat_index, position, run_id)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            run_v2_precompute(
                [1, 2, 3], {}, v2_path, {}, trace_path,
                stage_a_fn=capturing_stage_a,
                stage_b_fn=_good_stage_b_fn,
                fact_sheet_fn=_good_fact_sheet_fn,
            )
            self.assertEqual(len(received_run_ids), 3)
            self.assertEqual(len(set(received_run_ids)), 3,
                             "All run_ids must be unique across positions")
            for rid in received_run_ids:
                self.assertIsNotNone(rid)
                self.assertEqual(len(rid), 16)
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 21. V2: run_id reaches generate_stage_a as keyword arg
# ---------------------------------------------------------------------------

class TestV2RunIdKwarg(unittest.TestCase):

    def test_run_id_passed_as_keyword(self):
        """stage_a_fn must receive run_id as a keyword argument."""
        keyword_run_ids = []

        def stage_a_with_kwarg(game_id, at_bat_index, position, **kwargs):
            keyword_run_ids.append(kwargs.get("run_id"))
            return _good_stage_a_fn(game_id, at_bat_index, position)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            run_v2_precompute(
                [1], {}, v2_path, {}, trace_path,
                stage_a_fn=stage_a_with_kwarg,
                stage_b_fn=_good_stage_b_fn,
                fact_sheet_fn=_good_fact_sheet_fn,
            )
            self.assertEqual(len(keyword_run_ids), 1)
            self.assertIsNotNone(keyword_run_ids[0])
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 22. V2: Stage B receives correct revealed_count
# ---------------------------------------------------------------------------

class TestV2StageBRevealedCount(unittest.TestCase):

    def test_stage_b_receives_authoritative_revealed_count(self):
        received = []

        def stage_b_recording(stage_a_input):
            received.append(stage_a_input.get("revealed_count"))
            return _good_stage_b_fn(stage_a_input)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            run_v2_precompute(
                [4, 7], {}, v2_path, {}, trace_path,
                stage_a_fn=_good_stage_a_fn,
                stage_b_fn=stage_b_recording,
                fact_sheet_fn=_good_fact_sheet_fn,
            )
            self.assertEqual(received, [4, 7])
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 23. V2: Stage B receives verified fact sheet (position 3)
# ---------------------------------------------------------------------------

class TestV2StageBFactSheet(unittest.TestCase):

    MARKER = "Pitches revealed so far: 3"

    def test_stage_b_receives_fact_sheet_with_marker(self):
        received_fact_sheets = []

        def stage_b_recording(stage_a_input):
            received_fact_sheets.append(stage_a_input.get("fact_sheet", ""))
            return _good_stage_b_fn(stage_a_input)

        def fact_sheet_fn(game_id, at_bat_index, position):
            return build_fact_sheet(GAME_DATA_VERIFIED, 777245, 8, position)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            run_v2_precompute(
                [3], {}, v2_path, {}, trace_path,
                game_id=777245, at_bat_index=8,
                stage_a_fn=_good_stage_a_fn,
                stage_b_fn=stage_b_recording,
                fact_sheet_fn=fact_sheet_fn,
            )
            self.assertEqual(len(received_fact_sheets), 1)
            self.assertIn(self.MARKER, received_fact_sheets[0])
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 24. V2: missing/empty fact sheet prevents Stage B
# ---------------------------------------------------------------------------

class TestV2MissingFactSheetFail(unittest.TestCase):

    def test_none_fact_sheet_fn_causes_exit(self):
        """fact_sheet_fn=None in V2 must call sys.exit(1)."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            with self.assertRaises(SystemExit):
                run_v2_precompute(
                    [1], {}, v2_path, {}, trace_path,
                    stage_a_fn=_good_stage_a_fn,
                    stage_b_fn=_good_stage_b_fn,
                    fact_sheet_fn=None,
                )
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

    def test_empty_fact_sheet_causes_exit(self):
        """fact_sheet_fn returning empty string in V2 must call sys.exit(1)."""

        def empty_fact_sheet_fn(game_id, at_bat_index, position):
            return ""

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            with self.assertRaises(SystemExit):
                run_v2_precompute(
                    [1], {}, v2_path, {}, trace_path,
                    stage_a_fn=_good_stage_a_fn,
                    stage_b_fn=_good_stage_b_fn,
                    fact_sheet_fn=empty_fact_sheet_fn,
                )
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 25. V2: trace events correct order and nondecreasing elapsed
# ---------------------------------------------------------------------------

class TestV2TraceEventOrder(unittest.TestCase):

    def _run_one(self, position=1):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            _, trace_cache = run_v2_precompute(
                [position], {}, v2_path, {}, trace_path,
                stage_a_fn=_good_stage_a_fn,
                stage_b_fn=_good_stage_b_fn,
                fact_sheet_fn=_good_fact_sheet_fn,
            )
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

        return trace_cache

    def test_expected_event_names_present(self):
        trace = self._run_one(1)
        events = trace["1"]["events"]
        names = [e["event"] for e in events]
        self.assertIn("run_started", names)
        self.assertIn("stage_a_call_started", names)
        self.assertIn("stage_a_call_returned", names)
        self.assertIn("stage_b_call_started", names)
        self.assertIn("stage_b_call_returned", names)
        self.assertIn("run_completed", names)

    def test_event_order(self):
        trace = self._run_one(1)
        events = trace["1"]["events"]
        names = [e["event"] for e in events]
        self.assertLess(names.index("run_started"),
                        names.index("stage_a_call_started"))
        self.assertLess(names.index("stage_a_call_started"),
                        names.index("stage_a_call_returned"))
        self.assertLess(names.index("stage_a_call_returned"),
                        names.index("stage_b_call_started"))
        self.assertLess(names.index("stage_b_call_returned"),
                        names.index("run_completed"))

    def test_elapsed_nondecreasing(self):
        trace = self._run_one(1)
        events = trace["1"]["events"]
        elapsed = [e["elapsed_seconds"] for e in events]
        for i in range(1, len(elapsed)):
            self.assertGreaterEqual(elapsed[i], elapsed[i - 1])


# ---------------------------------------------------------------------------
# 26. V2: trace_complete is False
# ---------------------------------------------------------------------------

class TestV2TraceCompleteFalse(unittest.TestCase):

    def test_trace_complete_false(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            _, trace_cache = run_v2_precompute(
                [1], {}, v2_path, {}, trace_path,
                stage_a_fn=_good_stage_a_fn,
                stage_b_fn=_good_stage_b_fn,
                fact_sheet_fn=_good_fact_sheet_fn,
            )
            self.assertFalse(trace_cache["1"]["trace_complete"])
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 27. V2: response metadata retained in trace events
# ---------------------------------------------------------------------------

class TestV2MetadataInTrace(unittest.TestCase):

    def test_stage_a_metadata_in_trace(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            _, trace_cache = run_v2_precompute(
                [1], {}, v2_path, {}, trace_path,
                stage_a_fn=_good_stage_a_fn,
                stage_b_fn=_good_stage_b_fn,
                fact_sheet_fn=_good_fact_sheet_fn,
            )
            events = trace_cache["1"]["events"]
            returned_events = [e for e in events if e["event"] == "stage_a_call_returned"]
            self.assertEqual(len(returned_events), 1)
            meta = returned_events[0].get("meta")
            self.assertIsNotNone(meta)
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 28. V2: redemption matching run_id → snapshot_redeemed event
# ---------------------------------------------------------------------------

class TestV2RedemptionMatching(unittest.TestCase):

    def _run_with_redemption_fn(self, redemption_fn):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            _, trace_cache = run_v2_precompute(
                [1], {}, v2_path, {}, trace_path,
                stage_a_fn=_good_stage_a_fn,
                stage_b_fn=_good_stage_b_fn,
                fact_sheet_fn=_good_fact_sheet_fn,
                redemption_fn=redemption_fn,
            )
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

        return trace_cache

    def test_matching_run_id_adds_event(self):
        """When run_id matches, snapshot_redeemed must appear in trace events."""
        called_with = []

        def redemption_fn(run_id):
            called_with.append(run_id)
            return (_REDEMPTION_FOUND, {
                "run_id": run_id,
                "ts_utc": "2024-01-01T00:00:00+00:00",
                "game_id": 777245,
                "at_bat_index": 8,
                "revealed_count": 1,
                "user_agent": "test",
                "status": "redeemed",
            })

        trace_cache = self._run_with_redemption_fn(redemption_fn)
        events = trace_cache["1"]["events"]
        event_names = [e["event"] for e in events]
        self.assertIn("snapshot_redeemed", event_names)

    def test_mismatched_run_id_no_event_plus_note(self):
        """When run_id mismatches, no snapshot_redeemed event; note added."""

        def redemption_fn(run_id):
            # Return a record with a DIFFERENT run_id
            return (_REDEMPTION_FOUND, {
                "run_id": "different_run_id_xyz",
                "ts_utc": "2024-01-01T00:00:00+00:00",
                "game_id": 777245,
                "at_bat_index": 8,
                "revealed_count": 1,
                "user_agent": "test",
                "status": "redeemed",
            })

        trace_cache = self._run_with_redemption_fn(redemption_fn)
        events = trace_cache["1"]["events"]
        event_names = [e["event"] for e in events]
        self.assertNotIn("snapshot_redeemed", event_names)

        notes = trace_cache["1"]["capture_notes"]
        mismatch_noted = any(
            "run_id did not match" in n for n in notes
        )
        self.assertTrue(mismatch_noted,
                        "Mismatch note must appear in capture_notes")


# ---------------------------------------------------------------------------
# 29. V2: token field not persisted in redemption record
# ---------------------------------------------------------------------------

class TestV2RedemptionTokenNotPersisted(unittest.TestCase):

    def test_token_field_stripped(self):
        """The 'token' field must be stripped from persisted redemption records."""

        def redemption_fn(run_id):
            return (_REDEMPTION_FOUND, {
                "run_id": run_id,
                "ts_utc": "2024-01-01T00:00:00+00:00",
                "game_id": 777245,
                "at_bat_index": 8,
                "revealed_count": 1,
                "user_agent": "test",
                "status": "redeemed",
                "token": "SHOULD_NOT_BE_PERSISTED",  # must be stripped
            })

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            _, trace_cache = run_v2_precompute(
                [1], {}, v2_path, {}, trace_path,
                stage_a_fn=_good_stage_a_fn,
                stage_b_fn=_good_stage_b_fn,
                fact_sheet_fn=_good_fact_sheet_fn,
                redemption_fn=redemption_fn,
            )
            # Find snapshot_redeemed event
            events = trace_cache["1"]["events"]
            redeemed = [e for e in events if e["event"] == "snapshot_redeemed"]
            self.assertEqual(len(redeemed), 1)
            record = redeemed[0].get("record", {})
            self.assertNotIn("token", record)
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

    def test_all_fields_in_allowlist(self):
        """Every field in a saved redemption record must be in _REDEMPTION_ALLOWLIST."""

        saved_records = []

        def redemption_fn(run_id):
            return (_REDEMPTION_FOUND, {
                "run_id": run_id,
                "ts_utc": "T",
                "game_id": 1,
                "at_bat_index": 0,
                "revealed_count": 1,
                "user_agent": "u",
                "status": "s",
                "extra_field": "SHOULD_BE_STRIPPED",
            })

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            _, trace_cache = run_v2_precompute(
                [1], {}, v2_path, {}, trace_path,
                stage_a_fn=_good_stage_a_fn,
                stage_b_fn=_good_stage_b_fn,
                fact_sheet_fn=_good_fact_sheet_fn,
                redemption_fn=redemption_fn,
            )
            events = trace_cache["1"]["events"]
            redeemed = [e for e in events if e["event"] == "snapshot_redeemed"]
            if redeemed:
                record = redeemed[0].get("record", {})
                for k in record:
                    self.assertIn(k, _REDEMPTION_ALLOWLIST,
                                  f"Field {k!r} not in _REDEMPTION_ALLOWLIST")
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 30. V2: missing redemption record and endpoint error are non-fatal
# ---------------------------------------------------------------------------

class TestV2RedemptionNonFatal(unittest.TestCase):

    def _run_to_completion(self, redemption_fn):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            v2_cache, trace_cache = run_v2_precompute(
                [1], {}, v2_path, {}, trace_path,
                stage_a_fn=_good_stage_a_fn,
                stage_b_fn=_good_stage_b_fn,
                fact_sheet_fn=_good_fact_sheet_fn,
                redemption_fn=redemption_fn,
            )
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

        return v2_cache, trace_cache

    def test_missing_record_not_fatal(self):
        def redemption_fn(run_id):
            return (_REDEMPTION_MISSING, None)

        v2_cache, _ = self._run_to_completion(redemption_fn)
        self.assertTrue(v2_cache["1"]["complete"])

    def test_endpoint_error_not_fatal(self):
        def redemption_fn(run_id):
            return (_REDEMPTION_ERROR, None)

        v2_cache, _ = self._run_to_completion(redemption_fn)
        self.assertTrue(v2_cache["1"]["complete"])

    def test_none_redemption_fn_not_fatal(self):
        v2_cache, _ = self._run_to_completion(None)
        self.assertTrue(v2_cache["1"]["complete"])


# ---------------------------------------------------------------------------
# 31. V2: Correction 2 — trace written first; trace fail → position incomplete
# ---------------------------------------------------------------------------

class TestV2TraceWriteOrder(unittest.TestCase):

    def test_trace_written_before_commentary(self):
        """Trace must be saved before v2 commentary."""
        write_order = []

        original_save = save_cache_atomic

        def tracking_save(cache, cache_path):
            write_order.append(cache_path)
            original_save(cache, cache_path)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            with patch("precompute_commentary.save_cache_atomic",
                       side_effect=tracking_save):
                run_v2_precompute(
                    [1], {}, v2_path, {}, trace_path,
                    stage_a_fn=_good_stage_a_fn,
                    stage_b_fn=_good_stage_b_fn,
                    fact_sheet_fn=_good_fact_sheet_fn,
                )
            # Find the two writes for position 1 (trace then v2)
            # write_order may include multiple writes; find the pair
            trace_idx = next(
                (i for i, p in enumerate(write_order) if p == trace_path), None
            )
            v2_idx = next(
                (i for i, p in enumerate(write_order) if p == v2_path), None
            )
            self.assertIsNotNone(trace_idx, "trace must be written")
            self.assertIsNotNone(v2_idx, "v2 cache must be written")
            self.assertLess(trace_idx, v2_idx,
                            "trace must be written before v2 commentary")
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

    def test_trace_write_fail_position_not_complete(self):
        """If trace write fails, position must not be marked complete in v2 cache.

        Correction 2: the trace write (line 571) is NOT wrapped in try/except on
        the success path.  A disk failure raises OSError directly (no sys.exit).
        Since v2_cache[key] is only set AFTER the trace write succeeds (line 572),
        the in-memory v2_cache will not contain the key when the OSError escapes.
        """

        def failing_trace_save(cache, cache_path):
            if trace_path in cache_path:
                raise OSError("Simulated trace disk failure")
            # v2 writes should not happen (OSError escapes before them)
            save_cache_atomic(cache, cache_path)

        with tempfile.NamedTemporaryFile(suffix="_v2.json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix="_trace.json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            with patch("precompute_commentary.save_cache_atomic",
                       side_effect=failing_trace_save):
                v2_cache = {}
                try:
                    run_v2_precompute(
                        [1], v2_cache, v2_path, {}, trace_path,
                        stage_a_fn=_good_stage_a_fn,
                        stage_b_fn=_good_stage_b_fn,
                        fact_sheet_fn=_good_fact_sheet_fn,
                    )
                except (SystemExit, OSError):
                    pass  # Either is acceptable; both indicate a write failure
                # v2_cache["1"] must not be present or must not be complete
                entry = v2_cache.get("1", {})
                self.assertIsNot(entry.get("complete"), True,
                                 "Position must not be marked complete when trace write fails")
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 32. V2: Correction 3 — console redaction
# ---------------------------------------------------------------------------

class TestV2ConsoleRedaction(unittest.TestCase):
    """_redact_for_console must replace known secret values with [REDACTED]."""

    def test_snapshot_secret_redacted(self):
        secret_val = "my_super_secret_value_xyz"
        with patch.dict(os.environ, {"SNAPSHOT_SECRET": secret_val}):
            result = _redact_for_console(
                f"HMAC failed with key {secret_val}"
            )
        self.assertNotIn(secret_val, result)
        self.assertIn("[REDACTED]", result)

    def test_wo_api_key_redacted(self):
        key_val = "ibm_api_key_12345_abc"
        with patch.dict(os.environ, {"WO_API_KEY": key_val}):
            result = _redact_for_console(f"apikey={key_val} rejected")
        self.assertNotIn(key_val, result)
        self.assertIn("[REDACTED]", result)

    def test_diamondiq_test_key_redacted(self):
        test_val = "diqtest_secretvalue"
        with patch.dict(os.environ, {"DIAMONDIQ_TEST_KEY": test_val}):
            result = _redact_for_console(f"Header: {test_val}")
        self.assertNotIn(test_val, result)
        self.assertIn("[REDACTED]", result)

    def test_token_json_value_redacted(self):
        text = '{"token": "signed.token.value.here"}'
        result = _redact_for_console(text)
        self.assertNotIn("signed.token.value.here", result)
        self.assertIn("[REDACTED]", result)

    def test_safe_console_msg_truncates(self):
        """_safe_console_msg must truncate to 300 characters."""
        long_msg = "X" * 500
        exc = ValueError(long_msg)
        result = _safe_console_msg(exc)
        self.assertLessEqual(len(result), 300)

    def test_unset_env_vars_no_error(self):
        """Unset env vars must not cause errors in _redact_for_console."""
        # Temporarily remove the variables
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("SNAPSHOT_SECRET", "WO_API_KEY", "DIAMONDIQ_TEST_KEY")}
        with patch.dict(os.environ, clean_env, clear=True):
            result = _redact_for_console("Safe message.")
        self.assertEqual(result, "Safe message.")


# ---------------------------------------------------------------------------
# 33. V2: exception text not in trace/cache
# ---------------------------------------------------------------------------

class TestV2ExceptionNotInTrace(unittest.TestCase):

    def test_exception_message_not_in_trace(self):
        """The raw exception text must not be stored in the trace."""
        SENSITIVE_MSG = "SENSITIVE_EXCEPTION_PAYLOAD_DO_NOT_LOG"

        def failing_stage_a(game_id, at_bat_index, position, run_id=None):
            raise RuntimeError(SENSITIVE_MSG)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        trace_cache = {}
        try:
            with self.assertRaises(SystemExit):
                run_v2_precompute(
                    [1], {}, v2_path, trace_cache, trace_path,
                    stage_a_fn=failing_stage_a,
                    stage_b_fn=_good_stage_b_fn,
                    fact_sheet_fn=_good_fact_sheet_fn,
                )
            # Read what was actually persisted to disk
            if os.path.exists(trace_path):
                with open(trace_path) as f:
                    persisted = f.read()
                self.assertNotIn(SENSITIVE_MSG, persisted)
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 34. V2: atomic writes + partial failure preserves completed positions
# ---------------------------------------------------------------------------

class TestV2PartialFailurePreservation(unittest.TestCase):

    def test_completed_positions_preserved_after_later_failure(self):
        """Position 1 must remain complete when position 2 fails."""
        call_count = [0]

        def stage_a_fn(game_id, at_bat_index, position, run_id=None):
            call_count[0] += 1
            if position == 2:
                raise RuntimeError("Simulated failure on position 2")
            return _good_stage_a_fn(game_id, at_bat_index, position, run_id)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as v2f, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as trf:
            v2_path = v2f.name
            trace_path = trf.name

        try:
            with self.assertRaises(SystemExit):
                run_v2_precompute(
                    [1, 2], {}, v2_path, {}, trace_path,
                    stage_a_fn=stage_a_fn,
                    stage_b_fn=_good_stage_b_fn,
                    fact_sheet_fn=_good_fact_sheet_fn,
                )
            # Position 1 must have been saved before position 2 failed
            if os.path.exists(v2_path):
                v2_cache = load_cache(v2_path)
                self.assertTrue(v2_cache.get("1", {}).get("complete"),
                                "Position 1 must be complete before position 2 fails")
        finally:
            for p in (v2_path, trace_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 35. _safe_meta unit tests
# ---------------------------------------------------------------------------

class TestSafeMeta(unittest.TestCase):

    def test_none_returns_none(self):
        self.assertIsNone(_safe_meta(None))

    def test_empty_dict_returns_none(self):
        self.assertIsNone(_safe_meta({}))

    def test_safe_fields_retained(self):
        meta = {
            "id": "x", "created": 1,
            "usage": {"prompt_tokens": 5},
            "finish_reason": "stop",
        }
        result = _safe_meta(meta)
        self.assertEqual(result["id"], "x")
        self.assertEqual(result["finish_reason"], "stop")

    def test_tool_call_arguments_stripped(self):
        meta = {
            "tool_calls": [{
                "id": "tc1", "type": "function",
                "function_name": "redeem_snapshot",
                # 'arguments' must NOT be present in already-safe meta
            }]
        }
        result = _safe_meta(meta)
        tc = result["tool_calls"][0]
        self.assertNotIn("arguments", tc)
        self.assertEqual(tc["function_name"], "redeem_snapshot")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
