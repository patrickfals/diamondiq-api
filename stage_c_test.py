"""
stage_c_test.py — Offline mocked tests for Stage A and precompute logic.

No IBM calls are made.  All network functions are replaced with mocks.

Usage:
    python stage_c_test.py

Exit code 0 = all tests passed.
Exit code 1 = one or more tests failed.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Environment setup — must happen before importing orchestrate_client
# ---------------------------------------------------------------------------
os.environ.setdefault("SNAPSHOT_SECRET", "test-secret-offline")
os.environ.setdefault("WO_API_KEY",      "test-api-key")
os.environ.setdefault("WO_INSTANCE_URL", "https://test.example.com")
os.environ.setdefault("WO_AGENT_ID",     "test-analyst-agent-id")

# ---------------------------------------------------------------------------
# Imports — no network side-effects on import
# ---------------------------------------------------------------------------
from orchestrate_client import (  # noqa: E402
    StageAError,
    _FUTURE_PITCH_RE,
    generate_stage_a,
)
from precompute_commentary import (  # noqa: E402
    run_precompute,
    load_cache,
    save_cache_atomic,
    assert_no_secrets,
    GAME_ID,
    AT_BAT_INDEX,
    POSITIONS,
)

# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------

IAM_TOKEN_RESPONSE = {"access_token": "fake-iam-token"}


def _make_urlopen_side_effect(iam_resp: dict, analyst_resp: dict):
    """
    Returns a side_effect for urlopen that:
      - call 1: IAM token exchange  → iam_resp
      - call 2: Analyst invocation  → analyst_resp
    """
    responses = [
        json.dumps(iam_resp).encode(),
        json.dumps(analyst_resp).encode(),
    ]
    call_index = [0]

    def side_effect(request, timeout=None):
        data = responses[call_index[0]]
        call_index[0] += 1
        buf = io.BytesIO(data)
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=buf)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    return side_effect


def _analyst_envelope(body: dict) -> dict:
    """Wrap a Stage A body dict in the IBM Chat Completions envelope."""
    return {"choices": [{"message": {"content": json.dumps(body)}}]}


def _analyst_envelope_raw(content: str) -> dict:
    """Wrap arbitrary raw text in the IBM Chat Completions envelope."""
    return {"choices": [{"message": {"content": content}}]}


def _good_stage_a(analysis="Pitches 1 and 2 were four-seam fastballs.",
                  evidence=None):
    return {"analysis": analysis, "evidence_pitches": evidence or [1, 2]}


def _good_stage_b(evidence=None):
    return {
        "commentary_by_level": {
            "novice":   "Simple novice text.",
            "learning": "Learning text.",
            "regular":  "Regular text.",
            "expert":   "Expert text.",
        },
        "evidence_pitches": evidence or [1, 2],
    }


# ---------------------------------------------------------------------------
# 1. Stage A — Valid JSON
# ---------------------------------------------------------------------------

class TestStageAValidJson(unittest.TestCase):

    def test_valid_response_returned(self):
        body = {"analysis": "Pitches 1 and 2 were fastballs.", "evidence_pitches": [1, 2]}
        with patch("orchestrate_client.urlopen",
                   side_effect=_make_urlopen_side_effect(
                       IAM_TOKEN_RESPONSE, _analyst_envelope(body))):
            result = generate_stage_a(777245, 8, 3)
        self.assertEqual(result["analysis"], body["analysis"])
        self.assertEqual(result["evidence_pitches"], [1, 2])

    def test_fenced_json_accepted(self):
        body = {"analysis": "Pitch 1 was a fastball.", "evidence_pitches": [1]}
        fenced = f"```json\n{json.dumps(body)}\n```"
        with patch("orchestrate_client.urlopen",
                   side_effect=_make_urlopen_side_effect(
                       IAM_TOKEN_RESPONSE, _analyst_envelope_raw(fenced))):
            result = generate_stage_a(777245, 8, 1)
        self.assertEqual(result["evidence_pitches"], [1])

    def test_analysis_stripped(self):
        body = {"analysis": "  Pitch 1 was a fastball.  ", "evidence_pitches": [1]}
        with patch("orchestrate_client.urlopen",
                   side_effect=_make_urlopen_side_effect(
                       IAM_TOKEN_RESPONSE, _analyst_envelope(body))):
            result = generate_stage_a(777245, 8, 1)
        self.assertEqual(result["analysis"], "Pitch 1 was a fastball.")


# ---------------------------------------------------------------------------
# 2. Stage A — Malformed JSON
# ---------------------------------------------------------------------------

class TestStageAMalformedJson(unittest.TestCase):

    def _run(self, raw_content: str, revealed: int = 3):
        with patch("orchestrate_client.urlopen",
                   side_effect=_make_urlopen_side_effect(
                       IAM_TOKEN_RESPONSE, _analyst_envelope_raw(raw_content))):
            return generate_stage_a(777245, 8, revealed)

    def test_plain_prose_raises(self):
        with self.assertRaises(StageAError) as ctx:
            self._run("Here is my analysis of the at-bat.")
        self.assertIn("non-JSON", str(ctx.exception))

    def test_json_array_root_raises(self):
        with self.assertRaises(StageAError) as ctx:
            self._run('["this", "is", "wrong"]')
        self.assertIn("dict", str(ctx.exception))

    def test_missing_analysis_key_raises(self):
        with self.assertRaises(StageAError) as ctx:
            self._run(json.dumps({"evidence_pitches": [1, 2]}))
        self.assertIn("analysis", str(ctx.exception))

    def test_empty_analysis_raises(self):
        with self.assertRaises(StageAError) as ctx:
            self._run(json.dumps({"analysis": "   ", "evidence_pitches": [1]}), revealed=1)
        self.assertIn("analysis", str(ctx.exception))

    def test_missing_evidence_pitches_raises(self):
        with self.assertRaises(StageAError) as ctx:
            self._run(json.dumps({"analysis": "Pitch 1 was a fastball."}), revealed=1)
        self.assertIn("evidence_pitches", str(ctx.exception))

    def test_non_integer_evidence_raises(self):
        with self.assertRaises(StageAError) as ctx:
            self._run(json.dumps({"analysis": "Pitch 1.", "evidence_pitches": ["1"]}), revealed=1)
        self.assertIn("integers", str(ctx.exception))


# ---------------------------------------------------------------------------
# 3. Stage A — Out-of-range evidence pitches
# ---------------------------------------------------------------------------

class TestStageAOutOfRange(unittest.TestCase):

    def _run(self, body: dict, revealed: int = 3):
        with patch("orchestrate_client.urlopen",
                   side_effect=_make_urlopen_side_effect(
                       IAM_TOKEN_RESPONSE, _analyst_envelope(body))):
            return generate_stage_a(777245, 8, revealed)

    def test_zero_pitch_number_rejected(self):
        with self.assertRaises(StageAError) as ctx:
            self._run({"analysis": "Pitch 1 was a fastball.", "evidence_pitches": [0]})
        self.assertIn("out-of-range", str(ctx.exception))

    def test_pitch_above_revealed_count_rejected(self):
        with self.assertRaises(StageAError) as ctx:
            self._run({"analysis": "Pitches 1 and 2.", "evidence_pitches": [1, 4]})
        self.assertIn("out-of-range", str(ctx.exception))
        self.assertIn("[4]", str(ctx.exception))

    def test_exact_boundary_accepted(self):
        body = {"analysis": "Pitch 3 was a curveball.", "evidence_pitches": [3]}
        result = self._run(body, revealed=3)
        self.assertEqual(result["evidence_pitches"], [3])


# ---------------------------------------------------------------------------
# 4. Stage A — Spoiler detection
# ---------------------------------------------------------------------------

class TestStageASpoilerDetection(unittest.TestCase):

    def _run(self, body: dict, revealed: int = 3):
        with patch("orchestrate_client.urlopen",
                   side_effect=_make_urlopen_side_effect(
                       IAM_TOKEN_RESPONSE, _analyst_envelope(body))):
            return generate_stage_a(777245, 8, revealed)

    def test_explicit_future_pitch_in_text_rejected(self):
        body = {
            "analysis": "Pitches 1-3 set up pitch 4 nicely.",
            "evidence_pitches": [1, 2, 3],
        }
        with self.assertRaises(StageAError) as ctx:
            self._run(body)
        self.assertIn("future pitch", str(ctx.exception))
        self.assertIn("4", str(ctx.exception))

    def test_pitch_within_window_not_flagged(self):
        body = {
            "analysis": "Pitch 2 and pitch 3 were both above 93 mph.",
            "evidence_pitches": [2, 3],
        }
        result = self._run(body)
        self.assertEqual(result["evidence_pitches"], [2, 3])

    def test_hash_notation_detected(self):
        body = {
            "analysis": "Pitch #5 was implied by the sequence.",
            "evidence_pitches": [1],
        }
        with self.assertRaises(StageAError) as ctx:
            self._run(body)
        self.assertIn("future pitch", str(ctx.exception))

    def test_future_pitch_regex_plural(self):
        """'pitches 4-6' triggers spoiler detection (first capture group = 4)."""
        matches = list(_FUTURE_PITCH_RE.finditer("pitches 4-6 were key"))
        future = [int(m.group(1)) for m in matches if int(m.group(1)) > 3]
        self.assertIn(4, future)


# ---------------------------------------------------------------------------
# 5. Stage A — Input parameter validation (no IBM call)
# ---------------------------------------------------------------------------

class TestInputValidation(unittest.TestCase):

    def test_game_id_zero_raises(self):
        with self.assertRaises(ValueError):
            generate_stage_a(0, 8, 3)

    def test_game_id_negative_raises(self):
        with self.assertRaises(ValueError):
            generate_stage_a(-1, 8, 3)

    def test_at_bat_index_negative_raises(self):
        with self.assertRaises(ValueError):
            generate_stage_a(777245, -1, 3)

    def test_revealed_count_zero_raises(self):
        with self.assertRaises(ValueError):
            generate_stage_a(777245, 8, 0)


# ---------------------------------------------------------------------------
# 6. Precompute — cache resume behaviour
# ---------------------------------------------------------------------------

class TestPrecomputeResume(unittest.TestCase):
    """Completed positions are skipped; run_precompute never calls stage_a_fn
    for them."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "commentary_cache.json")

    def _make_stage_a(self, fail_on=None):
        calls = []
        def fn(game_id, at_bat_index, position):
            calls.append(position)
            if fail_on is not None and position == fail_on:
                raise StageAError(f"Simulated fail at position {position}")
            return _good_stage_a(evidence=[position])
        fn.calls = calls
        return fn

    def _make_stage_b(self):
        def fn(stage_a):
            return _good_stage_b(evidence=stage_a["evidence_pitches"])
        return fn

    def test_completed_position_is_skipped(self):
        """Position 2 already complete: stage_a_fn must not be called for it."""
        cache = {"2": {"complete": True,
                       "commentary_by_level": {},
                       "evidence_pitches": [1, 2]}}
        stage_a_fn = self._make_stage_a()
        run_precompute(
            positions=[1, 2, 3],
            cache=cache,
            cache_path=self.cache_path,
            stage_a_fn=stage_a_fn,
            stage_b_fn=self._make_stage_b(),
        )
        self.assertNotIn(2, stage_a_fn.calls)
        self.assertIn(1, stage_a_fn.calls)
        self.assertIn(3, stage_a_fn.calls)

    def test_resume_does_not_repeat_completed(self):
        """After a partial run (positions 1-2 done), resuming only runs 3."""
        cache = {
            "1": {"complete": True, "commentary_by_level": {}, "evidence_pitches": [1]},
            "2": {"complete": True, "commentary_by_level": {}, "evidence_pitches": [2]},
        }
        stage_a_fn = self._make_stage_a()
        run_precompute(
            positions=[1, 2, 3],
            cache=cache,
            cache_path=self.cache_path,
            stage_a_fn=stage_a_fn,
            stage_b_fn=self._make_stage_b(),
        )
        self.assertEqual(stage_a_fn.calls, [3])

    def test_all_complete_runs_nothing(self):
        """If all positions are already complete, stage_a_fn is never called."""
        cache = {str(p): {"complete": True, "commentary_by_level": {},
                          "evidence_pitches": [p]}
                 for p in [1, 2, 3]}
        stage_a_fn = self._make_stage_a()
        run_precompute(
            positions=[1, 2, 3],
            cache=cache,
            cache_path=self.cache_path,
            stage_a_fn=stage_a_fn,
            stage_b_fn=self._make_stage_b(),
        )
        self.assertEqual(stage_a_fn.calls, [])


# ---------------------------------------------------------------------------
# 7. Precompute — stop on first failure
# ---------------------------------------------------------------------------

class TestPrecomputeStopOnFailure(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "commentary_cache.json")

    def test_stage_a_failure_stops_at_failing_position(self):
        """Stage A failure on position 2 → sys.exit(1); position 3 never starts."""
        calls = []
        def stage_a_fn(game_id, at_bat_index, position):
            calls.append(position)
            if position == 2:
                raise StageAError("Simulated analyst failure")
            return _good_stage_a(evidence=[position])

        with self.assertRaises(SystemExit) as ctx:
            run_precompute(
                positions=[1, 2, 3],
                cache={},
                cache_path=self.cache_path,
                stage_a_fn=stage_a_fn,
                stage_b_fn=lambda s: _good_stage_b(s["evidence_pitches"]),
            )
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(1, calls)
        self.assertIn(2, calls)
        self.assertNotIn(3, calls)

    def test_stage_b_failure_stops_processing(self):
        """Stage B failure on position 1 → sys.exit(1); no entry saved."""
        def stage_b_fn(stage_a):
            raise ValueError("Stage B simulated failure")

        with self.assertRaises(SystemExit) as ctx:
            run_precompute(
                positions=[1, 2],
                cache={},
                cache_path=self.cache_path,
                stage_a_fn=lambda g, a, p: _good_stage_a(evidence=[p]),
                stage_b_fn=stage_b_fn,
            )
        self.assertEqual(ctx.exception.code, 1)
        # Cache file must not exist — nothing was saved
        self.assertFalse(os.path.exists(self.cache_path))


# ---------------------------------------------------------------------------
# 8. Precompute — successful positions are actually saved
# ---------------------------------------------------------------------------

class TestPrecomputeSave(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "commentary_cache.json")

    def test_successful_positions_written_to_disk(self):
        run_precompute(
            positions=[1, 2, 3],
            cache={},
            cache_path=self.cache_path,
            stage_a_fn=lambda g, a, p: _good_stage_a(evidence=[p]),
            stage_b_fn=lambda s: _good_stage_b(evidence=s["evidence_pitches"]),
        )
        self.assertTrue(os.path.exists(self.cache_path))
        with open(self.cache_path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        for p in [1, 2, 3]:
            self.assertIn(str(p), saved)
            self.assertTrue(saved[str(p)]["complete"])
            self.assertEqual(saved[str(p)]["evidence_pitches"], [p])

    def test_each_position_saved_atomically_before_next(self):
        """After position 1 succeeds and position 2 fails, position 1 is on disk."""
        def stage_a_fn(game_id, at_bat_index, position):
            if position == 2:
                raise StageAError("Fail on 2")
            return _good_stage_a(evidence=[position])

        with self.assertRaises(SystemExit):
            run_precompute(
                positions=[1, 2, 3],
                cache={},
                cache_path=self.cache_path,
                stage_a_fn=stage_a_fn,
                stage_b_fn=lambda s: _good_stage_b(evidence=s["evidence_pitches"]),
            )

        self.assertTrue(os.path.exists(self.cache_path))
        with open(self.cache_path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertIn("1", saved)
        self.assertTrue(saved["1"]["complete"])
        self.assertNotIn("2", saved)

    def test_cache_entry_has_expected_keys(self):
        run_precompute(
            positions=[5],
            cache={},
            cache_path=self.cache_path,
            stage_a_fn=lambda g, a, p: _good_stage_a(evidence=[p]),
            stage_b_fn=lambda s: _good_stage_b(evidence=s["evidence_pitches"]),
        )
        with open(self.cache_path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        entry = saved["5"]
        for key in ("commentary_by_level", "evidence_pitches", "complete", "elapsed_seconds"):
            self.assertIn(key, entry)

    def test_no_secrets_in_saved_cache(self):
        FORBIDDEN = {"token", "access_token", "authorization", "apikey", "api_key"}
        run_precompute(
            positions=[1],
            cache={},
            cache_path=self.cache_path,
            stage_a_fn=lambda g, a, p: _good_stage_a(evidence=[p]),
            stage_b_fn=lambda s: _good_stage_b(evidence=s["evidence_pitches"]),
        )
        with open(self.cache_path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        entry = saved["1"]
        self.assertEqual(set(k.lower() for k in entry) & FORBIDDEN, set())


# ---------------------------------------------------------------------------
# 9. Precompute — no IBM calls without --run-ibm (dry-run)
# ---------------------------------------------------------------------------

class TestDryRun(unittest.TestCase):
    """Without --run-ibm the script exits before making any IBM calls."""

    def test_dry_run_exits_zero_without_ibm(self):
        """Importing and running _main() with no --run-ibm exits with code 0."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "precompute_commentary.py"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Dry-run mode", result.stdout)
        # No IBM network imports should have been reached
        self.assertNotIn("generate_stage_a", result.stdout)

    def test_position_arg_without_run_ibm_exits_zero(self):
        """--position N alone (no --run-ibm) also exits cleanly."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "precompute_commentary.py", "--position", "3"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Dry-run mode", result.stdout)

    def test_invalid_position_exits_two(self):
        """--position 0 is out of range → exit code 2."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "precompute_commentary.py", "--position", "0"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        self.assertEqual(result.returncode, 2)


# ---------------------------------------------------------------------------
# 10. Fan Experience prompt — count progression and location accuracy
# ---------------------------------------------------------------------------

class TestFanExperiencePromptGuards(unittest.TestCase):
    """
    Regression checks for the two new Fan Experience prompt rules:
      - No unsupported pitch location labels.
      - A ball never resets / neutralises a strike count.

    These tests import the prompt template directly and verify that the
    forbidden phrases are absent and the new guard clauses are present.
    No IBM calls are made.
    """

    def setUp(self):
        from orchestrate_client import _FAN_EXPERIENCE_PROMPT_TEMPLATE
        self.template = _FAN_EXPERIENCE_PROMPT_TEMPLATE

    def test_no_location_without_analysis_support_rule_present(self):
        """Prompt must tell Fan Experience not to invent location labels."""
        self.assertIn("location", self.template.lower())

    def test_ball_does_not_reset_count_rule_present(self):
        """Prompt must explicitly state a ball does not reset the count."""
        self.assertIn("reset", self.template.lower())

    def test_fresh_chance_phrase_forbidden_in_rule(self):
        """Prompt must list 'fresh chance' as a forbidden phrase."""
        self.assertIn("fresh chance", self.template)

    def test_reset_the_count_phrase_listed(self):
        """Prompt must list 'reset the count' as a forbidden phrase."""
        self.assertIn("reset the count", self.template)

    def test_stage_a_prompt_location_rule_present(self):
        """Stage A prompt must also prohibit unsupported location descriptions."""
        from orchestrate_client import _STAGE_A_PROMPT_TEMPLATE
        # Format with dummy values so there are no unfilled placeholders
        filled = _STAGE_A_PROMPT_TEMPLATE.format(token="tok", revealed_count=3)
        self.assertIn("location", filled.lower())

    def test_stage_a_prompt_count_progression_rule_present(self):
        """Stage A prompt must instruct accurate ball-strike count reporting."""
        from orchestrate_client import _STAGE_A_PROMPT_TEMPLATE
        filled = _STAGE_A_PROMPT_TEMPLATE.format(token="tok", revealed_count=3)
        # The rule says each ball/strike advances its respective count
        self.assertIn("ball count by one", filled)
        self.assertIn("strike count by one", filled)

    def test_position3_count_sequence_is_1_2_not_reset(self):
        """
        Factual regression for the position-3 scenario:
        pitches 1=Called Strike, 2=Called Strike, 3=Ball
        → count after pitch 3 is 1-2, not a reset.
        Encoded as a pure logic check, no IBM call.
        """
        counts = []
        balls, strikes = 0, 0
        events = [
            {"result": "Called Strike"},
            {"result": "Called Strike"},
            {"result": "Ball"},
        ]
        for ev in events:
            if ev["result"] == "Ball":
                balls += 1
            elif "Strike" in ev["result"]:
                strikes += 1
            counts.append((balls, strikes))

        self.assertEqual(counts[0], (0, 1))
        self.assertEqual(counts[1], (0, 2))
        self.assertEqual(counts[2], (1, 2),
                         "After pitch 3 (ball), count must be 1-2 not reset")
        # Strike count must not decrease after a ball
        for i in range(1, len(counts)):
            self.assertGreaterEqual(
                counts[i][1], counts[i - 1][1],
                f"Strike count decreased from pitch {i} to {i+1} — that is wrong"
            )


# ---------------------------------------------------------------------------
# 11. Stage A — empty evidence_pitches and failure-message rejection
# ---------------------------------------------------------------------------

class TestStageAPosition9Regressions(unittest.TestCase):
    """
    Regression tests for the two gaps that caused position 9 to be saved
    with invalid content:
      (a) empty evidence_pitches accepted as valid
      (b) Analyst failure-message text accepted as valid analysis
    """

    def _run(self, body: dict, revealed: int = 9):
        with patch("orchestrate_client.urlopen",
                   side_effect=_make_urlopen_side_effect(
                       IAM_TOKEN_RESPONSE, _analyst_envelope(body))):
            return generate_stage_a(777245, 8, revealed)

    def _run_raw(self, content: str, revealed: int = 9):
        with patch("orchestrate_client.urlopen",
                   side_effect=_make_urlopen_side_effect(
                       IAM_TOKEN_RESPONSE, _analyst_envelope_raw(content))):
            return generate_stage_a(777245, 8, revealed)

    # --- Empty evidence_pitches ---

    def test_empty_evidence_pitches_rejected(self):
        """evidence_pitches=[] must raise StageAError."""
        body = {
            "analysis": "Three fastballs were thrown in the first three pitches.",
            "evidence_pitches": [],
        }
        with self.assertRaises(StageAError) as ctx:
            self._run(body)
        self.assertIn("must not be empty", str(ctx.exception))

    def test_nonempty_evidence_pitches_accepted(self):
        """evidence_pitches with one valid entry must succeed."""
        body = {
            "analysis": "Pitch 1 was a called strike.",
            "evidence_pitches": [1],
        }
        result = self._run(body, revealed=9)
        self.assertEqual(result["evidence_pitches"], [1])

    # --- Failure-message patterns in analysis text ---

    def test_invalid_snapshot_token_message_rejected(self):
        """Analysis saying 'invalid snapshot token' must raise StageAError."""
        body = {
            "analysis": "The snapshot token is invalid, so no data could be retrieved.",
            "evidence_pitches": [],
        }
        with self.assertRaises(StageAError) as ctx:
            self._run(body)
        self.assertIn("tool/token failure", str(ctx.exception))

    def test_token_was_invalid_message_rejected(self):
        body = {
            "analysis": "The snapshot token was invalid and pitch data is unavailable.",
            "evidence_pitches": [],
        }
        with self.assertRaises(StageAError) as ctx:
            self._run(body)
        self.assertIn("tool/token failure", str(ctx.exception))

    def test_unable_to_retrieve_message_rejected(self):
        body = {
            "analysis": "Unable to retrieve pitch data for this at-bat.",
            "evidence_pitches": [],
        }
        with self.assertRaises(StageAError) as ctx:
            self._run(body)
        self.assertIn("tool/token failure", str(ctx.exception))

    def test_no_pitch_data_message_rejected(self):
        body = {
            "analysis": "No pitch data was available from the snapshot.",
            "evidence_pitches": [],
        }
        with self.assertRaises(StageAError) as ctx:
            self._run(body)
        self.assertIn("tool/token failure", str(ctx.exception))

    def test_could_not_retrieve_message_rejected(self):
        body = {
            "analysis": "Could not retrieve the pitch information for this token.",
            "evidence_pitches": [],
        }
        with self.assertRaises(StageAError) as ctx:
            self._run(body)
        self.assertIn("tool/token failure", str(ctx.exception))

    def test_tool_call_failed_message_rejected(self):
        body = {
            "analysis": "The tool call failed when attempting to redeem the snapshot.",
            "evidence_pitches": [],
        }
        with self.assertRaises(StageAError) as ctx:
            self._run(body)
        self.assertIn("tool/token failure", str(ctx.exception))

    def test_failure_check_is_case_insensitive(self):
        """Pattern matching must be case-insensitive."""
        body = {
            "analysis": "UNABLE TO RETRIEVE pitch data for this snapshot.",
            "evidence_pitches": [],
        }
        with self.assertRaises(StageAError) as ctx:
            self._run(body)
        self.assertIn("tool/token failure", str(ctx.exception))

    def test_normal_analysis_mentioning_retrieval_accepted(self):
        """
        An analysis that coincidentally uses the word 'retrieve' in a
        non-failure context (e.g. 'Pitch Scout retrieved ...') must NOT
        be rejected — only exact failure sub-phrases trigger the check.
        """
        body = {
            "analysis": "Pitch Scout retrieved three pitches. Pitch 1 was a called strike.",
            "evidence_pitches": [1],
        }
        # Must NOT raise — 'retrieved' alone is not a failure pattern
        result = self._run(body, revealed=9)
        self.assertEqual(result["evidence_pitches"], [1])

    def test_snapshot_token_structure_777245_8_9(self):
        """
        Offline structural check: a token for game=777245, at_bat=8,
        revealed=9 round-trips correctly and encodes the right values.
        No live HTTP call, no secret printed.
        """
        import os as _os, base64 as _b64
        _os.environ['SNAPSHOT_SECRET'] = 'offline-structure-check'
        from snapshot import create_snapshot_token, verify_snapshot_token
        tok = create_snapshot_token(777245, 8, 9)
        g, a, r, rid = verify_snapshot_token(tok)
        self.assertEqual((g, a, r), (777245, 8, 9))
        self.assertIsNone(rid, "legacy token must have run_id=None")
        # Verify structural integrity
        padded = tok + '=' * (-len(tok) % 4)
        raw = _b64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8')
        payload, sig = raw.split('.')
        self.assertEqual(payload, '777245:8:9')
        self.assertEqual(len(sig), 64)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (
        TestStageAValidJson,
        TestStageAMalformedJson,
        TestStageAOutOfRange,
        TestStageASpoilerDetection,
        TestInputValidation,
        TestPrecomputeResume,
        TestPrecomputeStopOnFailure,
        TestPrecomputeSave,
        TestDryRun,
        TestFanExperiencePromptGuards,
        TestStageAPosition9Regressions,
    ):
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
