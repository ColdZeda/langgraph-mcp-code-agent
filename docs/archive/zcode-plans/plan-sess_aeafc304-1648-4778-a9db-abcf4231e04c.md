# 计划 A 最终执行方案（已吸收全部确认结果）

## 已确认方向
| 决策点 | 结论 |
|---|---|
| Agent UI | 本地 Web UI：FastAPI 后端 + Vue3 前端工程（不部署、不做桌面壳） |
| 毕设包装 | 中度：SSE 流式 + 提示工程文档化 + 物种推断 LLM 化 + ER 图；视频/部署暂缓 |
| Agent 仓库 | 私有仓直接全推（52+ 提交含简历草稿）；phase5→master 本地 merge 直推 |
| 毕设仓库 | Gitee 新建私有仓库 **pet-health-ai-platform**，先私有后转公开（转公开时机你定） |
| 新 key | 位置先留空（旧 key 已失效）；高德低优先级一行占位；**重心在 AI 功能** |
| 推送凭据 | 已缓存，我直接 push |
| 时间线 | 1 个月内开始投递 |

## 关键事实（调查实证）
- **两项目用不同 DeepSeek key**：Agent 的 `.env`（sk-0a2a0bf6…，已被 gitignore）不受毕设 key 吊销影响
- ai-agent-test：phase5 领先远端 52 提交；optimized 实际已完成（单 Agent 1.0 / 多 Agent 0.967 / STAR② 已填），AGENTS.md 与 handover.md 进度章节过期；`run_single_task()` 是现成非交互接口、`astream()` 已流式；`llm` 为模块级单例需工厂化（24 单测兜底）
- 毕设：非 git 仓库、无 README；真 DeepSeek 调用 + AiChatService 扎实提示工程（档案注入/数据充分性声明/五段式报告/rule 降级）；物种"识别"实为词典匹配；待清理项：application.yml:26 旧 key、location.js:2 + hospitals.vue:29 高德 key、project.config.json:32 AppID、backend/uploads/ 105 张个人照片、.github/ Copilot 痕迹、cp.txt、tmp_doc_update.json、target/unpackage/dist
- **小程序端 SSE 限制**：微信小程序不支持 fetch ReadableStream，须用 `wx.request` + `enableChunked: true` 分块接收（基础库 ≥2.20.1）

---

## 阶段 1：ai-agent-test 备份推送（0.5 天）
1. 提交 3 个未跟踪简历文件（docs/interview/resume.md、resume-new-glm.md、resume-new-glm-2.md）——私仓作素材存档
2. 刷新 AGENTS.md「Phase 5 进度」与 handover.md 过期小节（如实记载：单 Agent 1.0 / 多 Agent 0.967 / rag-bench 0.6/1.0 / STAR② 已填 / 下一步 Web UI），修掉"②待填"等过期引用
3. `git push origin phase5`（52+ 提交上云）
4. 本地 merge phase5 → master，push master
- **验收**：`git status` 干净、远端 phase5 与 master 一致

## 阶段 2：毕设中度包装（4-5 天）
**2.0 冷备份**：整目录 zip → `E:/e/backup/myfinalprogram-冷备-20260830.zip`（改任何东西前的完整回滚点）
**2.1 git 化 + 推私仓（备份达成）**：
- .gitignore 补 `backend/uploads/`、`cp.txt`、`tmp_doc_update.json`、`.github/`
- 删除垃圾：target/、unpackage/、dist/、Copilot 痕迹、cp.txt、tmp_doc_update.json
- `git init` + 基线 commit + push 到 pet-health-ai-platform（私有）
**2.2 脱敏 commit**：
- application.yml：`apiKey: ${DEEPSEEK_API_KEY:}`（默认值清空）
- 高德：新建 `uniapp-mini/utils/config.js` 导出 `AMAP_KEY = ''`（占位+注释），两处改引用——一行处理不花精力
- project.config.json：appid → `touristappid`
- README 标注演示账号（admin@local/admin123、123/123456）
**2.3 AI 聊天 SSE 流式（时间盒 1.5 天，重心）**：
- 后端：`/api/ai/chat/stream`（SseEmitter）+ AiService 流式读 DeepSeek `stream:true` 逐段转发；**失败/无 key/规则降级的内容也走 SSE 通道返回**（保证前端链路始终可测）
- 小程序端 aiChat.vue：`uni.request` + `enableChunked` 分块接收，打字机渲染，完成入库，保留 abort
- 自测：无 key 时验证降级链路与接口正确性；你后续自配新 key 后端到端复测（配置位置与 .env.example 我准备好，**key 由你自己填**）
**2.4 物种推断 LLM 化（时间盒 0.5 天）**：后端 `speciesInfer(breed)` 返回 {species, confidence}，低置信/失败回退词典；前端词典优先、LLM 兜底（带开关）
**2.5 文档化（1 天）**：
- `docs/prompt-engineering.md`：档案注入模板 / 健康记录统计注入 / 数据充分性声明 / 五段式报告 / 话题守卫与降级链路（附真实 prompt 与代码位置）
- ER 图：从 8-9 个 JPA 实体反推 Mermaid 图
- README.md：简介 / 三端架构 / 技术栈 / AI 架构章节（链接 prompt-engineering.md）/ 快速启动 / 演示账号
- commit + push（转公开等你手动决定）
**2.6 简历素材**：把 SSE / 提示工程方法论 / LLM 物种推断整理成 bullet 要点文本交付给你（你自己放进简历素材集合，不代写简历）

## 阶段 3：Agent Web UI（5-7 天）
**3.0** `git checkout -b feature/web-ui`
**3.1 后端 `app/web/`（1.5-2 天）**：FastAPI（`uv run uvicorn app.web.server:app --port 8000`）；启动时并行加载 6 MCP + file_tools 构建执行器/验收器；`WS /ws/chat` 每连接绑定 thread_id + 跨轮 history（沿用 10 轮截断）：
- **MVP 档**：任务完成后一次性推送结构化结果（plan / 工具 trace / verdict / final_response / token）
- **进阶档**：给 run_multi_agent 加异步事件回调，实时推 Planner 计划、每次工具调用、Verifier 判定与 LLM token 流
- REST：`GET /api/sessions`（扫 runtime/checkpoint）、`GET/POST /api/settings`
**3.2 模型切换改造（0.5-1 天）**：llm.py 加 `build_llm()` 工厂 + get/set；三处引用改可选参数注入（默认现单例，evals 零影响）；设置持久化 `runtime/web-settings.json`；改后必跑 `uv run python -m pytest tests/`
**3.3 Vue3 前端 `app/web/frontend/`（2-3 天）**：Vite + Vue3 + Pinia：Chat 视图（Markdown + 流式打字机）、执行时间线组件（Planner 卡片→工具调用步骤条→Verifier PASS/FAIL 徽章）、会话列表、设置页（模型/base_url/key + 测试连接）
**3.4 联调验收（1 天）**：文件类 / MySQL 类 / RAG 问答类任务各实测；换模型生效；Windows 工具链（Edge 9333 / WSL2）在服务模式正常
**3.5 文档 + 合并**：README 加 Web UI 章节 + 截图；AGENTS.md 架构图补 web 层；merge 回 phase5 → push → 本地 merge master 直推

## 阶段 4：收尾（1 天）
handover.md 全面刷新；resume-star.md 增补 Web UI bullet（只写架构与能力，**不编数字**）；可选补 `.gitattributes` + normalize 收掉 CRLF 隐患

---

## 风险与对策
- SSE 超期挤压 UI 时间 → 2.3/2.4 设时间盒，超时即降级（SSE 先保后端链路 + 管理端可验；小程序端 chunked 若卡壳则记 TODO）
- llm 工厂化牵动 evals → 全部带默认参数向后兼容；改后必跑单测；不动 evals 逻辑
- 无 key 期间 SSE 只能验证降级链路 → 配置位置留好，你自配新 key 后我复测端到端
- 每阶段完成即 push，灾难恢复粒度细
- 诚实原则：新功能不编数字，简历/STAR 只写架构与能力描述

## 需要你做的（对应时机）
1. 现在：Gitee 建私有空仓库 **pet-health-ai-platform**（建好把地址发我）
2. 阶段 2 期间（可选但推荐）：DeepSeek 控制台新建一把 key 自己填入本地配置，SSE 即可端到端自测
3. 将来：毕设仓库转公开由你手动操作

## 工作量汇总
阶段 1: 0.5 天 ｜ 阶段 2: 4-5 天（其中 AI 重心 2 天）｜ 阶段 3: 5-7 天 ｜ 阶段 4: 1 天 ｜ **合计 10-13 工作日**（1 个月内含缓冲）

## 执行方式
批准即授权执行方案内写操作（提交/推送/代码修改/文件清理）。按阶段推进，每阶段开始时我先同步该阶段具体动作清单；阶段内发现与调查不符的事实先停下说明再动手。