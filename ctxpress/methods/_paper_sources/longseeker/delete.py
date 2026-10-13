# Port of PolarSeeker/LongSeeker 1fc30b70, see reference_sources.json.
import re
from .context import ContextManager

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "delete",
            "description": "Delete one step (one round's tool response) from the steps list. Operates on the steps in \"### Previous Steps\" section. Only single steps can be deleted; steps inside a compressed block cannot be deleted individually.",
            "parameters": {
                "type": "object",
                "properties": {
                    "step": {
                        "type": "integer",
                        "description": "The step number to delete (tool response)."
                    }
                },
                "required": ["step"]
            }
        }
    }
]

def delete(context_manager, meta_tool_args: dict):
    """
    Mark one step as deleted by step number: set its content to "deleted" and keep the step entry.
    If step_num is inside a compressed block, that whole block's content becomes "deleted".
    """
    step_num = meta_tool_args.get("step")
    if step_num is None:
        raise ValueError("step is required")
    step_num = int(step_num)
    context_manager.mark_step_deleted(step_num)
