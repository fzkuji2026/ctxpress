"""Frozen statistics must preserve decisions and work without raw training data online."""
import copy
import json
import pytest
from ctxpress.core import calibration, engine
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import build, CostModel, WithMemory
from ctxpress.replay.calibrate import fit


def history(name="train", length=30):
    calls = [dict(seg="call", size=5, kind="read", res=["a.py"], text="cat a.py"),
             dict(seg="out", size=2500, kind="read", res=["a.py"], outpaths=[], sub="code", text="PRIVATE SOURCE\n" * 700)]
    reqs = [dict(before=calls, input=0, cached=0, t=1)]
    for i in range(1, length):
        reqs.append(dict(before=[dict(seg="call", size=5, kind="command", res=[], text="true"),
                                 dict(seg="out", size=200, kind="command", res=[], outpaths=[], text="ok\n" * 200)],
                         input=0, cached=0, t=i + 1))
    return dict(name=name, prefix=50, alpha=1.0, reqs=reqs)


def test_profile_roundtrip_preserves_cost_model_results(tmp_path):
    path = tmp_path / "fitted.json"
    ops = {kind: ["placeholder", "truncate", "structure"] for kind in ("read_code", "run", "call")}
    direct = CostModel([history(), history("train2", 40)], ops=ops, truncate_budget=100, lookahead=8)
    direct.save_profile(path)
    frozen = CostModel(profile=path, ops=ops, lookahead=8)
    for age in (1, 5, 21, 200):
        assert direct.curves.pm(dict(seg="out", kind="read"), age) == frozen.curves.pm(dict(seg="out", kind="read"), age)
    for request in (0, 35, 100):
        assert direct.rem(request) == frozen.rem(request)
    assert frozen.budget == 100
    assert engine.run(history("eval"), frozen) == engine.run(history("eval"), direct)
    assert "PRIVATE SOURCE" not in path.read_text(encoding="utf-8")


def test_loaded_profile_changes_real_responses_history_and_preserves_media(tmp_path):
    path = tmp_path / "fitted.json"
    fit([history()], path)
    factory = lambda: build({"class": "WithMemory", "args": {"inner": {"class": "CostModel", "args": {
        "profile": str(path), "allow_summary": False, "use_reexplore": False}}}})
    assert isinstance(factory(), WithMemory)
    rewriter = Rewriter(factory, store_dir=str(tmp_path / "store"), retrieve_tool=True)
    inputs = [{"role": "user", "content": "task"},
              {"type": "function_call", "call_id": "a", "name": "exec", "arguments": "cat a.py"},
              {"type": "function_call_output", "call_id": "a", "output": "source\n" * 2000},
              {"type": "function_call", "call_id": "b", "name": "exec", "arguments": "screenshot"},
              {"type": "function_call_output", "call_id": "b", "output": [{"type": "input_image", "image_url": "opaque"}]}]
    rewriter.rewrite_body(dict(input=inputs), "s")
    body, info = rewriter.rewrite_body(dict(input=inputs), "s")
    assert info["changed"] and inputs[-2] in body["input"] and inputs[-1] in body["input"]
    archived = next(item for item in body["input"] if item.get("call_id") == "a" and item["type"] == "function_call_output")
    assert "stored at" in archived["output"] and "session" in archived["output"]
    assert not factory().requires_summary


def test_invalid_profile_is_rejected_before_replacing_existing_file(tmp_path):
    path = tmp_path / "fitted.json"
    fit([history()], path)
    original = path.read_bytes()
    profile = calibration.load(path)
    for key, value in [("version", 999), ("lengths", []), ("truncate_budget", 0)]:
        invalid = copy.deepcopy(profile)
        invalid[key] = value
        with pytest.raises(ValueError):
            calibration.save(path, invalid)
        assert path.read_bytes() == original
    profile["curves"]["*"]["1"] = [float("nan"), 1]
    with pytest.raises(ValueError):
        calibration.save(path, profile)
    with pytest.raises(ValueError, match="budget"):
        CostModel(profile=path, truncate_budget=1)
    with pytest.raises(ValueError, match="ctxpress fit"):
        build({"class": "CostModel"})


def test_fit_cli_excludes_evaluation_session(tmp_path, monkeypatch, capsys):
    from ctxpress.replay import corpus
    from ctxpress.__main__ import main
    data = [history("train"), history("eval")]
    monkeypatch.setattr(corpus, "load_manifest", lambda *args, names=None, **kw: [tr for tr in data if not names or tr["name"] in names])
    path = tmp_path / "profile.json"
    main(["fit", "--manifest", "unused.yaml", "--exclude", "eval", "--output", str(path)])
    assert json.loads(capsys.readouterr().out)["sessions"] == 1
    profile = calibration.load(path)
    assert profile["provenance"]["sessions"] == ["train"] and len(profile["provenance"]["input_sha256"]) == 1
