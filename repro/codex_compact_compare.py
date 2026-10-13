"""Codex's compaction rule (ctxpress/methods/codex_compact.py) vs the replacement histories Codex 0.161 recorded.

For every `compacted` event in a rollout, the history Codex held just before it is rebuilt from the rollout
(the previous replacement history plus the response items since), the rule picks the retained messages, and they
are compared with the recorded replacement history minus its re-injected initial context (items without a
`retained_source`) and the compaction item.

    python repro/codex_compact_compare.py ROLLOUT [ROLLOUT ...]
"""
from __future__ import annotations
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ctxpress.methods.codex_compact import retained  # noqa: E402


def norm(item):
    return json.dumps({k: item.get(k) for k in ("type", "role", "content")}, sort_keys=True, ensure_ascii=False)


def events(path):
    lines = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    history, meta, out = [], [], []
    for line in lines:
        if line.get("type") == "response_item":
            history.append(line["payload"])
            meta.append(line.get("metadata"))
        elif line.get("type") == "compacted":
            p = line["payload"]
            rh = p.get("replacement_history") or []
            md = p.get("replacement_history_metadata") or [None] * len(rh)
            recorded = [x for x, m in zip(rh, md) if (m or {}).get("retained_source")]
            ours = retained(history, meta)
            out.append(dict(window=p.get("window_number"), history=len(history), recorded=len(recorded),
                            ours=len(ours), same=[norm(x) for x in ours] == [norm(x) for x in recorded],
                            recorded_items=recorded, ours_items=ours))
            history, meta = list(rh), list(md)
    return out


def main():
    tot = dict(rollouts=0, compactions=0, same=0)
    for path in sys.argv[1:]:
        tot["rollouts"] += 1
        for e in events(path):
            tot["compactions"] += 1
            tot["same"] += e["same"]
            if not e["same"]:
                print("DIFF", path[-60:], "window", e["window"], "recorded", e["recorded"], "ours", e["ours"])
                for tag, items in (("recorded", e["recorded_items"]), ("ours", e["ours_items"])):
                    for x in items:
                        print("   ", tag, x.get("type"), x.get("role"), repr(" ".join(t.get("text", "")[:90] for t in x.get("content") or [])))
    print(json.dumps(tot))


if __name__ == "__main__":
    main()
