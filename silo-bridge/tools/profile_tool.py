"""Profile tool — reads the user's YAML config."""

import json
from pathlib import Path

import yaml

PROFILE_PATH = Path(__file__).resolve().parent.parent / "config" / "profile.yaml"

PROFILE_TOOL_SCHEMA = {
    "name": "read_profile",
    "description": (
        "Read Shakti's personal profile — dietary restrictions, preferences, "
        "location, etc. Use this whenever you need to filter or personalize."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "enum": ["dietary", "preferences", "location", "all"],
                "description": "Which section to read. Defaults to 'all'.",
            }
        },
        "required": [],
    },
}


def read_profile(section: str = "all") -> str:
    """Return the requested profile section as JSON."""
    try:
        with open(PROFILE_PATH, "r") as f:
            profile = yaml.safe_load(f)
    except FileNotFoundError:
        return json.dumps({"error": "Profile file not found at " + str(PROFILE_PATH)})

    if section and section != "all":
        data = profile.get(section)
        if data is None:
            return json.dumps({"error": f"Section '{section}' not found in profile"})
        return json.dumps({section: data}, default=str)

    return json.dumps(profile, default=str)
