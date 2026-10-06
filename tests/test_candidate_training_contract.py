"""A valid policy seal is insufficient when it violates the experiment protocol."""
import copy
import json
import pytest
from ctxpress.core import policy
from ctxpress.harness.jobs import protocol as eval_protocol
from test_eval_protocol import inputs, candidate


@pytest.mark.parametrize('field', ['lambda_grid', 'constraint', 'reference_limit', 'args', 'params'])
def test_valid_retrained_policy_with_matching_provenance_cannot_change_protocol(tmp_path, field):
    source, root, binary, family = inputs(tmp_path)
    original = source.read_text(encoding='utf-8')
    document = json.loads(original)
    training = document['comparison']['candidate_training']
    if field == 'lambda_grid': training[field] = [0, 100000]
    elif field == 'constraint': training[field] = 'harm simulated proxy'
    elif field == 'reference_limit': training[field] = 210000
    elif field == 'args': training[field]['lookahead'] = 4
    elif field == 'params': training[field]['cached'] = .2
    source.write_text(json.dumps(document), encoding='utf-8')
    path, _ = candidate(tmp_path)
    policy.load(path)  # Internally valid, newly sealed, provenance updated by fixture.
    source.write_text(original, encoding='utf-8')
    with pytest.raises(ValueError, match='candidate_training.' + field):
        eval_protocol.configure(source, family=family, phase='comparison', data=root, bindir=binary)


def test_equivalent_defaults_aliases_and_numeric_spelling_are_accepted(tmp_path):
    source, root, binary, family = inputs(tmp_path)
    path, _ = candidate(tmp_path)
    original_policy = path.read_bytes()
    document = json.loads(source.read_text(encoding='utf-8'))
    training = document['comparison']['candidate_training']
    training['lambda_grid'] = [float(n) for n in reversed(training['lambda_grid'])]
    training['constraint'] = 'strict'
    training['args']['truncate_budget'] = 2000
    training['args']['allow_placeholder'] = True
    source.write_text(json.dumps(document), encoding='utf-8')
    cfg, _, _ = eval_protocol.configure(source, family=family, phase='comparison', data=root, bindir=binary)
    assert len(cfg['methods']) == 13 and path.read_bytes() == original_policy


@pytest.mark.parametrize('field', ['lambda_grid','constraint','reference_limit','args','params'])
def test_missing_training_declaration_is_rejected(tmp_path, field):
    source, root, binary, family = inputs(tmp_path)
    candidate(tmp_path)
    document = json.loads(source.read_text(encoding='utf-8'))
    del document['comparison']['candidate_training'][field]
    source.write_text(json.dumps(document), encoding='utf-8')
    with pytest.raises(ValueError, match='must declare ' + field):
        eval_protocol.configure(source, family=family, phase='comparison', data=root, bindir=binary)


def test_validation_does_not_mutate_training_or_policy(tmp_path):
    source, _, _, _ = inputs(tmp_path)
    path, _ = candidate(tmp_path)
    training = json.loads(source.read_text(encoding='utf-8'))['comparison']['candidate_training']
    bundle = policy.load(path)
    saved = copy.deepcopy((training,bundle))
    eval_protocol._candidate_training(training,bundle)
    assert (training,bundle) == saved
