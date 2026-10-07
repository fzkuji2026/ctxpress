"""Audit existing SWE-Pruner records offline, without loading weights or changing evidence.

Joins on trajectory/message, validates source identity, and recomputes text/count
agreement instead of trusting recorded pass flags. Exit 1 means incomplete or
mismatching published reproduction; it does not mean the adapter is broken.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

PREFIX = "Filtered some unrelevant parts judged by your context_focus_question, good try! Filtered Output:\n"
UNCHANGED = "All outputs are judged as relevent! Output:\n"
COUNTS = ("origin_token_cnt", "left_token_cnt", "model_input_token_cnt")


def key(row):
    # Historical Windows reports use backslashes; no files are opened via this key.
    return str(row["trajectory"]).replace("\\", "/"), row["message"]


def index(rows):
    result = {}
    for row in rows:
        k = key(row)
        if k in result:
            raise ValueError(f"duplicate trajectory/message: {k}")
        result[k] = row
    return result


def diagnose(inputs, results):
    source, observed = index(inputs), index(results)
    deltas = {k: Counter() for k in COUNTS}
    counts, cases = Counter(), []
    for k, result in observed.items():
        row = source.get(k)
        if row is None:
            counts["unmatched_results"] += 1
            continue
        if any(result.get(field) != row.get(field) for field in ("source_sha256", "instance_id", "threshold")):
            counts["identity_mismatch"] += 1
            continue
        response = result.get("response") or {}
        error = response.get("error_msg")
        if error:
            actual = f"[Pruner Error]: {error}\n\nOriginal Output:\n{row['text']}"
        elif response.get("left_token_cnt") == response.get("origin_token_cnt"):
            actual = UNCHANGED + row["text"]
        else:
            actual = PREFIX + response.get("pruned_code", "")
        text_same = actual == row["expected"]
        diff = {}
        expected = row.get("expected_stats") or {}
        for field in COUNTS:
            a, b = response.get(field), expected.get(field)
            # Missing values and booleans are not measured zeroes.
            delta = a - b if type(a) is int and type(b) is int else None
            diff[field] = delta
            deltas[field]["missing" if delta is None else str(delta)] += 1
        counts["compared"] += 1
        counts["text_same"] += text_same
        counts["counts_same"] += all(v == 0 for v in diff.values())
        counts["model_errors"] += bool(error)
        counts["adapter_same_recorded"] += result.get("adapter_same") is True
        counts["recorded_text_flag_disagrees"] += result.get("text_same") != text_same
        cases.append(dict(trajectory=k[0], message=k[1], text_same=text_same, count_deltas=diff,
                          source_tokens=response.get("origin_token_cnt"),
                          expected_sha256=hashlib.sha256(row["expected"].encode()).hexdigest(),
                          actual_sha256=hashlib.sha256(actual.encode()).hexdigest()))
    counts["inputs"] = len(source)
    counts["results"] = len(observed)
    counts["missing_results"] = len(source.keys() - observed.keys())
    complete = bool(source) and counts["compared"] == len(source) == len(observed)
    return dict(schema_version=1, scope="offline audit of recorded model responses; no inference",
                counts=dict(counts), complete=complete,
                published_match=complete and counts["text_same"] == len(source) and
                    counts["counts_same"] == len(source) and not counts["model_errors"],
                count_deltas={k: dict(v) for k, v in deltas.items()}, cases=cases,
                limitations=["A constant token offset does not establish its cause.",
                              "Recorded adapter flags are reported, not independently rerun.",
                              "No claim of model, tokenizer or attention-backend equivalence."])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inputs", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args(argv)
    blobs = [p.read_bytes() for p in (args.inputs, args.results)]
    report = diagnose(*[[json.loads(line) for line in b.decode("utf-8").splitlines() if line.strip()] for b in blobs])
    report["evidence_sha256"] = dict(zip(("inputs", "results"), (hashlib.sha256(b).hexdigest() for b in blobs)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False, indent=2))
    return int(not report["published_match"])


if __name__ == "__main__":
    raise SystemExit(main())
