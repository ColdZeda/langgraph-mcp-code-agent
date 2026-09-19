# Optimized 阶段优化计划 — 多 Agent 架构改造

> 状态：方案讨论定稿，待实施
> 关联文档：`../../evidence/evals-baseline-report.md`（评估体系）、`../../evidence/baseline-final.json`（baseline 存档）、`../interview/resume-star.md`（STAR 叙事）

---

## 一、背景与现状

- **架构**：单 Agent ReAct（`create_react_agent` 预构建），Plan→Execute→Verify 仅是系统 Prompt 的文字指导（软约束），无图节点强制执行
- **Baseline**：overall **0.983** / pass_rate **1.0**（30/30），唯一扣分题 E017（tool_selection，0.5 分）
- **关键事实**（本轮分析得出）：
  - E017 根因 = **Agent 路径解析行为问题**（把相对路径 `app/...` 错误拼到 workspace 下），**不是环境问题**——同类题 E003/E004/E008 读项目根文件均成功
  - 权限模型 = **写锁读放**：`FileManagementToolkit` 锁死 workspace（写），`code_tools` 无锁可读项目根（读）；Agent 不知道这个真实边界，自我设限导致 E017
  - 单次全量 evals ≈ 30 题 / 98 万 token / 17 分钟（baseline-final.json 实测）

## 二、优化目标

| 维度 | 目标 |
|---|---|
| 量化 | 0.983 → 1.0（修 E017），并保持全过 |
| 架构 | Plan→Execute→Verify 从 Prompt 软约束 → StateGraph 硬流程（可讲清、可扩展） |
| 简历 | STAR② 有量化对比（before/after 一对数字）+ 架构叙事 + 工程规范完整 |

## 三、阶段规划

### 阶段 0：预检 — RAG 体检（不阻塞，随时可跑）
- [ ] 跑 `uv run python evals/rag_bench.py`（工具层性能：延迟/准确率/召回率/排序）
- 不经过 LLM、不跑 evals，几乎零成本；产出 `runtime/runs/rag_bench_*.json`
- 目的：判断知识库质量是否拖累 Agent 的 `query_rag` 依赖

### 阶段 1：优化（工具描述 + Prompt 修 E017）
- [ ] 打磨 6 类 MCP 工具的 description/schema（所有 Agent 共享的"说明书"，先做一次架构直接继承）
- [ ] Prompt 加"路径使用规则"：读项目源码用相对路径（`app/...`），workspace 只放工作产物 → 修 E017
- [ ] evals：`--task E017` 单题连跑数次验证稳定 → 全量 `--all` 一次拿正式存档
- [ ] 结果归档 `docs/evidence/`，更新报告
- [ ] git 提交（提交 1，预期 0.983 → 1.0）

### 阶段 2：多 Agent 架构（D 路径，核心改造）
- [ ] 用 `StateGraph` 自定义图替换 `create_react_agent`（详见第四节）
- [ ] 全量 evals 对比阶段 1（如实记录波动）
- [ ] git 提交（提交 2）

### 阶段 3：收尾
- [ ] CRLF 行尾统一（19 个文件脏状态）：normalize + `.gitattributes`
- [ ] 更新文档：`../interview/resume-star.md`（填 STAR②）、handover（换窗口时再更新）
- [ ] git 提交（提交 3）

## 四、D 路径架构设计（核心）

### 4.1 图结构

```
[用户输入]
    ↓
Planner（纯 LLM，无工具，单次调用）
    → 输出 {steps: [...], verify_tools: [...], goal: "..."}
    ↓
Executor（复用现有 create_react_agent，全量工具，主体不改）
    → 输出：执行轨迹 + diff + 测试日志 + 最终回复
    ↓
Verifier（只读白名单 + 按计划动态挂载 2-5 个工具）
    → 对照「用户需求 + 计划 + 执行轨迹」验收
    ↓ pass → 回复用户（END）
    ↓ fail（带原因，最多 2 轮）→ 打回 Executor 重做
```

### 4.2 节点职责

| 节点 | 角色 | 工具 | 输出 |
|---|---|---|---|
| Planner | 规划员 | 无（纯 LLM） | 结构化计划 JSON（含 verify_tools 声明） |
| Executor | 执行员 | 全量（现状不变） | 执行轨迹 / diff / 测试日志 / 回复 |
| Verifier | 验收员 | 只读白名单（按计划挂载子集） | pass / fail + 原因 |

### 4.3 Verifier 工具白名单（代码级强制）

原则：**只读是硬约束（安全），覆盖是软目标（能力），挂载按计划动态声明**。

```
READONLY_TOOL_WHITELIST =
  code_tools 只读: read_file_range / generate_diff / analyze_ast / list_project_structure
+ MySQL 只读:      mysql_list_databases / mysql_list_tables / mysql_describe_tables / mysql_execute_query
+ RAG:             query_rag
+ 文件读类（FileManagementToolkit 中只读部分，实现时单独注册）

绝不挂载: mysql_insert_data / mysql_update_data / mysql_delete_data /
         mysql_create_database / mysql_create_table / mysql_execute_command /
         write_file / edit / delete / 危险命令类
```

- 白名单为**系统级强制**：工具未挂载 → Agent 调不到（不依赖 LLM 自觉）
- Planner 在计划中声明 `verify_tools` → Verifier 只挂载相关子集（降噪 + 省 token + 防选错）
- "轻量"含义：只读 + 工具少 + 通常一轮完成；不是"纯 LLM 听汇报"

### 4.4 打回机制

- Verifier fail 时必须带**具体原因**（"第 15 行改动是 X，但需求要求 Y"），Executor 据此重做
- 条件边计数，**最多 2 轮**，超限如实汇报"验证未通过"，不无限循环

### 4.5 各 Agent Prompt 要点（阶段 2 内完成）

- Planner：产出 JSON 计划；声明每步预期工具/结果；声明 verify_tools
- Executor：沿用现有执行规则（读前先看、改后 diff、跑验证）
- Verifier：以"用户需求 + 计划"为验收标准，输出 pass/fail+原因

## 五、关键设计决策记录

1. **E017 修复方式**：Prompt 引导（路径使用规则），**不用 setup_files**——setup_files 是绕路修，Prompt 修根因且保持 E017"读项目根文件"的语义
2. **权限模型**：description 中性化（说明书）+ 挂载白名单代码级强制（权限）——两层分离
3. **重跑策略**：单题验证 + 全量拿正式存档（overall 是 30 题加权，单题无法诚实产出新 overall）
4. **D vs E**：选 D（Planner/Executor/Verifier 角色分工流水线）；E（Critic 旁路监督）暂不做
5. **原 C 路径**：更名为"优化"，并入阶段 1（工具 description 在架构前做，各 Agent Prompt 定制在架构时做）
6. **提交纪律**：每阶段一个独立提交，git log 可讲故事、可回滚

## 六、执行原则

- 每次改动用**同一套 evals** 重跑对比，数字可归因
- 重跑前清残留：`runtime/checkpoint` / `chroma_db` / `data-knowledge` / MySQL 表 / WSL 文件
- 正式结果归档 `docs/evidence/` 纳入版本控制（`runtime/runs/` 被 .gitignore 忽略）
- 不编造数字：全部以存档为准

## 七、提交计划

| 提交 | 内容 | 预期 |
|---|---|---|
| 1 | 阶段 1（工具描述 + Prompt 修 E017）+ evals 存档 | 0.983 → 1.0 |
| 2 | 阶段 2（StateGraph 多 Agent）+ 对比存档 | 1.0 → 稳定性/新能力（如实记录） |
| 3 | 阶段 3（CRLF + 文档 + STAR②） | 工程规范收尾 |

## 八、遗留 / 待办

- [ ] 阶段 2 前：本计划文档即方案，实施时按 4.1-4.5 落地
- [ ] 可选优化：给 code_tools 加只读白名单（当前"读"无显式限制，靠约定）
- [ ] handover.md 内容更新：换窗口/换 Agent 时再做
