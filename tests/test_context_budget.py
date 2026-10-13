import pytest
from ctxpress.core.context_budget import Action, Budget, choose
from ctxpress.core.context_budget import TokenCalibration
from ctxpress.core.context_budget import ModelCapacity
from ctxpress.core.context_budget import choose_combinations
import time


def combinations(budget,occupied,evaluate,beam=64):
    return choose_combinations(budget,occupied,evaluate,beam_width=beam,
        deadline=time.monotonic()+5,remaining_requests=10,cached_rate=.01,
        input_rate=.1,latency_rate=0,seconds_available=5)


def test_combination_can_fit_when_neither_individual_replacement_fits():
    budget=Budget(1000,100,100,0,100)
    def evaluate(indices):
        return Action(str(indices),1000-150*len(indices))
    result=combinations(budget,[{1},{2}],evaluate)
    assert result['selected_indices']==(0,1)


def test_overlapping_group_and_unit_alternatives_never_apply_twice():
    seen=[]
    def evaluate(indices):
        seen.append(indices)
        return Action(str(indices),800-100*len(indices))
    combinations(Budget(1000,0,0,0,0),[{1},{1,2},{2}],evaluate)
    assert (0,1) not in seen and (1,2) not in seen and (0,1,2) not in seen
    assert (0,2) in seen


def test_shared_cache_cost_can_make_joint_action_profitable():
    def evaluate(indices):
        return Action(str(indices),1000-100*len(indices),
            cache_rewrite_tokens=150 if indices else 0)
    result=combinations(Budget(2000,0,0,0,0),[{1},{2}],evaluate)
    assert result['selected_indices']==(0,1)
    # Each action alone saves 10 but rewrites cost 13.5. Together save 20
    # and still rewrite the common suffix only once.
    assert result['candidates'][0]['key'] is not None


def test_infeasible_combinations_and_search_timeout_are_explicit():
    result=combinations(Budget(100,0,0,0,0),[{1}],lambda ids:Action(str(ids),200))
    assert result['selected_indices'] is None
    expired=choose_combinations(Budget(100,0,0,0,0),[{1}],lambda ids:Action(str(ids),10),
        beam_width=2,deadline=0,remaining_requests=1,cached_rate=0,input_rate=0,
        latency_rate=0,seconds_available=0)
    assert expired['status']=='planning_deadline'


def test_equal_cost_retains_original_instead_of_unnecessary_mutation():
    result=combinations(Budget(1000,0,0,0,0),[{1}],lambda ids:Action(str(ids),500))
    assert result['selected_indices']==()


def test_physical_model_limit_and_requested_output_have_separate_provenance():
    model = ModelCapacity(10000, 2000, 1000, 'explicit test service contract')
    budget, metadata = model.resolve(1500, growth_tokens=200, uncertainty_tokens=100, protected_tokens=300)
    assert budget.working_limit == 8200
    assert metadata['output_source'] == 'request'
    fallback, metadata = model.resolve(None, growth_tokens=200, uncertainty_tokens=100, protected_tokens=300)
    assert fallback.working_limit == 8700
    assert metadata['output_source'] == 'configured_fallback'
    with pytest.raises(ValueError):
        model.resolve(2001, growth_tokens=0, uncertainty_tokens=0, protected_tokens=0)


def test_model_capacity_cannot_be_silently_guessed_or_inconsistent():
    with pytest.raises(ValueError):
        ModelCapacity(1000, 2000, 100, 'test')
    with pytest.raises(ValueError):
        ModelCapacity(1000, 200, 100, '')


def test_token_estimates_use_only_observed_pairs_and_keep_cold_start_envelope():
    calibration = TokenCalibration(prior_ratio=1.2)
    assert calibration.estimate(100)['tokens'] == 120
    assert calibration.observe((1, 0), 100, 80)
    assert not calibration.observe((1, 0), 100, 900)
    estimate = calibration.estimate(100)
    assert estimate['tokens'] == 80
    assert estimate['tokens'] + estimate['uncertainty'] == pytest.approx(120)
    calibration.observe((2, 0), 100, 150)
    assert calibration.estimate(100)['upper_ratio'] == 1.5


def test_calibration_rejects_missing_usage_instead_of_learning_zero():
    calibration = TokenCalibration()
    with pytest.raises(ValueError):
        calibration.observe(1, 100, None)
    assert not calibration.observe(1, 100, 0)
    assert calibration.estimate(100)['samples'] == 0


def decision(budget, actions, **changes):
    args = dict(remaining_requests=10, cached_rate=.01, input_rate=.1,
                latency_rate=0, seconds_available=60)
    args.update(changes)
    return choose(budget, actions, **args)


def test_smaller_physical_window_changes_choice_without_fitting_it():
    actions = [Action('keep', 800), Action('compress', 400, auxiliary_cost=100)]
    assert decision(Budget(1000, 100, 50, 25, 200), actions)['selected'] == 'keep'
    result = decision(Budget(700, 100, 50, 25, 200), actions)
    assert result['selected'] == 'compress'
    assert result['candidates'][0]['reasons'] == ['context_capacity', 'insufficient_reserved_headroom']


def test_output_growth_and_uncertainty_are_additive_not_interchangeable():
    result = decision(Budget(1000, 200, 100, 50, 200), [Action('keep', 700)])
    assert result['input_limit'] == 800
    assert result['working_limit'] == 650
    assert result['selected'] is None
    assert result['candidates'][0]['reasons'] == ['insufficient_reserved_headroom']


def test_recovery_and_cache_breakage_can_outweigh_input_savings():
    budget = Budget(1000, 100, 50, 25, 200)
    keep = Action('keep', 800)
    assert decision(budget, [keep, Action('compress', 400)])['selected'] == 'compress'
    expensive = Action('compress', 400, recovery_cost=10, cache_rewrite_tokens=400)
    assert decision(budget, [keep, expensive])['selected'] == 'keep'


def test_shorter_remaining_task_makes_paid_compression_unprofitable():
    budget = Budget(1000, 100, 50, 25, 200)
    actions = [Action('keep', 800), Action('compress', 400, auxiliary_cost=20)]
    assert decision(budget, actions)['selected'] == 'compress'
    assert decision(budget, actions, remaining_requests=1)['selected'] == 'keep'


def test_impossible_protected_floor_never_drops_required_content():
    result = decision(Budget(1000, 200, 100, 50, 700),
                      [Action('keep', 900), Action('unsafe', 500)])
    assert result['selected'] is None
    assert not result['protected_feasible']
    assert 'below_protected_floor' in result['candidates'][1]['reasons']


def test_capacity_pressure_does_not_override_facts_or_deadline():
    result = decision(Budget(1000, 200, 100, 50, 200),
                      [Action('keep', 900), Action('lossy', 500, preserves_required_facts=False),
                       Action('slow', 500, seconds=61)])
    assert result['status'] == 'no_feasible_action'


@pytest.mark.parametrize('value', [-1, float('inf'), float('nan'), True])
def test_invalid_limits_rejected(value):
    with pytest.raises(ValueError):
        Budget(value, 0, 0, 0, 0)
