"""Process evaluation: request partition, cache-break causes, self-checks and the three inputs (log, evaluation, report)."""
import hashlib, json, sqlite3

import pytest
from ctxpress.live import usage as eval_usage
from ctxpress.harness.results import analysis as run_analysis
from ctxpress.__main__ import main as cli

PRICES = {"models": {"m": {"input": 2.0, "cached": 0.1, "cache_write": 2.5, "output": 10.0,
                           "unit": "USD_per_million_tokens", "source": "fixture", "as_of": "2026-10-06",
                           "long_context": {"input_tokens_threshold": 1000, "input": 4.0, "cached": 0.2, "cache_write": 5.0, "output": 15.0}}}}


def row(n, inp, read, out=10, session="s", **extra):
    return dict(dict(request=n, session=session, status=200, model="m", seconds=1.0, tokens_before=inp, tokens_after=inp,
                     changed=0, dropped=0, operations={}, usage=dict(input_tokens=inp, cached_tokens=read,
                     cache_write_tokens=0, output_tokens=out, reasoning_tokens=0)), **extra)


def kept(edited, host=False):
    return dict(prefix=dict(previous_items=4, kept_items=2 if edited else 4, kept_tokens=100, edited=edited),
                host_prefix=dict(previous_items=4, kept_items=1 if host else 4, edited=host))


def test_inferred_causes_for_logs_without_process_telemetry():
    rows = [row(1, 3000, 0), row(2, 5000, 2900),                       # prefix cached
            row(3, 6000, 500),                                          # provider miss: no edit
            row(4, 4000, 300, operations={"mask": 2}, changed=2),       # method edit
            row(5, 4200, 3900, changed=2),                              # cached again, no new edit
            row(6, 4400, 0, history_rebased=True)]                      # the host revised history
    table = run_analysis.request_table(rows, PRICES)
    assert [r["cache_break"] for r in table] == [False, False, True, True, False, True]
    assert [r["cause"] for r in table] == [None, "provider", "provider", "method", "provider", "host"]
    assert not any(r["measured"] for r in table)
    assert table[2]["cacheable"] == 5000 and table[2]["cache_miss"] == 4500
    assert table[3]["miss_cost"] == pytest.approx(3700 * (4.0 - 0.2) / 1e6)     # long-context tier
    metrics, _ = run_analysis.job_metrics(rows, [], PRICES, {"seconds": 9.0})
    cache = metrics["cache"]
    assert (cache["breaks"], cache["breaks_method"], cache["breaks_host"], cache["breaks_provider"]) == (3, 1, 1, 1)
    assert metrics["activity"]["edit_requests"] == 1 and metrics["activity"]["first_edit_request"] == 4


def test_measured_causes_override_inference():
    rows = [row(1, 3000, 0, prefix=None, host_prefix=None),
            row(2, 5000, 0, operations={"mask": 1}, **kept(False)),     # an operation, but the sent prefix is intact
            row(3, 5200, 0, **kept(True)),                              # the method changed what it sent
            row(4, 5400, 0, **kept(True, host=True))]                   # the host changed history first
    table = run_analysis.request_table(rows, PRICES)
    assert [r["cause"] for r in table] == [None, "provider", "method", "host"] and all(r["measured"] for r in table)
    assert run_analysis.job_metrics(rows, [], PRICES, {})[0]["cache"]["measured_share"] == 1.0


def test_sessions_are_separate_and_failed_or_stream_error_requests_are_not_priced():
    rows = [row(1, 3000, 0, session="a"), row(1, 3000, 0, session="b"), row(2, 3500, 2900, session="a"),
            dict(row(3, 0, 0, session="a"), status=503, usage=None), row(4, 3600, 3400, session="a", stream_error="cut")]
    table = run_analysis.request_table(rows, PRICES)
    assert not any(r["cache_break"] for r in table)
    metrics, _ = run_analysis.job_metrics(rows, [], PRICES, {})
    assert metrics["efficiency"]["main_requests"] == 3 and metrics["efficiency"]["failed_requests"] == 2


def test_cost_parts_and_self_check_agree_with_the_eval_usage_bill():
    main = [row(1, 800, 0), row(2, 1500, 700, out=40), row(3, 1600, 1400)]
    aux = [dict(type="summary", purpose="segment", status=200, model="m", seconds=2.0,
                usage=dict(input_tokens=900, cached_tokens=0, cache_write_tokens=0, output_tokens=200))]
    metrics, _ = run_analysis.job_metrics(main, aux, PRICES, {})
    bill = eval_usage.bill(eval_usage.analyze(dict(model="m", requests=3, rewrites=main + aux, usage=dict(summary_calls=1))), PRICES)
    assert metrics["cost"]["total"] == pytest.approx(bill["api_cost_at_declared_rates_usd"])
    assert metrics["checks"]["cost_parity"] is True and not metrics["checks"]["hard"]
    assert sum(metrics["cost"]["parts"].values()) == pytest.approx(metrics["cost"]["total"])
    assert metrics["cost"]["aux"] == pytest.approx((900 * 2.0 + 200 * 10.0) / 1e6)


def test_self_checks_flag_gaps_and_estimate_drift():
    rows = [row(1, 3000, 0, tokens_after=300), row(3, 3100, 2900, tokens_after=310)]   # request 2 never logged
    checks = run_analysis.job_metrics(rows, [], PRICES, {}, parse_errors=1)[0]["checks"]
    assert checks["request_gaps"] == 1 and checks["parse_errors"] == 1 and len(checks["hard"]) == 2
    assert checks["tokens_per_estimate"]["median"] == pytest.approx(10.0) and checks["warnings"]


def test_a_request_log_or_run_directory_is_analyzed_without_task_quality(tmp_path):
    run = tmp_path / "run"; run.mkdir()
    (run / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [row(1, 800, 0), row(2, 900, 700)]), encoding="utf-8")
    analysis, tables = run_analysis.analyze(run, prices=PRICES)
    family = analysis["families"][0]
    assert analysis["source"] == "log" and family["methods"][0]["usable"] == 1 and family["paired"] == []
    out = tmp_path / "out"
    assert cli(["analyze", str(run / "requests.jsonl"), "--output", str(out), "--no-figures"]) is None
    assert (out / "report.md").exists() and (out / "requests.csv").read_text(encoding="utf-8").count("\n") == 3


def _evaluation(tmp_path, jobs):
    directory = tmp_path / "eval"; directory.mkdir()
    db = sqlite3.connect(directory / "jobs.sqlite")
    db.execute("CREATE TABLE metadata (key TEXT, value TEXT)")
    db.execute("CREATE TABLE jobs (id TEXT, status TEXT, spec TEXT, result TEXT)")
    db.execute("INSERT INTO metadata VALUES ('plan', ?)", (json.dumps(dict(benchmark=dict(name="fam"), config=dict(prices=PRICES))),))
    for job_id, label, task, status, rows, resolved in jobs:
        result = None if rows is None else json.dumps(dict(model="m", requests=len(rows), rewrites=rows, seconds=5.0,
                                                           grade=dict(resolved=resolved, infra_invalid=False)))
        db.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job_id, status, json.dumps(dict(label=label, repeat=0, task=dict(id=task))), result))
    db.commit(); db.close()
    return directory


def test_an_evaluation_directory_uses_official_outcomes_and_pairs_against_no_compaction(tmp_path):
    base = [row(1, 800, 0), row(2, 900, 700)]
    edited = [row(1, 800, 0), row(2, 600, 0, operations={"drop": 1}, dropped=1)]
    directory = _evaluation(tmp_path, [("a", "no-compaction", "t0", "completed", base, True),
                                       ("b", "edit", "t0", "completed", edited, False),
                                       ("c", "edit", "t1", "pending", None, None)])
    analysis, _ = run_analysis.analyze(directory)
    family = analysis["families"][0]
    assert analysis["source"] == "evaluation" and family["reference"] == "no-compaction"
    jobs = {j["job_id"]: j for j in family["jobs"]}
    assert jobs["a"]["quality"] == 1.0 and jobs["b"]["quality"] == 0.0 and jobs["c"]["not_run"]
    pair = family["paired"][0]
    assert pair["pairs"] == 1 and pair["quality_delta"] == -1.0
    assert pair["cost"]["pooled_ratio"] == pytest.approx(jobs["b"]["cost"]["total"] / jobs["a"]["cost"]["total"])
    assert family["methods"][1]["not_run"] == 1


def _report_job(tmp_path, job_id, method, task, rows, cost, status="completed"):
    folder = tmp_path / "run" / "jobs" / job_id / "attempt-1"
    (folder / "agent").mkdir(parents=True)
    log = folder / "agent" / "ctxpress-requests.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    result = folder / "result.json"
    result.write_text(json.dumps({"seconds": 10.0, "execution_health": {"tool_calls": len(rows)}}), encoding="utf-8")
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    return dict(job_id=job_id, method=method, task_id=task, repeat=0, status=status, quality_admissible=True,
                metrics={"resolved": True}, api_cost_usd=cost, api_cost_complete=cost is not None,
                artifacts=[dict(path=str(result).replace("\\", "/"), sha256=digest(result)),
                           dict(path=str(log).replace("\\", "/"), sha256=digest(log))])


def _report(tmp_path, jobs):
    detail = tmp_path / "detail.json"
    detail.write_text(json.dumps(dict(reference="base", directory=str(tmp_path / "run"), prices=PRICES, jobs=jobs)), encoding="utf-8")
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(dict(families=[dict(benchmark="fam", detail_artifact=str(detail),
                       detail_sha256=hashlib.sha256(detail.read_bytes()).hexdigest())])), encoding="utf-8")
    return summary


def price(rows):
    return run_analysis.job_metrics(rows, [], PRICES, {})[0]["cost"]["total"]


def test_a_report_pairs_usable_jobs_and_keeps_excluded_and_unrun_jobs_visible(tmp_path):
    base = [row(1, 800, 0), row(2, 900, 700)]
    edited = [row(1, 800, 0), row(2, 600, 0, operations={"drop": 1}, dropped=1)]
    jobs = [_report_job(tmp_path, "m0-t0", "base", "t0", base, price(base)), _report_job(tmp_path, "m0-t1", "base", "t1", base, price(base)),
            _report_job(tmp_path, "m1-t0", "edit", "t0", edited, price(edited)),
            _report_job(tmp_path, "m1-t1", "edit", "t1", edited, price(edited)),
            dict(_report_job(tmp_path, "m2-t0", "late", "t0", base, None), status="pending", artifacts=[])]
    summary = _report(tmp_path, jobs)
    eligibility = tmp_path / "eligibility.json"
    eligibility.write_text(json.dumps(dict(report=dict(sha256=hashlib.sha256(summary.read_bytes()).hexdigest()),
                           jobs=[dict(benchmark="fam", job_id="m1-t1", exclude_from_method_effect_comparison=True)])), encoding="utf-8")
    analysis, tables = run_analysis.analyze(summary, eligibility)
    fam = analysis["families"][0]
    byid = {j["job_id"]: j for j in fam["jobs"]}
    assert analysis["source"] == "report" and all(byid[k]["cost_matches_report"] for k in ("m0-t0", "m1-t0"))
    assert byid["m1-t1"]["excluded"] and byid["m2-t0"]["not_run"] and not byid["m2-t0"]["problems"]
    pair = {p["method"]: p for p in fam["paired"]}
    assert pair["edit"]["pairs"] == 1 and pair["late"]["pairs"] == 0
    assert pair["edit"]["cost"]["pooled_ratio"] == pytest.approx(price(edited) / price(base))
    methods = {m["method"]: m for m in fam["methods"]}
    assert methods["edit"]["usable"] == 1 and methods["edit"]["excluded"] == 1 and methods["late"]["not_run"] == 1
    run_analysis.write(analysis, tables, tmp_path / "out", figures=False)
    assert "| edit | 1/2 |" in (tmp_path / "out" / "report.md").read_text(encoding="utf-8")


def test_changed_artifacts_and_cost_disagreement_are_problems_not_data(tmp_path):
    base = [row(1, 800, 0)]
    jobs = [_report_job(tmp_path, "m0-t0", "base", "t0", base, price(base)), _report_job(tmp_path, "m1-t0", "other", "t0", base, 99.0)]
    log = tmp_path / "run" / "jobs" / "m0-t0" / "attempt-1" / "agent" / "ctxpress-requests.jsonl"
    summary = _report(tmp_path, jobs)
    log.write_text(log.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    fam = run_analysis.analyze(summary)[0]["families"][0]
    problems = {j["job_id"]: j["problems"] for j in fam["jobs"]}
    assert any("changed since the report" in p for p in problems["m0-t0"])
    assert any("!= report" in p for p in problems["m1-t0"])
    assert all(not run_analysis._usable(j) for j in fam["jobs"])


def test_eligibility_from_another_report_is_refused(tmp_path):
    summary = _report(tmp_path, [_report_job(tmp_path, "m0-t0", "base", "t0", [row(1, 800, 0)], price([row(1, 800, 0)]))])
    other = tmp_path / "eligibility.json"
    other.write_text(json.dumps(dict(report=dict(sha256="0" * 64), jobs=[])), encoding="utf-8")
    with pytest.raises(ValueError, match="another report"):
        run_analysis.analyze(summary, other)


def test_context_follows_the_primary_session_while_cost_covers_every_session():
    rows = [row(1, 1000, 0), row(2, 2000, 900), row(3, 3000, 1900), row(1, 9000, 0, session="title")]
    metrics, _ = run_analysis.job_metrics(rows, [], PRICES, {})
    assert metrics["efficiency"]["sessions"] == 2 and metrics["efficiency"]["side_session_requests"] == 1
    assert metrics["context"]["api_input"]["max"] == 3000 and metrics["context"]["api_input"]["n"] == 3
    assert metrics["tokens"]["main"]["input"] == 15000
