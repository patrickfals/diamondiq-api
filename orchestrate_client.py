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
# build_fact_sheet() — spoiler-safe, human-readable fact sheet for Stage B
# ---------------------------------------------------------------------------

def build_fact_sheet(
    game_data: dict,
    game_id: int,
    at_bat_index: int,
    revealed_count: int,
) -> str:
    """
    Build a spoiler-safe plain-text fact sheet for the given snapshot position.

    Uses mlb_data.build_authorized_snapshot() as the single authoritative
    spoiler gate — no spoiler-filtering logic is duplicated here.

    Parameters
    ----------
    game_data      : full MLB Stats API response from mlb_data.fetch_game_data()
    game_id        : MLB game identifier
    at_bat_index   : zero-based at-bat index
    revealed_count : number of pitches to authorize (>= 0; 0 = pregame)

    Returns
    -------
    A multi-line plain-text string.  Contains only spoiler-safe information:
    batter/pitcher identities, inning context, entering-game stats (when
    available), and details for authorized pitches only.  The final outcome
    is included only when is_complete is True.

    Format note: "Pitches revealed so far: N" — never "of M".

    Raises
    ------
    mlb_data.DataError : propagated from build_authorized_snapshot() on
                         invalid inputs or missing at-bat.
    """
    from mlb_data import build_authorized_snapshot

    snap = build_authorized_snapshot(
        game_data,
        game_id=game_id,
        at_bat_index=at_bat_index,
        revealed_count=revealed_count,
    )

    lines = []

    # --- Header ---
    lines.append("=== DiamondIQ Verified Fact Sheet ===")
    lines.append(f"Game ID       : {snap['game_id']}")
    lines.append(f"At-bat index  : {snap['at_bat_index']}")
    half = snap.get("half_inning") or ""
    inning = snap.get("inning")
    if inning is not None:
        lines.append(f"Inning        : {half} {inning}".strip())
    lines.append(f"Batter        : {snap.get('batter') or 'Unknown'}")
    lines.append(f"Pitcher       : {snap.get('pitcher') or 'Unknown'}")

    # --- Entering-game stats (only when available for verified at-bats) ---
    batter_stats = snap.get("batter_stats")
    if batter_stats:
        lines.append(
            f"Batter entering-game AVG  : {batter_stats['avg']:.3f}"
            f" ({batter_stats['hits']} H / {batter_stats['at_bats']} AB)"
        )
    pitcher_stats = snap.get("pitcher_stats")
    if pitcher_stats:
        lines.append(
            f"Pitcher entering-game ERA : {pitcher_stats['era']:.2f}"
            f" ({pitcher_stats['earned_runs']} ER in"
            f" {pitcher_stats['outs_recorded']} outs)"
        )

    # --- Pitch summary header (exact required format) ---
    lines.append(f"Pitches revealed so far: {revealed_count}")

    # --- Per-pitch details for the authorized window ---
    pitches = snap.get("pitches", [])
    if pitches:
        lines.append("")
        lines.append("Pitch details (authorized window):")
        for pitch in pitches:
            num = pitch.get("number", "?")
            ptype = pitch.get("type") or "Unknown type"
            result = pitch.get("result") or "Unknown result"
            vel = pitch.get("velocity_mph")
            count_after = pitch.get("count_after_pitch") or {}

            vel_str = f", {vel:.1f} mph" if vel is not None else ""
            balls = count_after.get("balls", "?")
            strikes = count_after.get("strikes", "?")
            count_str = f" | count after: {balls}-{strikes}"

            lines.append(
                f"  Pitch {num}: {ptype} — {result}{vel_str}{count_str}"
            )
    else:
        lines.append("  (No pitches in authorized window)")

    # --- Final outcome: only when at-bat is complete ---
    if snap.get("is_complete"):
        outcome = snap.get("outcome") or "Unknown"
        lines.append("")
        lines.append(f"At-bat outcome: {outcome}")

    lines.append("=== End of Fact Sheet ===")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage A: Analyst invocation (snapshot-scoped, spoiler-safe)
# ---------------------------------------------------------------------------

# Regex that flags explicit forward references to pitch numbers not yet
# revealed.  Recognizes:
#   - single numbers  : "pitch 4",  "pitch #7"
#   - hyphenated range: "pitches 2-6"  (group 1 = lower, group 2 = upper)
#   - en-dash range   : "pitches 2–6"  (same)
#
# Groups:
#   group(1) : always present — the first (or only) pitch number
#   group(2) : present for ranges — the upper endpoint
_FUTURE_PITCH_RE = _re.compile(
    r"\bpitch(?:es)?\s*#?\s*(\d+)"
    r"(?:\s*[-\u2013]\s*(\d+))?",
    _re.IGNORECASE,
)


def _check_future_pitch_refs(text: str, revealed_count: int) -> list:
    """
    Scan *text* for explicit references to pitch numbers beyond revealed_count.

    Recognizes single numbers ("pitch 4"), hash notation ("pitch #7"),
    hyphenated ranges ("pitches 2-6"), and en-dash ranges ("pitches 2–6").
    For ranges the upper endpoint is also checked.

    Returns a sorted list of future pitch numbers found (values >
    revealed_count only).  Returns [] when no future references exist.
    """
    future = []
    for m in _FUTURE_PITCH_RE.finditer(text):
        n1 = int(m.group(1))
        if n1 > revealed_count:
            future.append(n1)
        if m.group(2) is not None:
            n2 = int(m.group(2))
            if n2 > revealed_count:
                future.append(n2)
    return future


# Forbidden count-reset phrases for Stage B validation.
_COUNT_RESET_PHRASES = (
    "fresh chance",
    "reset the count",
    "back in the count",
    "leveled the count",
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
    run_id=None,
) -> dict:
    """
    Invoke DiamondIQ Analyst for the given snapshot position and return a
    validated Stage A dict compatible with generate_fan_commentary().

    Parameters
    ----------
    game_id        : MLB game identifier (must be > 0)
    at_bat_index   : zero-based at-bat index (must be >= 0)
    revealed_count : number of pitches to authorize (must be >= 1)
    run_id         : optional non-secret run correlation ID; forwarded to
                     create_snapshot_token().  Must be exactly 16 lowercase
                     hex characters when provided (secrets.token_hex(8)).

    Returns
    -------
    dict with keys:
        - "analysis"          (str)       : verified analysis text
        - "evidence_pitches"  (list[int]) : pitch numbers from the authorized
                                            snapshot; range 1-revealed_count.
        - "response_metadata" (dict)      : safe IBM response metadata
                                            (key present only when available).

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

    # --- Create signed snapshot token; run_id forwarded when provided ---
    token = create_snapshot_token(game_id, at_bat_index, revealed_count,
                                  run_id=run_id)

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
    spoiler_refs = _check_future_pitch_refs(analysis, revealed_count)
    if spoiler_refs:
        raise StageAError(
            f"Analyst analysis references future pitch number(s) "
            f"beyond revealed_count={revealed_count}: {spoiler_refs}",
            raw_response=raw_content,
        )

    output = {
        "analysis": analysis.strip(),
        "evidence_pitches": evidence_pitches,
    }

    # --- Extract safe IBM response metadata (present only when available) ---
    meta = _extract_response_metadata(result)
    if meta:
        output["response_metadata"] = meta

    return output


# ---------------------------------------------------------------------------
# Safe IBM response metadata extraction
# ---------------------------------------------------------------------------

def _extract_response_metadata(result: dict) -> dict:
    """
    Extract an allowlisted set of safe IBM response metadata fields.

    Retained fields (only when present and the correct type):
        id           : response identifier string
        created      : creation timestamp integer
        usage        : dict of numeric token counts only
        finish_reason: reason the response stopped (from choices[0])
        tool_calls   : safe structural summary — only id, type, function_name.
                       Arguments, message content, and other payloads are
                       excluded entirely.

    Never returns, logs, or persists raw tool-call arguments, signed snapshot
    tokens, credentials, authorization headers, or complete IBM responses.

    Returns an empty dict when no recognized metadata is available.
    """
    meta: dict = {}

    if "id" in result and isinstance(result["id"], str):
        meta["id"] = result["id"]

    if "created" in result and isinstance(result["created"], int):
        meta["created"] = result["created"]

    if "usage" in result and isinstance(result["usage"], dict):
        # Retain only numeric token-count fields; skip strings/unknowns.
        safe_usage = {
            k: v for k, v in result["usage"].items()
            if isinstance(v, (int, float))
        }
        if safe_usage:
            meta["usage"] = safe_usage

    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            fr = choice.get("finish_reason")
            if isinstance(fr, str):
                meta["finish_reason"] = fr

            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                safe_calls = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    safe_tc: dict = {}
                    if "id" in tc and isinstance(tc["id"], str):
                        safe_tc["id"] = tc["id"]
                    if "type" in tc and isinstance(tc["type"], str):
                        safe_tc["type"] = tc["type"]
                    # Only the function name is safe; arguments are excluded.
                    func = tc.get("function") or {}
                    if isinstance(func, dict):
                        fname = func.get("name")
                        if isinstance(fname, str):
                            safe_tc["function_name"] = fname
                    if safe_tc:
                        safe_calls.append(safe_tc)
                if safe_calls:
                    meta["tool_calls"] = safe_calls

    return meta


# ---------------------------------------------------------------------------
# Stage B: Fan Experience direct invocation
# ---------------------------------------------------------------------------

_FAN_EXPERIENCE_LEVELS = ("novice", "learning", "regular", "expert")

# Double-brace {{ }} produces literal { } after .format() substitution.
# {stage_a_analysis} and {fact_sheet} are the two interpolation points.
_FAN_EXPERIENCE_PROMPT_TEMPLATE = """\
You are DiamondIQ Fan Experience. A verified baseball analysis and a \
VERIFIED FACT SHEET have been provided below. Using ONLY the facts stated \
in that analysis and fact sheet, write four short baseball broadcast \
commentaries — one per knowledge level.
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

VERIFIED FACT SHEET:
{fact_sheet}

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

Requirements per level:
- novice   (40-55 words)  : Everyday language. Explain strike, ball, count, \
and any pitch type mentioned. No jargon.
- learning (45-60 words)  : Assume basic baseball knowledge. Briefly name \
pitch types and explain how the count affects the at-bat.
- regular  (50-65 words)  : Natural broadcast commentary emphasizing \
sequencing, count progression, and observed developments.
- expert   (55-70 words)  : Verified velocities, pitch types, entering AVG/ERA \
when available, and distinctions between observation and inference. Only \
include statistical detail that is present in the analysis or fact sheet.

All four commentaries must describe consistent underlying facts. Only the \
complexity, terminology, and depth of explanation should change.
All four commentaries must be grounded exclusively in the provided analysis \
and fact sheet.
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
    Stage A data and a VERIFIED FACT SHEET, then return parsed JSON containing
    commentary for all four knowledge levels.

    Args:
        stage_a_input: dict with keys:
            - "analysis"       (str)       : verified Stage A analysis text
            - "evidence_pitches" (list[int]): pitch numbers from the verified
              snapshot; attached to the result unchanged.
            - "fact_sheet"     (str, optional) : pre-built fact sheet from
              build_fact_sheet().  When absent, an empty placeholder is used
              so callers that do not yet supply a fact sheet remain compatible.
            - "revealed_count" (int, required for spoiler validation) : number
              of pitches authorized.  When absent, FanCommentaryError is raised
              to prevent silent skipping of future-pitch validation.

    Returns:
        dict with keys:
            - "commentary_by_level": {"novice": str, "learning": str,
                                      "regular": str, "expert": str}
            - "evidence_pitches"   : list[int] (passed through from input)
            - "response_metadata"  : dict (present only when available)

    Raises:
        TypeError          : if stage_a_input is not a dict or required keys
                             are the wrong type.
        KeyError           : if WO_API_KEY, WO_INSTANCE_URL, or
                             WO_FAN_EXPERIENCE_AGENT_ID are not set.
        FanCommentaryError : if revealed_count is missing from stage_a_input;
                             if the IBM response is missing, malformed, or
                             incomplete; if any commentary level references a
                             future pitch or uses a forbidden count-reset phrase.
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

    # --- revealed_count is required for spoiler validation ---
    # Silently skipping validation when it is missing would be a safety gap.
    r_count = stage_a_input.get("revealed_count")
    if r_count is None:
        raise FanCommentaryError(
            "stage_a_input must include 'revealed_count' (int) so that "
            "future-pitch spoiler validation can be performed on each "
            "commentary level.  Add revealed_count to your stage_a_input."
        )
    if not isinstance(r_count, int) or r_count < 0:
        raise FanCommentaryError(
            f"stage_a_input['revealed_count'] must be a non-negative int, "
            f"got: {r_count!r}"
        )

    # Fact sheet is optional for backward compat; callers that supply it
    # enable the VERIFIED FACT SHEET section in the prompt.
    fact_sheet = stage_a_input.get("fact_sheet") or "(No fact sheet provided)"

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

    # Step 2: Call Fan Experience with the Stage A analysis + verified fact sheet
    prompt = _FAN_EXPERIENCE_PROMPT_TEMPLATE.format(
        stage_a_analysis=analysis,
        fact_sheet=fact_sheet,
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

    # --- Spoiler check: future-pitch references in all four levels ---
    for lvl, text in commentary.items():
        future = _check_future_pitch_refs(text, r_count)
        if future:
            raise FanCommentaryError(
                f"Fan Experience '{lvl}' commentary references future "
                f"pitch number(s) beyond revealed_count={r_count}: {future}",
                raw_response=raw_content,
            )

    # --- Count-reset phrase check on all four commentary levels ---
    for lvl, text in commentary.items():
        text_lower = text.lower()
        for phrase in _COUNT_RESET_PHRASES:
            if phrase in text_lower:
                raise FanCommentaryError(
                    f"Fan Experience '{lvl}' commentary contains forbidden "
                    f"count-reset phrase: {phrase!r}",
                    raw_response=raw_content,
                )

    # --- Attach evidence_pitches from verified input (never from IBM) ---
    parsed["evidence_pitches"] = evidence_pitches

    # --- Extract safe IBM response metadata (present only when available) ---
    meta = _extract_response_metadata(result)
    if meta:
        parsed["response_metadata"] = meta

    return parsed
