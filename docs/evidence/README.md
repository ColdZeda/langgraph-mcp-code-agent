# 评估结果存档

> **性质**：本目录是**只追加的结果存档**，不是活文档 —— 里面的 JSON **不要修改**。
> 说明文字（本文件）会随项目演进更新。

## ⚠️ 读这些数字前必须知道的四件事

1. **模型是旧的**：全部基于 `deepseek-v4-flash`（每条 JSON 里的 `model` 字段可核对）。
   换模型后的结果与这些数字**不可比**。
2. **评分口径偏软**：当时的 verifier 里有相当比例是**弱断言**——
   `ai_response_contains({"import"})`（回复里有这个词就算过）、
   `used_tools_subset({...})`（调对工具就算过）。
   实测统计：**14/30 题没有任何"产物级"断言**、**12/30 题连内容检查都没有**。
   所以这些分数应理解为"**回归通过率**"，**不是**"通用任务成功率"。
3. **`pass_rate` 偏乐观**：计分器把 `score >= 0.5` 记为通过，而部分 verifier 会给 0.5 的**部分分**
   → 部分匹配也算"通过"。
4. **项目正在改造**：第六版起的改造（`../..` 上级目录的 `program-fix第七版/`）
   会**重做评估体系**并在完成后归档新结果。这些旧文件的作用是
   **留存演进证据**（"优化前后"的对比数字来自这里）。

## 文件清单（12 个）

| 文件 | 内容 | 关键数字（取自文件本身） |
|---|---|---|
| `baseline-final.json` | **改造前基线**（单 Agent，30 题） | overall **0.983** / pass_rate 1.0 / avg_steps 11.3 / avg_latency 34.0s / total_tokens **978,865** |
| `evals-optimized-v1.json` | optimized 阶段 1 第 1 次全量 | overall 0.922 / pass_rate 0.933 / total_tokens 778,464 |
| `evals-optimized-v2.json` | optimized 阶段 1 第 2 次全量 | overall 0.989 / pass_rate 1.0 / total_tokens 961,619 |
| `evals-optimized-final.json` | **optimized 阶段 1 定稿**（单 Agent 优化后） | overall **1.0** / pass_rate 1.0 / avg_latency 30.3s / total_tokens **896,475** |
| `evals-multiagent-v1.json` | 多 Agent 架构第 1 次全量 | overall 0.956 / pass_rate 0.967 / total_tokens 2,120,452 |
| `evals-multiagent-v2.json` | 多 Agent 架构第 2 次全量 | overall 0.933 / pass_rate 0.933 / total_tokens 1,308,764 |
| `evals-multiagent-merged.json` | **多 Agent 定稿**（合并搜索结果） | overall **0.967** / pass_rate 0.967 / total_tokens 1,257,397 |
| `evals-multiagent-e011-verify.json` | 搜索题 E011 专项修复后的**单题**重跑 | overall 1.0 / latency 66.0s / tokens 25,777（1 题） |
| `evals-multiagent-e026-verify.json` | 搜索题 E026 专项修复后的**单题**重跑 | overall 1.0 / latency 70.2s / tokens 45,774（1 题） |
| `eval-mixed-e017-fixed.json` | E017 修复后的混跑结果 | overall 1.0 / pass_rate 1.0 / total_tokens 970,731 |
| `rag-bench-baseline.json` | **RAG 基准**（不是 30 题评估） | top1 **0.6** / top3 1.0 / recall **0.4** / 平均延迟 **13.4ms** |
| `evals-baseline-report.md` | **评估体系与指标报告**（含优化前后对比、题目与维度说明） | 文档 |

> 数字来源：每条都可用下面这条命令复核（以 `baseline-final.json` 为例）——
>
> ```bash
> uv run python -c "import json;d=json.load(open('docs/evidence/baseline-final.json',encoding='utf-8'));print(d['model'], d['overall'])"
> ```

## 这些结果是怎么跑出来的

```bash
# 全量（30 题）
uv run python evals/run_e2e.py --all --run-id <name>

# 单题（用新 run-id，避免覆盖）
uv run python evals/run_e2e.py --task E011 --run-id <name>

# RAG 基准
uv run python evals/rag_bench.py
```

- Runner 会把结果写到 `runtime/runs/{run_id}.json`（**该目录被 .gitignore 忽略**）；
  正式结果才复制到本目录纳入版本控制。
- 重跑前必须清残留（checkpoint / chroma_db / knowledge 根目录 / MySQL 表 / WSL uploads），
  否则 Agent 会读到上一次的状态。
