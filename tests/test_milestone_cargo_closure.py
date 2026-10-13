"""The repaired recipe is explicit, isolated and never changes source policy."""
import copy

import pytest

from ctxpress.benchmarks.milestone.cargo_closure import evaluation_policy


def test_vendor_recipe_preserves_policy_and_forbidden_crates():
    policy = dict(ecosystem=['cargo'], cargo_offline=True,
                  cache_forbid_globs=['/usr/local/cargo/registry/cache/*/ripgrep-*.crate'],
                  closure=dict(cache_paths=['/usr/local/cargo/registry/cache'],
                               evaluator_replace_paths=['/existing']))
    before = copy.deepcopy(policy)
    result = evaluation_policy(policy)
    assert result['closure']['evaluator_replace_paths'] == [
        '/existing', '/opt/vendor', '/usr/local/cargo/config.toml',
        '/usr/local/rustup', '/usr/local/cargo/bin']
    assert result['closure']['cache_paths'] == before['closure']['cache_paths']
    assert result['cache_forbid_globs'] == before['cache_forbid_globs']
    assert policy == before
    assert evaluation_policy(result) == result


@pytest.mark.parametrize('policy', [None, {}, {'ecosystem': ['go']},
    dict(ecosystem=['cargo'], cargo_offline=False, closure={}),
    dict(ecosystem=['cargo'], cargo_offline=True, closure={'cargo_mechanism':'unknown'})])
def test_other_closures_are_not_silently_reinterpreted(policy):
    assert evaluation_policy(policy) is policy


def test_invalid_overlay_declaration_fails_closed():
    with pytest.raises(ValueError, match='must be a list'):
        evaluation_policy(dict(ecosystem=['cargo'], cargo_offline=True,
                               closure={'evaluator_replace_paths':'/unexpected'}))


def test_raw_cache_copies_matching_index_sources_and_git_without_vendor_config():
    policy = dict(ecosystem=['cargo'], cargo_offline=True,
                  cache_forbid_globs=['/usr/local/cargo/registry/src/*/nu-*-0.10[6-9].*'],
                  closure=dict(cargo_mechanism='raw-cache',
                               cache_paths=['/usr/local/cargo/registry/cache']))
    before = copy.deepcopy(policy)
    result = evaluation_policy(policy)
    assert result['closure']['evaluator_replace_paths'] == [
        '/usr/local/cargo/registry/index', '/usr/local/cargo/registry/src', '/usr/local/cargo/git']
    assert result['closure']['cache_paths'] == before['closure']['cache_paths']
    assert result['cache_forbid_globs'] == before['cache_forbid_globs']
    assert policy == before and evaluation_policy(result) == result


def test_runtime_checks_repaired_recipe_and_rejects_old_image_binding():
    from ctxpress.benchmarks.milestone.images import PreparedImages
    prepared = PreparedImages.__new__(PreparedImages)
    parent, agent, repaired, old = ('sha256:' + x * 64 for x in '1234')
    prepared.repo = 'fixture'
    prepared.base = {'M1': parent}
    prepared.agent = {'id': agent}
    prepared.grading = {'M1': {'id': repaired}}
    calls = []
    def native(**kwargs):
        calls.append(kwargs)
        return repaired, parent[7:], agent[7:], repaired[7:]
    prepared.native = native
    policy = dict(ecosystem=['cargo'], cargo_offline=True, closure={})
    kwargs = dict(repo_name='fixture', milestone_id='M1', milestone_image=parent,
                  quarantine_config=policy, expected_closure_image_id=agent)
    prepared.overlay(**kwargs)
    assert calls[0]['quarantine_config'] == evaluation_policy(policy)
    assert policy['closure'] == {}
    prepared.grading['M1']['id'] = old
    with pytest.raises(ValueError, match='differs from its prepared image binding'):
        prepared.overlay(**kwargs)
