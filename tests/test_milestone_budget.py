"""Cumulative native budgets across sessions, partial tool output and idle waits."""
import json,signal,sys,time
import pytest
from ctxpress.benchmarks.milestone import budget


def append(path,kind,identity,complete=True):
    path.parent.mkdir(parents=True,exist_ok=True)
    row=dict(type='response_item',payload={'type':kind,'call_id':identity})
    with path.open('a', encoding='utf-8') as stream:stream.write(json.dumps(row)+('\n' if complete else ''))


def test_budget_counts_all_sessions_but_waits_for_completed_tool_outputs(tmp_path):
    first=tmp_path/'sessions/first.jsonl';second=tmp_path/'sessions/resumed.jsonl'
    current=budget.Budget(first.parent,60,2)
    append(first,'function_call','a');append(first,'function_call_output','a')
    assert current.poll() is None and current.count==1
    append(second,'custom_tool_call','b');append(second,'custom_tool_call_output','b',complete=False)
    assert current.poll() is None and current.count==2 and not current.complete
    with second.open('a', encoding='utf-8') as stream:stream.write('\n')
    assert current.poll()=='max_calls' and current.complete
    with pytest.raises(budget.Limit):current.remaining_ms(60000)
    current.write(tmp_path/'audit.json')
    assert json.loads((tmp_path/'audit.json').read_text(encoding='utf-8'))['tool_calls']==2


def test_resuming_never_resets_wall_time_or_overrides_a_shorter_native_timeout(tmp_path,monkeypatch):
    now=[100.0];monkeypatch.setattr(budget.time,'monotonic',lambda:now[0])
    current=budget.Budget(tmp_path,10,100)
    assert current.remaining_ms(2000)==2000
    now[0]=106.0;assert current.remaining_ms(18000000)==4000
    now[0]=110.0
    with pytest.raises(budget.Limit,match='timeout'):current.remaining_ms(2000)


def test_preparation_time_does_not_consume_deferred_budget(tmp_path,monkeypatch):
    now=[100.0];monkeypatch.setattr(budget.time,'monotonic',lambda:now[0])
    current=budget.Budget(tmp_path/'sessions',10,100,defer_start=True)
    now[0]=500.0;assert current.poll() is None
    current.write(tmp_path/'audit.json')
    audit=json.loads((tmp_path/'audit.json').read_text(encoding='utf-8'))
    assert audit['budget_started'] is False and audit['elapsed_seconds']==0
    assert current.remaining_ms(60000)==10000
    now[0]=506.0;current.begin()
    assert current.remaining_ms(60000)==4000
    now[0]=510.0
    with pytest.raises(budget.Limit,match='timeout'):current.remaining_ms(2000)


@pytest.mark.skipif(sys.platform!='linux',reason='native Linux signal watchdog')
def test_deadline_interrupts_native_idle_wait_and_restores_signal_state(tmp_path):
    before=signal.getsignal(signal.SIGALRM);current=budget.Budget(tmp_path,0.1,100)
    started=time.monotonic()
    with pytest.raises(budget.Limit,match='timeout'):
        with current.watch():time.sleep(5)
    assert time.monotonic()-started<2 and current.reason=='timeout'
    assert signal.getsignal(signal.SIGALRM)==before and signal.getitimer(signal.ITIMER_REAL)==(0.0,0.0)


@pytest.mark.skipif(sys.platform!='linux',reason='native Linux signal watchdog')
def test_tool_budget_interrupts_wait_without_rearming_during_native_cleanup(tmp_path):
    current=budget.Budget(tmp_path,0.3,1);path=tmp_path/'session.jsonl'
    with current.watch():
        append(path,'function_call','a');append(path,'function_call_output','a')
        try:time.sleep(5)
        except budget.Limit:
            # The native runner catches KeyboardInterrupt and does cleanup
            # before returning. A second wall alarm must not kill that cleanup.
            time.sleep(0.4)
    assert current.reason=='max_calls'


@pytest.mark.skipif(sys.platform!='linux',reason='native Linux signal watchdog')
def test_existing_timer_is_not_replaced(tmp_path):
    current=budget.Budget(tmp_path,10,10);signal.setitimer(signal.ITIMER_REAL,10)
    try:
        with pytest.raises(ValueError,match='another timer'):
            with current.watch():pass
        assert signal.getitimer(signal.ITIMER_REAL)[0]>0
    finally:signal.setitimer(signal.ITIMER_REAL,0)
