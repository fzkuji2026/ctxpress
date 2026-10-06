"""UTF-16 estimates and budget decisions against the actual TypeScript authors."""
import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ctxpress.core.compat import js_length
from ctxpress.live.context import LiveContext
from ctxpress.methods import CWL


@pytest.mark.parametrize("text,units", [("", 0), ("abcd", 4), ("中文", 2), ("😀" * 4, 8),
                                       ("中😀ab", 5), ("𝒜𐐀", 4), ("\ud800", 1)])
def test_js_string_length(text, units):
    assert js_length(text) == units


def test_cwl_counts_unicode_in_messages_reasoning_and_call_arguments():
    ctx = LiveContext(CWL())
    ctx.add_message("user", "😀" * 4)
    ctx.add_other("reasoning", 2, text="😀" * 4)
    ctx.add_call("c", "{}", name="😀", args="😀😀")
    ctx.add_output("c", "中😀ab")
    # user=2, one assistant message=(8+2+4)/4 rounded up=4, output=2.
    assert CWL.estimate(ctx) == 8


@pytest.fixture
def node():
    binary = os.environ.get("CTXPRESS_NODE") or shutil.which("node")
    if not binary:
        pytest.skip("Node with TypeScript transformation is not installed")
    check = subprocess.run([binary, "--experimental-transform-types", "--no-warnings", "-e", ""], capture_output=True)
    if check.returncode:
        pytest.skip("selected Node lacks --experimental-transform-types; select Node >=22.7 using CTXPRESS_NODE")
    return binary


def author_driver(node, directory, sessions):
    proc = subprocess.run([node, "--experimental-transform-types", "--no-warnings", "driver.mjs"], cwd=directory,
                          input=json.dumps({"sessions": sessions}), capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


def test_cwl_unicode_and_exact_author_budget_boundaries(node, tmp_path):
    from repro import cwl_compare as cmp
    if not Path(cmp.SRC).is_dir():
        pytest.skip("authors' CWL source is not available")
    cmp.prepare(str(tmp_path))
    def call(cid, name, args, result):
        return {"blocks": [{"type": "toolCall", "id": cid, "name": name, "arguments": args}], "results": {cid: result}}
    samples = []
    for text in ("😀" * 200, "中😀ab\n" * 120, "x" * 400):
        samples.append({"task": "任务😀", "threshold": 1000000, "events": [
            call("s", "delimiter", {"action": "start", "name": "探索😀", "type": "expl"}, ""),
            call("r", "read", {"path": "😀.py"}, text),
            call("e", "delimiter", {"action": "end", "description": "摘要😀"}, "")]})
    driver = tmp_path / "driver.mjs"
    original = driver.read_text(encoding="utf-8")
    driver.write_text('import { estimateTokens } from "./core/compaction-estimate.ts";\n' + original.replace(
        "JSON.stringify(out)", "JSON.stringify(out.map(views => views.at(-1).reduce((n, m) => n + estimateTokens(m), 0)))"), encoding="utf-8")
    boundaries = author_driver(node, tmp_path, samples)
    driver.write_text(original, encoding="utf-8")
    cases = []
    for sample, boundary in zip(samples, boundaries):
        for budget in (boundary - 1, boundary, boundary + 1):
            case = copy.deepcopy(sample)
            case["threshold"] = budget
            cases.append(case)
    theirs = author_driver(node, tmp_path, cases)
    for case, views in zip(cases, theirs):
        assert cmp.ours(case) == [cmp.canon_theirs(view) for view in views]
    assert len(theirs[0][-1]) < len(theirs[1][-1])


def test_dtoc_unicode_envelopes_hide_restore_and_saved_tokens_match_author(node, tmp_path):
    from repro import dtoc_compare as cmp
    if not Path(cmp.SRC).is_dir():
        pytest.skip("authors' DTOC source is not available")
    cmp.prepare(str(tmp_path))
    sample = {"rounds": [[{"id": f"c{i}", "tool": "read", "output": text, "end": 1790000000000 + i}
                          for i, text in enumerate(("😀" * 4, "中文😀ab", "𝒜𐐀\n"))],
                         [{"id": "hide", "tool": "manage_context", "args": {"enable": [], "disable": ["tk_001", "tk_002"]},
                           "end": 1790000000010}],
                         [{"id": "restore", "tool": "manage_context", "args": {"enable": ["tk_001"], "disable": []},
                           "end": 1790000000020}]]}
    theirs = author_driver(node, tmp_path, [sample])[0]
    assert cmp.ours(sample) == theirs
    assert json.loads(theirs[0][0][1])["estimated_tokens"] == 2
    assert json.loads(theirs[1][0][1])["status"] == "hidden"
    assert json.loads(theirs[2][0][1])["tool_result"] == "😀" * 4


def test_cwl_measures_arguments_as_json_stringify_of_the_parsed_object():
    from ctxpress.live.context import LiveContext
    from ctxpress.methods import CWL
    spaced, compact = LiveContext(CWL()), LiveContext(CWL())
    for ctx, args in ((spaced, '{"path": "\u00e9.py", "n": [1, 2]}'), (compact, '{"path":"é.py","n":[1,2]}')):
        ctx.add_message("user", "task")
        ctx.add_call("c", "x", name="read", args=args)
    assert CWL.estimate(spaced) == CWL.estimate(compact)
