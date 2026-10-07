import json

import pytest

from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import ACON, AgentFold, AgentFoldTools, ReSum, TokenPilot, build
from repro.swe_pruner_diagnose import diagnose, PREFIX
from repro.swe_pruner_model import comparison_passed
from ctxpress.methods import ACONSource, TokenPilotLifecycle
from ctxpress.live.context import LiveContext


def append(items, cid, name, args=None, output="file contents " * 100):
    items.extend([dict(type="function_call", call_id=cid, name=name, arguments=json.dumps(args or {})),
                  dict(type="function_call_output", call_id=cid, output=output)])


def test_agent_folds_and_deeply_refolds_without_auxiliary_model():
    rw = Rewriter(lambda: build({"class": "AgentFoldTools"}))
    items = [dict(role="user", content="Keep the task")]
    append(items, "a", "read")
    out, _ = rw.rewrite_body(dict(input=items), "s")
    assert "[AgentFold step 1]" in str(out)
    append(items, "f", "fold_context", dict(start_step=1, end_step=1, summary="ALPHA evidence"))
    out, _ = rw.rewrite_body(dict(input=items), "s")
    assert "ALPHA evidence" in str(out) and "file contents" not in str(out)
    assert "Folded steps 1-1" in str(out)
    append(items, "b", "read")
    append(items, "f2", "fold_context", dict(start_step=1, end_step=3, summary="ALPHA and BETA"))
    out, _ = rw.rewrite_body(dict(input=items), "s")
    assert "ALPHA and BETA" in str(out) and "ALPHA evidence" not in str(out)
    assert "Keep the task" in str(out)
    ctx = rw.sessions["s"].ctx
    assert ctx.M["op_segment_summary"] == 2
    rw.rewrite_body(dict(input=items), "s")
    assert ctx.M["op_segment_summary"] == 2


def test_fold_rejects_partial_fold_and_preserves_user_correction():
    rw = Rewriter(AgentFoldTools)
    items = [dict(role="user", content="task")]
    append(items, "a", "read")
    append(items, "b", "read")
    append(items, "f", "fold_context", dict(start_step=1, end_step=2, summary="both steps"))
    rw.rewrite_body(dict(input=items), "s")
    append(items, "bad", "fold_context", dict(start_step=2, end_step=3, summary="partial"))
    out, _ = rw.rewrite_body(dict(input=items), "s")
    assert "cannot split" in str(out) and "both steps" in str(out)
    items.append(dict(role="user", content="New requirement"))
    append(items, "c", "read")
    append(items, "bad2", "fold_context", dict(start_step=1, end_step=5, summary="omit correction"))
    out, _ = rw.rewrite_body(dict(input=items), "s")
    assert "protected history" in str(out) and "New requirement" in str(out)


def test_fold_rejects_incomplete_parallel_pair():
    rw = Rewriter(AgentFoldTools)
    items = [dict(role="user", content="task"),
             dict(type="function_call", call_id="a", name="read", arguments="{}")]
    append(items, "f", "fold_context", dict(start_step=1, end_step=1, summary="oops"))
    out, _ = rw.rewrite_body(dict(input=items), "s")
    assert "Error:" in str(out)
    # The host renderer omits dangling calls independently; the method retains it.
    assert any(x.get("call_id") == "a" for x in rw.sessions["s"].ctx.ctx)


@pytest.mark.parametrize("args", [None, {}, dict(start_step=True, end_step=1, summary="s"),
    dict(start_step=2, end_step=1, summary="s"), dict(start_step=1, end_step=1, summary=" ")])
def test_fold_argument_errors(args):
    assert AgentFoldTools().call_tool("fold_context", args)[1]


@pytest.mark.parametrize("cls,args", [(ReSum, {"k": 0}), (ReSum, {"k": True}),
    (ReSum, {"k": 1.5}), (ACON, {"t_obs": -1}), (ACON, {"t_hist": False}),
    (AgentFold, {"deep": 0}), (TokenPilot, {"a": -1}), (TokenPilot, {"score": "false"})])
def test_invalid_baseline_parameters_fail_before_start(cls, args):
    with pytest.raises(ValueError):
        cls(**args)


def pruner_pair():
    source = dict(trajectory="a/b.json", message=1, instance_id="task", source_sha256="abc", threshold=.5,
                  text="raw", expected=PREFIX + "kept", expected_stats=dict(origin_token_cnt=5, left_token_cnt=2, model_input_token_cnt=9))
    result = dict(source, trajectory="a\\b.json", response=dict(origin_token_cnt=5, left_token_cnt=2,
                  model_input_token_cnt=8, pruned_code="kept"), text_same=True, adapter_same=True)
    return source, result


def test_pruner_offset_does_not_pass_published_acceptance():
    source, result = pruner_pair()
    report = diagnose([source], [result])
    assert report["complete"] and not report["published_match"]
    assert report["count_deltas"]["model_input_token_cnt"] == {"-1": 1}
    counts = dict(cases=1, adapter_same=1, text_same=1, source_tokens_same=1, kept_tokens_same=1,
                  model_input_tokens_same=0, model_errors=0)
    assert comparison_passed(counts, "adapter") and not comparison_passed(counts)
    assert not comparison_passed({})


def test_pruner_diagnostics_reject_duplicate_and_wrong_evidence():
    source, result = pruner_pair()
    with pytest.raises(ValueError, match="duplicate"):
        diagnose([source, source], [result])
    result["source_sha256"] = "different"
    assert not diagnose([source], [result])["complete"]
    assert not diagnose([source], [])["complete"]


def test_pruner_flags_recomputed_instead_of_trusted():
    source, result = pruner_pair()
    result["response"]["pruned_code"] = "different"
    report = diagnose([source], [result])
    assert report["counts"]["text_same"] == 0
    assert report["counts"]["recorded_text_flag_disagrees"] == 1


class Model:
    def __init__(self, answer=None):
        self.answer, self.calls = answer, []

    def summarize_prompt(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        if self.answer is not None:
            return self.answer
        if kwargs['purpose'] == 'lifecycle':
            data = json.loads(user)
            return json.dumps(dict(baseVersion=data['baseVersion'], taskUpdates=[dict(taskId='t', objective='done',
                lifecycle='evictable', completionEvidence=['finished'], unresolvedQuestions=[],
                coveredTurnAbsIds=list(dict.fromkeys(x['turn'] for x in data['delta'])))]))
        return '# Refined Observation\nOBS' if kwargs['purpose'] == 'observation' else '# History Summary\nHISTORY'


def test_acon_source_runs_both_operations_through_common_model_service():
    service = Model()
    rw = Rewriter(lambda: ACONSource(t_hist=0, t_obs=0, summary_model='aux'), summarizer=service)
    items = [dict(role='user', content='TASK {{ history }}')]
    append(items, 'a', 'read')
    append(items, 'b', 'read')
    out, info = rw.rewrite_body(dict(input=items), 's')
    assert len(service.calls) == 3
    assert {c[2]['purpose'] for c in service.calls} == {'observation', 'history'}
    assert all(c[2]['model'] == 'aux' for c in service.calls)
    assert 'HISTORY' in str(out) and 'OBS' in str(out)
    assert 'TASK {{ history }}' in str(out)
    rw.rewrite_body(dict(input=items), 's')
    assert len(service.calls) == 3


def test_acon_empty_or_failed_summary_keeps_history():
    for response in ('# Refined Observation\n ', ''):
        rw = Rewriter(lambda: ACONSource(t_obs=0, t_hist=999999), summarizer=Model(response))
        items = [dict(role='user', content='task')]
        append(items, 'a', 'read')
        out, _ = rw.rewrite_body(dict(input=items), 's')
        assert 'file contents' in str(out)


def test_tokenpilot_uses_real_model_interface_and_retains_newest(tmp_path):
    service = Model()
    rw = Rewriter(lambda: TokenPilotLifecycle(batch_turns=1, min_chars=0, estimator_model='aux'),
                  summarizer=service, store_dir=str(tmp_path))
    items = [dict(role='user', content='task')]
    append(items, 'a', 'read')
    append(items, 'b', 'read')
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert service.calls and service.calls[0][2] == dict(purpose='lifecycle', model='aux')
    results = {x['call_id']: x['output'] for x in out['input'] if x.get('type') == 'function_call_output'}
    assert 'stored at' in results['a'] and 'file contents' in results['b']
    rw.rewrite_body(dict(input=items), 's')
    assert len(service.calls) == 1


@pytest.mark.parametrize('answer', ['not JSON', '{"baseVersion":99,"taskUpdates":[]}',
    '{"baseVersion":0,"taskUpdates":[{"taskId":"t","objective":"x","lifecycle":"evictable","coveredTurnAbsIds":["999"]}]}'])
def test_tokenpilot_invalid_judgment_keeps_context_and_registry(answer):
    rw = Rewriter(lambda: TokenPilotLifecycle(batch_turns=1), summarizer=Model(answer))
    items = [dict(role='user', content='task')]
    append(items, 'a', 'read')
    append(items, 'b', 'read')
    out, _ = rw.rewrite_body(dict(input=items), 's')
    ctx = rw.sessions['s'].ctx
    assert ctx.method.registry['version'] == 0 and ctx.M['lifecycle_invalid'] == 1
    assert 'stored at' not in str(out)


def test_tokenpilot_mixed_ownership_is_protected():
    method = TokenPilotLifecycle(batch_turns=1, min_chars=0)
    service = Model(json.dumps(dict(baseVersion=0, taskUpdates=[dict(taskId='old', objective='done', lifecycle='evictable',
        coveredTurnAbsIds=['1'], completionEvidence=['done']), dict(taskId='active', objective='still needed',
        lifecycle='active', coveredTurnAbsIds=['1'])])))
    ctx = LiveContext(method, summarizer=service)
    ctx.add_message('user', 'task')
    for i in range(2):
        ctx.add_call(str(i), '{}', name='read', args='{}')
        ctx.add_output(str(i), 'evidence ' * 100)
    ctx.before_request()
    assert all(s.get('form') == 'full' for s in ctx.outputs())


@pytest.mark.parametrize('name', ['AgentFoldTools', 'ACONSource', 'TokenPilotLifecycle'])
def test_new_baselines_use_common_anthropic_adapter_and_preserve_tools(name):
    from ctxpress.live import anthropic
    from test_anthropic_host import conversation, valid
    body = conversation(3, size=6000)
    rw = Rewriter(lambda: build({'class': name}), summarizer=Model())
    out, _ = anthropic.rewrite_messages(rw, body)
    assert valid(out) and out['tools'] == body['tools']


@pytest.mark.parametrize('name', ['AgentFoldTools', 'ACONSource', 'TokenPilotLifecycle'])
def test_new_baselines_have_shared_network_free_checks(name):
    from ctxpress.harness.checks.method import check
    report = check(name, turns=12, output_chars=4096)
    assert report['contract_valid'] and report['triggered']
