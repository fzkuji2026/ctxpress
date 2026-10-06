"""Real execution evidence, with infrastructure failures and missing usage kept visible."""
from __future__ import annotations
import html, json
from pathlib import Path
from ctxpress.harness.eval_plan import atomic_json, verify
from ctxpress.live import usage as eval_usage
from ctxpress.harness import eval_compare, eval_mechanism, eval_outcomes
from ctxpress.harness import task as tasks


def report(directory):
    from ctxpress.harness.evaluation import database, execution_observation
    with database(directory) as connection:
        plan = json.loads(connection.execute("SELECT value FROM metadata WHERE key='plan'").fetchone()[0])
        jobs = [dict(row) for row in connection.execute("SELECT * FROM jobs ORDER BY id")]
    verify(plan,check_inputs=False)
    groups = {}
    synthetic = False
    prices = plan["config"].get("prices")
    for job in jobs:
        spec = json.loads(job["spec"])
        key = spec["label"]
        group = groups.setdefault(key, dict(method=key, planned=0, completed=0, failed=0, interrupted=0, cancelled=0, pending=0, running=0,
            valid_grades=0, resolved=0, valid_code_samples=0,passed_code_samples=0,valid_rewards=0, reward_metrics={}, valid_milestone_metrics=0,milestone_metrics=[], infra_invalid=0, grading_errors=0, ungraded=0, observed_continuations=0, observed_mutations=0, api_input_tokens=0, api_cached_tokens=0,
            api_output_tokens=0, summary_calls=0, native_compaction_calls=0, passthrough_calls=0, native_compaction_unobserved_runs=0, pruner_calls=0, pruner_input_tokens=0, pruner_usage_unknown=0,
            missing_api_usage=0, usage_unobserved_runs=0, usage_conflicts=0, method_overhead_estimate=0.0, seconds=0.0, jobs=[], _accounting=[]))
        group["planned"] += 1
        group[job["status"]] += 1
        evidence = dict(id=job["id"], task_id=tasks.identity(spec),
            boundary=(spec.get('boundary') or {}).get('id'), start_mode=plan['config'].get('start_mode', 'checkpoint'),
            repeat=spec["repeat"], status=job["status"], attempt=job["attempt"], error=job["error"])
        if (spec.get('resources') or {}).get('gpu_device_ids'):
            evidence['gpu_device_ids'] = spec['resources']['gpu_device_ids']
        if (spec.get('resources') or {}).get('verifier_gpu_device_ids'):
            evidence['verifier_gpu_device_ids'] = spec['resources']['verifier_gpu_device_ids']
        group['native_compaction_unobserved_runs'] += job['result'] is None
        if job["result"] is not None:
            result = json.loads(job["result"])
            observation = execution_observation(result)
            group["observed_continuations"] += observation["valid"]
            evidence["execution_observation"] = observation
            mechanism = eval_mechanism.observe(result)
            evidence['mechanism_observation'] = mechanism
            group['observed_mutations'] += mechanism['rendered_mutation_observed']
            group['native_compaction_unobserved_runs'] += not mechanism['native_compaction_logging_available']
            synthetic = synthetic or bool(result.get("test_only")) or bool((result.get('grade') or {}).get('test_only'))
            grade = result.get("grade") or {}
            outcome = eval_outcomes.observe(grade, result)
            evidence['outcome_observation'] = outcome
            group['infra_invalid'] += outcome['infra_invalid']
            group['grading_errors'] += outcome['grading_error']
            group['valid_grades'] += outcome['boolean_valid']
            group['resolved'] += outcome['resolved'] is True
            group['valid_code_samples']+=outcome['code_sample_valid']
            group['passed_code_samples']+=outcome['sample_passed'] is True
            group['ungraded'] += outcome['ungraded']
            group['valid_rewards'] += outcome['rewards_valid']
            eval_outcomes.add_rewards(group['reward_metrics'], outcome['rewards'])
            group['valid_milestone_metrics'] += outcome['milestone_metrics_valid']
            if grade.get('quality_kind')=='milestone' or 'official_metrics' in grade:
                group['milestone_metrics'].append(dict(job_id=job['id'],task_id=evidence['task_id'],
                    repeat=spec['repeat'],valid=outcome['milestone_metrics_valid'],
                    scoring_complete=grade.get('scoring_complete'),submission_complete=grade.get('submission_complete'),
                    coverage=grade.get('coverage'),official_metrics=grade.get('official_metrics')))
            usage = result.get("usage") or {}
            for field in ("pruner_calls", "pruner_input_tokens", "pruner_usage_unknown", "method_overhead_estimate"):
                group[field] += usage.get(field, 0)
            accounting = eval_usage.analyze(result, plan['config']['model'])
            group['_accounting'].append(accounting)
            for field in ('api_input_tokens','api_cached_tokens','api_output_tokens','summary_calls','native_compaction_calls','passthrough_calls','missing_api_usage','usage_unobserved_runs'):
                group[field] += accounting[field]
            group['usage_conflicts'] += len(accounting['usage_conflicts'])
            group["seconds"] += result.get("seconds", 0)
            evidence.update(stop=result.get("stop"), grade=grade, context=result.get("boundary_context_evidence"),
                            environment=result.get('environment'), gpu=result.get('gpu'), api_usage_complete=accounting['complete'],
                            usage_conflicts=accounting['usage_conflicts'], proxy_log=result.get("proxy_log"))
            for field in ('protocol', 'real_run_verified', 'separate_verifier', 'official_artifacts',
                          'verifier_gpu', 'network_policy', 'harbor_api', 'swe_api', 'grading_run_id',
                          'pro_version', 'agent_report', 'regrade_report', 'submission', 'fresh_regrade',
                          'code_samples', 'sample_id', 'artifact_kind'):
                if field in result:
                    evidence[field] = result[field]
        group["jobs"].append(evidence)
    from ctxpress.harness.code_report import summarize
    code_metrics=summarize(plan,jobs)
    for group in groups.values():
        if group['method'] in code_metrics:
            metrics=code_metrics[group['method']];group['code_metrics']=metrics
            group['valid_code_samples']=metrics['valid_samples']
            group['passed_code_samples']=sum(row['passed_samples'] for row in metrics['tasks'])
        group["resolve_rate_valid_grades"] = group["resolved"] / group["valid_grades"] if group["valid_grades"] else None
        group["api_usage_complete"] = not (group["missing_api_usage"] or group["usage_unobserved_runs"] or group['usage_conflicts']) and group["completed"] == group["planned"]
        accounting = eval_usage.combine(group.pop('_accounting'), plan['config']['model'])
        accounting['complete'] = accounting['complete'] and group['api_usage_complete']
        group.update(eval_usage.bill(accounting, prices))
    return dict(schema="ctxpress.eval.report", version=1, scope=plan["config"]["scope"], plan_sha256=plan["sha256"],
        code_sha256=plan["code_sha256"], model=plan["config"]["model"], backend=plan["config"]["backend"],
        environment_manifest_sha256=(plan.get('environment_snapshot') or {}).get('sha256'),
        grading_manifest_sha256=(plan.get('grading_snapshot') or {}).get('sha256'),
        task_resource_manifest_sha256=(plan.get('task_resources') or {}).get('sha256'),
        grading_scope=(plan.get('task_resources') or plan.get('grading_snapshot') or {}).get('scope','official grading inputs are not declared'),
        benchmark_release=(plan.get('task_resources') or {}).get('release'),
        environment_scope=('declared task files, prepared service image IDs and GPU UUIDs when provided; host system libraries, GPU driver and external model versions are not frozen'
            if plan.get('task_resources') else 'base/boundary image IDs and complete auxiliary workspace when a manifest is declared; host runtime and external model versions are not frozen'),
        benchmark=plan.get('benchmark'), benchmark_declared='benchmark' in plan,
        prices=prices, evidence="synthetic mechanism fixtures; no task-quality evidence" if synthetic else "real execution only; mechanism checks do not establish task quality",
        cost_scope="counterfactual API estimate at declared rates, not a ChatGPT invoice; reported main, summary/reflection and recorded native compaction tokens priced once per request and requested model; cache writes replace ordinary input pricing, declared long-context rates apply to the full request above its input threshold; missing model IDs, required rates or usage keep total cost unknown; legacy static rates have limited scope and legacy logs may omit writes/native compaction; dedicated pruner and host compute costs are separate",
        comparison=eval_compare.compare(plan,jobs),
        methods=list(groups.values()))


def write_report(directory):
    directory = Path(directory)
    result = report(directory)
    atomic_json(directory / "report.json", result)
    cells = []
    for method in result["methods"]:
        rate = method["resolve_rate_valid_grades"]
        native = method['native_compaction_calls']
        if method['native_compaction_unobserved_runs']:
            native = f"{native}（部分未记录）" if native else "未记录"
        fields = [method["method"], method["planned"], method["completed"], method["observed_continuations"], method["observed_mutations"], method["failed"] + method["interrupted"] + method['cancelled'],
                  method["valid_grades"],method['valid_code_samples'], method["infra_invalid"], method["grading_errors"], "—" if rate is None else f"{rate:.1%}",
                  method["api_input_tokens"], method["api_cached_tokens"], '未知' if method['api_cache_write_tokens'] is None else method['api_cache_write_tokens'],
                  method['missing_api_cache_write_usage'], method["api_output_tokens"], method["summary_calls"], native, method["missing_api_usage"], method["usage_unobserved_runs"],method['usage_conflicts']]
        cells.append("<tr>" + "".join("<td>" + html.escape(str(value)) + "</td>" for value in fields) + "</tr>")
    headings = ["方法", "计划", "完成", "收到模型响应", "观察到改写", "失败/中断/取消", "有效任务评分","有效代码样本", "基础设施无效", "评分错误", "有效评分解决率", "API 输入", "缓存读取", "缓存写入", "缺失写入用量请求", "输出", "摘要调用", "原生压缩调用", "缺失用量", "未观察到主请求的运行", "用量冲突"]
    markup = '<!doctype html><html lang="zh"><meta charset="utf-8"><title>ctxpress evaluation</title>'
    markup += '<style>body{font:15px system-ui;margin:32px}table{border-collapse:collapse}th,td{padding:8px;border:1px solid #bbb}th{text-align:left}code{word-break:break-all}</style>'
    markup += '<h1>ctxpress 实验记录</h1><p>范围：' + html.escape(result["scope"]) + '；模型：' + html.escape(result["model"]) + '</p>'
    if result['benchmark']:
        markup += '<p>Benchmark：' + html.escape(result['benchmark']['title']) + '；任务集合：' + html.escape(result['benchmark']['suite']) + '；模式：' + html.escape(result['benchmark']['mode']) + '</p>'
    markup += '<p>证据类型：' + ('合成机制数据；不构成真实任务质量证据。' if result['evidence'].startswith('synthetic') else '真实执行记录；机制检查不证明任务效果。') + '</p>'
    markup += '<p>计划：<code>' + result["plan_sha256"] + '</code></p><p>费用为声明费率下的公开 API 反事实估算，不代表 ChatGPT 实际账单。用量包含主模型、摘要/反思和已记录的原生压缩调用，按每次请求和请求中的模型分别估算。输入包含普通输入、缓存读取和缓存写入；写入按替代费率计价，不叠加普通输入费。声明的长上下文费率仅在单次请求输入严格超过阈值时应用于整次请求。缺少模型、必要费率或用量时总费用为未知；旧静态费率估算范围有限，旧日志的缺失写入量保持未知。专用剪枝模型和宿主计算费用另计。</p>'
    if result['task_resource_manifest_sha256']:
        markup += '<p>任务资源清单：<code>' + result['task_resource_manifest_sha256'] + '</code>；所选任务、官方源码/依赖和服务镜像按此清单绑定。</p>'
    elif result['environment_manifest_sha256']:
        markup += '<p>镜像与需求目录清单：<code>' + result['environment_manifest_sha256'] + '</code>。</p>'
    else:
        markup += '<p>未声明跨任务的镜像与需求目录清单；每次实际运行的镜像 ID 保存在 JSON 记录中。</p>'
    if result['task_resource_manifest_sha256']:
        markup += '<p>官方运行依赖由任务资源清单声明；宿主系统库、GPU 驱动和外部模型版本尚未整体冻结。</p>'
    elif result['grading_manifest_sha256']:
        markup += '<p>官方评分输入清单：<code>' + result['grading_manifest_sha256'] + '</code>。宿主 Python、系统库及外部模型版本尚未整体冻结。</p>'
    else:
        markup += '<p>官方评分依赖与外部模型版本尚未固定。</p>'
    markup += '<table><thead><tr>' + ''.join('<th>' + heading + '</th>' for heading in headings) + '</tr></thead><tbody>' + ''.join(cells) + '</tbody></table>'
    if any(method['milestone_metrics'] for method in result['methods']):
        markup += '<h2>SWE-Milestone 官方指标</h2><p>按任务与重复运行保留作者指标原值及分母。未提交节点按作者规则计零分；指标有效性、完整提交和完整评分报告覆盖分别列出。无效结果的数值仅作诊断。</p>'
        metric_names=('graded','submitted','evaluated','scoreable','infra_invalid','resolved','resolve_pct',
                      'score_1000','score_full','score_reliable','precision','recall','total_milestones')
        headings=['方法','任务','重复','指标有效','完整提交','完整评分报告']+list(metric_names)
        markup += '<table><thead><tr>'+''.join('<th>'+html.escape(name)+'</th>' for name in headings)+'</tr></thead><tbody>'
        def flag(value):return '是' if value is True else ('否' if value is False else '未知')
        for method in result['methods']:
            for row in method['milestone_metrics']:
                metrics=row['official_metrics'] or {}
                fields=[method['method'],row['task_id'],row['repeat'],flag(row['valid']),
                        flag(row['submission_complete']),flag(row['scoring_complete'])]
                fields += [metrics.get(name,'未知') for name in metric_names]
                markup += '<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in fields)+'</tr>'
        markup += '</tbody></table>'
    markup += '<h2>模型用量与声明费率下的 API 费用</h2><p>按请求模型计价；返回身份是否记录完整单独报告，不表示返回快照与请求别名必然等价。</p><table><thead><tr><th>方法</th><th>请求模型</th><th>返回模型及次数</th><th>缺失返回身份</th><th>主请求</th><th>摘要/反思</th><th>原生压缩</th><th>旁路调用</th><th>输入</th><th>缓存读取</th><th>缓存写入</th><th>缺失写入用量请求</th><th>长上下文请求</th><th>缺失费率字段</th><th>输出</th><th>USD</th></tr></thead><tbody>'
    def returned_fields(bucket):
        missing = bucket.get('missing_response_model_requests')
        return [', '.join(f'{name}: {count}' for name,count in sorted(bucket['response_models'].items())) or '未记录',
                '未知' if missing is None else missing]
    for method in result['methods']:
        for model, usage in method['usage_by_model'].items():
            cost = usage['api_cost_at_declared_rates_usd']
            fields = [method['method'], model, *returned_fields(usage), usage['main_requests'], usage['summary_calls'], usage['native_compaction_calls'],
                      usage.get('passthrough_calls', 0),
                      usage['api_input_tokens'], usage['api_cached_tokens'], '未知' if usage['api_cache_write_tokens'] is None else usage['api_cache_write_tokens'],
                      usage['missing_api_cache_write_usage'], '未知' if usage['long_context_requests'] is None else usage['long_context_requests'],
                      ', '.join(usage['missing_rate_fields']) or ('模型费率未声明' if usage['prices'] is None else '—'),
                      usage['api_output_tokens'], '未知' if cost is None else f'{cost:.6f}']
            markup += '<tr>' + ''.join('<td>' + html.escape(str(value)) + '</td>' for value in fields) + '</tr>'
        if method['unattributed_usage']['requests']:
            unknown = method['unattributed_usage']
            fields = [method['method'], '模型未记录', *returned_fields(unknown), unknown['main_requests'], unknown['summary_calls'], unknown['native_compaction_calls'],
                      unknown.get('passthrough_calls', 0),
                      unknown['api_input_tokens'], unknown['api_cached_tokens'], '未知' if unknown['api_cache_write_tokens'] is None else unknown['api_cache_write_tokens'],
                      unknown['missing_api_cache_write_usage'], '未知', '模型未记录', unknown['api_output_tokens'], '未知']
            markup += '<tr>' + ''.join('<td>' + html.escape(str(value)) + '</td>' for value in fields) + '</tr>'
        total = method['api_cost_at_declared_rates_usd']
        missing = method.get('missing_response_model_requests')
        identity = '返回身份记录完整' if method.get('response_model_observation_complete') else '返回身份记录不完整或未知'
        label = '全部计划运行的 API 总费用估算；' + identity + '；缺失：' + ('未知' if missing is None else str(missing))
        roles = method.get('api_cost_by_role')
        if roles:
            names = dict(main='主请求', summary='摘要/反思', native_compaction='原生压缩', passthrough='旁路调用')
            label += '；按调用方：' + '，'.join(names[k] + ' ' + ('未知' if v is None else f'{v:.6f}') for k, v in roles.items())
        markup += '<tr><td>' + html.escape(method['method']) + '</td><td colspan="14">' + html.escape(label) + '</td><td>' + ('未知' if total is None else f'{total:.6f}') + '</td></tr>'
    markup += '</tbody></table>'
    if any(method.get('code_metrics') for method in result['methods']):
        markup+='<h2>代码样本与 pass@k</h2><p>每题使用独立 Codex 会话生成样本。指标由冻结作者估计器汇总；缺少样本或评分证据时保持未知，不计入仓库任务解决率。解码由模型服务控制，本入口未声明温度或 top-p。</p>'
        markup+='<table><thead><tr><th>方法</th><th>题数</th><th>每题样本数</th><th>有效/计划样本</th><th>pass@k</th><th>缺项</th></tr></thead><tbody>'
        for method in result['methods']:
            metrics=method.get('code_metrics')
            if not metrics:continue
            scores=', '.join('pass@'+k+': '+('未知' if value is None else f'{value:.1%}') for k,value in metrics['pass_at_k'].items())
            fields=[method['method'],metrics['task_count'],metrics['samples_per_task'],
                    str(metrics['valid_samples'])+'/'+str(metrics['planned_samples']),scores,json.dumps(metrics['unavailable'],ensure_ascii=False)]
            markup+='<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in fields)+'</tr>'
        markup+='</tbody></table>'
    if any(job.get('separate_verifier') for method in result['methods'] for job in method['jobs']):
        markup += '<h2>独立官方评分环境</h2><p>分别保存 Agent 与 verifier 镜像和清理证据；ctxpress 对照实验不等于模型发布原协议复现。合成检查不构成真实运行验证。</p>'
        markup += '<table><thead><tr><th>方法</th><th>任务</th><th>运行协议</th><th>Agent 镜像</th><th>Verifier 镜像</th><th>真实运行已验证</th></tr></thead><tbody>'
        for method in result['methods']:
            for job in method['jobs']:
                separate = job.get('separate_verifier')
                if not separate:
                    continue
                fields = [method['method'], job['task_id'], job.get('protocol', '未声明'),
                          separate.get('agent_image'), separate.get('verifier_image'), job.get('real_run_verified', False)]
                markup += '<tr>' + ''.join('<td>' + html.escape(str(value)) + '</td>' for value in fields) + '</tr>'
        markup += '</tbody></table>'
    if any(job.get('gpu_device_ids') or job.get('verifier_gpu_device_ids') for method in result['methods'] for job in method['jobs']):
        markup += '<h2>GPU 运行环境</h2><p>设备证据来自实际任务容器的查询，单独保留数量、型号、显存和驱动；硬件查询不证明 CUDA 工作负载或任务效果。</p>'
        markup += '<table><thead><tr><th>方法</th><th>任务</th><th>阶段</th><th>状态</th><th>声明的设备 UUID</th><th>观察到的设备</th></tr></thead><tbody>'
        for method in result['methods']:
            for job in method['jobs']:
                for phase, binding, observation in (('Agent', 'gpu_device_ids', 'gpu'),
                                                    ('Verifier', 'verifier_gpu_device_ids', 'verifier_gpu')):
                    if not job.get(binding):
                        continue
                    fields = [method['method'],job['task_id'],phase,job['status'],', '.join(job[binding]),
                              json.dumps(job[observation],ensure_ascii=False) if job.get(observation) else '未记录']
                    markup += '<tr>' + ''.join('<td>' + html.escape(str(value)) + '</td>' for value in fields) + '</tr>'
        markup += '</tbody></table>'
    if any(method['reward_metrics'] for method in result['methods']):
        markup += '<h2>官方 verifier 数值指标</h2><p>各指标保留原始尺度；均值不转换为任务解决率。</p>'
        markup += '<table><thead><tr><th>方法</th><th>指标</th><th>有效结果数</th><th>均值</th><th>最小值</th><th>最大值</th></tr></thead><tbody>'
        for method in result['methods']:
            for name,metric in sorted(method['reward_metrics'].items()):
                fields = [method['method'], name, metric['count'], metric['mean'], metric['minimum'], metric['maximum']]
                markup += '<tr>' + ''.join('<td>' + html.escape(str(value)) + '</td>' for value in fields) + '</tr>'
        markup += '</tbody></table>'
    if result['comparison'] is not None:
        comparison = result['comparison']
        markup += '<h2>与基线的逐边界比较</h2><p>参考方法：' + html.escape(comparison['reference']) + '。完整观测解决率须在每个边界上不低于参考方法；这不是统计非劣效证明或新任务质量保证。</p>'
        markup += '<p>费用按声明费率逐请求估算已报告的主模型、摘要/反思及原生压缩 API 用量，不代表 ChatGPT 实际账单；专用模型、宿主计算和完整账单尚未计入。合成结果与缺失证据不产生达标结论。</p>'
        verdicts = dict(mechanism_only='仅机制检查',incomplete_or_unverified='证据不完整',observed_quality_regression='逐边界质量约束未满足',
            observed_quality_constraint_met_cost_unavailable='观测质量约束通过；费用未知',observed_quality_constraint_met_api_cost_lower='观测质量约束通过；API 费用下降',
            observed_quality_constraint_met_api_cost_not_lower='观测质量约束通过；API 费用未下降')
        markup += '<table><thead><tr><th>方法</th><th>结论范围</th><th>基线 API 费用 USD</th><th>方法 API 费用 USD</th><th>节省 USD</th></tr></thead><tbody>'
        for candidate in comparison['candidates']:
            fields = [candidate['method'],verdicts[candidate['verdict']]]
            fields += ['—' if candidate[key] is None else f"{candidate[key]:.6f}" for key in ('reference_api_cost_usd','candidate_api_cost_usd','api_saving_usd')]
            markup += '<tr>'+''.join('<td>'+html.escape(value)+'</td>' for value in fields)+'</tr>'
        markup += '</tbody></table>'
    markup += '</html>'
    (directory / "report.html").write_text(markup, encoding="utf-8")
    return dict(report=str((directory / "report.json").resolve()), html=str((directory / "report.html").resolve()),
                methods=len(result["methods"]), completed=sum(method["completed"] for method in result["methods"]))
