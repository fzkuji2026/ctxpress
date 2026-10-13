# Port of PolarSeeker/LongSeeker 1fc30b70, see reference_sources.json.
import re
from .context import ContextManager

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "snippet",
            "description": "Extract a snippet from a tool response at a specified step, keeping only the content between reserve_prefix and reserve_suffix, and replace the step content with this snippet. reserve_prefix and reserve_suffix are matched only in the text after \"**Tool response**\". This tool operates on the steps listed in the \"### Previous Steps\" section.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reserve_prefix": {
                        "type": "string",
                        "description": "The prefix string to mark the start of the content to reserve"
                    },
                    "reserve_suffix": {
                        "type": "string",
                        "description": "The suffix string to mark the end of the content to reserve"
                    },
                    "step_id": {
                        "type": "integer",
                        "description": "The step id to extract snippet from"
                    }
                },
                "required": ["reserve_prefix", "reserve_suffix", "step_id"]
            }
        }
    }
]

def _snippet_one_step(context_manager: ContextManager, step_id: int, list_of_segment_args: list, log=None):
    """Apply snippet for one step: extract segments and replace step content."""
    step_key = None
    for key in context_manager.step_dict.keys():
        start, end = key
        if start <= step_id <= end:
            step_key = key
            break
    if step_key is None:
        (log or print)(f"Error: Step {step_id} not found in context.")
        return
    start, end = step_key
    if start != end:
        (log or print)(f"Error: Cannot apply snippet to compressed step range ({start}~{end}).")
        return
    content = context_manager.step_dict[step_key]["content"]
    tool_response_match = re.search(r'\*\*Tool response\*\*\n(.*)', content, re.DOTALL)
    tool_response = content if not tool_response_match else tool_response_match.group(1).strip()
    snippets = []
    for args in list_of_segment_args:
        reserve_prefix = args.get("reserve_prefix", "")
        reserve_suffix = args.get("reserve_suffix", "")
        prefix_pos = tool_response.find(reserve_prefix)
        if prefix_pos == -1:
            (log or print)(f"Error: Could not find reserve_prefix in step {step_id} tool response.")
            return
        suffix_pos = tool_response.find(reserve_suffix, prefix_pos)
        if suffix_pos != -1 and suffix_pos > prefix_pos:
            snippet_end = suffix_pos + len(reserve_suffix)
        else:
            snippet_end = len(tool_response)
        snippet_start = prefix_pos
        snippets.append(tool_response[snippet_start:snippet_end])
    new_body = "\n\n---\n\n".join(snippets)
    if tool_response_match:
        new_content = content[: tool_response_match.start(1)] + new_body
    else:
        new_content = new_body
    context_manager.step_dict[step_key]["content"] = new_content

def snippet(context_manager: ContextManager, meta_tool_args: list, log=None):
    """
    meta_tool_args: list of dicts, each with step_id, reserve_prefix, reserve_suffix.
    Group by step_id, then for each step extract segments and replace content once.
    """
    if not meta_tool_args:
        return
    from collections import defaultdict
    by_step = defaultdict(list)
    for args in meta_tool_args:
        step_id = args.get("step_id")
        if step_id is not None:
            by_step[step_id].append(args)
    for step_id, args_list in by_step.items():
        _snippet_one_step(context_manager, step_id, args_list, log=log)
