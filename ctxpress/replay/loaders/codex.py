"""Codex CLI rollout (~/.codex/sessions/**/rollout-*.jsonl) -> trace."""
from __future__ import annotations
import json
from datetime import datetime
from ctxpress.core.trace import PATH, TEST, classify, norm, sub_of, unescape, finish, patch_anchors


def text_of(o):
    if isinstance(o, str):
        return o
    if isinstance(o, list):
        return "".join(x.get("text", "") for x in o if isinstance(x, dict))
    return json.dumps(o) if o is not None else ""


def ts(d):
    t = d.get("timestamp")
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() if t else None
    except Exception:
        return None


def load(path, stop_at_compaction=True, keep_text=True, name=None, fit_alpha=True):
    """stop_at_compaction: keep only the part before the first real compaction (the counterfactual replay
    needs the agent's behaviour without compaction)."""
    reqs, cur, calls, compacted = [], [], {}, False
    for line in open(path, encoding="utf-8", errors="ignore"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        p = d.get("payload") or {}
        if not isinstance(p, dict):
            continue
        if d.get("type") == "compacted":
            compacted = True
            if stop_at_compaction:
                break
            continue
        pt = p.get("type")
        t = ts(d)
        if d.get("type") == "response_item":
            if pt in ("custom_tool_call", "function_call"):
                inp = p.get("input") or p.get("arguments") or ""
                kind, res = classify(inp)
                calls[p.get("call_id")] = (kind, res, t, inp)
                seg = dict(seg="call", size=len(inp) // 4 + 8, kind=kind, res=sorted(res), t=t)
                if kind == "edit":
                    seg["anchors"] = {p_: patch_anchors(inp, p_) for p_ in res}
                if keep_text:
                    seg["text"] = inp
                cur.append(seg)
            elif pt in ("custom_tool_call_output", "function_call_output"):
                kind, res, t0, inp = calls.get(p.get("call_id"), ("other", set(), None, ""))
                out = text_of(p.get("output"))
                outp = sorted({norm(m.group(1)) for m in PATH.finditer(out[:20000])})[:200] if kind in ("search", "command") else []
                seg = dict(seg="out", size=len(out) // 4 + 8, kind=kind, res=sorted(res), outpaths=outp,
                           sub=sub_of(kind, res), test=bool(kind == "command" and TEST.search(unescape(inp))),
                           dur=(t - t0) if (t and t0) else None, t=t)
                if keep_text:
                    seg["text"] = out
                cur.append(seg)
            elif pt == "message":
                cur.append(dict(seg="msg", size=len(text_of(p.get("content"))) // 4 + 4, role=p.get("role"), t=t))
            elif pt == "reasoning":
                cur.append(dict(seg="reason", size=0, t=t))
        elif pt == "token_count":
            u = (p.get("info") or {}).get("last_token_usage") or {}
            if not u.get("input_tokens"):
                continue
            k = max([i + 1 for i, x in enumerate(cur) if x["seg"] == "out"] or [0])
            if not reqs:
                k = max(k, max([i + 1 for i, x in enumerate(cur) if x["seg"] == "msg" and x.get("role") == "user"] or [0]))
            rt = u.get("reasoning_output_tokens", 0) or 0
            for x in cur[k:]:
                if x["seg"] == "reason":
                    x["size"] = rt
            reqs.append(dict(input=u["input_tokens"], cached=u.get("cached_input_tokens", 0),
                             out_tokens=u.get("output_tokens", 0), t=t, before=cur[:k]))
            cur = cur[k:]
    return finish(name or path, path, reqs, compacted, "codex", fit=fit_alpha)


def requests_from_rollout(path, limit=None):
    """The requests Codex sent in a recorded session: (base instructions, [input of each request]). Every request
    carries the full history, rebuilt as the response items recorded before each token_count event; stops at
    Codex's own first compaction (after it, the history was rewritten by the server)."""
    import copy
    items, out, instr = [], [], ""
    for line in open(path, encoding="utf-8"):
        try:
            d = json.loads(line)
        except ValueError:                          # a truncated last line in a killed run
            continue
        p = d.get("payload") or {}
        if d.get("type") == "session_meta" and isinstance(p, dict) and isinstance(p.get("base_instructions"), dict):
            instr = p["base_instructions"].get("text", "")
        if d.get("type") == "compacted":
            break
        if d.get("type") == "response_item" and isinstance(p, dict):
            items.append(p)
        elif isinstance(p, dict) and p.get("type") == "token_count" and (p.get("info") or {}).get("last_token_usage"):
            out.append(copy.deepcopy(items))
            if limit and len(out) >= limit:
                break
    return instr, out
