# Reproducing the original methods

These scripts compare ctxpress methods with the authors' own code or released data. The SWE-Pruner model
check runs a local GPU model; the other comparisons make no model calls.
The originals and the recorded Codex sessions are not in this repository. The scripts look for them in
`$CTXPRESS_DATA` (default: `../data`, next to this repository): originals under `repro/`, sessions under `tb4-jobs/`.

| Directory | Source |
|---|---|
| `agentdiet/` | figshare 10.6084/m9.figshare.30073654 (CC BY 4.0): `artifact/README.md` and `artifact/code/` only, read from the zip with HTTP range requests (`remote_zip.py`); the 46.6 MB `trajs.7z` not downloaded |
| `pi-cwl/` | github.com/Kiz8-Team/pi-cwl @ cd2ea3f (MIT): `core/context-filter.ts`, `core/chunk.ts`, `core/tools/delimiter.ts`, `core/compaction/compaction.ts` and related tests only |
| `dtoc/` | github.com/chaturvediabhay24/opencode @ 57bf2e1 (MIT, fork of anomalyco/opencode): DTOC files (`session/dtoc.ts`, `tool/manage_context.ts/.txt`, `command/template/dtoc.txt`, `session/message-v2.ts`, `session/prompt.ts`) and `dtoc-vs-upstream.diff` (GitHub compare) only |
| `the-complexity-trap/`, `ct-traj/` | github.com/JetBrains-Research/the-complexity-trap (commit bf15b5f); HF dataset JetBrains-Research/the-complexity-trap (sweagent, Gemini 2.5 Flash: `baseline_N_1_M_10` and `baseline_raw`) |
| `cliffcompaction/` | github.com/nguyenvuthientrang/cliffcompaction |
| `clawvm/` | github.com/mpi-dsg/clawvm |
| `pichay/` | github.com/fsgeek/pichay |
| `swe-pruner/` | github.com/Ayanami1314/swe-pruner (with the 0.6B model) |

| Script | What is compared | Result |
|---|---|---|
| `complexity_trap_compare.py` | Our `ComplexityTrap(10)` on the raw history of every step of all 500 released masking trajectories vs. the messages their SWE-agent actually sent; plus the paper's headline numbers from their run metadata and evaluation files | 1,074,554 / 1,074,554 observations identical (21,955 steps). Solve rate 35.6% vs 32.8% (paper 35.6 / 32.8), cost per instance $0.177 vs $0.415 (paper 0.18 / 0.41) |
| `complexity_trap_summary_compare.py` | Our `ComplexityTrapSummary(21, 10)` and `ComplexityTrapHybrid(43, 10, 10)` vs. the authors' unmodified `SummarizeEveryNTurns` (+ `LastNObservations`) and their summary prompt builder, configs `..._N=21_M=10.yaml` / `..._N=43_M=10_masking_M=10.yaml`, on the raw history of every step of all 500 released raw trajectories; both sides use the same deterministic stand-in summary (a hash of system + user prompt), so equal summaries imply equal prompts | 25,014 / 25,014 requests identical in each mode (132 steps after the harness's own exit message skipped: no model query). LLM-Summary: 173 trajectories reach a checkpoint, 106 two or more; hybrid: 106 and 58, 356,819 masked observations. No real summary model |
| `agentdiet_compare.py` | Our `AgentDiet(reflect_model="gpt-5-mini-2025-08-07")` vs. the authors' unmodified `traj_analyzer.py` and their `MessageManager` (from `agents/expert.py`), on the steps of all 500 released Complexity Trap raw trajectories, with the same deterministic stand-in reflection model (2/3 short rewrites, 1/3 rejected long ones; ours strips its added output-format sentence before hashing). tiktoken is stubbed on their side to our token count | 19,377 / 19,377 steps identical: 10,569 reflections, 6,954 rewrites accepted, 3,615 rejected. No real reflection model; the released trajectories with real rewrites are in the undownloaded `trajs.7z` |
| `cwl_compare.py` | Our `CWL` vs. the authors' `filterContext` and `delimiter` tool run by Node (`--experimental-transform-types`; their files unmodified except `.js` -> `.ts` import paths, `estimateTokens` copied verbatim, UI / schema packages stubbed), on 300 seeded random annotated sessions (seed 1: valid and rejected delimiter calls, dependencies, open episodes, user messages inside episodes, read / grep / find / ls / glob / bash / edit / write, thresholds 1.5k-12k), compared after every round | 19,822 / 19,822 rounds identical (seed 1); 25,054 message blocks evicted by the end. Sessions include emoji / CJK / accented text and host-style tool arguments (spaced, ASCII-escaped). Synthetic sessions: there is no released delimiter-annotated trajectory; measuring the raw argument string instead of JSON.stringify of the parsed arguments differs in 3 of the first 1,777 rounds |
| `dtoc_compare.py` | Our `DTOC` vs. the authors' `session/dtoc.ts` registry and `tool/manage_context.ts` `execute` run by Node (import paths changed; `effect`, the Tool module and the .txt import stubbed), with the visible / hidden envelopes as the two `JSON.stringify` expressions copied verbatim from `session/message-v2.ts`, on 300 seeded random sessions (seed 1: parallel calls, existing / unknown / repeated / already-hidden keys, invalid arguments) | 9,726 / 9,726 rounds identical: 4,113 manage_context calls, 3,121 outputs hidden at the end. Synthetic sessions: there is no released DTOC trajectory |
| `cliff_compare.py` | Our `CliffCompaction` (exact port) vs. their `Engine` (Responses dialect), every request of every recorded Codex session, thresholds 20k / 50k / 100k | 25,773 / 25,773 requests byte-identical (1,661 compactions, 17,847 substitutions); `runs/repro/cliff_<threshold>.jsonl` |
| `cliff_reactive_compare.py` | Synthetic Responses histories at 4 thresholds and 2 head sizes, comparing proactive preparation, each reactive request and rung, exhaustion, and the next appended-history request with the local author's engine | 8 cases match. No real provider or task execution; HTTP recognition and transport are checked separately with loopback fixtures. The common proxy recognizes explicit error codes/messages more conservatively than the author's broad raw-body matching. |
| `pichay_compare.py` | Our `Pichay` pager vs. their `MessageStore` + pager + fault detection, every request, age 4 and 2 | Refactored comparison: 17,114 / 17,182 requests byte-identical; 68 have a tool-description difference due to the author's append-only message store. Operation counts match (934 evictions, 84 page faults). `exact` counts raw equality, `same` includes only this explicitly checked store quirk, and `store_quirk` counts the exception. Earlier `pichay.jsonl` predates the framework refactor; use `pichay_4.log` / `pichay_2.log` for that rerun. |
| `clawvm_compare.py` | Their Tier-2 simulator and their runtime engine, each with the selector swapped for ours | 144 / 144 rows identical on both paths; their run equals their committed reference; runtime-engine means match the paper's Table 4 (Retrieval 67.75 vs 67.8 explicit faults, Retr.+Cache 10.75 vs 10.8, Comp-Hybrid 1.42 vs 1.5, ClawVM 0 vs 0, thrash 0.901 vs 0.901) |

What each port leaves out, and how it is mapped onto Codex, is in the method's docstring and in `ctxpress list`.
The recorded full-corpus counts above belong to the individual comparison runs, not to every subsequent
unit-test run. The new UTF-16 edge checks directly run the CWL and DTOC TypeScript on emoji, mixed text,
and exact threshold boundaries (`tests/test_unicode_ports.py`); they require a local Node supporting
`--experimental-transform-types` (the present check used installed Node 24.14.1). With Windows Node,
pytest's temporary directory must be on a Windows-accessible filesystem. No packages are installed by
these checks. The fake-summary/reflection and token-library substitutions above remain limitations.

`ctxpress doctor codex --codex-bin /path/to/codex --offline-tools-check --output runs/codex-tools.json`
exercises the installed Codex, real MCP server and proxy against a local
synthetic API. It checks the next request after `delimiter` and `manage_context`, including eviction,
dependency restoration and hide/restore acknowledgements. It uses an empty temporary Codex home and
fake credentials, blocks auxiliary outbound connections, and saves requests/proxy logs only under the
chosen local run directory. It does not establish task scores or real API billing.

`python -m repro.evaluation_inventory` (from the repository root) inventories the fixed eight families,
installed CLI, local task data and existing images without pulling images or running a model/container.
The fixed protocol is [configs/experiment.protocol.json](../configs/experiment.protocol.json); how to run it is in [docs/evaluation.md](../docs/evaluation.md).

Every ported method is written with the framework's own operations (`ctxpress/core`), not by vendoring the
original code; where the framework lacked a concept (host request sizes, turns, method-written summaries), it was
added to the framework.

The earlier rule approximations are kept under other names (`ClearThenSummarize`, `PichayApprox`, `ClawVMApprox`)
because the website's §8–§9 replay results were computed with them.

## SWE-Pruner

Three different checks are kept separate:

1. `python repro/swe_pruner_compare.py --output runs/repro/swe_pruner_adapter.json`
   extracts the author's client and output-handling functions from local source (hashes in the report),
   supplies controlled service responses, and compares the actual Responses requests. All 40 cases match,
   including empty output, missing question, the 500-character boundary, unchanged output, and errors.
   This proves adapter behavior for those branches, not model accuracy or published task scores.
2. `python repro/swe_pruner_inputs.py` downloads SWE-bench Verified metadata and source files only after
   permission. It parses, never executes, trajectory commands. Only simple `cat`, `cat -n`, `nl`, and `sed -n`
   reads before any possibly mutating command are accepted. The current local data yields 405 input pairs;
   later reads, unsupported commands, and truncated observations are excluded. Each pair carries the base
   commit, source hash, recorded pruning settings and expected token counts.
3. Run `python repro/swe_pruner_model.py` in the author's already-installed model environment. It loads
   local weights with network access disabled in HuggingFace, compares the output with the published
   trajectory, compares token counts separately, and also runs each real response through the adapter check.
   Per-case results are flushed to `runs/repro/swe_pruner_model.jsonl`; the final report records weights,
   source hashes, device and library versions. A text/count mismatch remains a mismatch in the report.

The model script defaults to native attention. `--attention sdpa` disables unused fusion-attention weights;
the backend is recorded as a reproduction difference. The completed 405-case SDPA check has 271 exact
published-text matches, 405 source-token matches, and 405 adapter matches, with no model errors. All model
input counts are one token below the recorded counts. These discrepancies are unresolved, so this is not a
complete model reproduction. A diagnostic rerun of the 100 smallest inputs with native attention produced
the same pruned text as SDPA in all 100 cases; both match the published text in 93 cases. This does not establish
backend equivalence for the remaining inputs. Local reports are `runs/repro/swe_pruner_model.summary.json`
and `runs/repro/swe_pruner_model_native100.summary.json`; each records input, source and weight hashes.

These scripts do not rerun SWE-bench tasks or reproduce aggregate solve rates. The adapter script also
accepts `--records` JSONL containing `text`, `query`, and `response` to check another set of real service outputs.

`swe_pruner_model.py` now defaults to `--acceptance published`: any published-text or token-count mismatch
returns a failing exit status. `--acceptance adapter` explicitly tests only integration. An empty run never
passes. Existing evidence files are unchanged. Inspect them without loading a model using:

```bash
python repro/swe_pruner_diagnose.py --inputs /prepared/inputs.jsonl --results /recorded/model.jsonl --output runs/repro/new-diagnosis.json
```

The offline diagnostic joins by trajectory/message, verifies source identity, rejects duplicate rows,
recomputes equality instead of trusting flags and records file hashes. The existing 405-case data still
has 271 text matches and a -1 model-input-token offset in every case; neither discrepancy is fixed or
explained by that audit. Tokenizer/model reruns require the already prepared dependencies.

## New shared-interface adaptations

Runtime methods remain in `ctxpress.methods` and use the same Codex/Claude/Python/evaluation interfaces.
These scripts only execute author code as independent test oracles; no new baseline runner is introduced.
Source files must match the pinned LF-normalized hashes in `reference_sources.json` before execution.
Raw file hashes are also recorded in each report; scripts never download source or install dependencies.

```bash
python repro/acon_compare.py --original /prepared/acon --output runs/repro/acon.json
python repro/tokenpilot_compare.py --original /prepared/LightRSI --node node --output runs/repro/tokenpilot.json
python repro/acm_compare.py --original /prepared/agentic-context-management --output runs/repro/acm.json
```

| Comparison | Observed checks | Limits |
|---|---|---|
| ACONSource vs Microsoft ACON `d63f9ae` | 117/117 template, rendered-prompt, argument, parsing and threshold checks | Requires existing Jinja2 for independent template rendering; same substituted token counter; no real compression model, optimized guideline search or distillation |
| TokenPilotLifecycle vs LightRSI `9f0f193` | 300/300 seeded selection cases; unmodified TypeScript analyzer under Node 24 | Requires local Node >=22.7; only candidate selection, not LLM judgments, ingestion or the entire runtime. On WSL with Windows Node, place output on a Windows-accessible filesystem |
| ACM vs author `f06f90e` | 500/500 normalized active histories; 127/127 normalized archives | Identical fake summaries; exact, documented empty-range error normalization; no real model, query-memory, restart or parallel-call comparison |

AgentFoldTools implements autonomous granular/deep folding through the shared method tool protocol. It is
a paper-mechanism adaptation with host-contract tests, not an author-code or trained-model reproduction.
