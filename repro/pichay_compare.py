"""Pichay: ctxpress's Pichay (framework method, through the request rewriter) vs the authors' MessageStore + pager.

Every recorded Codex session is replayed as the request sequence Codex sent. Each request goes
  - through ctxpress: Rewriter(Pichay) on the Responses input, and
  - through the original: the input mapped to Anthropic messages (`to_messages`, below), then
    pichay.message_store.MessageStore(PageStore()).ingest(...) and PageStore.detect_faults (the gateway's order).
For every tool output, what is sent (the original or a stub, and the stub's text) must be the same on both sides,
and the eviction, fault and pin counts must match. (Comparing whole messages is not meaningful: when a recorded
request ends inside a model step, the next request adds a call to the same assistant message, and the original
MessageStore, being append-only, keeps its earlier copy without that call.) `to_messages` is the adapter for feeding the original: a model step is one assistant message,
each run of call outputs one user message of tool_result blocks, a call that reads one file is Read {file_path},
other shell calls Bash {command} (the same rules ctxpress's Pichay uses).

    python repro/pichay_compare.py [--orig data/repro/pichay/src] [--age 4 2]
"""
from __future__ import annotations
import argparse, copy, glob, json, os, re, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))   # originals, recorded sessions
sys.path.insert(0, os.path.join(ROOT, "ctxpress"))
from ctxpress.replay.loaders.codex import requests_from_rollout as requests
from ctxpress.core.engine import is_model
from ctxpress.core.trace import classify
from ctxpress.live.rewrite import Rewriter, output_text, message_text, CALL_TYPES, OUT_TYPES
from ctxpress.methods.pichay import Pichay, tool_of


def to_messages(items):
    msgs, prev_out = [], False
    for it in items:
        t = it.get("type") or ("message" if "role" in it else "")
        x = dict(htype=t, role=it.get("role"), seg="msg" if t == "message" else "call" if t in CALL_TYPES else "other")
        if is_model(x):
            if not msgs or msgs[-1]["role"] != "assistant" or prev_out:
                msgs.append({"role": "assistant", "content": []})
            if t in CALL_TYPES:
                text = it.get("input") or it.get("arguments") or ""
                kind, res = classify(text)
                name, inp = tool_of(it.get("name") or t, kind, sorted(res), text)
                msgs[-1]["content"].append({"type": "tool_use", "id": it.get("call_id", ""), "name": name, "input": inp})
            elif t == "message":
                msgs[-1]["content"].append({"type": "text", "text": message_text(it)})
            prev_out = False
        elif t in OUT_TYPES:
            if not prev_out:
                msgs.append({"role": "user", "content": []})
            msgs[-1]["content"].append({"type": "tool_result", "tool_use_id": it.get("call_id", ""), "content": output_text(it)})
            prev_out = True
        elif t == "message" and it.get("role") == "user":
            msgs.append({"role": "user", "content": [{"type": "text", "text": message_text(it)}]})
            prev_out = False
        # developer / system messages ride in Anthropic's `system` field; other items are not messages
    return msgs


STUB = re.compile(r" — .*? \((?=[\d,]+ bytes)", re.S)          # a stub without its description of the tool


def results(msgs):
    return {b["tool_use_id"]: b["content"] for m in msgs if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result"}


def compare(path, age):
    from pichay.pager import PageStore
    from pichay.message_store import MessageStore
    _, reqs = requests(path)
    ps = PageStore(); ms = MessageStore("s", ps)
    method = Pichay(age=age)
    rw = Rewriter(lambda: method)
    res = dict(requests=len(reqs), same=0, exact=0, store_quirk=0, first_diff=None)
    for i, inp in enumerate(reqs):
        ms.ingest(to_messages(copy.deepcopy(inp)), age_threshold=age, min_evict_size=500)
        ps.detect_faults(ms.messages)
        body, _ = rw.rewrite_body({"model": "m", "input": copy.deepcopy(inp)}, "s")
        theirs, mine = results(ms.messages), results(to_messages(body["input"]))
        known = {b["id"] for m in ms.messages if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"}
        diff = [k for k, v in theirs.items() if mine.get(k) != v]
        res["exact"] += not diff and set(theirs) <= set(mine)
        # the original's store lacks a call whose assistant message grew after it was stored, so its stub names the
        # tool "unknown"; the stub's handle and size must still be ours
        quirk = [k for k in diff if k not in known and STUB.sub(" (", theirs[k]) == STUB.sub(" (", mine.get(k) or "")]
        if not set(diff) - set(quirk) and set(theirs) <= set(mine):
            res["same"] += 1
            res["store_quirk"] += bool(quirk)
        elif res["first_diff"] is None:
            res["first_diff"] = i
    m = rw.sessions["s"].ctx.method.M if rw.sessions else dict(evictions=0, gc=0, faults=0, pins=0)
    res.update(theirs=dict(evictions=ps.unique_evictions, gc=ps.gc_count, faults=len(ps.faults), pins=ps.pin_count),
               ours=dict(evictions=m["evictions"], gc=m["gc"], faults=m["faults"], pins=m["pins"]))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orig", default=os.path.join(DATA, "repro", "pichay", "src"))
    ap.add_argument("--age", type=int, nargs="+", default=[4, 2])
    ap.add_argument("--sessions", default=os.path.join(DATA, "tb4-jobs", "**", "rollout-*.jsonl"))
    ap.add_argument("--out", default=os.path.join(ROOT, "ctxpress", "runs", "repro", "pichay.jsonl"))
    a = ap.parse_args()
    sys.path.insert(0, a.orig)
    paths = sorted(glob.glob(a.sessions, recursive=True))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tot = dict(sessions=0, requests=0, same=0, exact=0, store_quirk=0, evictions=0, gc=0, faults=0, pins=0, counts_equal=0)
    with open(a.out, "w", encoding="utf-8") as fh:
        for age in a.age:
            for p in paths:
                r = compare(p, age)
                r.update(session=os.path.relpath(p, ROOT), age=age)
                fh.write(json.dumps(r) + "\n"); fh.flush()
                tot["sessions"] += 1; tot["requests"] += r["requests"]; tot["same"] += r["same"]
                tot["exact"] += r["exact"]; tot["store_quirk"] += r["store_quirk"]
                tot["counts_equal"] += r["theirs"] == r["ours"]
                for k in ("evictions", "gc", "faults", "pins"):
                    tot[k] += r["theirs"][k]
                if r["first_diff"] is not None or r["theirs"] != r["ours"]:
                    print("DIFF", age, r["session"], r["first_diff"], r["theirs"], r["ours"], flush=True)
    print(json.dumps(tot))


if __name__ == "__main__":
    main()
