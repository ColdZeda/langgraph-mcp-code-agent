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
    CHROMA_DIR,
    KNOWLEDGE_DIR,
    MYSQL_DATABASE,
    PERMISSIONS_LOG,
    RUNS_DIR,
    WORKSPACE_DIR,
)
from app.code_agent.security.permissions import (
    HEADLESS_PERMISSION_MODE,
    AutoApprover,
    mode_label,
)
from evals import verifiers as V

#: 单题默认墙钟上限（秒）。超时不等于 0 分 —— 判定器照跑，产物照样验。
DEFAULT_TASK_TIMEOUT = 300

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
    removed = 0
    if not KNOWLEDGE_DIR.exists():
        return 0
    for child in KNOWLEDGE_DIR.iterdir():
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


def _clean_wsl_uploads(path: str) -> int:
    """清 WSL 上传目录里的文件（保留 `.gitkeep`）。"""
    ok, _ = V.wsl_available()
    if not ok:
        return 0
    q = V.shlex_quote(path)
    rc, out = V.wsl_run(f"find {q} -maxdepth 1 -type f ! -name '.gitkeep' -delete && echo cleaned")
    if rc != 0 or "cleaned" not in out:
        return 0
    return 1


def prepare_run(
    *,
    mysql_databases: Iterable[str] = (),
    mysql_tables: Iterable[tuple[str, str]] = (),
    wsl_uploads: str | None = None,
) -> dict:
    """整轮开始前的清残留 + 环境快照（快照会写进结果 JSON，报告里的"口径"靠它）。"""
    from app.code_agent.rag import store

    snapshot: dict[str, Any] = {}
    snapshot["workspace_cleaned"] = clean_workspace()
    snapshot["knowledge_root_cleaned"] = _clean_knowledge_root()
    snapshot["mysql_cleaned"] = _clean_mysql(mysql_databases, mysql_tables)
    snapshot["wsl_uploads_cleaned"] = _clean_wsl_uploads(wsl_uploads) if wsl_uploads else 0

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
    snapshot["chroma_dir"] = str(CHROMA_DIR)
    snapshot["database"] = MYSQL_DATABASE
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
    """跑一道题：执行 → （超时/异常也）执行判定器 → 汇总。"""
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
    )
    try:
        result = await asyncio.wait_for(call, timeout=spec.timeout_sec)
    except TimeoutError:
        # ⚠️ 超时不等于"什么都没发生"：产物可能已经写出来了。
        #    旧口径 (run_e2e.py:219) 只在 status == "completed" 时才跑判定器 →
        #    超时题**一个断言都不执行就记 0 分**，那是另一种失真。
        timeout_hit = True
        status = "timeout"
        result = {
            "ok": False,
            "error": f"TimeoutError: 超过 {spec.timeout_sec}s",
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
        "timeout_sec": spec.timeout_sec,
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
