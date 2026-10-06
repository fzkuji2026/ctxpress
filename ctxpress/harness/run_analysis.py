"""Process evaluation: one fixed set of efficiency, token, cost, context and cache metrics for any run.

    ctxpress analyze <path> [--eligibility FILE] [--prices FILE] [--reference LABEL] --output DIR

<path> is any of
- a request log (`requests.jsonl`, `ctxpress-requests.jsonl`) or a directory holding one (a `ctxpress codex` /
  `ctxpress claude` run): one job, no task quality; prices from --prices (a protocol/plan JSON or a prices object);
- an evaluation directory (`jobs.sqlite`, read-only): every finished job, official quality as interpreted by
  eval_outcomes, prices from the plan;
- a strict formal report (`summary.json`): its job list, admissibility and recorded artifact hashes; each job's
  hashes are checked and its recomputed cost must equal the report's. --eligibility marks excluded jobs.
Benchmarks score the task; this measures the context process that produced it. Nothing here calls a model, grades,
retries or writes outside --output; missing values stay missing.

Definitions (per job; "main" = the agent's own model requests, "aux" = summary/reflection calls a method makes and
the host's own native compaction calls; a request counts when its status is 2xx without a stream error):
- efficiency: agent seconds, main requests (ok/failed), tool calls, model latency (main + aux + side), rewrite
  overhead, sessions and requests outside the primary (most active) session.
- tokens: input = uncached + cache read + cache write; output; reasoning. Main, aux and side ("passthrough": model
  calls forwarded unchanged) separately; their sum is what eval_usage and reports bill.
- cost: those four parts priced per request at the declared rates (long-context tier by that request's input).
- context (primary session): API input per main request (first, max, mean, median, p90, last, growth); the proxy's
  estimate of history before/after the method and the reduction; mean composition of what was sent by kind.
- cache: read share of input. A cache break is a main request whose cache read falls short of the cacheable prefix
  min(previous input, this input) by >= 1024 tokens and covers < half of it. Its cause is "host" when the host
  changed earlier history, "method" when the method changed what it sent before, else "provider". With process
  telemetry (`prefix`, `host_prefix`) the cause is measured from item digests; older logs infer it (history rebase =
  host; new operations or more changed/dropped items = method). Missed tokens are priced at (input - cached rate).
- method activity: requests whose sent history changed, first such request, operation counts, aux calls by purpose.
Self-checks per job: log parse errors, request-number gaps, ok requests without usage, cost parity with
eval_usage's bill, real-token/estimate ratio, telemetry coverage (and, for reports, artifact hashes and cost parity
with the report). Jobs failing a hard check are listed as problems and left out of summaries.
Paired comparisons use (task, repeat) pairs where both jobs are usable.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, math, sqlite3, statistics
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "ctxpress.process_evaluation"
BREAK_MIN_TOKENS = 1024
BREAK_SHARE = 0.5
PARTS = ("uncached", "cache_read", "cache_write", "output")
TOKEN_KEYS = ("input", "uncached", "cache_read", "cache_write", "output", "reasoning")
CAUSES = ("method", "host", "provider")
ESTIMATE_RATIO = (0.5, 2.0)          # real API tokens per estimated token outside this range is flagged


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _count(value):
    return type(value) is int and value >= 0


def _ok(row):
    return type(row.get("status")) is int and 200 <= row["status"] < 300 and not row.get("stream_error")


def rates(prices, model, input_tokens):
    rate = (prices or {}).get("models", {}).get(model)
    if rate is None:
        return None
    tier = rate.get("long_context")
    return tier if tier and input_tokens > tier["input_tokens_threshold"] else rate


def tokens(usage):
    """Partition of one request's usage, or None when it is incomplete."""
    usage = usage if isinstance(usage, dict) else {}
    inp, read, out = usage.get("input_tokens"), usage.get("cached_tokens"), usage.get("output_tokens")
    write = usage.get("cache_write_tokens")
    if not (_count(inp) and _count(read) and _count(out)) or read > inp:
        return None
    write = write if _count(write) else 0
    if read + write > inp:
        return None
    reasoning = usage.get("reasoning_tokens")
    return dict(input=inp, uncached=inp - read - write, cache_read=read, cache_write=write, output=out,
                reasoning=reasoning if _count(reasoning) else 0)


def cost(parts, rate):
    if parts is None or rate is None or (parts["cache_write"] and "cache_write" not in rate):
        return None
    return dict(uncached=parts["uncached"] * rate["input"] / 1e6, cache_read=parts["cache_read"] * rate["cached"] / 1e6,
                cache_write=parts["cache_write"] * rate.get("cache_write", 0) / 1e6, output=parts["output"] * rate["output"] / 1e6)


def split_rows(rows):
    """Main requests, and aux calls: method summaries/reflections, host native compaction, and model calls the
    proxy forwarded unchanged ("passthrough": host side calls such as titles or tool-less helper requests)."""
    main, aux = [], []
    for row in rows:
        if "request" in row:
            main.append(row)
        elif row.get("type") in ("summary", "native_compaction", "passthrough"):
            aux.append(dict(row, purpose=row.get("purpose") or row["type"]))
    return main, aux


def read_log(path):
    rows, errors = [], 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:                       # a terminated request may leave a partial row
            errors += 1
    return rows, errors


def _cause(row, prev):
    """('method' | 'host' | None, measured?) for how this request's sent history differs from the previous one."""
    if "prefix" in row:                          # process telemetry: digests of what was received and sent
        prefix, host = row.get("prefix") or {}, row.get("host_prefix") or {}
        if host.get("edited"):
            return "host", True
        return ("method" if prefix.get("edited") else None), True
    if row.get("history_rebased"):
        return "host", False
    edited = bool(row.get("operations")) or (prev is not None and (
        row.get("changed", 0) > prev["changed"] or row.get("dropped", 0) > prev["dropped"]))
    return ("method" if edited else None), False


def request_table(main, prices):
    """One normalized row per main request, in log order, with cache-break attribution."""
    table, previous = [], {}
    for row in main:
        ok = _ok(row)
        parts = tokens(row.get("usage")) if ok else None
        rate = rates(prices, row.get("model"), parts["input"]) if parts else None
        priced = cost(parts, rate)
        session = row.get("session")
        prev = previous.get(session)
        edit, measured = _cause(row, prev)
        record = dict(request=row.get("request"), session=session, ok=ok, model=row.get("model"),
                      latency=row.get("seconds"), rewrite_seconds=row.get("rewrite_seconds"),
                      tokens_before=row.get("tokens_before"), tokens_after=row.get("tokens_after"),
                      edit=edit, measured=measured, operations=row.get("operations") or {},
                      composition=row.get("composition"), fixed_changed=row.get("fixed_changed"),
                      **{k: (parts or {}).get(k) for k in TOKEN_KEYS},
                      cost=sum(priced.values()) if priced else None, cost_parts=priced,
                      cacheable=None, cache_miss=None, cache_break=False, cause=None, miss_cost=None)
        if parts and prev is not None and prev["input"] is not None:
            cacheable = min(prev["input"], parts["input"])
            miss = max(0, cacheable - parts["cache_read"])
            record.update(cacheable=cacheable, cache_miss=miss, cause=edit or "provider",
                          cache_break=miss >= BREAK_MIN_TOKENS and parts["cache_read"] < BREAK_SHARE * cacheable,
                          miss_cost=miss * (rate["input"] - rate["cached"]) / 1e6 if rate else None)
        if ok:
            previous[session] = dict(input=parts["input"] if parts else None,
                                     changed=row.get("changed", 0), dropped=row.get("dropped", 0))
        table.append(record)
    return table


def _stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return dict(n=0, first=None, max=None, mean=None, median=None, p90=None, last=None)
    ordered = sorted(values)
    return dict(n=len(values), first=values[0], max=ordered[-1], mean=statistics.fmean(values),
                median=statistics.median(values), p90=ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)], last=values[-1])


def _slope(values):
    points = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(points) < 2:
        return None
    mx = statistics.fmean(i for i, _ in points); my = statistics.fmean(v for _, v in points)
    den = sum((i - mx) ** 2 for i, _ in points)
    return sum((i - mx) * (v - my) for i, v in points) / den if den else None


def _total(rows, key):
    values = [r[key] for r in rows]
    return sum(values) if all(v is not None for v in values) else None


def _group(rows):
    out = {k: _total(rows, k) for k in TOKEN_KEYS + ("cost",)}
    for part in PARTS:
        values = [r["cost_parts"][part] if r["cost_parts"] else None for r in rows]
        out["cost_" + part] = sum(values) if all(v is not None for v in values) else None
    return out


def _add(a, b):
    return None if a is None or b is None else a + b


def job_metrics(main, aux, prices, result, parse_errors=0):
    """All process metrics and self-checks for one job; returns (metrics, request table)."""
    table = request_table(main, prices)
    good = [r for r in table if r["ok"]]
    aux_rows = []
    for row in aux:
        ok = _ok(row)
        parts = tokens(row.get("usage")) if ok else None
        priced = cost(parts, rates(prices, row.get("model"), parts["input"]) if parts else None)
        aux_rows.append(dict(purpose=row["purpose"], ok=ok, latency=row.get("seconds"), **{k: (parts or {}).get(k) for k in TOKEN_KEYS},
                             cost=sum(priced.values()) if priced else None, cost_parts=priced))
    aux_ok = [r for r in aux_rows if r["ok"] and r["purpose"] != "passthrough"]
    side_ok = [r for r in aux_rows if r["ok"] and r["purpose"] == "passthrough"]
    m, a, side = _group(good), _group(aux_ok), _group(side_ok)
    total = _add(_add(m["cost"], a["cost"]), side["cost"])     # what eval_usage bills, split here by role
    sessions = {}
    for r in good:
        sessions[r["session"]] = sessions.get(r["session"], 0) + 1
    primary = max(sessions, key=sessions.get) if sessions else None    # the agent's own conversation
    convo = [r for r in good if r["session"] == primary]
    inputs = [r["input"] for r in convo]
    before = [r["tokens_before"] for r in convo if r["tokens_before"] is not None]
    after = [r["tokens_after"] for r in convo if r["tokens_after"] is not None]
    ops, purposes, composition = {}, {}, {}
    composed = [r for r in convo if r["composition"]]
    for r in good:
        for key, value in r["operations"].items():
            ops[key] = ops.get(key, 0) + value
    for r in composed:
        for key, value in r["composition"].items():
            composition[key] = composition.get(key, 0) + value
    for r in aux_rows:
        purposes[r["purpose"]] = purposes.get(r["purpose"], 0) + 1
    edits = [r["request"] for r in good if r["edit"] == "method"]
    latency = [r["latency"] for r in good + aux_ok + side_ok if r["latency"] is not None]
    overhead = [r["rewrite_seconds"] for r in good if r["rewrite_seconds"] is not None]
    health = result.get("execution_health") or {}
    cache = dict(read_share=(m["cache_read"] / m["input"]) if m["input"] else None, breaks=0,
                 measured_share=(sum(r["measured"] for r in good) / len(good)) if good else None)
    for cause in CAUSES:
        rows = [r for r in good if r["cause"] == cause]
        cache.update({f"breaks_{cause}": sum(r["cache_break"] for r in rows),
                      f"missed_tokens_{cause}": sum(r["cache_miss"] or 0 for r in rows),
                      f"miss_cost_{cause}": sum(r["miss_cost"] or 0 for r in rows)})
        cache["breaks"] += cache[f"breaks_{cause}"]
    cache["miss_cost"] = sum(cache[f"miss_cost_{c}"] for c in CAUSES)
    metrics = dict(
        efficiency=dict(agent_seconds=result.get("seconds"), stop=result.get("stop"), main_requests=len(good),
                        failed_requests=len(table) - len(good), tool_calls=health.get("tool_calls", result.get("calls")),
                        model_seconds=sum(latency) if latency else 0.0, aux_calls=len(aux_ok), side_calls=len(side_ok),
                        failed_aux_calls=len(aux_rows) - len(aux_ok) - len(side_ok), sessions=len(sessions),
                        side_session_requests=len(good) - len(convo), rewrite_seconds=sum(overhead) if overhead else None),
        tokens=dict(main={k: m[k] for k in TOKEN_KEYS}, aux={k: a[k] for k in TOKEN_KEYS}, side={k: side[k] for k in TOKEN_KEYS}),
        cost=dict(total=total, main=m["cost"], aux=a["cost"], side=side["cost"],
                  parts={p: _add(_add(m["cost_" + p], a["cost_" + p]), side["cost_" + p]) for p in PARTS},
                  aux_share=(a["cost"] / total) if total and a["cost"] is not None else None),
        context=dict(api_input=_stats(inputs), growth_per_request=_slope(inputs),
                     history_before=_stats(before), history_after=_stats(after),
                     history_reduction=(1 - sum(after) / sum(before)) if before and sum(before) else None,
                     composition_mean={k: v / len(composed) for k, v in composition.items()} if composed else None),
        cache=cache,
        activity=dict(edit_requests=len(edits), first_edit_request=edits[0] if edits else None, operations=ops,
                      aux_purposes=purposes, host_edits=sum(r["edit"] == "host" for r in good),
                      fixed_changes=sum(r["fixed_changed"] is True for r in good)))
    metrics["checks"] = self_checks(main, aux, table, metrics, prices, result, parse_errors)
    return metrics, table


def self_checks(main, aux, table, metrics, prices, result, parse_errors):
    """Is this job's process record complete and internally consistent?"""
    gaps, seen = 0, {}
    for row in main:
        n, session = row.get("request"), row.get("session")
        if type(n) is int:
            if session in seen and n != seen[session] + 1 and n > seen[session]:
                gaps += 1
            seen[session] = max(n, seen.get(session, 0))
    good = [r for r in table if r["ok"]]
    missing_usage = sum(r["input"] is None for r in good)
    ratios = sorted(r["input"] / r["tokens_after"] for r in good if r["input"] and r["tokens_after"])
    ratio = statistics.median(ratios) if ratios else None
    parity = None
    if prices is not None and metrics["cost"]["total"] is not None:
        from ctxpress.live import usage as eval_usage
        usage = dict(requests=len(main), usage=dict(summary_calls=sum(r.get("type") == "summary" for r in aux),
                     native_compaction_calls=sum(r.get("type") == "native_compaction" for r in aux),
                     passthrough_calls=sum(r.get("type") == "passthrough" for r in aux)))
        try:
            billed = eval_usage.bill(eval_usage.analyze(dict(result, rewrites=main + aux, **usage)), prices)["api_cost_at_declared_rates_usd"]
            parity = None if billed is None else abs(billed - metrics["cost"]["total"]) < 1e-6
        except (ValueError, KeyError, TypeError):
            parity = None
    hard = []
    if parse_errors:
        hard.append(f"{parse_errors} unparsable log lines")
    if gaps:
        hard.append(f"{gaps} request-number gaps")
    if parity is False:
        hard.append("cost differs from eval_usage's bill")
    warnings = []
    if missing_usage:
        warnings.append(f"{missing_usage} ok requests without complete usage")
    if ratio is not None and not ESTIMATE_RATIO[0] <= ratio <= ESTIMATE_RATIO[1]:
        warnings.append(f"API tokens per estimated token {ratio:.2f}")
    return dict(parse_errors=parse_errors, request_gaps=gaps, ok_without_usage=missing_usage, cost_parity=parity,
                tokens_per_estimate=dict(median=ratio, min=ratios[0] if ratios else None, max=ratios[-1] if ratios else None),
                telemetry_coverage=metrics["cache"]["measured_share"], hard=hard, warnings=warnings)


def quality(grade, result):
    """(kind, value) of the official outcome as eval_outcomes interprets it; (None, None) when ungraded/invalid."""
    from ctxpress.harness import eval_outcomes
    outcome = eval_outcomes.observe(grade or {}, result)
    if outcome["boolean_valid"]:
        return "resolved", float(outcome["resolved"])
    if outcome["code_sample_valid"]:
        return "sample_passed", float(outcome["sample_passed"])
    if outcome["milestone_metrics_valid"] and isinstance(outcome["official_metrics"], dict):
        value = outcome["official_metrics"].get("score_1000")
        return "milestone.score_1000", value if type(value) in (int, float) else None
    if outcome["rewards_valid"]:
        name = "reward" if "reward" in outcome["rewards"] else sorted(outcome["rewards"])[0]
        return "reward." + name, float(outcome["rewards"][name])
    return None, None


def _job(job_id, method, task_id, repeat, status, **extra):
    return dict(dict(job_id=job_id, method=method, task_id=task_id, repeat=repeat, status=status, quality_kind=None, quality=None,
                     quality_admissible=False, report_cost=None, report_cost_complete=None, excluded=False, not_run=False,
                     problems=[]), **extra)


def _finish(row, main, aux, prices, result, parse_errors=0):
    metrics, table = job_metrics(main, aux, prices, result, parse_errors)
    row.update(metrics)
    row["problems"] += metrics["checks"]["hard"]
    return table


# ---- inputs -------------------------------------------------------------------------------------------------

def from_log(path, prices=None):
    path = Path(path)
    if path.is_dir():
        candidates = [path / "requests.jsonl", path / "agent" / "ctxpress-requests.jsonl", path / "ctxpress-requests.jsonl"]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            raise ValueError("no request log in this directory")
    rows, errors = read_log(path)
    main, aux = split_rows(rows)
    labels = {r.get("model") for r in main if r.get("model")}
    row = _job(path.parent.name, "session", str(path), 0, "completed", source=str(path))
    table = _finish(row, main, aux, prices, {}, errors)
    family = dict(benchmark="log", directory=str(path.parent), prices=prices, reference=None, jobs=[row],
                  models=sorted(labels))
    return [family], {"log": {row["job_id"]: table}}


def _ro(path):
    return sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)


def from_eval(directory, prices=None, reference=None):
    directory = Path(directory)
    connection = _ro(directory / "jobs.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        plan = json.loads(connection.execute("SELECT value FROM metadata WHERE key='plan'").fetchone()[0])
        rows = [dict(r) for r in connection.execute("SELECT id, status, spec, result FROM jobs ORDER BY id")]
    finally:
        connection.close()
    from ctxpress.harness import task as tasks
    prices = prices or plan["config"].get("prices")
    jobs, tables = [], {}
    for job in rows:
        spec = json.loads(job["spec"])
        row = _job(job["id"], spec["label"], tasks.identity(spec), spec.get("repeat", 0), job["status"])
        if job["status"] != "completed" or job["result"] is None:
            row["not_run"] = True
            jobs.append(row); continue
        result = json.loads(job["result"])
        row["quality_kind"], row["quality"] = quality(result.get("grade"), result)
        row["quality_admissible"] = row["quality"] is not None
        main, aux = split_rows(result.get("rewrites") or [])
        tables[job["id"]] = _finish(row, main, aux, prices, result)
        row["report_cost_complete"] = row["cost"]["total"] is not None and not row["checks"]["ok_without_usage"] and \
            row["efficiency"]["failed_requests"] == 0
        jobs.append(row)
    labels = list(dict.fromkeys(j["method"] for j in jobs))
    reference = reference or ("no-compaction" if "no-compaction" in labels else labels[0] if labels else None)
    declared = plan.get("benchmark")
    name = (declared.get("name") if isinstance(declared, dict) else declared) or plan["config"].get("benchmark") or directory.name
    family = dict(benchmark=name, directory=str(directory),
                  prices=prices, reference=reference, jobs=jobs)
    return [family], {family["benchmark"]: tables}


def _artifact(job, *names):
    """The job's own artifact (inside jobs/<id>/attempt-N/) with one of these relative paths."""
    for item in job.get("artifacts", []):
        parts = item["path"].split("/jobs/" + job["job_id"] + "/", 1)
        if len(parts) == 2 and parts[1].split("/", 1)[-1] in names:
            return item
    return None


def _report_quality(metrics):
    if "resolved" in metrics:
        return "resolved", None if metrics["resolved"] is None else float(metrics["resolved"])
    if "milestone.score_1000" in metrics:
        return "milestone.score_1000", metrics["milestone.score_1000"]
    return (next(iter(metrics)), metrics[next(iter(metrics))]) if metrics else (None, None)


def from_report(report, eligibility=None, families=None, reference=None):
    report = Path(report)
    summary = json.loads(report.read_text(encoding="utf-8"))
    excluded = set()
    if eligibility:
        data = json.loads(Path(eligibility).read_text(encoding="utf-8"))
        if data.get("report", {}).get("sha256") != sha256(report):
            raise ValueError("eligibility file belongs to another report")
        excluded = {(j["benchmark"], j["job_id"]) for j in data["jobs"] if j.get("exclude_from_method_effect_comparison")}
    out, tables = [], {}
    for family in summary["families"]:
        if families and family["benchmark"] not in families:
            continue
        detail_path = Path(family["detail_artifact"])
        if sha256(detail_path) != family["detail_sha256"]:
            raise ValueError("detail artifact changed since the report: " + str(detail_path))
        detail = json.loads(detail_path.read_text(encoding="utf-8"))
        prices, jobs, table = detail.get("prices"), [], {}
        for job in detail["jobs"]:
            kind, value = _report_quality(job.get("metrics") or {})
            row = _job(job["job_id"], job["method"], job["task_id"], job.get("repeat", 0), job["status"],
                       quality_kind=kind, quality=value, quality_admissible=job.get("quality_admissible") is True,
                       report_cost=job.get("api_cost_usd"), report_cost_complete=job.get("api_cost_complete") is True,
                       excluded=(family["benchmark"], job["job_id"]) in excluded)
            jobs.append(row)
            if job["status"] != "completed":
                row["not_run"] = True              # pending, running or failed before a result (kept, never imputed)
                continue
            log = _artifact(job, "agent/ctxpress-requests.jsonl", "requests.jsonl")
            res = _artifact(job, "result.json")
            if log is None or res is None or not Path(log["path"]).exists():
                row["problems"].append("request log or result missing"); continue
            for item in (log, res):
                if sha256(item["path"]) != item["sha256"]:
                    row["problems"].append("artifact changed since the report: " + item["path"])
            rows, errors = read_log(log["path"])
            main, aux = split_rows(rows)
            table[job["job_id"]] = _finish(row, main, aux, prices, json.loads(Path(res["path"]).read_text(encoding="utf-8")), errors)
            recomputed = row["cost"]["total"]
            row["cost_matches_report"] = (None if not row["report_cost_complete"] or recomputed is None
                                          else abs(recomputed - row["report_cost"]) < 1e-6)
            if row["cost_matches_report"] is False:
                row["problems"].append(f"recomputed cost {recomputed} != report {row['report_cost']}")
        out.append(dict(benchmark=family["benchmark"], directory=detail.get("directory"), prices=prices,
                        reference=reference or detail.get("reference") or "no-compaction", jobs=jobs))
        tables[family["benchmark"]] = table
    return out, tables


def load_prices(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("prices",):
        if isinstance(data.get(key), dict):
            return data[key]
    if isinstance(data.get("config"), dict) and isinstance(data["config"].get("prices"), dict):
        return data["config"]["prices"]
    if "models" in data:
        return data
    raise ValueError("no prices object in " + str(path))


def analyze(path, eligibility=None, prices=None, reference=None, families=None):
    path = Path(path)
    if path.is_file() and path.suffix == ".json":
        found, tables = from_report(path, eligibility, families, reference)
        source = "report"
    elif path.is_dir() and (path / "jobs.sqlite").exists():
        found, tables = from_eval(path, prices, reference)
        source = "evaluation"
    else:
        found, tables = from_log(path, prices)
        source = "log"
    for family in found:
        family["methods"] = method_summaries(family["jobs"])
        family["paired"] = paired(family["jobs"], family["reference"]) if family["reference"] else []
    return dict(schema=SCHEMA, version=1, checked_at=datetime.now(timezone.utc).isoformat(), source=source,
                input=str(path), input_sha256=sha256(path) if path.is_file() else None,
                eligibility=str(eligibility) if eligibility else None, families=found,
                limits=["descriptive; single runs per task do not establish causal effects or non-inferiority",
                        "cache breaks are inferred from usage counts; the provider does not say why a prefix missed",
                        "proxy history sizes and composition are serialized-chars/4 estimates, not tokenizer counts"]), tables


# ---- summaries ----------------------------------------------------------------------------------------------

def _usable(job):
    """Quality-admissible, cost-complete, not excluded, run, and passing every hard check."""
    return (job["quality_admissible"] and not job["excluded"] and not job["problems"] and not job["not_run"]
            and job["report_cost_complete"] is not False and job.get("cost", {}).get("total") is not None)


def _process_usable(job):
    """For logs without task quality: run and passing every hard check."""
    return not job["not_run"] and not job["problems"] and "cost" in job


FIELDS = {   # name -> getter on an analyzed job
    "quality": lambda j: j["quality"],
    "cost": lambda j: j["cost"]["total"],
    "cost_aux": lambda j: j["cost"]["aux"],
    "cost_side": lambda j: j["cost"]["side"],
    "cost_uncached": lambda j: j["cost"]["parts"]["uncached"],
    "cost_cache_read": lambda j: j["cost"]["parts"]["cache_read"],
    "cost_cache_write": lambda j: j["cost"]["parts"]["cache_write"],
    "cost_output": lambda j: j["cost"]["parts"]["output"],
    "agent_seconds": lambda j: j["efficiency"]["agent_seconds"],
    "model_seconds": lambda j: j["efficiency"]["model_seconds"],
    "rewrite_seconds": lambda j: j["efficiency"]["rewrite_seconds"],
    "main_requests": lambda j: j["efficiency"]["main_requests"],
    "tool_calls": lambda j: j["efficiency"]["tool_calls"],
    "input_tokens": lambda j: sum(j["tokens"][g]["input"] or 0 for g in ("main", "aux", "side")),
    "uncached_tokens": lambda j: sum(j["tokens"][g]["uncached"] or 0 for g in ("main", "aux", "side")),
    "output_tokens": lambda j: sum(j["tokens"][g]["output"] or 0 for g in ("main", "aux", "side")),
    "max_input": lambda j: j["context"]["api_input"]["max"],
    "mean_input": lambda j: j["context"]["api_input"]["mean"],
    "growth_per_request": lambda j: j["context"]["growth_per_request"],
    "history_reduction": lambda j: j["context"]["history_reduction"],
    "cache_read_share": lambda j: j["cache"]["read_share"],
    "cache_breaks": lambda j: j["cache"]["breaks"],
    **{f"cache_breaks_{c}": (lambda c: lambda j: j["cache"][f"breaks_{c}"])(c) for c in CAUSES},
    "miss_cost": lambda j: j["cache"]["miss_cost"],
    **{f"miss_cost_{c}": (lambda c: lambda j: j["cache"][f"miss_cost_{c}"])(c) for c in CAUSES},
    "edit_requests": lambda j: j["activity"]["edit_requests"],
}
RATIO_FIELDS = ("cost", "input_tokens", "uncached_tokens", "output_tokens", "max_input", "mean_input",
                "agent_seconds", "model_seconds", "main_requests")


def _value(job, name):
    try:
        return FIELDS[name](job)
    except (KeyError, TypeError):
        return None


def method_summaries(jobs):
    out = []
    usable_of = usable_for(dict(jobs=jobs))
    for method in dict.fromkeys(j["method"] for j in jobs):
        rows = [j for j in jobs if j["method"] == method]
        usable = [j for j in rows if usable_of(j)]
        summary = dict(method=method, jobs=len(rows), usable=len(usable), excluded=sum(j["excluded"] for j in rows),
                       not_run=sum(j["not_run"] for j in rows), problems=sum(bool(j["problems"]) for j in rows),
                       cost_incomplete=sum(not j["not_run"] and j["report_cost_complete"] is False for j in rows),
                       warnings=sum(bool(j.get("checks", {}).get("warnings")) for j in rows), operations={})
        for name in FIELDS:
            values = [v for v in (_value(j, name) for j in usable) if v is not None]
            summary[name] = dict(n=len(values), mean=statistics.fmean(values) if values else None,
                                 median=statistics.median(values) if values else None, total=sum(values) if values else None)
        for j in usable:
            for key, value in j["activity"]["operations"].items():
                summary["operations"][key] = summary["operations"].get(key, 0) + value
        out.append(summary)
    return out


def paired(jobs, reference):
    ref = {(j["task_id"], j["repeat"]): j for j in jobs if j["method"] == reference and _usable(j)}
    out = []
    for method in [m for m in dict.fromkeys(j["method"] for j in jobs) if m != reference]:
        pairs = [(ref[(j["task_id"], j["repeat"])], j) for j in jobs
                 if j["method"] == method and _usable(j) and (j["task_id"], j["repeat"]) in ref]
        entry = dict(method=method, reference=reference, pairs=len(pairs),
                     quality_delta=statistics.fmean(c["quality"] - r["quality"] for r, c in pairs)
                     if pairs and all(None not in (r["quality"], c["quality"]) for r, c in pairs) else None)
        for name in RATIO_FIELDS:
            values = [(_value(r, name), _value(c, name)) for r, c in pairs]
            values = [(a, b) for a, b in values if a and b is not None]
            entry[name] = dict(n=len(values), pooled_ratio=sum(b for _, b in values) / sum(a for a, _ in values) if values else None,
                               geomean_ratio=math.exp(statistics.fmean(math.log(b / a) for a, b in values))
                               if values and all(b > 0 for _, b in values) else None)
        out.append(entry)
    return out


# ---- outputs ------------------------------------------------------------------------------------------------

def _fmt(value, digits=3):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def markdown(analysis):
    lines = ["# 过程评测", "", f"输入（{analysis['source']}）：`{analysis['input']}`，生成于 {analysis['checked_at']}。",
             "全部数字由原始逐请求日志与声明价格重算；口径见文末。", ""]
    for fam in analysis["families"]:
        jobs = fam["jobs"]
        ran = [j for j in jobs if not j["not_run"]]
        checked = [j for j in ran if j.get("checks", {}).get("cost_parity") is not None]
        reported = [j for j in ran if j.get("cost_matches_report") is not None]
        lines += [f"## {fam['benchmark']}", "",
                  f"作业 {len(jobs)}，已运行 {len(ran)}，未运行 {len(jobs) - len(ran)}；硬检查问题 {sum(bool(j['problems']) for j in jobs)}，"
                  f"警告 {sum(bool(j.get('checks', {}).get('warnings')) for j in ran)}；费用与 eval_usage 一致 "
                  f"{sum(j['checks']['cost_parity'] is True for j in checked)}/{len(checked)}"
                  + (f"，与报告一致 {sum(j['cost_matches_report'] for j in reported)}/{len(reported)}" if reported else "")
                  + f"；过程遥测覆盖 {_fmt(statistics.fmean(j['checks']['telemetry_coverage'] for j in ran if j.get('checks', {}).get('telemetry_coverage') is not None) if any(j.get('checks', {}).get('telemetry_coverage') is not None for j in ran) else None, 2)}。", "",
                  "| 方法 | 可用 | 质量 | 费用 $ | 辅助/旁路 $ | 主请求 | 工具调用 | Agent 秒 | 输入 token | 未缓存 token | 最大输入 | 缓存读占比 | 断裂 方法/宿主/服务端 | 断裂损失 $ 方法/宿主/服务端 | 改写请求 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for m in fam["methods"]:
            g = lambda k, d=1: _fmt(m[k]["mean"], d)
            t = lambda k, d=3: _fmt(m[k]["total"], d)
            lines.append(f"| {m['method']} | {m['usable']}/{m['jobs']} | {g('quality', 3)} | {g('cost', 4)} | {g('cost_aux', 4)}/{g('cost_side', 4)} | "
                         f"{g('main_requests')} | {g('tool_calls')} | {g('agent_seconds', 0)} | {g('input_tokens', 0)} | {g('uncached_tokens', 0)} | "
                         f"{g('max_input', 0)} | {g('cache_read_share', 3)} | "
                         f"{t('cache_breaks_method', 0)}/{t('cache_breaks_host', 0)}/{t('cache_breaks_provider', 0)} | "
                         f"{t('miss_cost_method')}/{t('miss_cost_host')}/{t('miss_cost_provider')} | {g('edit_requests')} |")
        lines.append("")
        if fam["paired"]:
            lines += [f"相对 `{fam['reference']}` 的配对比值：合并比 = 方法总量 / 参照总量（括号内为逐对比值的几何均值）。", "",
                      "| 方法 | 配对 | 质量差 | 费用 | 输入 token | 未缓存 token | 输出 token | 最大输入 | Agent 时间 | 主请求 |",
                      "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
            for p in fam["paired"]:
                r = lambda k: f"{_fmt(p[k]['pooled_ratio'], 2)}（{_fmt(p[k]['geomean_ratio'], 2)}）"
                lines.append(f"| {p['method']} | {p['pairs']} | {_fmt(p['quality_delta'])} | {r('cost')} | {r('input_tokens')} | "
                             f"{r('uncached_tokens')} | {r('output_tokens')} | {r('max_input')} | {r('agent_seconds')} | {r('main_requests')} |")
            lines.append("")
        flagged = [j for j in jobs if j["problems"] or j.get("checks", {}).get("warnings")]
        if flagged:
            lines += ["检查结果：", ""] + [f"- `{j['job_id']}` {j['method']}: " + "; ".join(j["problems"] + j.get("checks", {}).get("warnings", []))
                                       for j in flagged] + [""]
    lines += ["## 口径", "", __doc__.split("Definitions", 1)[1].strip(), ""]
    return "\n".join(lines)


def _flat(job):
    out = dict(job_id=job["job_id"], method=job["method"], task_id=job["task_id"], repeat=job["repeat"], status=job["status"],
               quality_kind=job["quality_kind"], usable=_usable(job), excluded=job["excluded"], not_run=job["not_run"],
               cost_parity=job.get("checks", {}).get("cost_parity"), cost_matches_report=job.get("cost_matches_report"),
               problems="; ".join(job["problems"]), warnings="; ".join(job.get("checks", {}).get("warnings", [])))
    for name in FIELDS:
        out[name] = _value(job, name)
    return out


REQUEST_COLUMNS = ["benchmark", "job_id", "request", "session", "ok", "model", "latency", "rewrite_seconds", *TOKEN_KEYS, "cost",
                   "tokens_before", "tokens_after", "edit", "measured", "fixed_changed", "operations", "composition",
                   "cacheable", "cache_miss", "cache_break", "cause", "miss_cost"]


def write(analysis, tables, output, figures=True):
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    (output / "analysis.json").write_text(json.dumps(analysis, indent=1, ensure_ascii=False), encoding="utf-8")
    (output / "report.md").write_text(markdown(analysis), encoding="utf-8")
    jobs = [_flat(j) for fam in analysis["families"] for j in fam["jobs"]]
    with open(output / "jobs.csv", "w", newline="", encoding="utf-8") as stream:
        w = csv.DictWriter(stream, fieldnames=list(jobs[0])); w.writeheader(); w.writerows(jobs)
    with open(output / "requests.csv", "w", newline="", encoding="utf-8") as stream:
        w = csv.DictWriter(stream, fieldnames=REQUEST_COLUMNS, extrasaction="ignore"); w.writeheader()
        for fam in analysis["families"]:
            for job_id, table in tables[fam["benchmark"]].items():
                for row in table:
                    w.writerow(dict(row, benchmark=fam["benchmark"], job_id=job_id, operations=json.dumps(row["operations"]),
                                    composition=json.dumps(row["composition"]) if row["composition"] else ""))
    methods = [dict(benchmark=fam["benchmark"], method=m["method"], jobs=m["jobs"], usable=m["usable"],
                    **{f"{k}_mean": m[k]["mean"] for k in FIELDS}) for fam in analysis["families"] for m in fam["methods"]]
    with open(output / "methods.csv", "w", newline="", encoding="utf-8") as stream:
        w = csv.DictWriter(stream, fieldnames=list(methods[0])); w.writeheader(); w.writerows(methods)
    if figures:
        from ctxpress.harness.run_analysis_plot import plot
        plot(analysis, tables, output, usable_for)


def usable_for(family):
    """Which jobs a family's summaries use: task-graded families need quality; bare logs only need clean records."""
    return _usable if any(j["quality_kind"] for j in family["jobs"]) else _process_usable


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ctxpress analyze", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="request log or run directory, evaluation directory, or strict report summary.json")
    ap.add_argument("--output", required=True, help="new directory for report.md, analysis.json, CSVs and figures")
    ap.add_argument("--eligibility", help="comparison-eligibility.json of the same strict report")
    ap.add_argument("--prices", help="JSON with declared prices (a protocol/plan file or a prices object)")
    ap.add_argument("--reference", help="method label to pair against (default: the report's, else no-compaction)")
    ap.add_argument("--family", action="append", help="benchmark to include from a report (default: all)")
    ap.add_argument("--no-figures", action="store_true")
    a = ap.parse_args(argv)
    analysis, tables = analyze(a.path, a.eligibility, load_prices(a.prices) if a.prices else None, a.reference, a.family)
    write(analysis, tables, a.output, figures=not a.no_figures)
    for fam in analysis["families"]:
        ran = [j for j in fam["jobs"] if not j["not_run"]]
        print(f"{fam['benchmark']}: {len(fam['jobs'])} jobs, {len(ran)} run, "
              f"{sum(bool(j['problems']) for j in fam['jobs'])} with problems, "
              f"{sum(bool(j.get('checks', {}).get('warnings')) for j in ran)} with warnings")
    print(Path(a.output) / "report.md")


if __name__ == "__main__":
    main()
