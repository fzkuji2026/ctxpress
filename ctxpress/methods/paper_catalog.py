"""Fixed online-context paper cohort. Metadata and config preparation, not a runner."""
from copy import deepcopy


VERSION = 'online-context-papers-20261012-v2'

# Public author code includes the AgentDiet archived research artifact. Do not
# substitute similarly named repositories, legacy heuristics or memory systems.
PAPERS = (
    dict(id='2508.21433', name='The Complexity Trap', repository='https://github.com/JetBrains-Research/the-complexity-trap',
         methods=['ComplexityTrap', 'ComplexityTrapSummary', 'ComplexityTrapHybrid'],
         comparison=['repro/complexity_trap_compare.py', 'repro/complexity_trap_summary_compare.py'],
         limits='Summary comparisons use deterministic model substitutes; host trajectories and real model outputs differ.'),
    dict(id='2509.23586', name='AgentDiet', repository='https://doi.org/10.6084/m9.figshare.30073654',
         source_kind='author research artifact', methods=['AgentDiet'], comparison=['repro/agentdiet_compare.py'],
         limits='Fake reflection model and token/compression libraries in source comparisons; Responses prefill is adapted.'),
    dict(id='2510.00615', name='ACON', repository='https://github.com/microsoft/acon',
         methods=['ACONSource'], comparison=['repro/acon_compare.py'],
         limits='Published AppWorld guidelines and optimizer protocol only; no guideline optimization or distillation.'),
    dict(id='2509.13313', name='ReSum', repository='https://github.com/Alibaba-NLP/DeepResearch/tree/main/WebAgent/WebResummer',
         methods=['ReSumSource'], comparison=['repro/resum_compare.py'],
         limits='Author inference prompts and trigger; no ReSumTool weights or GRPO training; host turn/token mapping differs.'),
    dict(id='2510.24699', name='AgentFold', repository='https://github.com/Alibaba-NLP/DeepResearch/tree/main/WebAgent/AgentFold',
         methods=['AgentFoldSource'], comparison=['repro/folding_compare.py'],
         limits='Author step replacement/formatting and visible compress-block parsing; native host tool execution, protected history and prompt serialization differ; trained policy not loaded.'),
    dict(id='2601.16746', name='SWE-Pruner', repository='https://github.com/Ayanami1314/swe-pruner',
         methods=['SWEPruner'], comparison=['repro/swe_pruner_compare.py'],
         requires=['author pruning model service'],
         limits='Requires real pruning endpoint; 271/405 published text matches, residual differences unresolved; heuristic fallback excluded.'),
    dict(id='2606.17016', name='TokenPilot', repository='https://github.com/zjunlp/LightRSI',
         methods=['TokenPilotLifecycle'], comparison=['repro/tokenpilot_compare.py'],
         limits='Author selection core; host lifecycle estimation prompt, serialization and scheduling differ.'),
    dict(id='2607.23809', name='ACM', repository='https://github.com/lixiaochuan2020/agentic-context-management',
         methods=['ACM'], comparison=['repro/acm_compare.py'],
         limits='Within-task context editing; common host policy, not author trained 9B policy. Fake summaries in source tests.'),
    dict(id='2606.11213', name='CWL', repository='https://github.com/Kiz8-Team/pi-cwl',
         methods=['CWL'], comparison=['repro/cwl_compare.py'],
         limits='Author filter and delimiter port; cannot prevent native host file edits inside exploration chunks.'),
    dict(id='2609.26121', name='DTOC', repository='https://github.com/chaturvediabhay24/opencode',
         methods=['DTOC'], comparison=['repro/dtoc_compare.py'],
         limits='Author output registry/visibility port; host timestamps, strategy instruction delivery and error mapping differ.'),
    dict(id='2510.11967', name='Context-Folding', repository='https://github.com/MiaoLu3/Context_Folding',
         methods=['ContextFoldingTools'], comparison=['repro/context_folding_compare.py'],
         limits='Host branch/return and request-boundary budget/fallback adaptation; cannot interrupt in-flight native tools or reproduce separate author generation/RL policy.'),
    dict(id='2512.22733', name='FoldAct', repository='https://github.com/SHAO-Jiaqi757/FoldAct',
         methods=['FoldActSource'], comparison=['repro/folding_compare.py'],
         limits='Author inference window selector, observation-prefix clipping and persistent forced last-three-turn mode; host token estimates and native turn grouping differ; no trained weights or loss functions.'),
    dict(id='2604.19572', name='TACO', repository='https://github.com/multimodal-art-projection/TACO', methods=['TACO'], comparison=['repro/context_additions_compare.py'], limits='作者规则过滤、生成与反馈修正规则；任务内规则池，未复现跨任务演化实验；辅助模型走统一计费。'),
    dict(id='2605.30842', name='CoMem', repository='https://github.com/horizon-llm/CoMem', methods=['CoMem'], comparison=['repro/context_additions_compare.py'], limits='作者提示词与异步k步摘要；统一宿主消息与计数适配，未复现训练及推理引擎预填优化。'),
    dict(id='2605.05191', name='LongSeeker', repository='https://github.com/PolarSeeker/LongSeeker', methods=['LongSeeker'], comparison=['repro/context_additions_compare.py'], limits='作者五种上下文操作；统一宿主工具接口与调用配对保护，未加载训练权重。'),
    dict(id='2605.28069', name='ZipRL', repository='https://github.com/huzhexin/ZipRL', methods=['ZipRL'], comparison=['repro/context_additions_compare.py'], limits='作者摘要等级提示词与解析器；原搜索循环适配为宿主工具，未复现RL训练与权重。'),
)


# The control runs under the same window: the host's own compaction alone. Each paper method is read against it.
CONTROLS = ('HostDefault',)
LABELS = {'HostDefault': 'Vanilla Codex'}  # the cohort runs on Codex; class names stay host-neutral


def needs_extra_model(paper):
    """A method that needs its own deployed model is a different kind of comparison; it joins the
    cohort only when that service is given explicitly."""
    return bool(paper.get('requires'))


def describe(window=None):
    from ctxpress.methods import REGISTRY
    from ctxpress.methods.paper_profile import WINDOW
    window = window or WINDOW
    papers = deepcopy(list(PAPERS))
    for paper in papers:
        paper['paper_url'] = 'https://arxiv.org/abs/' + paper['id']
        paper['interface_registered'] = all(name in REGISTRY for name in paper['methods'])
        paper['full_paper_reproduction'] = False
    from ctxpress.methods.paper_profile import describe as profile_description
    return dict(version=VERSION, context_profile=profile_description(window), papers=papers, paper_count=len(papers),
                method_count=sum(len(p['methods']) for p in papers), controls=list(CONTROLS),
                extra_model_methods=[name for p in PAPERS if needs_extra_model(p) for name in p['methods']],
                scope='Online interaction-history and tool-output compression with public author code; fixed cohort.',
                evidence='Source-function comparisons and host contract tests are not trained-policy or real benchmark equivalence.')


def method_entries(pruner_url=None, *, normalized=True, window=None):
    from ctxpress.methods import build
    from ctxpress.methods.paper_profile import WINDOW, arguments
    entries = []
    papers = [paper for paper in PAPERS if pruner_url or not needs_extra_model(paper)]
    for name in (*CONTROLS, *(name for paper in papers for name in paper['methods'])):
        args = arguments(name, window or WINDOW) if normalized else {}
        if name == 'SWEPruner' and pruner_url:
            args['url'] = pruner_url
        entry = {'class': name, 'args': args, 'label': LABELS.get(name, name)}
        build(entry)
        entries.append(entry)
    return entries


def validate_entries(entries, require_services=True):
    from ctxpress.methods import build
    allowed = {name for p in PAPERS for name in p['methods']} | set(CONTROLS)
    if not isinstance(entries, list) or not entries:
        raise ValueError('paper cohort requires a nonempty methods list')
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('class') not in allowed:
            raise ValueError('method is outside the fixed online-context paper cohort')
        name = entry['class']
        if name in seen:
            raise ValueError('duplicate paper method: ' + name)
        seen.add(name)
        method = build(entry)
        if name == 'SWEPruner' and require_services:
            method.validate_live()  # Never silently substitute definition-line heuristics.
    return True


def configure(base, pruner_url=None, window=None):
    """Replace an explicit new experiment's methods (settings for `window`); never launch or alter old runs."""
    config = deepcopy(base)
    config['methods'] = method_entries(pruner_url, window=window)
    validate_entries(config['methods'])
    config['workers'] = 1
    if isinstance(config.get('comparison'), dict):
        config['comparison']['reference'] = LABELS['HostDefault']
    return config
