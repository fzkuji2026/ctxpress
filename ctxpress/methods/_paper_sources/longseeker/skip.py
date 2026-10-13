# Port of PolarSeeker/LongSeeker 1fc30b70, see reference_sources.json.
import re
from .context import ContextManager

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "skip",
            "description": "Skip this step, do nothing.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]

def skip() -> str:
    """Skip this step, do nothing."""
    return "Skipped."
