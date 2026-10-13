# Port of PolarSeeker/LongSeeker 1fc30b70, see reference_sources.json.
import re
from .context import ContextManager

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "rollback",
            "description": "Rollback context to a specified step. This tool operates on the steps listed in the \"### Previous Steps\" section.",
            "parameters": {
                "type": "object",
                "properties": {
                    "step": {
                        "type": "integer",
                        "description": "The step number to rollback to. This step and all steps after it will be cleared (you will redo from this step). Use 1 to restart from the beginning."
                    },
                    "reason": {
                        "type": "string",
                        "description": "The reason for rolling back, explaining why you need to return to this step. **If any candidates have been excluded, explicitly point them out**."
                    }
                },
                "required": ["step", "reason"]
            }
        }
    }
]

def rollback(context_manager, meta_tool_args: dict):
    """
    Rollback context to a specified step.
    Removes this step and all steps after it; the next action will be this step number again.
    Use step 1 to restart from the beginning (step 0 is treated as 1).

    Args:
        context_manager: ContextManager instance
        meta_tool_args: Dictionary containing "step" as int (target step) and "reason" as str
    """
    target_step = meta_tool_args.get("step")
    if target_step is None:
        raise ValueError("step is required")
    target_step = int(target_step)
    reason = meta_tool_args.get("reason", "No reason provided")

    current_step = context_manager.get_step_max()
    # Store the first removed step's k[0] (e.g. rollback to 15 with compressed 10~20 → store 10)
    effective_target = context_manager.get_effective_rollback_target(target_step)
    context_manager.remove_steps_after(target_step)
    context_manager.add_rollback_history(current_step, effective_target, reason)
