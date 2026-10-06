"""File-backed policies must validate on the host and retain container paths at launch."""
import copy
import json
import shlex
from types import SimpleNamespace

import pytest

from ctxpress.harness.runtime import codex_agent
from ctxpress.harness.runtime import method_inputs
from ctxpress.benchmarks.harbor import driver as harbor_driver
from ctxpress.benchmarks.milestone import codex_hook as milestone_codex
from ctxpress.harness.runtime.method_inputs import host_entry, validate_live
from ctxpress.core import policy
from ctxpress.replay.tune import tune
from test_harbor_codex import OfficialFixture, settings
from test_milestone_codex import OfficialFixture as MilestoneFixture, settings as milestone_settings
from test_policy import history


@pytest.fixture
def staged(tmp_path):
    original = tmp_path / 'policy.json'
    tune([history('train-a'), history('train-b')], original, [0],
         method_args=dict(allow_summary=False, use_reexplore=False, lookahead=4))
    entry = {'class': 'AutoCostModel', 'args': {'policy': str(original)}}
    directory = tmp_path / 'method-inputs'
    mounted = method_inputs.freeze(entry, directory)
    original.unlink()  # Validation must depend only on the frozen copy.
    return mounted, directory


@pytest.mark.parametrize('wrapper', [None, 'WithMemory', 'Composed'])
def test_mounted_policy_builds_without_mutating_nested_container_arguments(staged, wrapper):
    entry, directory = staged
    if wrapper == 'WithMemory':
        entry = {'class': wrapper, 'args': {'inner': entry}}
    elif wrapper == 'Composed':
        entry = {'class': wrapper, 'args': {'methods': [entry]}}
    original = copy.deepcopy(entry)
    validate_live(entry, str(directory))
    assert entry == original
    assert '/ctxpress-method/' in json.dumps(entry)
    assert '/ctxpress-method/' not in json.dumps(host_entry(entry, directory))


@pytest.mark.parametrize('runner', ['harbor', pytest.param('milestone', marks=pytest.mark.linux_only), 'swe'])   # mounts need Linux paths
def test_official_launcher_validates_actual_policy_and_keeps_container_path(staged, monkeypatch, runner):
    entry, directory = staged
    if runner == 'milestone':
        cfg = dict(milestone_settings(), method=entry, profiles=str(directory))
        agent = milestone_codex.framework(MilestoneFixture, cfg)()
        command = agent.build_run_command('fixture', None, '/tmp/prompt')
        assert str(directory) + ':/ctxpress-method:ro' in agent.get_container_mounts()
    else:
        cfg = dict(settings(), method=entry, profiles=str(directory))
        if runner == 'swe':
            from ctxpress.benchmarks.swe.trial import agent_class
            monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE', '/synthetic-auth-not-read')
            agent = agent_class(dict(cfg, run={'max_calls': 100}, folder=str(directory.parent)), lambda _: None)()
        else:
            agent = codex_agent.framework(OfficialFixture, SimpleNamespace, cfg)()
        agent._ctxpress_proxy = 'http://127.0.0.1:3456'
        command = agent.create_run_agent_commands('fixture')[-1].command
    args = shlex.split(command)
    assert json.loads(args[args.index('--args') + 1]) == entry['args']
    assert str(directory) not in command


def test_cost_profile_is_resolved_by_the_same_mount_contract(staged):
    entry, directory = staged
    bundle = policy.load(next(directory.iterdir()))
    profile = directory.parent / 'profile.json'
    profile.write_text(json.dumps(bundle['statistics']))
    mounted = method_inputs.freeze({'class': 'CostModel', 'args': {'profile': str(profile)}},
                                          directory.parent / 'cost-inputs')
    profile.unlink()
    validate_live(mounted, directory.parent / 'cost-inputs')


@pytest.mark.parametrize('damage', ['missing-directory', 'tamper', 'symlink', 'escape'])
def test_staged_policy_binding_fails_closed(staged, damage):
    entry, directory = staged
    path = next(directory.iterdir())
    if damage == 'missing-directory':
        directory = None
    elif damage == 'tamper':
        path.write_text('{}')
    elif damage == 'symlink':
        other = path.with_name('other.json')
        path.rename(other)
        path.symlink_to(other)
    else:
        entry['args']['policy'] = '/ctxpress-method/../' + path.name
    with pytest.raises(ValueError, match='mounted method input'):
        validate_live(entry, directory)


@pytest.mark.parametrize('runner', ['legacy', 'modern', 'pier'])
def test_file_policy_survives_official_trial_lifecycle(staged, tmp_path, monkeypatch, runner):
    from ctxpress.benchmarks.harbor import modern as harbor_modern
    from ctxpress.benchmarks.deepswe import pier_trial
    entry, directory = staged
    if runner == 'legacy':
        import test_harbor_driver as fixture
        target = fixture
    elif runner == 'modern':
        import test_harbor_modern as fixture
        target = harbor_modern
    else:
        import test_deep_swe as fixture
        target = pier_trial
    original_run = target.run_trial
    async def run(request, *args, **kwargs):
        return await original_run(dict(request, method=entry, profiles=str(directory)), *args, **kwargs)
    monkeypatch.setattr(target, 'run_trial', run)
    original_framework = codex_agent.framework
    received = []
    def framework(base, exec_input, cfg, *args, **kwargs):
        received.append(copy.deepcopy(cfg))
        return original_framework(base, exec_input, cfg, *args, **kwargs)
    monkeypatch.setattr(codex_agent, 'framework', framework)
    if runner == 'legacy':
        fixture.test_worker_invokes_trial_with_owned_hooks_and_verifier_after_agent(tmp_path, monkeypatch, False)
    elif runner == 'modern':
        fixture.test_modern_trial_create_preserves_author_collection_and_fresh_grader_lifecycle(
            tmp_path, monkeypatch, True, False, False, False, False)
    else:
        fixture.test_pier_worker_uses_fresh_grader_after_collect_and_checked_agent_stop(tmp_path, monkeypatch, False)
    assert len(received) == 1
    assert received[0]['method'] == entry
    assert received[0]['profiles'] == str(directory)
