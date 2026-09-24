"""评估 runner（阶段 6 从零重建）。

## 它负责的四件事（每一件都是旧口径的一个坑）

| # | 旧口径的坑 | 这里怎么做 |
|---|---|---|
| 1 | 残留互相污染：上一题的产物让下一题的 `file_exists` 白过 | **每题前清空 `runtime/workspace/`**；整轮开始前清 chroma / 知识库根目录 / MySQL 测试库 / WSL uploads |
| 2 | single 与 multi 共用 checkpoint 线程 → 第二轮读到第一轮状态 | thread_id = `eval-{run_id}-{task_id}-{mode}`（**带 mode 带 run**） |
| 3 | 环境不可用静默变 0 分；超时题一个判定器都不跑 | 判定器**永远跑**（超时/异常也跑）；不可用 → `skip`；全 skip → `unavailable`（**不计入总分**） |
| 4 | 通过 = `score >= 0.5`（部分分也算过） | 通过 = **满分**；部分分单列 `partial` 并在报告里披露占比 |

## 每题跑完记录什么

`passed / score / partial / unavailable`、每一条断言的档位与结果、
`verdict / route / retry_count / budget_exceeded`、token / 步数 / 耗时、
**分节点耗时与 token**、以及**权限痕迹**（过了几次确认闸门、几次高危、审计里的批准条数）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent.code_agent import run_single_task
from app.code_agent.config import (
    MYSQL_DATABASE,
    PERMISSIONS_LOG,
    RUNS_DIR,
    TASK_TOKEN_BUDGET,
    VM_UPLOADS_DIR,
    WORKSPACE_DIR,
)
from app.code_agent.security.permissions import (
    HEADLESS_PERMISSION_MODE,
    AutoApprover,
    mode_label,
)
from evals import verifiers as V
from evals.env import chroma_dir

#: 单题默认墙钟上限（秒）。超时不等于 0 分 —— 判定器照跑，产物照样验。
DEFAULT_TASK_TIMEOUT = 300

#: 整轮的墙钟上限**覆盖值**（秒）；`None` = 用每题的 `timeout_sec`。
#: `0`（或任何 <=0）= **不设超时，只计量**（与任务级 token 预算的 `<=0 = 不限制` 对称）。
#:
#: ⚠️ 为什么要这个开关（2026-09-24）：要回答"multi 到底能不能做完这题"，
#: 就不能让人为的闸门（token 上限 / 墙钟上限）先把答案掐掉。当时 multi 的 E015
#: 就是被 token 上限终止的；改成"只计量"之后才测出它其实 60s / 114k token 就能做对。
#: 用环境变量而不是改 30 个 TaskSpec：`EVAL_TASK_TIMEOUT=0` 一行就够，且**会写进结果快照**（可自证口径）。
EVAL_TIMEOUT_OVERRIDE: int | None = (
    int(os.environ["EVAL_TASK_TIMEOUT"])
    if os.environ.get("EVAL_TASK_TIMEOUT", "").strip()
    else None
)


def effective_timeout(timeout_sec: int) -> int:
    """算这道题**实际生效**的墙钟上限：整轮覆盖值优先；`<=0` 一律表示"不设超时"。"""
    if EVAL_TIMEOUT_OVERRIDE is not None:
        return EVAL_TIMEOUT_OVERRIDE
    return timeout_sec


#: 判定器不参与"通过"，只记分数
__all__ = [
    "DEFAULT_TASK_TIMEOUT",
    "TaskSpec",
    "clean_workspace",
    "prepare_run",
    "run_all",
    "run_one_task",
    "save_run",
]


@dataclass
class TaskSpec:
    """一道评估题。

    - `checks`：判定器列表。**至少一条 `TIER_STATE`**（`tests/test_evals_tasks.py` 机械守着）。
    - `kind`：`basic`（基础题）/ `long`（长任务）/ `adversarial`（对抗题）。
    - `setup(workspace)`：跑之前往 workspace 里铺预置文件（比如"修这个语法错误"要先有个坏文件）。
    - `cleanup()`：跑完之后收拾**题目特有**的残留（全局残留由 `prepare_run` 管）。
    - `permission_mode` / `approver_factory`：对抗题用来固定"只读档"或"只拦高危"。
    """

    id: str
    dimension: str
    kind: str
    prompt: str
    checks: Sequence[V.CheckFn]
    difficulty: str = "medium"
    dimensions: tuple[str, ...] = ()
    timeout_sec: int = DEFAULT_TASK_TIMEOUT
    setup: Callable[[Path], None] | None = None
    cleanup: Callable[[], None] | None = None
    permission_mode: str | None = None
    approver_factory: Callable[[], Any] | None = None
    note: str = ""

    # 是否允许「自动注入」把知识库内容拼进 Executor 提示词。
    # ⚠️ **评估默认关闭**（2026-09-24 用户决定）—— 8 个维度里没有一个是"抵抗错误知识"，
    #    开着等于给每道题都加一个题集作者没打算测的变量，结果无法归因。
    #    ⚠️ 目前只有 **E022** 声明为 True：它是 `query_rag` 唯一的端到端覆盖题，
    #    保留注入可以观察"知识库内容（含干扰项）如何影响回答"。
    #    ⚠️ 这会让 E022 的输入条件与其余 29 题**不同**，报告里必须披露；
    #    但它在 single / multi 两轮里条件一致，所以两轮之间可比。
    inject_knowledge: bool = False
    #: 这题会用到的 MySQL 库 / 表 —— 整轮开始前只清这些名字（**绝不扫全库乱删**）
    mysql_databases: tuple[str, ...] = ()
    mysql_tables: tuple[tuple[str, str], ...] = ()

    @property
    def all_dimensions(self) -> tuple[str, ...]:
        return (self.dimension, *self.dimensions)


# ═══════════════════════════════════════════════════════════════════
# 清残留
# ═══════════════════════════════════════════════════════════════════


def clean_workspace() -> int:
    """清空 `runtime/workspace/`（file 工具的根，也是评估的沙箱目录）。

    ⚠️ 这是"每题都做"的动作：不清的话上一题写的 `hello.py` 会让下一题的
    `file_exists("hello.py")` **白过** —— 状态断言越硬，残留污染越隐形。
    """
    removed = 0
    if WORKSPACE_DIR.exists():
        for child in WORKSPACE_DIR.iterdir():
            if child.name == ".gitkeep":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            removed += 1
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    return removed


def _clean_knowledge_root() -> int:
    """清掉知识库**根目录**下的散文件（Agent 自学习写进去的），保留 `real_knowledge/` 与 `distractors/`。"""
    from evals.env import knowledge_dir

    root = knowledge_dir()
    removed = 0
    if not root.exists():
        return 0
    for child in root.iterdir():
        if child.is_dir():
            continue
        child.unlink(missing_ok=True)
        removed += 1
    return removed


def _clean_mysql(databases: Iterable[str], tables: Iterable[tuple[str, str]] = ()) -> list[str]:
    """删掉评估用过的库/表（**只删题集自己声明的名字**，绝不扫全库乱删）。"""
    import pymysql

    from app.code_agent.config import (
        MYSQL_CHARSET,
        MYSQL_HOST,
        MYSQL_PASSWORD,
        MYSQL_PORT,
        MYSQL_USER,
    )

    done: list[str] = []
    try:
        conn = pymysql.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            charset=MYSQL_CHARSET,
            connect_timeout=5,
        )
    except Exception:  # noqa: BLE001 —— 库没起就不清，后面判定器会记 skip
        return done
    try:
        with conn.cursor() as cur:
            for db in databases:
                cur.execute(f"DROP DATABASE IF EXISTS `{db}`")  # noqa: S608 —— 名字来自题集常量
                done.append(f"db:{db}")
            for db, table in tables:
                cur.execute(f"DROP TABLE IF EXISTS `{db}`.`{table}`")  # noqa: S608
                done.append(f"table:{db}.{table}")
        conn.commit()
    finally:
        conn.close()
    return done


def _reset_knowledge() -> dict:
    """把知识库复位到"只有预置的那 7 篇"，**每题开跑前都做一次**。

    为什么必须做（2026-09-23 smoke 实测）：**模型自己会调 `save_knowledge`** ——
    跑 E007 时它把"MySQL 查询工具的经验"写进了知识库（872 字符 / 3 块，chroma 35 → 38 块）。
    应用层的自动沉淀在评估里是关掉的（`run_single_task(auto_deposit=False)`），
    但**"模型主动调工具"这条路关不掉**：`save_knowledge` 是产品里真实存在的工具，
    为了评测把它从工具集里摘掉，等于换了口径（工具数 32 → 31）。
    于是换个思路：**不堵工具，而是把知识库在每题前复位** —— 和"每题清空 workspace"同源。

    写法上有个坑：**不能调 `store.ensure_seeded()`** —— 它带进程级 `_seeded` 标志，
    进程内只灌一次，第二次调用直接跳过（那样就永远不清）。
    要调 `store.seed_knowledge_base()`（真正的扫描+增量+清理），
    它会因为"文件已删但块还在"把残留 source 的块清掉。

    开销很小：预置 7 篇 mtime 没变 → 不重建，只做一次全库 metadata 扫 + 可能的删除。
    """
    removed = _clean_knowledge_root()
    from app.code_agent.rag import store

    try:
        store.seed_knowledge_base()
        return {"removed": removed, "chunks": store.get_chunk_count()}
    except Exception as exc:  # noqa: BLE001 —— 复位失败不该让整道题挂掉，但要看得见
        return {"removed": removed, "chunks": -1, "error": f"{type(exc).__name__}: {exc}"}


def _clean_wsl_uploads(path: str | None) -> dict:
    """清 WSL 上传目录里的文件（保留 `.gitkeep`），返回**可分辨的**执行记录。

    ⚠️ 返回值故意不是 0/1（2026-09-22 订正 #33）：这个函数以前返回 `1` = "清了"、
    `0` = **三种完全不同的情况**（没传路径 / WSL 不可用 / 命令失败），
    于是"整整一轮都在跳过清理"被一个 `0` 盖住了、没人看得出来。
    现在把"试没试（`attempted`）/ 成没成（`ok`）/ 删了几个（`removed`）"分开记。

    删完再核一次目录——把"命令说成功了"升级成"目录**确实**空了"。
    """
    if not path:
        return {"attempted": False, "skipped": True, "path": "", "removed": 0, "left": None}
    ok, why = V.wsl_available()
    if not ok:
        return {
            "attempted": True,
            "ok": False,
            "path": path,
            "removed": 0,
            "left": None,
            "error": why,
        }
    q = V.shlex_quote(path)
    # `-delete -print`：GNU find 会打印**真正删掉**的那些文件 → 一条命令同时得到"删了哪些"
    rc, out = V.wsl_run(f"find {q} -maxdepth 1 -type f ! -name '.gitkeep' -delete -print")
    if rc != 0:
        return {
            "attempted": True,
            "ok": False,
            "path": path,
            "removed": 0,
            "left": None,
            "error": (out or "无输出")[:200],
        }
    removed = len([ln for ln in out.splitlines() if ln.strip()])
    verify_rc, verify_out = V.wsl_run(f"find {q} -maxdepth 1 -type f ! -name '.gitkeep' | wc -l")
    tail = verify_out.strip().splitlines()[-1].strip() if verify_out.strip() else ""
    left = int(tail) if verify_rc == 0 and tail.isdigit() else -1
    return {
        "attempted": True,
        "ok": left == 0,
        "path": path,
        "removed": removed,
        "left": left,
        **({"error": "删完仍有残留"} if left != 0 else {}),
    }


def prepare_run(
    *,
    mysql_databases: Iterable[str] = (),
    mysql_tables: Iterable[tuple[str, str]] = (),
    wsl_uploads: str | None = VM_UPLOADS_DIR,
) -> dict:
    """整轮开始前的清残留 + 环境快照（快照会写进结果 JSON，报告里的"口径"靠它）。

    ⚠️ `wsl_uploads` **默认就是真实目录**（2026-09-22 订正 #33）：它原来的默认值是 `None`
    = "不清"，而 `run_all()` 又没传 → **整整一轮都不清**，于是 E014 的两条 WSL 断言
    （文件存在 + 内容一致）能被上一轮的残留蒙过 → **假阳性**。
    **"默认不安全"的默认值本身就是要修的 bug** —— 现在默认清；真要跳过就显式传 `None`，
    那时快照里会留下 `skipped: true`，一眼能看出"这一轮没清"。
    """
    from app.code_agent.rag import store

    snapshot: dict[str, Any] = {}
    snapshot["workspace_cleaned"] = clean_workspace()
    snapshot["knowledge_root_cleaned"] = _clean_knowledge_root()
    snapshot["mysql_cleaned"] = _clean_mysql(mysql_databases, mysql_tables)
    snapshot["wsl_uploads"] = _clean_wsl_uploads(wsl_uploads)

    # chroma 重建（增量：mtime 没变就跳过）—— 知识库根目录刚清过，这里保证索引与文件一致
    try:
        store.ensure_seeded()
        snapshot["chunks"] = store.get_chunk_count()
        snapshot["rerank_enabled"] = store.get_reranker() is not None
    except Exception as exc:  # noqa: BLE001
        snapshot["chunks"] = -1
        snapshot["chroma_error"] = f"{type(exc).__name__}: {exc}"
    snapshot["mysql_available"] = V.mysql_available()[0]
    snapshot["wsl_available"] = V.wsl_available()[0]
    snapshot["chroma_dir"] = str(chroma_dir())
    snapshot["database"] = MYSQL_DATABASE
    # 任务级 token 硬上限的**生效值**（`<=0` = 只计量不拦截，见 `context.over_task_budget`）。
    # ⚠️ 必须写进快照：2026-09-24 的 multi 轮就是**关掉上限**跑的（为了区分"预算掐断"与"agent 跑偏"），
    #    归档的结果 JSON 得能**自证**这件事 —— 否则读数据的人无从判断那轮的口径。
    snapshot["task_token_budget"] = TASK_TOKEN_BUDGET
    # 墙钟上限的**生效策略**：`None` = 每题用自己的 timeout_sec；`0` = 不设超时只计量。
    snapshot["task_timeout_override"] = EVAL_TIMEOUT_OVERRIDE
    return snapshot


# ═══════════════════════════════════════════════════════════════════
# 权限审计（"N 次写操作全部经过确认闸门"这句话的证据）
# ═══════════════════════════════════════════════════════════════════


def _audit_offset() -> int:
    try:
        return PERMISSIONS_LOG.stat().st_size
    except OSError:
        return 0


def _audit_since(offset: int) -> list[dict]:
    """读 `runtime/permissions.log` 在 offset 之后追加的行（解析失败的行跳过）。"""
    try:
        with PERMISSIONS_LOG.open("r", encoding="utf-8") as fp:
            fp.seek(offset)
            lines = fp.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _audit_stats(entries: Sequence[dict]) -> dict:
    decisions: dict[str, int] = {}
    for entry in entries:
        key = str(entry.get("decision") or "?")
        decisions[key] = decisions.get(key, 0) + 1
    return {
        "lines": len(entries),
        "decisions": decisions,
        "highRisk": sum(1 for e in entries if e.get("high_risk")),
        "tools": sorted({str(e.get("tool")) for e in entries}),
    }


# ═══════════════════════════════════════════════════════════════════
# 单题执行
# ═══════════════════════════════════════════════════════════════════


async def run_one_task(
    spec: TaskSpec,
    *,
    mode: str,
    run_id: str,
    tools: list | None = None,
) -> dict:
    """跑一道题：复位知识库 → 清工作目录 → 执行 → （超时/异常也）执行判定器 → 汇总。"""
    knowledge_reset = _reset_knowledge()
    clean_workspace()
    if spec.setup is not None:
        spec.setup(WORKSPACE_DIR)

    approver = spec.approver_factory() if spec.approver_factory else AutoApprover()
    audit_offset = _audit_offset()
    started = time.perf_counter()
    status = "completed"
    timeout_hit = False

    call = run_single_task(
        spec.prompt,
        thread_id=f"eval-{run_id}-{spec.id}-{mode}",
        mode=mode,
        permission_mode=spec.permission_mode or HEADLESS_PERMISSION_MODE,
        approver=approver,
        tools=tools,
        auto_inject=spec.inject_knowledge,
    )
    try:
        timeout = effective_timeout(spec.timeout_sec)
        if timeout > 0:
            result = await asyncio.wait_for(call, timeout=timeout)
        else:
            # 不设超时（只计量）：`EVAL_TASK_TIMEOUT=0` 或题目自己的 timeout_sec <= 0。
            # 内层仍有护栏：Executor 的 ReAct `recursion_limit=100` + 节点级 token 剪枝。
            result = await call
    except TimeoutError:
        # ⚠️ 超时不等于"什么都没发生"：产物可能已经写出来了。
        #    旧口径 (run_e2e.py:219) 只在 status == "completed" 时才跑判定器 →
        #    超时题**一个断言都不执行就记 0 分**，那是另一种失真。
        timeout_hit = True
        status = "timeout"
        result = {
            "ok": False,
            "error": f"TimeoutError: 超过 {effective_timeout(spec.timeout_sec)}s",
            "response": "",
            "tool_trace": [],
            "conversation": [],
            "step_count": 0,
            "token_usage": 0,
            "verdict": "",
            "verdict_passed": None,
            "retry_count": 0,
        }
    except Exception as exc:  # noqa: BLE001 —— runner 不能因为一道题炸掉整轮
        status = "error"
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "response": "",
            "tool_trace": [],
            "conversation": [],
            "step_count": 0,
            "token_usage": 0,
            "verdict": "",
            "verdict_passed": None,
            "retry_count": 0,
        }
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    ctx = V.RunContext(task_id=spec.id, mode=mode, result=result)
    checks: list[V.Check] = []
    for factory in spec.checks:
        try:
            checks.append(factory(ctx))
        except Exception as exc:  # noqa: BLE001 —— 判定器自身出错要看得见，不能静默算过
            checks.append(
                V.Check(
                    getattr(factory, "__name__", "check"),
                    V.TIER_STATE,
                    False,
                    f"判定器自身抛异常：{type(exc).__name__}: {exc}",
                )
            )
    verdict = V.evaluate(checks)
    if spec.cleanup is not None:
        try:
            spec.cleanup()
        except Exception:  # noqa: BLE001 —— 收拾残留失败不影响成绩
            pass

    audit = _audit_since(audit_offset)
    return {
        "id": spec.id,
        "dimension": spec.dimension,
        "dimensions": list(spec.all_dimensions),
        "kind": spec.kind,
        "difficulty": spec.difficulty,
        "mode": mode,
        "status": status,
        "timeout_sec": effective_timeout(spec.timeout_sec),
        "timeout_hit": timeout_hit,
        "ok": bool(result.get("ok")),
        "error": str(result.get("error") or ""),
        # ── 成绩 ──
        **verdict,
        "checks": [c.to_dict() for c in checks],
        "tier_summary": V.summarize(checks),
        # ── 过程指标 ──
        "elapsed_ms": elapsed_ms,
        "token_usage": int(result.get("token_usage") or 0),
        "step_count": int(result.get("step_count") or 0),
        "tool_calls": len(result.get("tool_trace") or []),
        "tools_used": [str(t.get("name")) for t in (result.get("tool_trace") or [])],
        "verdict": str(result.get("verdict") or "")[:500],
        "verdict_passed": result.get("verdict_passed"),
        "route": str(result.get("route") or ""),
        "retry_count": int(result.get("retry_count") or 0),
        "budget_exceeded": bool(result.get("budget_exceeded")),
        "pruned_messages": int(result.get("pruned_messages") or 0),
        "node_timings": result.get("node_timings") or {},
        # ── 每题开跑前的复位（残留会让后面的题跑在不一样的环境里）──
        "knowledge_reset": knowledge_reset,
        # ── 权限痕迹 ──
        "permission": result.get("permission")
        or {
            "mode": spec.permission_mode or HEADLESS_PERMISSION_MODE,
            "modeLabel": mode_label(spec.permission_mode or HEADLESS_PERMISSION_MODE),
            "approver": getattr(approver, "label", ""),
            "asked": getattr(approver, "asked_count", 0),
            "granted": getattr(approver, "granted_count", 0),
            "highRiskAsked": getattr(approver, "high_risk_asked", 0),
            "highRiskTools": [],
        },
        "audit": _audit_stats(audit),
        # ── 自动注入「注了什么」（评估默认关闭；只有 inject_knowledge=True 的题会有内容）──
        #    两个用处：① 万一某题挂了，能判断「是不是注入的知识导致的」——
        #       E004 就是这么被定位的：它因为注入里的错误知识而去"核查知识库"，
        #       多调了一次 query_rag，撞上 `used_no_tools()` 断言（0.75）；
        #    ② 其余题记成空数组，本身就是「注入确实被关掉了」的证据。
        "knowledge_injected": list(result.get("knowledge_injected") or []),
        "response": str(result.get("response") or "")[:4000],
    }


# ═══════════════════════════════════════════════════════════════════
# 整轮执行
# ═══════════════════════════════════════════════════════════════════


def _aggregate(records: Sequence[dict]) -> dict:
    available = [r for r in records if not r["unavailable"]]
    passed = [r for r in available if r["passed"]]
    partial = [r for r in available if r["partial"]]
    tier_totals: dict[str, dict] = {
        t: {"total": 0, "passed": 0, "failed": 0, "skipped": 0} for t in V.TIERS
    }
    for r in records:
        for tier, slot in r["tier_summary"].items():
            for key in ("total", "passed", "failed", "skipped"):
                tier_totals[tier][key] += slot[key]
    return {
        "tasks": len(records),
        "available": len(available),
        "unavailable": len(records) - len(available),
        "passed": len(passed),
        # ⚠️ 分母是"真跑出来结果的题"，不是全部题 —— unavailable 不能进分母（也不能当 0 分）
        "pass_rate": round(len(passed) / len(available), 4) if available else None,
        "partial": len(partial),
        "partial_rate": round(len(partial) / len(available), 4) if available else None,
        "score_avg": round(sum(r["score"] for r in available) / len(available), 4)
        if available
        else None,
        "timeout": sum(1 for r in records if r["status"] == "timeout"),
        "error": sum(1 for r in records if r["status"] == "error"),
        "tokens": sum(r["token_usage"] for r in records),
        "elapsed_sec": round(sum(r["elapsed_ms"] for r in records) / 1000, 1),
        "tool_calls": sum(r["tool_calls"] for r in records),
        "steps": sum(r["step_count"] for r in records),
        "tiers": tier_totals,
    }


def _by_dimension(records: Sequence[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in records:
        for dim in r["dimensions"]:
            slot = out.setdefault(dim, {"tasks": 0, "passed": 0, "available": 0, "score_sum": 0.0})
            slot["tasks"] += 1
            if not r["unavailable"]:
                slot["available"] += 1
                slot["score_sum"] += r["score"]
                slot["passed"] += int(r["passed"])
    for slot in out.values():
        n = slot["available"]
        slot["pass_rate"] = round(slot["passed"] / n, 4) if n else None
        slot["score_avg"] = round(slot["score_sum"] / n, 4) if n else None
        slot.pop("score_sum")
    return out


async def run_all(
    tasks: Sequence[TaskSpec],
    *,
    mode: str,
    run_id: str,
    only: Iterable[str] | None = None,
    limit: int | None = None,
    reuse_tools: bool = True,
    on_task: Callable[[dict], None] | None = None,
) -> dict:
    """跑一整轮：清残留 → 逐题执行 → 汇总。

    `reuse_tools=True` 时**只加载一次 MCP 工具**（6 个 server 的启动开销实测 8~16 秒/次，
    60 次就是十几分钟）—— 工具本身无状态（每次调用自建会话），复用不影响题目隔离。
    """
    selected = [t for t in tasks if only is None or t.id in set(only)]
    if limit is not None:
        selected = selected[:limit]

    snapshot = prepare_run(
        mysql_databases=sorted({db for t in selected for db in t.mysql_databases}),
        mysql_tables=sorted({pair for t in selected for pair in t.mysql_tables}),
    )
    # 懒加载工具（复用同一条 server 通道）
    tools = None
    if reuse_tools:
        from app.code_agent.agent.code_agent import _load_all_tools

        tools = await _load_all_tools()

    records: list[dict] = []
    started = time.perf_counter()
    for i, spec in enumerate(selected, 1):
        record = await run_one_task(spec, mode=mode, run_id=run_id, tools=tools)
        records.append(record)
        if on_task is not None:
            on_task(record)
        print(
            f"[{i}/{len(selected)}] {record['id']:<6} {record['status']:<9} "
            f"{'PASS' if record['passed'] else ('N/A' if record['unavailable'] else 'FAIL')} "
            f"score={record['score']} {record['elapsed_ms'] / 1000:.1f}s "
            f"tok={record['token_usage']}",
            flush=True,
        )

    from app.code_agent.model.llm import registry

    return {
        "run_id": run_id,
        "mode": mode,
        "started_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "wall_sec": round(time.perf_counter() - started, 1),
        "env": snapshot,
        "models": registry.role_models() if hasattr(registry, "role_models") else {},
        "totals": _aggregate(records),
        "by_dimension": _by_dimension(records),
        "tasks": records,
    }


def save_run(payload: dict) -> Path:
    """存到 `runtime/runs/`（gitignore）；正式结果由人再复制到 `docs/evidence/`。"""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{payload['run_id']}.json"
    with path.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    return path


def task_inventory(tasks: Sequence[TaskSpec]) -> dict:
    """题集自检：题量 / 维度覆盖 / 是否有状态断言 / 长任务与对抗题占比。"""
    dims: dict[str, int] = {}
    kinds: dict[str, int] = {}
    missing_state = []
    for t in tasks:
        for d in t.all_dimensions:
            dims[d] = dims.get(d, 0) + 1
        kinds[t.kind] = kinds.get(t.kind, 0) + 1
        # 判定器是闭包，档位要到执行时才知道 —— 这里用工厂上挂的标记（见 tasks.py 的 `state()` 包装）
        marks = [getattr(f, "__tier__", None) for f in t.checks]
        if V.TIER_STATE not in marks:
            missing_state.append(t.id)
    return {
        "total": len(tasks),
        "dimensions": dims,
        "kinds": kinds,
        "without_state_assertion": missing_state,
    }
