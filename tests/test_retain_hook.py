"""Joint policies: RetainHook shrinks outputs by a policy's retain() only where it costs little cache, and the
evolve loop builds tool / joint / retention / combined arms from one policy format."""
import json
from ctxpress.live.rewrite import Rewriter, output_text
from ctxpress.methods import build
from ctxpress.evolve import evaluate, policy

SOURCE = "\n".join(f"    y = x + {i}  # body line {i}" for i in range(400))
POLICY = '''from ctxpress.evolve.runtime import cli
GUIDANCE = ""
RETAIN = {"t": 300000, "keep_recent": 2}
TOOLS = {}
def view(cmd, out, ctx):
    return None
def retain(info, ctx):
    if info["age"] >= 1 and "src/m.py" in info["cmd"]:
        return "[read of src/m.py shrunk after use; run it again to see it]"
    return None
if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
'''


def pair(cid, cmd, out):
    return [dict(type='function_call', call_id=cid, name='shell', arguments=json.dumps(dict(cmd=cmd))),
            dict(type='function_call_output', call_id=cid, output=out)]


def sent(out, cid):
    return output_text(next(x for x in out['input'] if x.get('call_id') == cid and 'output' in x))


def write_policy(tmp_path):
    p = tmp_path / "joint_policy.py"
    p.write_text(POLICY)
    return str(p)


def test_shrink_waits_when_it_would_rebuild_a_long_suffix(tmp_path):
    rw = Rewriter(lambda: build({"class": "RetainHook", "args": {"policy": write_policy(tmp_path), "horizon": 6}}))
    items = [dict(type='message', role='user', content='Fix it')] + pair('a', 'cat src/m.py', SOURCE)
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert sent(out, 'a') == SOURCE                     # just entered: age 0
    items += pair('b', 'cat other.py', SOURCE) + pair('c', 'cat third.py', SOURCE)
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert sent(out, 'a') == SOURCE                     # shrinking it would rebuild two long outputs after it for little gain


def test_shrink_is_applied_when_it_pays_for_the_rebuild(tmp_path):
    rw = Rewriter(lambda: build({"class": "RetainHook", "args": {"policy": write_policy(tmp_path), "horizon": 400}}))
    items = [dict(type='message', role='user', content='Fix it')] + pair('a', 'cat src/m.py', SOURCE)
    rw.rewrite_body(dict(input=items), 's')
    items += pair('b', 'ls', 'm.py')
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert sent(out, 'a').startswith('[read of src/m.py shrunk')


def test_policy_without_retain_changes_nothing():
    rw = Rewriter(lambda: build({"class": "RetainHook", "args": {"policy": "ctxpress.methods.readless_policies.evolved.g002_c01"}}))
    items = [dict(type='message', role='user', content='Fix it')] + pair('a', 'cat src/m.py', SOURCE) + pair('b', 'ls', 'x')
    out, _ = rw.rewrite_body(dict(input=items), 's')
    out, _ = rw.rewrite_body(dict(input=items + pair('c', 'ls', 'y')), 's')
    assert sent(out, 'a') == SOURCE


def test_retain_overrides_are_clamped():
    assert policy.retain_overrides({"t": 300000, "keep_recent": 2, "bogus": 1, "spec_keep_frac": True}) == {"t": 100000, "keep_recent": 2}


def test_arm_entries_of_each_space():
    tool = evaluate.method("g002_c01")
    assert [m["class"] for m in tool["args"]["methods"]] == ["ReadLess", "infinite-context"]
    joint = evaluate.method("r000_noretain", space="joint")
    assert [m["class"] for m in joint["args"]["methods"]] == ["ReadLess", "infinite-context", "RetainHook"]
    ret = evaluate.method("r000_noretain", space="retention")
    assert [m["class"] for m in ret["args"]["methods"]] == ["infinite-context", "RetainHook"]
    combo = evaluate.method("g002_c01", retention="r000_noretain")
    assert [m["class"] for m in combo["args"]["methods"]] == ["ReadLess", "infinite-context", "RetainHook"]
    assert combo["args"]["methods"][2]["args"]["policy"].endswith("r000_noretain")
    van = evaluate.vanilla_method("g002_c01")
    assert [m["class"] for m in van["args"]["methods"]] == ["ReadLess", "HostDefault"]
    for entry in (tool, joint, ret, combo, van, evaluate.vanilla_method(None)):
        build(entry)                                     # every arm builds


def test_seed_and_ablation_policies_pass_the_checks():
    for cid in ("r000_noretain", "a002_c01_nopointer"):
        with open(policy.path_of(cid)) as f:
            assert policy.static_check(f.read()) == []
    with open(policy.path_of("a002_c01_nopointer")) as f:
        src = f.read()
    assert "more lines: " not in src.split("def _map")[0].split("def _excerpt")[1]
