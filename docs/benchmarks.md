# Benchmark 适配器

9 个 benchmark 家族共用 `ctxpress eval` 的计划、执行、恢复和报告入口；本页说明各适配器怎样读取任务、执行 Agent、调用官方评分以及需要准备哪些资源。命令与配置见 [评测文档](evaluation.md)。各家族目前各有一个原始任务的真实执行与官方评分证据，下文逐项写明验证边界。

`ctxpress eval tasks --start-mode task_start --data /path/to/SWE-Milestone-data` 读取完整仓库 itinerary、里程碑、依赖与评分输入。native worker 已接入作者 Trial、新会话/恢复、watcher、DAG、Git 提交捕获、异步评分、collector 和所属 testcontainers 服务管理；实际作者代码已与生产资源组件在合成 IO 下组合核对。`execution_supported` 与 `official_grading_supported` 为 true，范围为已准备的 Linux CPU itinerary；完整 Navidrome 已取得真实官方评分。静态目录标志的含义见下文。启动要求 `run.grade=true`、真实 Git 版本证据 `native_data_version=true`、每个活动里程碑的准备评分镜像及明确的 `service_images`；缺项计划拒绝启动。服务 API 不支持的挂载/网络请求返回明确错误，具体任务的真实执行另行验证。

本地任务发现示例（只读，不下载或启动实验）：

```bash
ctxpress eval tasks --benchmark swe-bench-verified --start-mode task_start --data /prepared/swe-bench
ctxpress eval tasks --benchmark terminal-bench --start-mode task_start --data /prepared/terminal-bench/tasks
ctxpress eval tasks --benchmark terminal-bench-science --start-mode task_start --data /prepared/terminal-bench-science/tasks
ctxpress eval tasks --benchmark deep-swe --start-mode task_start --data /prepared/deep-swe/tasks
ctxpress eval capture-task-resources --benchmark terminal-bench --data /prepared/terminal-bench/tasks --spec /prepared/resource-spec.json --task task-id --output /prepared/task-resources.json
```

SWE-bench 数据目录需要一个 `instances.jsonl` 或 `instances.json`；可用 `dataset_manifest.json` 声明 `dataset` 和 `revision`，不同子集的声明不得互换。完整原始数据仅作为评分输入，Agent 说明从白名单字段生成；适配器可导出官方预测 JSONL 并读取实例报告。Terminal-Bench 当前读取 Harbor 格式（`task.toml`、`instruction.md`、`environment/`、`tests/test.sh`），支持 Science 的学科目录，不读取旧版 `task.yaml`；参考解不进入 Agent 输入。官方数值 reward 保留指标名称和尺度，报告分别汇总，不自动换算为解决率。

SWE-bench 的执行需要明确的数据 revision（与资源 release 相同）、已准备的 Linux CPU Agent/verifier 镜像、Linux/Python ≥3.10、冻结的 `swebench`/`dependencies` 树及固定 Codex。旧版 harness 使用额外 `test_specs/<instance_id>.json` 恢复官方 TestSpec 并核对本地评分脚本；新版需要包含 image/eval_script/log_parser/eval_type 的数据，并在所选任务资源中明确 `verifier_cap_add: ["SYS_ADMIN"]`。不运行环境生成器、build/pull 或多模态资源下载。两个容器均在 `/testbed` 核对干净基准提交；新 Codex 会话结束并完成进程/凭据清理后，导出包含提交、暂存和新增文件的补丁，由全新 verifier 内的官方 `run_instance` 应用、测试和评分。空补丁保留，缺失官方报告或不完整评分明确计为评分错误，不计任务质量。示例见 [SWE-bench 配置](../configs/swe_bench.example.json)，准备细节见 [评测文档](evaluation.md)；Verified 已取得原始任务的真实官方评分，未覆盖其他子集。

Science 复用同一 Harbor 任务读取、冻结计划、Trial 执行与官方评分入口，但要求数据根目录的 `dataset_manifest.json` 声明 `dataset: "terminal-bench-science"` 和明确 `revision`（例如 `"0.1.0"`）。它与 Terminal-Bench 分别绑定任务身份，不允许将 Terminal-Bench 的来源声明直接改作 Science 使用。两者的 CPU 首题已取得真实官方 reward；NVIDIA GPU 仍只有合成契约检查，未验证真实 GPU 任务。

`capture-task-resources` 的 JSON specification 使用 `schema: "ctxpress.eval.task_resource_spec"`、`version: 1`，声明 `benchmark`、`release`、所选 `tasks`（每项包含 `agent_image` 和 `grading_images`），以及 `trees`（每项包含 `root`、明确选择的 `files` 和可选空 `directories`）。命令只检查已存在的本地镜像和文件；输出清单通过 `environment.resources` 绑定到 `start_mode: "task_start"` 的计划。共用计划使用 `scope: "mechanism"` 或 `"benchmark"`，不冒充 ≥128k 录制边界的正式比较。数据、官方代码和依赖复制后可以移走原来源；凭据不进入副本，宿主 Python 和系统库仍非完整冻结环境。

多容器任务可在每项任务声明可选 `service_images`，例如 `{"database": "prepared-db:version"}`；主服务仍由 `agent_image` 声明，不在 `service_images` 中重复 `main`。辅助服务逐个固定为本地镜像内容 ID，缺失镜像直接报错；声明资源不会创建容器或扩大任务选择。

Harbor 执行入口通过独立子进程调用官方 `Trial` 与 verifier，使用固定 CLI，保留官方提示渲染/轨迹读取，模型请求经过 ctxpress。需要资源清单中的 `harbor` 树（官方仓库布局，包括 `pyproject.toml` 和 `src/harbor/`）及 `dependencies` 树（已准备的完整传递依赖与真实 Harbor dist-info），固定运行环境为 Linux/Python ≥3.12；子进程以 `-I -S -B` 启动，只增加冻结输入的导入路径，启动前检查源码/元数据版本，不自动安装依赖。凭据通过运行时 `CTXPRESS_CODEX_AUTH_FILE` 指定，只上传到容器私有目录，不进入计划或日志副本。

每个服务使用固定镜像，保留 CPU/内存限制与内部通信；模型通信使用 Unix socket 到本地回环转发，可接声明的 HTTP 上游代理。工具预算或取消会先停止容器内 Agent 进程组、核对删除凭据，再执行官方评分/清理。每个 trial 在容器启动前记录资源及凭据是否可能已上传；重试时检查 Docker daemon、项目/运行标签与镜像身份后恢复，不删除任务镜像。GPU 初始化失败且未上传凭据时可直接删除所属容器，无需先成功启动 GPU。Agent 超时或非零退出后仍可保留官方 verifier 的有效 reward，同时记录 Agent 异常。未绑定阶段 GPU、未声明评分镜像以及缺失运行依赖会列入计划缺项，不能默默跳过任务。

GPU 任务在资源 specification 的对应 task 中声明 `gpu_device_ids`：完整 NVIDIA GPU UUID 列表，数量必须等于 `task.toml` 的 `environment.gpus`。捕获仅验证声明并检查本地镜像，不启动 GPU 发现容器；主服务通过 [Compose device reservation](https://docs.docker.com/compose/how-tos/gpu-support/) 使用这些 UUID，辅助服务不获得 GPU。容器启动后、上传凭据前，用 `nvidia-smi` 核对实际 UUID、数量与 `gpu_types`，记录型号、显存和驱动；型号不符、设备缺失或查询失败直接停止。CUDA 镜像中的可见设备环境按 [NVIDIA GPU enumeration](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/docker-specialized.html) 固定，CPU/辅助服务不继承 `all` 默认值。目前支持完整 NVIDIA GPU，MIG 与辅助服务单独请求 GPU 尚未支持。

同宿主/用户的 ctxpress 任务按 Docker daemon + GPU UUID 取得进程间锁：同卡排队，不同卡可并行，Agent 超时从获得资源并开始执行后计算。同一计划的调度器将冲突 GPU 任务保留为 pending，CPU/其他 GPU 任务可继续使用空闲 worker 槽位。失败保留的 GPU 容器会阻止下一任务抢占；外部工作负载没有由该锁排除。硬件证据进入单次结果、资源日志和 JSON/HTML 报告，驱动版本仍是观察值，未整体冻结。当前验证包括合成 GPU/Trial 契约、本地真实 socket/进程与跨进程锁检查，没有真实 GPU/CUDA 或正式模型运行；Harbor/Pier 的隔离依赖与首题镜像已准备，CPU 首题证据见上文；这不表示 TB 4.0/Science 全量或模型发布协议已复现。

`deep-swe` 已提供单服务 CPU 任务的从头执行与独立官方评分代码：冻结本地 v1.1 任务和 `pier`/`dependencies` 输入树，用 Pier >0.3.0 的官方 `Trial.create`、collect hook 和独立 verifier。`agent_image` 与 `grading_images.verifier` 必须是不同的已准备镜像，评分镜像提前包含作者 `tests/`；启动 verifier 后按冻结文件哈希核对其中的评分脚本。Codex 运行时没有测试/评分日志挂载，退出后检查进程停止和凭据删除，再执行作者的已提交 patch 捕获，停止 Agent 环境，向全新 verifier 传入 `model.patch`。原始 artifacts、CTRF、reward 与日志保留路径/哈希；作者的二元 `reward` 按成功/失败读取，`-1` 崩溃标记计为基础设施无效，测试比例/数量保留原值。配置见 [DeepSWE 示例](../configs/deep_swe.example.json)，准备要求见 [评测文档](evaluation.md)。一个原始 CPU 任务已通过真实独立官方评分，不能推广为完整 DeepSWE 成绩；不自动下载任务、镜像或安装依赖。

Terminal-Bench/Science 现在按冻结源码选择旧 `Trial` 或新版 `SingleStepTrial` API。新版调用官方 `Trial.create`，保留作者 collect、多服务产物收集及 ArtifactHandler，支持共享评分和单服务独立评分；后者在 Agent 容器清理确认后启动，评分环境只取得 verifier 日志和官方传入产物。资源中可分别声明 `gpu_device_ids` 与 `verifier_gpu_device_ids`，调度与宿主锁覆盖两阶段的 UUID，报告分阶段保存硬件证据。独立评分需声明 `grading_images.verifier`；作者配置或 tests/Dockerfile 定义评分镜像时，镜像内测试须匹配冻结文件。模型通信仍限于 socket，工具服务使用私有网络，记录为 `ctxpress_comparison`；网络 allowlist/动态切换、多步骤、评分侧 Compose 多服务与 MIG 尚不支持。只有新版完整入口才能运行 separate/collect；旧版本和过渡 API 明确拒绝。代码与合成生命周期检查不构成真实官方运行验证。

`swe-polybench` 已接入 Full/500/Verified 本地 JSON/JSONL、四种语言与 Bug Fix/Feature/Refactoring 分类，经共用 `eval` 计划/后台执行/恢复/报告入口运行。任务 manifest 声明 `AmazonScience/SWE-PolyBench`、`AmazonScience/SWE-PolyBench_500` 或 `AmazonScience/SWE-PolyBench_Verified` 与 revision；Agent 仅取得问题和干净仓库。冻结 `polybench`、`dependencies` 后调用作者 `evaluate_instance`、补丁应用、语言 parser 和评分函数，以独立准备镜像重新评分；两阶段镜像的 repository WorkingDir 必须相同。保留作者 F2P/P2P、空补丁和原始报告语义，测试补丁失败不计任务质量。源码检索指标、运行时修复原生包与自动准备资源未接入。配置见 [PolyBench 示例](../configs/polybench.example.json)。一个原始任务已完成真实独立官方评分，结果未解决；其余题目、语言和类别不能由此视为已验证。

`swe-bench-pro` 使用显式 V1/V2 manifest 分派：V1 读取本地题目 JSON/JSONL，在独立镜像中调用冻结作者的 Docker pipeline 和 `main()`；V2 使用 Harbor 任务，冻结 LockedCodex 捕获补丁，再由 PatchReplayAgent 在全新 trial 中评分。Agent 原始 reward 不作为 V2 成绩。两版本都绑定数据里的基准提交、保存补丁和原始报告，并核对进程、凭据和容器清理；实例 ID 中的 SHA 是修复提交，不能当作基准。V2 支持 public/HARD-51 的显式成员列表，V1 使用 public。示例见 [Pro V1](../configs/swe_pro_v1.example.json)、[Pro V2](../configs/swe_pro_v2.example.json)；网络隔离属于 `ctxpress_comparison`。V2 一个原始任务已通过真实独立官方重评分；V1 尚未完成真实运行验证。

`bigcodebench` 接入 Complete/Instruct、Full/Hard；数据 manifest 声明 split、subset、数据源与 revision。每个 `repeats` 是新的 Codex 会话，Agent 仅取得所选提示与代码起点，输出 `solution.py` 并导出官方 `task_id/solution` JSONL。独立 verifier 调用冻结作者的参考答案检查、代码检查和 pass@k 估计器；参考答案未通过使评分无效，代码失败/超时保留作者语义。报告单列代码样本和 pass@k，缺少计划样本、评分证据或样本数不足时保持未知。示例见 [BigCodeBench 配置](../configs/bigcodebench.example.json)；Codex 的服务端解码未声明温度/top-p，属于 `ctxpress_comparison`。一个原始任务已取得真实通过样本，不代表完整 benchmark 的 pass@k。

接入范围为 Terminal-Bench 4.0、Terminal-Bench-Science 0.1、DeepSWE v1.1、SWE-Milestone、SWE-bench（Verified/Lite/完整集）、SWE-bench Pro、SWE-PolyBench、BigCodeBench 这 8 个编码家族，以及为运行 ACM 作者模型加入的 BrowseComp-Plus（见下）。各适配器已接通任务选择、输入冻结、Agent 派发、产物导出、官方评分和报告，并各有一个原始任务的真实接入证据。 `ctxpress eval benchmarks` 中的 `real_run_verified` 是静态目录标志，仍保持 false；它不读取本地运行记录，不能据此判断某次实际作业是否验收通过。单次运行以绑定原始产物的 `compare` 报告和独立清理核验为准。首题证据不代表全部版本、题型或资源环境均已真实验证；具体支持边界、来源、协议与验收见 [ROADMAP](../ROADMAP.md) 和上文各适配器说明。

`browsecomp-plus` 运行固定版本的 ACM 作者 Agent（`lixiaochuan2020/agentic-context-management@f06f90e`）：作者自己的检索循环（本地 BM25 上的 `search` / `get_document`，可选 `manage_context` / `query_memory`）原样执行，训练过的检查点看到的提示词、工具和 token 提示与训练时相同。它通过 ctxpress 代理调用模型，计划里的方法改写它的 Chat Completions 请求，用量、费用和方法动作与其他家族一样记录。`backend` 为 `acm_author`；`model` 写 LiteLLM 名称 `openai/<服务名>`；`environment` 声明作者仓库、作者 Python、题目集（作者格式的 JSON 列表）、解密后的标准答案、可选 qrels、BM25 索引目录、Agent 模型上游，以及摘要模型和评分模型的 `{model, upstream}`。计划对作者源码、题目、答案文件和索引的每个文件计算哈希。每个作业启动三个本地代理（Agent、摘要、评分各一个，分别写日志），作者进程只拿到占位密钥，真实密钥由代理从运行环境 `CTXPRESS_AGENT_API_KEY`、`CTXPRESS_SUMMARIZER_API_KEY`、`CTXPRESS_GRADER_API_KEY` 取得。评分调用作者的 `evaluate_browsecomp_plus`（LLM 评判）；评判缺失或无法解析计为评分错误，作者进程没有产出结果计为基础设施无效。带自有 Agent 工具的方法（DTOC、CWL、ACM 等）不能用于这个家族，因为作者循环只提供它自己的工具。目前只用本地替身检查过（替身仓库、假模型服务和假评判），没有真实权重、索引或评分。

模型发布成绩不能直接按模型品牌推断 Agent：[DeepSWE 官方榜单](https://deepswe.datacurve.ai/run)使用 Pier + mini-swe-agent；[Science 0.1 官方评测](https://www.tbench.ai/news/terminal-bench-science-0-1)列出 GPT 配 Codex、Claude 配 Claude Code。我们分别准备原协议复刻与固定 Codex 的上下文方法比较，保留版本、宿主、提示、资源、重复数与评分设置；改用另一 Agent 的结果不冒充原发布成绩。
