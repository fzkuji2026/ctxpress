"""
AgentFold: 1-8, 9, 10, 11, 12-18
Meta-ReAct: A, B, 8, 9, 10, 11

### Checkpoint A
xxx

---

### Checkpoint B
xxx

---

**Motivation**: {motivation}
**Standard Tool Call**: {standard_tool_call_str}
**Standard Tool Response**: {standard_tool_response}

---

**Motivation**: {motivation}
**Standard Tool Call**: {standard_tool_call_str}
**Standard Tool Response**: {standard_tool_response}
"""

class ContextManager:
    def __init__(self):
        """Initialize context manager with step dictionary and rollback history."""
        self.step_dict = {}
        self.rollback_history = []  # list of {"target_step": int, "step": int, "reason": str}
    
    def append(self, key: tuple, content: str):
        """
        Append a new context entry.
        
        Args:
            key: Unique identifier as tuple (start, end) where start and end are integers
            content: Content of the entry (str)
        """
        entry = {
            "content": content
        }
        
        self.step_dict[key] = entry
    
    def delete(self, key: tuple):
        """
        Remove and return a context entry by key.
        
        Args:
            key: Unique identifier as tuple (start, end)
            
        Returns:
            The removed entry dict, or None if not found
        """
        return self.step_dict.pop(key, None)

    def get_step_max(self) -> int:
        """Return the maximum step number in step_dict (max of all end values)."""
        if not self.step_dict:
            return 0
        return max(k[1] for k in self.step_dict)

    def mark_step_deleted(self, step_num: int):
        """For the step (or block containing step_num): replace only the tool response part with 'deleted', keep motivation and tool call. Skip if no **Tool response** (e.g. compressed step)."""
        marker = "**Tool response**"
        for key in self.step_dict:
            if key[0] <= step_num <= key[1]:
                content = self.step_dict[key]["content"]
                if marker not in content:
                    continue
                idx = content.find(marker)
                self.step_dict[key]["content"] = content[:idx] + marker + "\n[Deleted]"

    def get_effective_rollback_target(self, target_step: int) -> int:
        """Return k[0] of the first step that would be removed (min k[0] among keys with k[0] >= target_step). E.g. rollback to 15 with compressed (10,20) → 10. Call before remove_steps_after."""
        if target_step <= 1:
            return 1
        candidates = [k[0] for k in self.step_dict if k[0] >= target_step]
        return min(candidates) if candidates else target_step

    def remove_steps_after(self, target_step: int):
        """
        Exclusive rollback: remove this step and all steps after it.
        E.g. rollback to step 14 → remove step 14, 15, 16, ... (keep 1..13). Next action will be step 14.
        Rollback to step 1 (or 0) → clear all steps; next action will be step 1.
        Keys are (start, end); we remove any key where start >= target_step.
        """
        if target_step <= 1:
            self.step_dict.clear()
            return
        keys_to_remove = [k for k in self.step_dict if k[0] >= target_step]
        for k in keys_to_remove:
            self.step_dict.pop(k, None)

    def add_rollback_history(self, step_num: int, target_step: int, reason: str):
        """
        Add a rollback event to history.
        Clears all rollback records whose target_step is greater than the current rollback target,
        then appends this event so multiple rollbacks to the same step (e.g. step 1 for restart) are all kept.
        """
        self.rollback_history = [h for h in self.rollback_history if h["target_step"] < target_step]
        self.rollback_history.append({"target_step": target_step, "step": step_num, "reason": reason})
    
    def update_context_dict_list(self, motivation, standard_tool_call_str, standard_tool_response, tool_count: int):
        """
        Update the context dictionary with the new information.
        
        Args:
            motivation: Motivation text
            standard_tool_call_str: Standard tool call string
            standard_tool_response: Standard tool response
            tool_count: Current tool count (int)
        """
        content = f"**Motivation**\n{motivation}\n\n**Tool call**\n{standard_tool_call_str}\n\n**Tool response**\n{standard_tool_response}"
        key = (tool_count, tool_count)
        self.append(key, content)
    
    def _format_dict_content(self, header: str = ""):
        """Format step_dict + rollback_history in one list, sorted by position. Rollback sorted by target_step; label: [Rollbacked step]."""
        # Items: (sort_key, 'step'|'rollback', payload)
        items = []
        for key in self.step_dict:
            items.append((key, "step", self.step_dict[key]))
        _rb_end = 10 ** 9
        for i, h in enumerate(self.rollback_history):
            target_step = h["target_step"]  # sort by step we rolled back to; use negative so rollback appears before that step
            items.append(((target_step, -(_rb_end + i)), "rollback", h))
        if not items:
            return f"{header}\n\nEMPTY" if header else "EMPTY"
        items.sort(key=lambda x: x[0])

        lines = []
        for sort_key, kind, payload in items:
            if kind == "rollback":
                h = payload
                line = f"**[Rollbacked step]**: rollbacked to step {h['target_step']}; {h['reason']}"
            else:
                start, end = sort_key
                entry = payload
                if start == end:
                    content = entry["content"]
                    is_compressed = "**Motivation**" not in content
                    label = f"**[Compressed Step {start}]**" if is_compressed else f"**[Step {start}]**"
                else:
                    label = f"**[Compressed Step {start}~{end}]**"
                line = f"{label}\n\n{entry['content']}"
            lines.append(line)
        return f"{header}\n\n" + "\n\n---\n\n".join(lines) if header else "\n\n---\n\n".join(lines)

    def get_context_for_messages(self):
        """Single section: steps and rollbacks interleaved by position, label [Rollbacked Step xxx] for rollbacks."""
        return self._format_dict_content("")
    

