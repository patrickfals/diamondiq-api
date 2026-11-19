import json
import os
import re as _re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from snapshot import create_snapshot_token


def analyze_at_bat(game_id: int, at_bat_index: int, knowledge_level: str) -> str:
    # Read credentials from backend environment variables
    api_key = os.environ["WO_API_KEY"]
    instance_url = os.environ["WO_INSTANCE_URL"].rstrip("/")
    agent_id = os.environ["WO_AGENT_ID"]

    # Step 1: Exchange IBM API key for an IAM access token
    token_data = urlencode({
        "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
        "apikey": api_key
    }).encode("utf-8")

    token_request = Request(
        "https://iam.cloud.ibm.com/identity/token",
        data=token_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )

    with urlopen(token_request, timeout=20) as response:
        access_token = json.load(response)["access_token"]

    # Step 2: Ask the deployed Analyst to analyze an MLB at-bat
    prompt = (
        f"Analyze MLB game ID {game_id}, at-bat index {at_bat_index}. "
        f"The fan's baseball knowledge level is: {knowledge_level}. "
        "Use DiamondIQ Pitch Scout to retrieve and analyze the real MLB "
        "pitch data, then DiamondIQ Fan Experience to personalize the story. "
        "Write 2-3 engaging sentences. Do not invent statistics, "
        "contact quality, or player intentions."
    )

    payload = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "stream": False
    }).encode("utf-8")

    agent_request = Request(
        f"{instance_url}/v1/orchestrate/{agent_id}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    with urlopen(agent_request, timeout=120) as response:
        result = json.load(response)

    # Extract the Analyst's written response
    return result["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Stage A: Analyst invocation (snapshot-scoped, spoiler-safe)
# ---------------------------------------------------------------------------

# Regex that flags explicit forward references to pitch numbers not yet
# revealed (e.g. "pitch 4", "pitch 5", "pitches 4-6", "pitch #7").
# Checked after extraction; always validated against revealed_count.
_FUTURE_PITCH_RE = _re.compile(
    r"\bpitch(?:es)?\s*#?\s*(\d+)",
    _re.IGNORECASE,
)

# Double-brace {{ }} produces literal { } after .format() substitution.
# {token} and {revealed_count} are the only interpolation points.
_STAGE_A_PROMPT_TEMPLATE = """\
You are DiamondIQ Analyst. Analyze the baseball at-bat snapshot described \
by the signed token below.

Snapshot token: {token}

Delegate to your collaborator agent DiamondIQ Pitch Scout to retrieve the \
authorized pitch data. Pitch Scout must call the redeem_snapshot tool \
exactly once using the snapshot token provided above. Do NOT call \
redeem_snapshot yourself and do NOT delegate to DiamondIQ Fan Experience.

Your analysis must be grounded exclusively in the pitch data returned by \
the snapshot. Do not reference pitches or events that fall outside the \
revealed window (pitches 1\u2013{revealed_count}). Do not claim pitcher intent, \
meaningful velocity trends, or statistical patterns without direct supporting \
evidence in the snapshot. Do not describe a strike as "called" or "swinging" \
unless that classification is explicitly present in the pitch data. Do not \
describe pitch location (e.g. "low", "inside", "high") unless the pitch data \
explicitly includes that description; plate coordinates alone do not constitute \
a location label. Report ball-strike counts accurately: each ball advances \
the ball count by one and each strike advances the strike count by one — a \
ball does not reset or neutralise a strike count already on the batter.

Respond with valid JSON only \u2014 no prose before or after, no Markdown fences. \
Use exactly this structure:

{{
  "analysis": "...",
  "evidence_pitches": [1, 2]
}}

Requirements:
- "analysis": a concise factual description of the pitch sequence (non-empty \
string). Must not reference any pitch number greater than {revealed_count}.
- "evidence_pitches": list of integer pitch numbers (from 1 to \
{revealed_count}) that directly support your analysis.
"""


class StageAError(ValueError):
    """Raised when the Analyst returns an invalid or incomplete Stage A response.

    Attributes:
        raw_response: the unmodified string returned by the IBM agent.
                      Never include this in log output or HTTP responses.
    """
    def __init__(self, message: str, raw_response: str = "") -> None:
        super().__init__(message)
        self.raw_response = raw_response


def generate_stage_a(
    game_id: int,
    at_bat_index: int,
    revealed_count: int,
) -> dict:
    """
    Invoke DiamondIQ Analyst for the given snapshot position and return a
    validated Stage A dict compatible with generate_fan_commentary().

    Parameters
    ----------
    game_id        : MLB game identifier (must be > 0)
    at_bat_index   : zero-based at-bat index (must be >= 0)
    revealed_count : number of pitches to authorize (must be >= 1)

    Returns
    -------
    dict with keys:
        - "analysis"         (str)       : verified analysis text
        - "evidence_pitches" (list[int]) : pitch numbers from the authorized
                                           snapshot; range 1-revealed_count.

    Raises
    ------
    ValueError   : if any parameter is out of range, or SNAPSHOT_SECRET /
                   WO_* environment variables are unset.
    StageAError  : if the IBM response is missing, malformed, out-of-range,
                   or contains detected future-pitch spoilers.
    """
    if game_id <= 0:
        raise ValueError(f"game_id must be > 0, got {game_id}")
    if at_bat_index < 0:
        raise ValueError(f"at_bat_index must be >= 0, got {at_bat_index}")
    if revealed_count < 1:
        raise ValueError(f"revealed_count must be >= 1, got {revealed_count}")

    # --- Create signed snapshot token (raises ValueError if secret missing) ---
    token = create_snapshot_token(game_id, at_bat_index, revealed_count)

    api_key = os.environ["WO_API_KEY"]
    instance_url = os.environ["WO_INSTANCE_URL"].rstrip("/")
    agent_id = os.environ["WO_AGENT_ID"]

    # Step 1: Exchange IBM API key for an IAM access token
    token_data = urlencode({
        "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
        "apikey": api_key
    }).encode("utf-8")

    token_request = Request(
        "https://iam.cloud.ibm.com/identity/token",
        data=token_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )

    with urlopen(token_request, timeout=20) as response:
        access_token = json.load(response)["access_token"]

    # Step 2: Invoke DiamondIQ Analyst with the snapshot token
    prompt = _STAGE_A_PROMPT_TEMPLATE.format(
        token=token,
        revealed_count=revealed_count,
    )

    payload = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "stream": False
    }).encode("utf-8")

    agent_request = Request(
        f"{instance_url}/v1/orchestrate/{agent_id}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    with urlopen(agent_request, timeout=120) as response:
        result = json.load(response)

    raw_content = result["choices"][0]["message"]["content"]

    # --- Strip optional Markdown fences ---
    cleaned = _strip_fences(raw_content)

    # --- Parse JSON ---
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise StageAError(
            f"Analyst returned non-JSON content: {exc}",
            raw_response=raw_content,
        ) from exc

    if not isinstance(parsed, dict):
        raise StageAError(
            f"Analyst JSON root must be a dict, got {type(parsed).__name__}",
            raw_response=raw_content,
        )

    # --- Validate "analysis" field ---
    analysis = parsed.get("analysis")
    if not isinstance(analysis, str) or not analysis.strip():
        raise StageAError(
            "Analyst response missing non-empty 'analysis' string. "
            f"Got keys: {list(parsed.keys())}",
            raw_response=raw_content,
        )

    # --- Reject analysis text that reports a tool/token failure ---
    # The Analyst sometimes returns a valid-JSON response whose "analysis"
    # field describes a snapshot-token error or unavailable pitch data
    # rather than actual pitch analysis.  Catch the most common patterns.
    _FAILURE_PATTERNS = (
        "invalid snapshot token",
        "snapshot token is invalid",
        "token is invalid",
        "token was invalid",
        "failed to retrieve",
        "unable to retrieve",
        "couldn't retrieve",
        "could not retrieve",
        "no pitch data",
        "pitch data is not available",
        "pitch data was not available",
        "pitch information is not available",
        "pitch information was not available",
        "tool call failed",
        "tool returned an error",
        "redeem_snapshot failed",
        "snapshot could not be redeemed",
    )
    analysis_lower = analysis.strip().lower()
    for pattern in _FAILURE_PATTERNS:
        if pattern in analysis_lower:
            raise StageAError(
                f"Analyst analysis reports a tool/token failure "
                f"(matched: {pattern!r}). Cannot use as Stage A output.",
                raw_response=raw_content,
            )

    # --- Validate "evidence_pitches" field ---
    evidence_pitches = parsed.get("evidence_pitches")
    if not isinstance(evidence_pitches, list):
        raise StageAError(
            "Analyst response missing 'evidence_pitches' list. "
            f"Got keys: {list(parsed.keys())}",
            raw_response=raw_content,
        )

    # Must be non-empty — an empty list means no pitch was actually analysed
    if len(evidence_pitches) == 0:
        raise StageAError(
            "'evidence_pitches' must not be empty; "
            "the Analyst must cite at least one pitch from the snapshot.",
            raw_response=raw_content,
        )

    # All values must be integers
    if not all(isinstance(p, int) for p in evidence_pitches):
        raise StageAError(
            f"'evidence_pitches' must be a list of integers, "
            f"got: {evidence_pitches!r}",
            raw_response=raw_content,
        )

    # All values must be in [1, revealed_count]
    out_of_range = [p for p in evidence_pitches if p < 1 or p > revealed_count]
    if out_of_range:
        raise StageAError(
            f"'evidence_pitches' contains out-of-range values "
            f"(must be 1-{revealed_count}): {out_of_range}",
            raw_response=raw_content,
        )

    # --- Spoiler check: explicit future-pitch references in analysis text ---
    # Scan for "pitch N" / "pitches N" patterns; reject any N > revealed_count.
    spoiler_refs = [
        int(m.group(1))
        for m in _FUTURE_PITCH_RE.finditer(analysis)
        if int(m.group(1)) > revealed_count
    ]
    if spoiler_refs:
        raise StageAError(
            f"Analyst analysis references future pitch number(s) "
            f"beyond revealed_count={revealed_count}: {spoiler_refs}",
            raw_response=raw_content,
        )

    return {
        "analysis": analysis.strip(),
        "evidence_pitches": evidence_pitches,
    }


# ---------------------------------------------------------------------------
# Stage B: Fan Experience direct invocation
# ---------------------------------------------------------------------------

_FAN_EXPERIENCE_LEVELS = ("novice", "learning", "regular", "expert")

# Double-brace {{ }} produces literal { } after .format() substitution.
# {stage_a_analysis} is the only interpolation point.
_FAN_EXPERIENCE_PROMPT_TEMPLATE = """\
You are DiamondIQ Fan Experience. A verified baseball analysis has been \
provided below. Using ONLY the facts stated in that analysis, write four \
short baseball broadcast commentaries \u2014 one per knowledge level.
Do not invent pitches, statistics, or events not present in the analysis.
Do not reveal pitches or outcomes that are not explicitly stated in the \
analysis. Only mention the final outcome if it is explicitly stated in the \
analysis.
Do not describe a strike as "called" or "swinging" unless that classification \
is explicitly stated in the analysis.
Do not claim pitcher intent (e.g. "trying to", "setting up", "attempting"), \
meaningful velocity trends, or statistical patterns unless the analysis \
explicitly states them with supporting evidence.
Do not describe pitch location (e.g. "low", "inside", "high") unless the \
analysis explicitly uses that description.
Never imply that a ball resets, neutralises, or erases a strike count. A ball \
advances the ball count by one; the strikes already recorded remain. Do not \
use phrases like "fresh chance", "reset the count", "back in the count", or \
any other wording that suggests the batter's deficit was erased by a ball.

ANALYSIS:
{stage_a_analysis}

Respond with valid JSON only \u2014 no prose before or after, no Markdown fences. \
Use exactly this structure:

{{
  "commentary_by_level": {{
    "novice": "...",
    "learning": "...",
    "regular": "...",
    "expert": "..."
  }}
}}

Requirements per level (each approximately 35-60 words):
- novice: plain language, no jargon, explain what happened simply
- learning: basic terms (strike, ball, count), light colour commentary
- regular: pitch sequence framing, momentum language a typical fan would enjoy
- expert: velocity, pitch usage patterns, strategic intent a scout would notice \
  (only when that detail is present in the analysis)

All four commentaries must be grounded exclusively in the provided analysis.
"""


class FanCommentaryError(ValueError):
    """Raised when Fan Experience returns an invalid or incomplete response.

    Attributes:
        raw_response: the unmodified string returned by the IBM agent.
                      Never include this in log output or HTTP responses.
    """
    def __init__(self, message: str, raw_response: str = "") -> None:
        super().__init__(message)
        self.raw_response = raw_response


def _strip_fences(text: str) -> str:
    """Remove surrounding whitespace and optional Markdown code fences."""
    text = text.strip()
    # Match ```json ... ``` or ``` ... ```
    m = _re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if m:
        return m.group(1).strip()
    return text


def generate_fan_commentary(stage_a_input: dict) -> dict:
    """
    Call Fan Experience directly (no Analyst, no Pitch Scout) with verified
    Stage A data and return parsed JSON containing commentary for all four
    knowledge levels, plus the original evidence_pitches unchanged.

    Args:
        stage_a_input: dict with keys:
            - "analysis" (str): verified Stage A analysis text
            - "evidence_pitches" (list[int]): pitch numbers from the
              verified snapshot; attached to the result unchanged.

    Returns:
        dict with keys:
            - "commentary_by_level": {"novice": str, "learning": str,
                                      "regular": str, "expert": str}
            - "evidence_pitches": list[int] (passed through from input)

    Raises:
        TypeError: if stage_a_input is not a dict or required keys are
                   the wrong type.
        KeyError: if WO_API_KEY, WO_INSTANCE_URL, or
                  WO_FAN_EXPERIENCE_AGENT_ID are not set.
        FanCommentaryError: if the IBM response is missing, malformed,
                            or incomplete. The raw_response attribute
                            holds the unmodified agent string for
                            debugging; never log or return it.
    """
    # --- Validate input ---
    if not isinstance(stage_a_input, dict):
        raise TypeError(
            f"stage_a_input must be a dict, got {type(stage_a_input).__name__}"
        )
    analysis = stage_a_input.get("analysis")
    evidence_pitches = stage_a_input.get("evidence_pitches")
    if not isinstance(analysis, str) or not analysis.strip():
        raise TypeError(
            "stage_a_input['analysis'] must be a non-empty string"
        )
    if not isinstance(evidence_pitches, list) or not all(
        isinstance(p, int) for p in evidence_pitches
    ):
        raise TypeError(
            "stage_a_input['evidence_pitches'] must be a list of ints"
        )

    api_key = os.environ["WO_API_KEY"]
    instance_url = os.environ["WO_INSTANCE_URL"].rstrip("/")
    fan_agent_id = os.environ["WO_FAN_EXPERIENCE_AGENT_ID"]

    # Step 1: Exchange IBM API key for an IAM access token
    token_data = urlencode({
        "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
        "apikey": api_key
    }).encode("utf-8")

    token_request = Request(
        "https://iam.cloud.ibm.com/identity/token",
        data=token_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )

    with urlopen(token_request, timeout=20) as response:
        access_token = json.load(response)["access_token"]

    # Step 2: Call Fan Experience directly with the Stage A analysis
    prompt = _FAN_EXPERIENCE_PROMPT_TEMPLATE.format(
        stage_a_analysis=analysis
    )

    payload = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "stream": False
    }).encode("utf-8")

    agent_request = Request(
        f"{instance_url}/v1/orchestrate/{fan_agent_id}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    with urlopen(agent_request, timeout=120) as response:
        result = json.load(response)

    raw_content = result["choices"][0]["message"]["content"]

    # --- Strip whitespace and optional Markdown fences ---
    cleaned = _strip_fences(raw_content)

    # --- Parse JSON ---
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise FanCommentaryError(
            f"Fan Experience returned non-JSON content: {exc}",
            raw_response=raw_content,
        ) from exc

    # --- Validate root is a dict ---
    if not isinstance(parsed, dict):
        raise FanCommentaryError(
            f"Fan Experience JSON root must be a dict, got "
            f"{type(parsed).__name__}",
            raw_response=raw_content,
        )

    # --- Validate commentary_by_level ---
    commentary = parsed.get("commentary_by_level")
    if not isinstance(commentary, dict):
        raise FanCommentaryError(
            "Fan Experience response missing 'commentary_by_level' dict. "
            f"Got keys: {list(parsed.keys())}",
            raw_response=raw_content,
        )

    missing = [lvl for lvl in _FAN_EXPERIENCE_LEVELS if lvl not in commentary]
    if missing:
        raise FanCommentaryError(
            f"Fan Experience response missing required level(s): {missing}",
            raw_response=raw_content,
        )

    empty = [
        lvl for lvl in _FAN_EXPERIENCE_LEVELS
        if not isinstance(commentary[lvl], str) or not commentary[lvl].strip()
    ]
    if empty:
        raise FanCommentaryError(
            f"Fan Experience returned empty commentary for level(s): {empty}",
            raw_response=raw_content,
        )

    # --- Attach evidence_pitches from verified input (never from IBM) ---
    parsed["evidence_pitches"] = evidence_pitches

    return parsed
