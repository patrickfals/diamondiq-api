
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen


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
