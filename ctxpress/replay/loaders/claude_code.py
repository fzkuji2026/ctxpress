"""Claude Code transcript (~/.claude/projects/<project>/<session>.jsonl) -> trace.

One API response can be split over several transcript lines that share message.id; a request is the first
line of each new message.id. Real input = input_tokens + cache_read_input_tokens + cache_creation_input_tokens.
Sub-agent (sidechain) lines are skipped: they run in their own context.
Stops at the first compaction (a 'system' line with subtype compact_boundary) when stop_at_compaction."""
from __future__ import annotations
import json
from datetime import datetime
from ctxpress.core.trace import PATH, SPEC, TEST, classify, norm, sub_of, finish

EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
SEARCH_TOOLS = {"Grep", "Glob", "LS"}


def ts(d):
    t = d.get("timestamp")
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() if t else None
    except Exception:
        return None


def text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""


def classify_tool(name, inp):
    if name in EDIT_TOOLS:
        p = norm(str(inp.get("file_path") or inp.get("notebook_path") or ""))
        anchors = []
        for e in ([inp] if name != "MultiEdit" else inp.get("edits", [])):
            anchors += [l.strip() for l in str(e.get("old_string", "")).split("\n") if len(l.strip()) >= 4]
        return "edit", {p}, {p: anchors}
    if name == "Read":
        p = norm(str(inp.get("file_path", "")))
        return ("spec" if SPEC.search(p) else "read"), {p}, None
    if name in SEARCH_TOOLS:
        return "search", set(), None
    if name == "Bash":
        k, res = classify(str(inp.get("command", "")))
        return k, res, None
    return "other", set(), None


def load(path, stop_at_compaction=True, keep_text=True, name=None, fit_alpha=True):
    reqs, cur, calls, compacted, seen = [], [], {}, False, set()
    for line in open(path, encoding="utf-8", errors="ignore"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("isSidechain"):
            continue
        if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
            compacted = True
            if stop_at_compaction:
                break
            continue
        m = d.get("message")
        if not isinstance(m, dict):
            continue
        t = ts(d)
        content = m.get("content")
        if d.get("type") == "assistant":
            mid = m.get("id")
            u = m.get("usage") or {}
            if mid and mid not in seen and u:
                seen.add(mid)
                k = len(cur)
                real = (u.get("input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0) + (u.get("cache_creation_input_tokens", 0) or 0)
                reqs.append(dict(input=real, cached=u.get("cache_read_input_tokens", 0) or 0,
                                 out_tokens=u.get("output_tokens", 0) or 0, t=t, before=cur[:k]))
                cur = cur[k:]
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    kind, res, anchors = classify_tool(b.get("name"), inp)
                    s = json.dumps(inp, ensure_ascii=False)
                    calls[b.get("id")] = (kind, res, t, s)
                    seg = dict(seg="call", size=len(s) // 4 + 8, kind=kind, res=sorted(res), t=t)
                    if anchors is not None:
                        seg["anchors"] = anchors
                    if keep_text:
                        seg["text"] = s
                    cur.append(seg)
                elif b.get("type") == "text":
                    cur.append(dict(seg="msg", size=len(b.get("text", "")) // 4 + 4, role="assistant", t=t))
                elif b.get("type") == "thinking":
                    cur.append(dict(seg="reason", size=len(b.get("thinking", "")) // 4, t=t))
        elif d.get("type") == "user":
            if isinstance(content, str):
                cur.append(dict(seg="msg", size=len(content) // 4 + 4, role="user", t=t)); continue
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    kind, res, t0, inp = calls.get(b.get("tool_use_id"), ("other", set(), None, ""))
                    out = text_of(b.get("content"))
                    outp = sorted({norm(x.group(1)) for x in PATH.finditer(out[:20000])})[:200] if kind in ("search", "command") else []
                    seg = dict(seg="out", size=len(out) // 4 + 8, kind=kind, res=sorted(res), outpaths=outp,
                               sub=sub_of(kind, res), test=bool(kind == "command" and TEST.search(inp)),
                               dur=(t - t0) if (t and t0) else None, t=t)
                    if keep_text:
                        seg["text"] = out
                    cur.append(seg)
                elif b.get("type") == "text":
                    cur.append(dict(seg="msg", size=len(b.get("text", "")) // 4 + 4, role="user", t=t))
    return finish(name or path, path, reqs, compacted, "claude-code", fit=fit_alpha)
