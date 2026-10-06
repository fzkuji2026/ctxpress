# 测试

单元测试、与作者实现的对照测试，以及全方法冒烟测试。

## 单元测试与对照测试

```bash
pip install -e '.[test,sim,itemmodel]'
python -m pytest tests -q -rs
```

录制数据与作者对照测试读取 `CTXPRESS_DATA`（默认仓库旁的 `../data`）；缺少数据或平台条件的用例会跳过并说明原因，跳过不算已通过对照。CWL / DTOC 的作者 TypeScript 对照需要支持 `--experimental-transform-types` 的 Node（22.6 及以上）。评测相关的 Linux 专用用例在其他系统上跳过（`linux_only` 标记）；`tests/test_layering.py` 检查包之间的依赖方向。与各论文原版代码逐条对照的脚本见 [repro/README.md](../repro/README.md)。

## 全方法冒烟测试

```bash
ctxpress smoke --output runs/smoke                          # 全部方法
ctxpress smoke --method DTOC --method CWL --turns 40 --output runs/smoke-two
```

每个方法都经过真实的代理：本地假 Responses API 代替模型，脚本化的 Agent 按 Codex 的方式发送完整历史（读文件、搜索、跑测试、改文件，长输出带关注问题），方法自己的摘要 / 反思 / 剪枝模型照常调用，CWL、DTOC、ACM 的方法工具在真实的 `ctxpress mcp` 服务里执行。每个方法结束时用 `ctxpress analyze` 输出统一统计。结果写到 `smoke.json`、`smoke.md` 和每个方法各自的目录（请求日志、`analysis.json`）。用量是假模型给的合成数，不代表质量或真实费用；`tests/test_smoke.py` 在每次测试时检查全部方法都能跑通并输出统计。
