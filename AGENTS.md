# AI Agent Test — LangChain + LangGraph Code Agent

Python 3.13 multi-agent coding assistant (Planner → Executor → Verifier, LangGraph StateGraph) that orchestrates MCP-based tools, backed by configurable LLM.

## Project
- **Stack**: Python 3.13, [uv](https://docs.astral.sh/uv/), LangChain 0.3, LangGraph ≥0.6, FastMCP
- **LLM**: `ChatOpenAI` → default DeepSeek `deepseek-v4-flash` (`api.deepseek.com`), switchable via `.env`
- **Entry**: `main.py` → `app/code_agent/agent/code_agent.py` → `run_agent()` (async, REPL loop)
- **Config**: `.env` loaded by `app/code_agent/config.py`; `pyproject.toml`

## Commands
| Purpose          | Command                              |
| ---------------- | ------------------------------------ |
| Install deps     | `uv sync`                            |
| Run agent        | `uv run python main.py`              |
| With session ID  | `uv run python main.py --thread-id x`|
| Debug mode       | `uv run python main.py --debug`      |
| Run tests        | `uv run python -m pytest tests/ evals/` |

## Architecture

```
main.py (CLI: argparse, --thread-id, --debug)
└── app/code_agent/agent/code_agent.py  ← REPL loop + evals 非交互接口 (run_single_task)
    └── app/code_agent/agent/multi_agent.py  ← Planner → Executor → Verifier StateGraph

app/web/server.py (FastAPI, 端口 8000)  ← 本地 Web UI
├── WS /ws/chat（thread_id + history，任务级互斥）+ REST（sessions/settings）
└── frontend/  ← Vue3 + Vite（dist 已入库，改后需 npm run build）
    ├── app/code_agent/config.py         ← All env-var config
    ├── app/code_agent/model/llm.py      ← ChatOpenAI instance ("llm")
    ├── app/code_agent/utils/mcp.py      ← load_mcp_tools() factory
    ├── app/code_agent/mcp_servers/      ← MCP SERVERS (each is a FastMCP app)
    │   ├── powershell_tools.py          → subprocess + psutil
    │   ├── browser_tools.py             → Selenium Edge + SearXNG
    │   ├── mysql_tools.py               → PyMySQL + _safe_ident anti-injection
    │   ├── vm.py                        → WSL2 bridge + dangerous cmd blocklist
    │   └── code_tools.py                → AST/diff/structure tools
    ├── app/code_agent/rag/rag.py        ← RAG MCP Server (ChromaDB + sentence-transformers)
    ├── app/code_agent/tools/
    │   ├── file_tools.py                → LangChain FileManagementToolkit
    │   └── file_saver.py                → FileCheckpointSaver (JSON + pickle)
    ├── evals/                           ← 30 end-to-end tasks (6 维度) + runner
    └── tests/                           ← 24 unit tests + 12 tool-level integration tests
```

## Conventions
- Package manager: **uv**; `uv sync` / `uv add` / `uv run python`, never `pip`
- MCP servers log to **stderr** (stdout = JSON-RPC channel)
- Config lives in `app/code_agent/config.py`, values from `.env` with defaults
- Checkpoints: `runtime/checkpoint/{thread_id}/*.json`
- Embeddings: `../embedding-model/` (local, ModelScope download)
- `FileCheckpointSaver` (not `MemorySaver`) for cross-restart persistence
- Chinese user-facing strings in prompts and debug output
- Feature branch + PR workflow on Gitee

## Notes
- 5 get_stdio_* wrapper files removed in Phase 4; tools loaded via `load_mcp_tools()` factory
- `mcp/` directory renamed to `mcp_servers/` in Phase 4 to avoid official `mcp` package conflict
- `pyautogui` dependency removed in Phase 5; PowerShell uses subprocess only

## 当前进度（2026-08-30 更新）
- **阶段**: optimized 优化期已完成；下一步：本地 Web UI（FastAPI + Vue3，不部署，详见 handover.md 第五节）
- **Baseline** (`docs/evidence/baseline-final.json`): overall **0.983**, pass_rate **1.0**（30/30 全过），总 token 978,865
- **optimized 阶段 1（单 Agent 修复）** (`evals-optimized-final.json`): overall **1.0**（六维全 1.0，896,475 token）
- **optimized 阶段 2（多 Agent Planner→Executor→Verifier）** (`evals-multiagent-merged.json`): overall **0.967**（E013 为 v2 偶发 timeout 记录，单题重跑可过）；搜索题专项修复 E011/E026：耗时 216s→66-102s、单题 token -90%
- **RAG 基准** (`rag-bench-baseline.json`): top1 0.6 / top3 1.0 / recall 0.4 / 平均延迟 13.4ms
- **STAR**: docs/interview/resume-star.md 两段式 STAR 已全部填完（① 0.983 / ② 1.0）
- **已知遗留**: E013 偶发 timeout 未根治（低优先级）；CRLF 未加 .gitattributes（待 normalize）
- **已知坑**:
  - evals 用 `uv run python evals/run_e2e.py`（pytest 不认 --task）；结果绝对路径存 runtime/runs/；断点续跑靠 run-id 一致
  - **重跑前必须清** checkpoint/chroma_db/data-knowledge 残留（Agent 自学习知识）/MySQL 表/WSL 文件
  - runtime/runs/ 被 .gitignore 忽略；正式结果归档到 docs/evidence/ 纳入版本控制
  - 评估体系与指标详见 docs/evidence/evals-baseline-report.md
