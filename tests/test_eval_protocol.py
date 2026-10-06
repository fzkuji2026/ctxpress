"""Protocol-to-config integration; local synthetic inputs, no model or Docker."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from ctxpress import benchmarks
from ctxpress.__main__ import main
from ctxpress.harness import eval_plan, eval_protocol
from ctxpress.core.params import DEFAULT
from ctxpress.replay.tune import tune
from test_eval_families import NAMES, fixture_data
from test_model_accounting import rates
from test_policy import history


PROJECT = Path(__file__).resolve().parents[1]


def inputs(tmp_path, benchmark='swe-bench-verified'):
    configs = tmp_path/'configs'; configs.mkdir()
    protocol = json.loads((PROJECT/'configs/experiment.protocol.json').read_text(encoding='utf-8'))
    for row in protocol['families']:
        shutil.copyfile(PROJECT/'configs'/row['template'], configs/row['template'])
    root = fixture_data(tmp_path, benchmark)
    identifiers = [item['id'] for item in benchmarks.get(benchmark).task_instances(root)]
    row = next(item for item in protocol['families'] if item['benchmark'] == benchmark)
    row['pilot_tasks'] = identifiers[:1]
    row['comparison_tasks'] = identifiers
    row['comparison_task_count'] = len(identifiers)
    binary = tmp_path/'bin'; binary.mkdir(); (binary/'codex').write_bytes(b'synthetic CLI fixture')
    source = configs/'protocol.json'; source.write_text(json.dumps(protocol), encoding='utf-8')
    return source, root, binary, row['family']


def candidate(tmp_path):
    directory = tmp_path/'runs/prepared'; directory.mkdir(parents=True)
    path = directory/'cost-policy.json'
    training = json.loads((tmp_path/'configs/protocol.json').read_text(encoding='utf-8'))['comparison']['candidate_training']
    tune([history('retro'), history('payments')], path, training['lambda_grid'],
         params=DEFAULT.override(training['params']), method_args=training['args'],
         kind=training['constraint'].removesuffix(' simulated proxy'),
         reference={'class': 'CodexAutoCompact', 'args': {'t': training['reference_limit']}})
    sources = []
    for session, task_id in [('retro', 'retro-console-soc'), ('payments', 'payments-pipeline-fix')]:
        raw = directory/(session+'.json'); raw.write_text(json.dumps(history(session)), encoding='utf-8')
        sources.append(dict(session=session, task_id=task_id, file=raw.name, sha256=eval_plan.file_sha256(raw)))
    # This is deliberately a synthetic provenance fixture. These declarations
    # test bindings and exclusion checks, not whether a corpus is genuinely real.
    proof = dict(schema='ctxpress.cost-policy.provenance', version=1, synthetic_inputs=False,
                 policy_sha256=json.loads(path.read_text(encoding='utf-8'))['sha256'], policy_file_sha256=eval_plan.file_sha256(path), sources=sources,
                 task_ids_excluded_from_evaluation=['retro-console-soc', 'payments-pipeline-fix'],
                 all_navidrome_histories_excluded=True)
    proof_path = directory/'cost-policy.provenance.json'; proof_path.write_text(json.dumps(proof), encoding='utf-8')
    return path, proof_path


@pytest.mark.parametrize('benchmark', NAMES)
@pytest.mark.parametrize('phase', ['pilot', 'method-pilot', 'comparison'])
def test_protocol_compiles_each_family_preserving_cohort_methods_and_grading(tmp_path, benchmark, phase):
    source, root, binary, family = inputs(tmp_path, benchmark)
    if phase == 'comparison':
        candidate(tmp_path)
    cfg, receipt, plan = eval_protocol.configure(source, family=family, phase=phase, data=root, bindir=binary)
    protocol = json.loads(source.read_text(encoding='utf-8'))
    row = next(item for item in protocol['families'] if item['family'] == family)
    cohort = 'pilot' if phase == 'method-pilot' else phase
    stage = protocol['method_pilot' if phase == 'method-pilot' else phase]
    assert cfg['tasks'] == row[cohort+'_tasks'] and cfg['model'] == protocol['models']['agent']
    assert cfg['reasoning'] == protocol['models']['agent_reasoning']
    repeats = row.get('comparison_repeats', stage['repeats']) if phase == 'comparison' else stage['repeats']
    assert cfg['repeats'] == repeats and cfg['workers'] == stage['workers']
    assert cfg['run']['max_calls'] == 100 and cfg['run']['timeout'] == 1800 and cfg['run']['grade']
    assert receipt['run_count'] == len(cfg['tasks']) * len(cfg['methods']) * cfg['repeats']
    assert receipt['sources'][str(source)] == eval_plan.file_sha256(source)
    assert receipt['config_sha256'] == hashlib.sha256(eval_plan.canonical(cfg).encode()).hexdigest()
    assert not receipt['resources_complete'] and not receipt['spending_authorized']
    assert receipt['experiments_started'] == receipt['downloads_started'] == 0
    assert len(receipt['task_hashes']) == len(cfg['tasks'])
    if phase == 'comparison':
        assert len(cfg['methods']) == 13
        frozen = next(item for item in cfg['methods'] if item['class'] == 'AutoCostModel')
        assert Path(frozen['args']['policy']).is_absolute()
        assert next(item for item in cfg['methods'] if item['class'] == 'AgentDiet')['args']['reflect_model'] == 'gpt-6-luna'
    elif phase == 'method-pilot':
        indexed = {entry['class']: entry for entry in protocol['comparison']['methods']}
        assert cfg['methods'] == [indexed[name] for name in stage['method_classes']]
        assert len(cfg['methods']) == 7 and len(cfg['tasks']) == 1
        assert all(entry['class'] != 'AutoCostModel' for entry in cfg['methods'])
        assert next(entry for entry in cfg['methods'] if entry['class'] == 'AgentDiet')['args']['reflect_model'] == 'gpt-6-luna'
    else:
        assert cfg['methods'] == [{'class': 'NoCompaction', 'args': {}}]
    if benchmark == 'bigcodebench':
        assert cfg['run']['code']['pass_k'] == [1, 5, 10] and cfg['run']['code']['calibrated']
        assert cfg['run']['grading_timeout'] == 1800
    # The resulting ordinary eval configuration is usable even from a different
    # output directory; neither policy nor data paths are relative to that file.
    again = eval_plan.compile_plan(cfg, tmp_path/'elsewhere')
    assert again['sha256'] == plan['sha256']


@pytest.mark.parametrize('change,message', [
    ('missing', 'freeze exact'), ('duplicate', 'unique explicit'), ('excluded', 'excluded training'),
    ('wrong-family', 'declared family'), ('missing-family', 'each of the eight'),
    ('pilot-outside', 'comparison cohort'), ('bool-version', 'task-start protocol'),
    ('wrong-count', 'task count'), ('bool-seed', 'selection seed')])
def test_invalid_cohorts_and_family_scope_fail_before_writing(tmp_path, change, message):
    source, root, binary, family = inputs(tmp_path)
    protocol = json.loads(source.read_text(encoding='utf-8')); row = next(r for r in protocol['families'] if r['family'] == family)
    if change == 'missing': row['pilot_tasks'] = None
    if change == 'duplicate': row['pilot_tasks'] *= 2
    if change == 'excluded': row['pilot_tasks'] = ['retro-console-soc']
    if change == 'wrong-family': row['benchmark'] = 'deep-swe'
    if change == 'missing-family': protocol['families'].pop()
    if change == 'pilot-outside': row['pilot_tasks'] = ['not-in-cohort']
    if change == 'bool-version': protocol['version'] = True
    if change == 'wrong-count': row['comparison_task_count'] += 1
    if change == 'bool-seed': protocol['selection']['seed'] = True
    source.write_text(json.dumps(protocol), encoding='utf-8'); out = tmp_path/'generated.json'
    with pytest.raises(ValueError, match=message):
        eval_protocol.write(source, out, family=family, phase='pilot', data=root, bindir=binary)
    assert not out.exists() and not out.with_name(out.name+'.provenance.json').exists()


@pytest.mark.parametrize('change,message', [
    ('synthetic', 'non-synthetic'), ('hash', 'non-synthetic'), ('source', 'training source has changed'),
    ('sessions', 'different training sessions'), ('leak', 'overlaps'), ('navidrome', 'Navidrome'),
    ('reflection', 'reflection model differs'), ('missing-proof', 'candidate provenance'),
    ('file-hash', 'declared file hash')])
def test_comparison_requires_bound_training_candidate_and_explicit_reflection(tmp_path, change, message):
    source, root, binary, family = inputs(tmp_path)
    _, proof_path = candidate(tmp_path)
    proof = json.loads(proof_path.read_text(encoding='utf-8')); protocol = json.loads(source.read_text(encoding='utf-8'))
    if change == 'synthetic': proof['synthetic_inputs'] = True
    if change == 'hash': proof['policy_sha256'] = '0'*64
    if change == 'file-hash': proof['policy_file_sha256'] = '0'*64
    if change == 'source': (proof_path.parent/proof['sources'][0]['file']).write_text('changed', encoding='utf-8')
    if change == 'sessions': protocol['comparison']['candidate_training']['sessions'] = ['other', 'payments']
    if change == 'leak': proof['sources'][0]['task_id'] = next(r for r in protocol['families'] if r['family'] == family)['comparison_tasks'][0]
    if change == 'navidrome': proof['all_navidrome_histories_excluded'] = False
    if change == 'reflection': next(r for r in protocol['comparison']['methods'] if r['class'] == 'AgentDiet')['args']['reflect_model'] = 'wrong'
    if change == 'missing-proof': protocol['comparison']['required_candidate'].pop('provenance')
    proof_path.write_text(json.dumps(proof), encoding='utf-8'); source.write_text(json.dumps(protocol), encoding='utf-8')
    with pytest.raises(ValueError, match=message):
        eval_protocol.configure(source, family=family, phase='comparison', data=root, bindir=binary)


def test_legacy_provenance_content_hash_is_accepted_and_raw_file_hash_is_recorded(tmp_path):
    source, root, binary, family = inputs(tmp_path)
    path, proof_path = candidate(tmp_path)
    proof = json.loads(proof_path.read_text(encoding='utf-8')); proof.pop('policy_file_sha256')
    proof_path.write_text(json.dumps(proof), encoding='utf-8')
    _, receipt, _ = eval_protocol.configure(source, family=family, phase='comparison', data=root, bindir=binary)
    assert receipt['sources'][str(path)] == eval_plan.file_sha256(path)


@pytest.mark.parametrize('phase,count', [('pilot', 1), ('method-pilot', 7)])
def test_uniform_cli_writes_provenance_and_preserves_unknown_cost(tmp_path, capsys, phase, count):
    source, root, binary, family = inputs(tmp_path)
    prices = tmp_path/'prices.json'; prices.write_text(json.dumps({'models': {'gpt-6.1-sol': rates()}}), encoding='utf-8')
    output = tmp_path/'proposals/pilot.json'
    main(['eval', 'configure', str(source), '--family', family, '--phase', phase, '--data', str(root),
          '--bindir', str(binary), '--prices', str(prices), '--output', str(output)])
    result = json.loads(capsys.readouterr().out)
    cfg = json.loads(output.read_text(encoding='utf-8')); receipt = json.loads(Path(result['provenance']).read_text(encoding='utf-8'))
    assert cfg['prices'] == json.loads(prices.read_text(encoding='utf-8')) and result['run_count'] == count
    assert receipt['sources'][str(prices)] == eval_plan.file_sha256(prices)
    main(['eval', 'plan', str(output), '--output', str(tmp_path/'plan.json')])
    assert json.loads(capsys.readouterr().out)['run_count'] == count
    original = output.read_bytes()
    with pytest.raises(ValueError, match='already exists'):
        eval_protocol.write(source, output, family=family, phase=phase, data=root, bindir=binary)
    assert output.read_bytes() == original


@pytest.mark.parametrize('change,message', [
    ('missing-stage', 'method_pilot stage'), ('empty', 'unique explicit'),
    ('duplicate', 'unique explicit'), ('unknown', 'comparison baselines only'),
    ('candidate', 'comparison baselines only'), ('missing-control', 'NoCompaction'),
    ('duplicate-baseline', 'unique declared comparison'), ('reflection', 'reflection model differs')])
def test_method_pilot_rejects_unfrozen_methods_before_writing(tmp_path, change, message):
    source, root, binary, family = inputs(tmp_path)
    protocol = json.loads(source.read_text(encoding='utf-8'))
    stage = protocol['method_pilot']
    if change == 'missing-stage': del protocol['method_pilot']
    if change == 'empty': stage['method_classes'] = []
    if change == 'duplicate': stage['method_classes'].append(stage['method_classes'][0])
    if change == 'unknown': stage['method_classes'].append('InventedMethod')
    if change == 'candidate': stage['method_classes'].append('AutoCostModel')
    if change == 'missing-control': stage['method_classes'].remove('NoCompaction')
    if change == 'duplicate-baseline': protocol['comparison']['methods'].append(protocol['comparison']['methods'][0])
    if change == 'reflection':
        next(entry for entry in protocol['comparison']['methods'] if entry['class'] == 'AgentDiet')['args']['reflect_model'] = 'wrong'
    source.write_text(json.dumps(protocol), encoding='utf-8'); output = tmp_path/'method-pilot.json'
    with pytest.raises(ValueError, match=message):
        eval_protocol.write(source, output, family=family, phase='method-pilot', data=root, bindir=binary)
    assert not output.exists() and not output.with_name(output.name+'.provenance.json').exists()


def test_configuration_cannot_overwrite_input_or_write_inside_dataset(tmp_path):
    source, root, binary, family = inputs(tmp_path)
    original = source.read_bytes()
    for destination in (source, root/'new-config.json', binary/'codex'):
        with pytest.raises(ValueError, match='outside their input'):
            eval_protocol.write(source, destination, family=family, phase='pilot', data=root, bindir=binary)
    assert source.read_bytes() == original and not (root/'new-config.json').exists()


@pytest.mark.parametrize('benchmark', NAMES)
def test_family_comparison_repeats_are_explicit_and_do_not_leak_to_other_families(tmp_path, benchmark):
    source, root, binary, family = inputs(tmp_path, benchmark)
    candidate(tmp_path)
    protocol = json.loads(source.read_text(encoding='utf-8'))
    selected = next(row for row in protocol['families'] if row['family'] == family)
    other = next(row for row in protocol['families'] if row['family'] != family)
    selected['comparison_repeats'] = 1
    other['comparison_repeats'] = 2
    source.write_text(json.dumps(protocol), encoding='utf-8')
    cfg, receipt, plan = eval_protocol.configure(source, family=family, phase='comparison', data=root, bindir=binary)
    assert cfg['repeats'] == 1 and {job['repeat'] for job in plan['jobs']} == {0}
    assert plan['run_count'] == len(cfg['tasks']) * 13
    assert receipt['agent_timeout_seconds_upper_bound'] == plan['run_count'] * cfg['run']['timeout']
    assert cfg['tasks'] == selected['comparison_tasks'] and cfg['run']['max_calls'] == 100
    assert receipt['sources'][str(source)] == eval_plan.file_sha256(source)
    del selected['comparison_repeats']
    source.write_text(json.dumps(protocol), encoding='utf-8')
    default, _, default_plan = eval_protocol.configure(source, family=family, phase='comparison', data=root, bindir=binary)
    assert default['repeats'] == protocol['comparison']['repeats'] == 3
    assert default_plan['run_count'] == plan['run_count'] * 3
    assert default['methods'] == cfg['methods'] and default['run'] == cfg['run']


@pytest.mark.parametrize('phase', ['pilot', 'method-pilot'])
def test_comparison_repeat_override_does_not_change_pilot_budget(tmp_path, phase):
    source, root, binary, family = inputs(tmp_path)
    protocol = json.loads(source.read_text(encoding='utf-8'))
    next(row for row in protocol['families'] if row['family'] == family)['comparison_repeats'] = 7
    source.write_text(json.dumps(protocol), encoding='utf-8')
    cfg, _, plan = eval_protocol.configure(source, family=family, phase=phase, data=root, bindir=binary)
    assert cfg['repeats'] == 1 and {job['repeat'] for job in plan['jobs']} == {0}
    assert plan['run_count'] == (7 if phase == 'method-pilot' else 1)


@pytest.mark.parametrize('value', [0, -1, True, None, '2', 1.5])
def test_invalid_family_repeat_override_rejected_before_writing(tmp_path, value):
    source, root, binary, family = inputs(tmp_path)
    protocol = json.loads(source.read_text(encoding='utf-8'))
    next(row for row in protocol['families'] if row['family'] == family)['comparison_repeats'] = value
    source.write_text(json.dumps(protocol), encoding='utf-8'); output = tmp_path / 'invalid.json'
    with pytest.raises(ValueError, match='comparison_repeats'):
        eval_protocol.write(source, output, family=family, phase='pilot', data=root, bindir=binary)
    assert not output.exists()
