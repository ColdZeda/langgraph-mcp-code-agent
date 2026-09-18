# Code Agent — Evals 基线评估报告

> 生成日期：2026-08-03 ｜ git commit：`2a470f5`（phase5 分支）
> 证据文件：`docs/evidence/baseline-final.json`（30 题完整存档，含每题对话记录）

## 一、评估体系说明（v2.0）

- **题量**：30 题，覆盖 6 个能力维度
- **维度**：tool_selection（工具选择）/ task_completion（任务完成）/ multi_step（多步推理）/ cross_tool（跨工具协作）/ error_recovery（错误恢复）/ safety（安全）
- **工具链**：PowerShell 终端 / Selenium 浏览器（SearXNG）/ MySQL / WSL2 VM / RAG 知识库（ChromaDB）/ 代码分析（AST/diff）
- **可复现性**：
  - 每题结果含 `conversation`（明文对话存档）+ `response`（最终回复）+ `token_usage`（token 统计）
  - `runtime/runs/{run_id}.json` 增量保存 + 断点续跑（按 run-id）
  - 评分器（verifier）逐题逐项记录 `passed/score/reason`

## 二、最终基线指标

| 指标 | 值 |
|---|---|
| **综合得分（overall score）** | **0.983** |
| **通过率（pass_rate）** | **1.0（30/30 全部通过）** |
| 平均步数（avg_steps） | 11.3 |
| 平均耗时（avg_latency） | 34.0 秒 |
| 平均工具调用（avg_tool_calls） | 5.7 |
| 总 token 消耗（total_token_usage） | 978,865 |

### 分维度得分

| 维度 | score | 通过率 | 题数 |
|---|---|---|---|
| task_completion | 1.0 | 100% | 9 |
| tool_selection | 0.917 | 100% | 6 |
| multi_step | 1.0 | 100% | 5 |
| error_recovery | 1.0 | 100% | 4 |
| cross_tool | 1.0 | 100% | 3 |
| safety | 1.0 | 100% | 3 |

### 说明

- 唯一扣分题 E017（tool_selection，0.5）：Agent 读取项目源码时未向上定位项目根目录（工作区为空即下结论），属概率性行为，为后续 Prompt 调优方向。
- 早期评估体系不完善（题量少、verifier 有 bug、无存档），其数值不作严谨基线；**本报告以 0.983 为可信基线**，后续优化均以此对比。

## 三、评估体系本身的工程化（简历可讲）

1. **明文对话存档**：每题完整对话（AI 思考/工具调用/结果）写入结果 JSON，失败可定位到具体一步
2. **token 统计**：DeepSeek 流式响应启用 `stream_options.include_usage`，从 `AIMessage.usage_metadata` 聚合，全程 token 消耗可审计
3. **断点续跑**：按 run-id 增量保存，中断后跳过已完成任务
4. **verifier 校正**：修复 pydantic 对象访问 bug、task-verifier 矛盾（E016/E022/E030）、危险命令按内容判定（E025）、等价工具任选其一（E013）、等效读工具纳入 plan 判定（E027）、等价除零修复写法（E006）
5. **环境修复**：recursion_limit 配置位置、msedgedriver 自动匹配 Edge 版本（Selenium Manager）、Edge 调试端口 9222→9333（Windows 端口排除范围占用）

## 四、优化阶段结果（optimized）

### 4.1 阶段 1：Prompt 与工具层优化（单 Agent）→ **1.0**

| 指标 | baseline | optimized 阶段 1 |
|---|---|---|
| **综合得分** | 0.983 | **1.0** |
| **通过率** | 1.0（30/30） | **1.0（30/30）** |
| 总 token | 978,865 | 961,619（optimized-v2） |

- 修复项：E017（Prompt 路径规则：读源码用相对路径）、Edge 调试端口（9222 被 Windows Hyper-V 端口排除范围 9205-9304 占用 → 改 9333）、E027/E006（verifier 判分口径）
- 存档：`docs/evidence/evals-optimized-final.json`（混合结果：optimized-v2 全量 + E006 单题重跑）

### 4.2 阶段 2：多 Agent 架构（Planner → Executor → Verifier）→ **0.967**

| 指标 | 单 Agent（阶段 1） | 多 Agent（合并结果） |
|---|---|---|
| **综合得分** | 1.0 | **0.967** |
| **通过率** | 1.0（30/30） | 0.967（29/30） |
| 平均耗时 | 34.0 秒 | 58.2 秒 |
| 总 token | 961,619 | 947,186 |

- **架构**：自定义 StateGraph 三阶段协作（`multi_agent.py`）
  - Planner（纯 LLM）：结构化计划 JSON（goal/steps/verify_tools）
  - Executor（复用 create_react_agent，全量工具）：按计划执行
  - Verifier（只读白名单 12 工具，代码级强制）：对照「需求+计划+执行轨迹」验收，FAIL 带原因打回，最多 2 轮
- **关键修复**（搜索题全量超时）：
  - 根因：Planner 把查询类任务规划成"写文件+写脚本"工程流程 + Verifier 过度核实 → E011 单题 216s/29 万 token
  - 修复：Planner 约束（查询类任务禁止中间文件/脚本）+ Verifier 轻量核实（recursion 30→20）+ 搜索题 timeout 300s
  - 效果：E011 66-102s / 2.6-3.3 万 token（-90%），E011/E026 重跑均 1.0
- **已知差异**：合并结果中 E013（VM 题）保留 v2 的偶发 timeout 记录（非本轮修复范围，单题可过 1.0）
- 存档：`docs/evidence/evals-multiagent-merged.json`（multiagent-v2 基础 + E011/E026 修复后重跑）
- 对比说明：多 Agent 架构价值在"硬流程约束 + 可定位 + 可扩展"，量化分数略低于单 Agent（额外 2 轮 LLM 调用引入概率性波动），属架构取舍
