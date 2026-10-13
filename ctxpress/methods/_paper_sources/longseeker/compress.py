# Port of PolarSeeker/LongSeeker 1fc30b70, see reference_sources.json.
import re
from .context import ContextManager

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "compress",
            "description": "Compress multiple steps into a single step with a summary. This tool operates on the steps listed in the \"### Previous Steps\" section.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "integer",
                        "description": "The starting step number to compress (inclusive)"
                    },
                    "end": {
                        "type": "integer",
                        "description": "The ending step number to compress (inclusive)"
                    },
                    "summary": {
                        "type": "string",
                        "description": "A summary of what was accomplished in the current step sequence. Focus on what actions were taken, what information was gathered, and which discovered information meet the requirements and which do NOT meet the requirements. Be specific. If there is already information about items being excluded or being candidates, preserve that information in the summary."
                    }
                },
                "required": ["start", "end", "summary"]
            }
        }
    }
]

def compress(context_manager: ContextManager, meta_tool_args: dict):
    """
    Compress multiple steps into a single step with a summary.
    
    Args:
        context_manager: ContextManager instance
        meta_tool_args: Dictionary containing "start" as int, "end" as int, and "summary" as str
    
    Raises:
        ValueError: If required parameters are missing or invalid
    """
    
    start = meta_tool_args.get("start")
    end = meta_tool_args.get("end")
    summary = meta_tool_args.get("summary", "")
    
    if start is None or end is None:
        raise ValueError("start and end are required")
    
    if start > end:
        print(f"Error: start ({start}) must be <= end ({end})")
        return
    
    if not summary or summary == "":
        print("Error: summary is required")
        return
    
    # Keys that overlap [start, end]
    keys_overlapping = [
        k for k in context_manager.step_dict
        if k[0] <= end and k[1] >= start
    ]

    # Must not cut through any block: each overlapping block must be fully inside [start, end]
    for (ks, ke) in keys_overlapping:
        if ks < start or ke > end:
            print(
                f"Error: Compress range [{start}, {end}] cuts through existing block step {ks}~{ke}. "
                "Range must align with whole blocks (e.g. use start=1, end=7 to merge all, not 1-2)."
            )
            return

    # Gaps in step numbers are allowed (e.g. after rollback). Just merge all overlapping blocks into (start, end).
    if not keys_overlapping:
        print(f"Error: No existing steps overlap range [{start}, {end}].")
        return

    for key in keys_overlapping:
        del context_manager.step_dict[key]
    context_manager.step_dict[(start, end)] = {"content": summary}
