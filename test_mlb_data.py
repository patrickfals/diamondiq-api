"""
test_mlb_data.py -- Focused offline regression tests for mlb_data.py.

Tests cover:
  1. Guard 1: revealed_count type/sign validation (negative, float, bool, None, str).
  2. Guard 2: out-of-range error message does not reveal total_pitches.
  3. Guard 3: zero-pitch at-bat at position 0 is not complete; outcome is None.
  4. Schema compatibility: build_authorized_snapshot() return keys match
     the existing /snapshot/redeem response schema exactly.
  5. Spoiler regression: positions 0, 3, 9 on a synthetic 10-pitch at-bat --
     no forbidden strings, no future pitch numbers, no outcome until complete.
  6. Stats extraction: verified entering-game values hits=79 (AVG 0.244),
     earned_runs=31 (ERA 4.70) using correct fixture arithmetic.
  7. build_fact_sheet() spoiler regression: positions 0, 3, 9 contain no
     future pitches, unrevealed outcomes, total pitch count, or postgame stats.
     (Section 7 is skipped with a clear message if build_fact_sheet is not yet
     implemented; it activates automatically once the function exists.)

No network calls are made.  All tests use minimal synthetic fixtures.
"""

import json
import sys
import unittest

sys.path.insert(0, ".")
from mlb_data import (
    DataError,
    build_authorized_snapshot,
    extract_batter_stats,
    extract_pitcher_stats,
)


# ---------------------------------------------------------------------------
# Synthetic game-data fixture builder
# ---------------------------------------------------------------------------
# 10 pitches: pitches 1-2 are Called Strike, pitches 3-10 are Ball.
# Final outcome = "Single".
# game_id=0 is not in VERIFIED_STATS_AT_BATS, so batter/pitcher stats are None
# for the standard fixture.  A separate stats-enabled fixture is built for
# section 6 and 7 by using game_id=777245 / at_bat_index=8.

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

    Fixture arithmetic (verified offline):
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

    batter_id  = 100
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

    return {
        "liveData": {
            "plays": {
                "allPlays": [
                    {
                        "about": {
                            "atBatIndex": 8 if with_verified_stats else 0,
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


# Standard 10-pitch fixture (game_id=0, no verified stats)
GAME_DATA_10 = _make_game_data(10)
# Zero-pitch at-bat fixture
GAME_DATA_0  = _make_game_data(0)
# Verified-stats fixture (game_id=777245, at_bat_index=8)
GAME_DATA_VERIFIED = _make_game_data(10, with_verified_stats=True)


def _snap(revealed_count, game_data=None, game_id=0, at_bat_index=0):
    if game_data is None:
        game_data = GAME_DATA_10
    return build_authorized_snapshot(
        game_data, game_id=game_id,
        at_bat_index=at_bat_index, revealed_count=revealed_count,
    )


FORBIDDEN_STRINGS = ["Single", "In play"]


# ---------------------------------------------------------------------------
# 1. Guard 1: revealed_count type and sign validation
# ---------------------------------------------------------------------------

class TestRevealedCountValidation(unittest.TestCase):

    def test_negative_int_raises(self):
        with self.assertRaises(DataError) as ctx:
            _snap(-1)
        self.assertIn(">=", str(ctx.exception))

    def test_large_negative_raises(self):
        with self.assertRaises(DataError):
            _snap(-9999)

    def test_float_raises(self):
        with self.assertRaises(DataError) as ctx:
            _snap(3.0)
        self.assertIn("non-negative integer", str(ctx.exception))

    def test_bool_raises(self):
        # bool is a subclass of int in Python; must still be rejected
        with self.assertRaises(DataError) as ctx:
            _snap(True)
        self.assertIn("non-negative integer", str(ctx.exception))

    def test_none_raises(self):
        with self.assertRaises(DataError):
            _snap(None)

    def test_string_raises(self):
        with self.assertRaises(DataError):
            _snap("3")

    def test_zero_is_valid(self):
        result = _snap(0)
        self.assertEqual(result["pitches"], [])
        self.assertEqual(result["revealed_count"], 0)

    def test_positive_int_is_valid(self):
        result = _snap(3)
        self.assertEqual(len(result["pitches"]), 3)


# ---------------------------------------------------------------------------
# 2. Guard 2: out-of-range error message must not reveal total_pitches
# ---------------------------------------------------------------------------

class TestOutOfRangeErrorMessage(unittest.TestCase):

    def test_beyond_total_raises(self):
        with self.assertRaises(DataError):
            _snap(11)

    def test_message_does_not_contain_total_count(self):
        try:
            _snap(11)
            self.fail("Expected DataError")
        except DataError as exc:
            self.assertNotIn("10", str(exc),
                "Error message must not reveal total pitch count")

    def test_message_does_not_contain_word_total(self):
        try:
            _snap(11)
            self.fail("Expected DataError")
        except DataError as exc:
            self.assertNotIn("total", str(exc).lower())


# ---------------------------------------------------------------------------
# 3. Guard 3: zero-pitch at-bat is never complete at position 0
# ---------------------------------------------------------------------------

class TestZeroPitchAtBat(unittest.TestCase):

    def test_zero_pitch_atbat_pos0_not_complete(self):
        result = build_authorized_snapshot(
            GAME_DATA_0, game_id=0, at_bat_index=0, revealed_count=0
        )
        self.assertFalse(result["is_complete"])

    def test_zero_pitch_atbat_pos0_outcome_none(self):
        result = build_authorized_snapshot(
            GAME_DATA_0, game_id=0, at_bat_index=0, revealed_count=0
        )
        self.assertIsNone(result["outcome"])

    def test_zero_pitch_atbat_pos0_no_total_pitches_key(self):
        result = build_authorized_snapshot(
            GAME_DATA_0, game_id=0, at_bat_index=0, revealed_count=0
        )
        self.assertNotIn("total_pitches", result)

    def test_normal_atbat_pos0_not_complete(self):
        result = _snap(0)
        self.assertFalse(result["is_complete"])

    def test_normal_atbat_pos0_outcome_none(self):
        result = _snap(0)
        self.assertIsNone(result["outcome"])


# ---------------------------------------------------------------------------
# 4. Schema compatibility with existing /snapshot/redeem response
# ---------------------------------------------------------------------------

class TestSchemaCompatibility(unittest.TestCase):

    PARTIAL_KEYS = {
        "game_id", "at_bat_index", "revealed_count", "is_complete",
        "batter", "pitcher", "inning", "half_inning",
        "outcome", "pitches", "batter_stats", "pitcher_stats",
    }
    COMPLETE_ONLY_KEYS = {"total_pitches"}

    EXPECTED_PITCH_KEYS = {
        "number", "type", "result", "velocity_mph",
        "plate_x", "plate_z", "count_after_pitch",
    }

    def test_partial_has_exactly_partial_keys(self):
        result = _snap(3)
        self.assertFalse(result["is_complete"])
        self.assertEqual(set(result.keys()), self.PARTIAL_KEYS)

    def test_complete_adds_total_pitches(self):
        result = _snap(10)
        self.assertTrue(result["is_complete"])
        self.assertEqual(set(result.keys()),
                         self.PARTIAL_KEYS | self.COMPLETE_ONLY_KEYS)

    def test_pos0_has_exactly_partial_keys(self):
        result = _snap(0)
        self.assertEqual(set(result.keys()), self.PARTIAL_KEYS)

    def test_pitch_dict_fields(self):
        result = _snap(3)
        for pitch in result["pitches"]:
            self.assertEqual(set(pitch.keys()), self.EXPECTED_PITCH_KEYS)


# ---------------------------------------------------------------------------
# 5. Spoiler regression: positions 0, 3, 9
# ---------------------------------------------------------------------------

class TestSpoilerRegression(unittest.TestCase):

    def _assert_no_spoilers(self, revealed_count):
        result = _snap(revealed_count)
        body_text = json.dumps(result)

        for fs in FORBIDDEN_STRINGS:
            self.assertNotIn(fs, body_text,
                f"pos {revealed_count}: forbidden string {fs!r} in body")

        self.assertIsNone(result["outcome"],
            f"pos {revealed_count}: outcome must be None")

        for pitch in result["pitches"]:
            self.assertLessEqual(pitch["number"], revealed_count,
                f"pos {revealed_count}: future pitch {pitch['number']} exposed")

        self.assertEqual(len(result["pitches"]), revealed_count)
        self.assertNotIn("total_pitches", result)
        self.assertFalse(result["is_complete"])

    def test_pos0(self):
        self._assert_no_spoilers(0)

    def test_pos3(self):
        self._assert_no_spoilers(3)

    def test_pos9(self):
        self._assert_no_spoilers(9)

    def test_pos10_complete_reveals_outcome(self):
        result = _snap(10)
        self.assertTrue(result["is_complete"])
        self.assertEqual(result["outcome"], "Single")
        self.assertEqual(result["total_pitches"], 10)

    def test_pos3_no_pitch_beyond_3(self):
        result = _snap(3)
        future = [p["number"] for p in result["pitches"] if p["number"] > 3]
        self.assertEqual(future, [])

    def test_pos9_no_pitch_beyond_9(self):
        result = _snap(9)
        future = [p["number"] for p in result["pitches"] if p["number"] > 9]
        self.assertEqual(future, [])


# ---------------------------------------------------------------------------
# 6. Stats extraction: verified entering-game values
# ---------------------------------------------------------------------------
# Fixture: season_hits=80, game_hits=1  -> entering_hits=79, entering_ab=324
#          season_ab=328,  game_ab=4    -> AVG = round(79/324, 3) = 0.244
#          season_er=32,   game_er=1    -> entering_er=31
#          season_outs=188, game_outs=10-> entering_outs=178
#          ERA = round(27*31/178, 2) = 4.7
# These values match the verified live production values confirmed by stage1_test.py.

class TestStatsExtraction(unittest.TestCase):

    def _make_teams(self, season_hits, season_ab, game_hits, game_ab,
                    season_er, season_outs, game_er, game_outs):
        return {
            "home": {
                "players": {
                    "ID10": {
                        "seasonStats": {
                            "batting":  {"hits": season_hits, "atBats": season_ab},
                            "pitching": {"earnedRuns": season_er, "outs": season_outs},
                        },
                        "stats": {
                            "batting":  {"hits": game_hits, "atBats": game_ab},
                            "pitching": {"earnedRuns": game_er, "outs": game_outs},
                        },
                    }
                }
            }
        }

    def test_batter_hits_79_avg_0244(self):
        """season_hits=80, game_hits=1, season_ab=328, game_ab=4 -> hits=79, AVG=0.244"""
        teams = self._make_teams(80, 328, 1, 4, 32, 188, 1, 10)
        result = extract_batter_stats(teams, 10)
        self.assertIsNotNone(result)
        self.assertEqual(result["hits"], 79)
        self.assertEqual(result["at_bats"], 324)
        self.assertEqual(result["avg"], 0.244)
        self.assertEqual(result["basis"], "entering_game")

    def test_pitcher_er_31_era_470(self):
        """season_er=32, game_er=1, season_outs=188, game_outs=10 -> er=31, ERA=4.7"""
        teams = self._make_teams(80, 328, 1, 4, 32, 188, 1, 10)
        result = extract_pitcher_stats(teams, 10)
        self.assertIsNotNone(result)
        self.assertEqual(result["earned_runs"], 31)
        self.assertEqual(result["outs_recorded"], 178)
        self.assertEqual(result["era"], 4.7)
        self.assertEqual(result["basis"], "entering_game")

    def test_negative_intermediate_returns_none(self):
        """game_hits > season_hits -> impossible -> None"""
        teams = self._make_teams(5, 50, 10, 4, 1, 30, 0, 3)
        self.assertIsNone(extract_batter_stats(teams, 10))

    def test_zero_denominator_returns_none(self):
        """entering_ab=0 -> None"""
        teams = self._make_teams(0, 0, 0, 0, 1, 10, 0, 3)
        self.assertIsNone(extract_batter_stats(teams, 10))

    def test_hits_exceeds_ab_returns_none(self):
        """entering_hits > entering_ab -> impossible -> None"""
        teams = self._make_teams(20, 10, 0, 0, 1, 10, 0, 3)
        self.assertIsNone(extract_batter_stats(teams, 10))

    def test_verified_snapshot_batter_stats(self):
        """
        build_authorized_snapshot with game_id=777245 / at_bat_index=8 and
        the verified fixture produces batter_stats.hits=79, avg=0.244.
        """
        result = build_authorized_snapshot(
            GAME_DATA_VERIFIED,
            game_id=777245,
            at_bat_index=8,
            revealed_count=3,
        )
        bs = result.get("batter_stats")
        self.assertIsNotNone(bs, "batter_stats must be present for verified at-bat")
        self.assertEqual(bs["hits"], 79)
        self.assertEqual(bs["avg"], 0.244)
        self.assertEqual(bs["basis"], "entering_game")

    def test_verified_snapshot_pitcher_stats(self):
        """
        build_authorized_snapshot with game_id=777245 / at_bat_index=8 and
        the verified fixture produces pitcher_stats.earned_runs=31, era=4.7.
        """
        result = build_authorized_snapshot(
            GAME_DATA_VERIFIED,
            game_id=777245,
            at_bat_index=8,
            revealed_count=3,
        )
        ps = result.get("pitcher_stats")
        self.assertIsNotNone(ps, "pitcher_stats must be present for verified at-bat")
        self.assertEqual(ps["earned_runs"], 31)
        self.assertEqual(ps["era"], 4.7)
        self.assertEqual(ps["basis"], "entering_game")


# ---------------------------------------------------------------------------
# 7. build_fact_sheet() spoiler regression
# ---------------------------------------------------------------------------
# These tests activate automatically once build_fact_sheet is importable from
# orchestrate_client.  Until then they are skipped with an explanatory message.

try:
    from orchestrate_client import build_fact_sheet
    _FACT_SHEET_AVAILABLE = True
except ImportError:
    _FACT_SHEET_AVAILABLE = False


@unittest.skipUnless(_FACT_SHEET_AVAILABLE, "build_fact_sheet not yet implemented")
class TestFactSheetSpoilerRegression(unittest.TestCase):
    """
    Verify that build_fact_sheet() produces no spoilers at positions 0, 3, 9.

    Checks:
    - No final outcome string (e.g. "Single") at partial positions.
    - No "In play" result string at partial positions.
    - No pitch numbers greater than revealed_count.
    - No total pitch count or "of N" phrasing.
    - No postgame statistics (hits=80, season totals).
    - Entering-game stats use verified values: hits=79, earned_runs=31.
    - Position 0 (pregame) contains batter/pitcher names but no pitch data.
    """

    def _fact_sheet(self, revealed_count):
        return build_fact_sheet(
            GAME_DATA_VERIFIED,
            game_id=777245,
            at_bat_index=8,
            revealed_count=revealed_count,
        )

    def _assert_no_spoilers(self, text, revealed_count):
        self.assertNotIn("Single", text,
            f"pos {revealed_count}: 'Single' (final outcome) must not appear")
        self.assertNotIn("In play", text,
            f"pos {revealed_count}: 'In play' must not appear")
        # No total pitch count revealed ("of 10", "of N")
        self.assertNotRegex(text, r"\bof\s+\d+\b",
            f"pos {revealed_count}: total pitch count must not appear")
        # No postgame season stat (season_hits=80 must not appear; entering=79 may)
        self.assertNotIn("80", text,
            f"pos {revealed_count}: postgame hits total 80 must not appear")

    def _assert_no_future_pitches(self, text, revealed_count):
        import re
        for m in re.finditer(r"\bpitch\s+(\d+)", text, re.IGNORECASE):
            num = int(m.group(1))
            self.assertLessEqual(num, revealed_count,
                f"pos {revealed_count}: future pitch {num} referenced in fact sheet")

    def test_pos0_no_spoilers(self):
        text = self._fact_sheet(0)
        self.assertNotIn("Single", text)
        self.assertNotIn("In play", text)
        # Position 0 must have no pitch data
        self.assertNotRegex(text, r"Pitch\s+1\b")

    def test_pos0_has_batter_and_pitcher_names(self):
        text = self._fact_sheet(0)
        self.assertIn("Test Batter", text)
        self.assertIn("Test Pitcher", text)

    def test_pos0_has_entering_stats(self):
        text = self._fact_sheet(0)
        # AVG 0.244 and ERA 4.7 are non-spoiler entering-game values
        self.assertIn("0.244", text)
        self.assertIn("4.7", text)

    def test_pos3_no_spoilers(self):
        text = self._fact_sheet(3)
        self._assert_no_spoilers(text, 3)

    def test_pos3_no_future_pitches(self):
        text = self._fact_sheet(3)
        self._assert_no_future_pitches(text, 3)

    def test_pos3_has_three_pitches(self):
        text = self._fact_sheet(3)
        # Should mention pitches 1, 2, 3 but not 4+
        self.assertRegex(text, r"Pitch\s+1\b")
        self.assertRegex(text, r"Pitch\s+2\b")
        self.assertRegex(text, r"Pitch\s+3\b")
        self.assertNotRegex(text, r"Pitch\s+4\b")

    def test_pos3_uses_entering_stats(self):
        text = self._fact_sheet(3)
        self.assertIn("79", text)   # entering_hits=79
        self.assertIn("31", text)   # entering_er=31
        self.assertNotIn("80", text)  # season_hits=80 must not appear

    def test_pos9_no_spoilers(self):
        text = self._fact_sheet(9)
        self._assert_no_spoilers(text, 9)

    def test_pos9_no_future_pitches(self):
        text = self._fact_sheet(9)
        self._assert_no_future_pitches(text, 9)

    def test_pos9_no_pitch_10(self):
        text = self._fact_sheet(9)
        self.assertNotRegex(text, r"Pitch\s+10\b")

    def test_format_says_pitches_revealed_so_far(self):
        """Fact sheet must say 'Pitches revealed so far: N', not 'of N'."""
        text = self._fact_sheet(3)
        self.assertIn("revealed so far", text.lower())
        self.assertNotRegex(text, r"of\s+\d+")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestRevealedCountValidation,
        TestOutOfRangeErrorMessage,
        TestZeroPitchAtBat,
        TestSchemaCompatibility,
        TestSpoilerRegression,
        TestStatsExtraction,
        TestFactSheetSpoilerRegression,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
