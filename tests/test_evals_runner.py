"""阶段 6 · runner 的守卫测试。

守的是**旧口径的四个坑**（每一个都在方案里点过名）：

| 坑 | 这里对应哪条测试 |
|---|---|
| 残留污染下一题 | `test_clean_workspace_*` |
| single/multi 共用 checkpoint 线程 | `test_thread_id_contains_run_and_mode` |
| 超时题一个判定器都不跑、直接记 0 | `test_verifiers_still_run_when_task_times_out` |
| 环境不可用静默变 0 分、部分分也算通过 | `test_aggregate_*` |
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import runner as R  # noqa: E402
from evals import verifiers as V  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_workspace(tmp_path, monkeypatch):
    """把 workspace 与审计日志都引到 tmp —— 免得测试动到真实产物。"""
    monkeypatch.setattr(R, "WORKSPACE_DIR", tmp_path / "ws")
    monkeypatch.setattr(R, "PERMISSIONS_LOG", tmp_path / "permissions.log")
    monkeypatch.setattr(V, "WORKSPACE_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    yield


def _spec(**kwargs) -> R.TaskSpec:
    base = dict(
        id="T001",
        dimension="task_completion",
        kind="basic",
        prompt="做点什么",
        checks=(V.file_exists("out.txt"),),
    )
    base.update(kwargs)
    return R.TaskSpec(**base)


# ═══════════════════════════════════════════════════════════════════
# 清残留
# ═══════════════════════════════════════════════════════════════════


def test_clean_workspace_removes_previous_artifacts(tmp_path):
    ws = tmp_path / "ws"
    (ws / "leftover.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "sub").mkdir()
    (ws / "sub" / "deep.txt").write_text("junk", encoding="utf-8")

    removed = R.clean_workspace()

    assert removed == 2
    assert list(ws.iterdir()) == [], "工作目录必须被清空，否则上一题的产物会让下一题白过"


def test_clean_workspace_keeps_gitkeep(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".gitkeep").write_text("", encoding="utf-8")
    R.clean_workspace()
    assert (ws / ".gitkeep").exists()


# ═══════════════════════════════════════════════════════════════════
# WSL 上传目录的清理（订正 #33：它曾经**一整轮都没清**）
# ═══════════════════════════════════════════════════════════════════


def test_wsl_cleanup_is_on_by_default():
    """**"默认不清"就是要修的 bug**：默认值必须是真的目录，不是 `None`。

    2026-09-22 之前 `prepare_run(wsl_uploads=None)` + `run_all` 没传 →
    整整一轮都不清 `/home/leprite/nginx/uploads/`，E014 的两条 WSL 断言会被残留蒙过。
    """
    import inspect

    from app.code_agent.config import VM_UPLOADS_DIR

    default = inspect.signature(R.prepare_run).parameters["wsl_uploads"].default
    assert default == VM_UPLOADS_DIR, (
        "WSL 上传目录的清理必须是默认行为（否则调用方一忘就又静默跳过）"
    )


def test_wsl_cleanup_explicitly_skipped_is_recorded():
    """真要跳过就必须**看得出来**（旧代码里它和"清过了""清理失败"都返回 0）。"""
    record = R._clean_wsl_uploads(None)
    assert record["skipped"] is True
    assert record["attempted"] is False


def test_wsl_cleanup_reports_unavailable(monkeypatch):
    monkeypatch.setattr(V, "wsl_available", lambda: (False, "找不到 wsl.exe"))
    record = R._clean_wsl_uploads("/home/x/uploads")
    assert record["attempted"] is True
    assert record["ok"] is False
    assert "wsl.exe" in record["error"]


def test_wsl_cleanup_counts_deleted_files(monkeypatch):
    """`removed` 必须是**真删掉的数量**（0 表示"本来就是干净的"，与"跳过了"不同）。"""
    calls: list[str] = []

    def fake_run(script, timeout=30):
        calls.append(script)
        if "-delete" in script:
            return 0, "/home/x/uploads/a.txt\n/home/x/uploads/b.txt\n"
        return 0, "0\n"

    monkeypatch.setattr(V, "wsl_available", lambda: (True, ""))
    monkeypatch.setattr(V, "wsl_run", fake_run)

    record = R._clean_wsl_uploads("/home/x/uploads")
    assert record["ok"] is True
    assert record["removed"] == 2
    assert record["left"] == 0
    assert len(calls) == 2, "删完必须再核一次目录（把'命令说成功了'升级成'目录确实空了'）"


def test_wsl_cleanup_fails_when_leftovers_remain(monkeypatch):
    """删完还有残留 → 必须记成失败，不能因为 find 返回 0 就当成清干净了。"""

    def fake_run(script, timeout=30):
        return (0, "/home/x/uploads/a.txt\n") if "-delete" in script else (0, "1\n")

    monkeypatch.setattr(V, "wsl_available", lambda: (True, ""))
    monkeypatch.setattr(V, "wsl_run", fake_run)

    record = R._clean_wsl_uploads("/home/x/uploads")
    assert record["ok"] is False
    assert record["left"] == 1
    assert "残留" in record["error"]


def test_wsl_cleanup_reports_delete_failure(monkeypatch):
    monkeypatch.setattr(V, "wsl_available", lambda: (True, ""))
    monkeypatch.setattr(V, "wsl_run", lambda script, timeout=30: (1, "find: 权限不够"))
    record = R._clean_wsl_uploads("/home/x/uploads")
    assert record["ok"] is False
    assert "权限" in record["error"]


async def test_run_all_actually_cleans_wsl_uploads(monkeypatch, tmp_path):
    """端到端：`run_all` → `prepare_run` 这条链上**真的**把路径传到了清理函数。

    这是"文档说清了、代码没做"那类问题的机器守卫 —— 只测 `prepare_run` 自己不够，
    因为原来的坑恰恰在**调用方**。
    """
    from app.code_agent.config import VM_UPLOADS_DIR
    from app.code_agent.rag import store

    seen: dict = {}

    def spy(path):
        seen["path"] = path
        return {"attempted": bool(path), "ok": True, "path": path or "", "removed": 0, "left": 0}

    monkeypatch.setattr(R, "_clean_wsl_uploads", spy)
    monkeypatch.setattr(R, "_clean_mysql", lambda *a, **k: [])
    monkeypatch.setattr(R, "_clean_knowledge_root", lambda: 0)
    monkeypatch.setattr(store, "ensure_seeded", lambda *a, **k: None)
    monkeypatch.setattr(store, "get_chunk_count", lambda *a, **k: 0)
    monkeypatch.setattr(store, "get_reranker", lambda *a, **k: None)
    monkeypatch.setattr(V, "mysql_available", lambda: (False, "测试不连库"))
    monkeypatch.setattr(V, "wsl_available", lambda: (False, "测试不连 WSL"))

    payload = await R.run_all([], mode="single", run_id="unit", reuse_tools=False)

    assert seen["path"] == VM_UPLOADS_DIR, "run_all 必须清 WSL 上传目录（订正 #33）"
    assert payload["env"]["wsl_uploads"]["attempted"] is True


# ═══════════════════════════════════════════════════════════════════
# 知识库复位（订正 #35：模型会自己写知识库）
# ═══════════════════════════════════════════════════════════════════


async def test_knowledge_is_reset_before_every_task(monkeypatch):
    """每题开跑前都要**清根目录散文件 + 重扫知识库**。

    2026-09-23 smoke 实测：模型自己调了 `save_knowledge`（E007），
    写进去的条目会进向量库 → 后面的题检索时会看到它 → 破坏"每题独立可比"。
    关掉工具会改变口径（32 → 31），所以改成**每题复位**。
    """
    from app.code_agent.rag import store

    order: list[str] = []

    def spy_clean() -> int:
        order.append("clean_root")
        return 2

    def spy_seed() -> None:
        order.append("seed")

    async def ok_task(prompt, thread_id="eval", mode="auto", **kwargs):
        order.append("agent")
        return {"ok": True, "response": "done", "tool_trace": []}

    monkeypatch.setattr(R, "_clean_knowledge_root", spy_clean)
    monkeypatch.setattr(store, "seed_knowledge_base", spy_seed)
    monkeypatch.setattr(store, "get_chunk_count", lambda: 35)
    monkeypatch.setattr(R, "run_single_task", ok_task)

    record = await R.run_one_task(_spec(), mode="single", run_id="r1")

    assert order == ["clean_root", "seed", "agent"], "复位必须发生在 Agent 开跑之前"
    assert record["knowledge_reset"] == {"removed": 2, "chunks": 35}


async def test_knowledge_reset_uses_seed_not_ensure_seeded(monkeypatch):
    """**不能用 `ensure_seeded()`** —— 它带进程级 `_seeded` 标志，第二次调用直接跳过（永远不清）。"""
    from app.code_agent.rag import store

    calls: list[str] = []
    monkeypatch.setattr(store, "ensure_seeded", lambda *a, **k: calls.append("ensure_seeded"))
    monkeypatch.setattr(store, "seed_knowledge_base", lambda *a, **k: calls.append("seed"))
    monkeypatch.setattr(store, "get_chunk_count", lambda: 35)
    monkeypatch.setattr(R, "_clean_knowledge_root", lambda: 0)

    R._reset_knowledge()
    R._reset_knowledge()

    assert calls == ["seed", "seed"], "两次都要真的重扫，不能被 _seeded 短路"


def test_knowledge_reset_failure_is_recorded_not_fatal(monkeypatch):
    """复位失败**不该让整道题挂掉**，但必须看得见（不许静默吞掉）。"""
    from app.code_agent.rag import store

    def boom() -> None:
        raise RuntimeError("向量库连不上")

    monkeypatch.setattr(store, "seed_knowledge_base", boom)
    monkeypatch.setattr(R, "_clean_knowledge_root", lambda: 1)

    record = R._reset_knowledge()

    assert record["removed"] == 1
    assert record["chunks"] == -1
    assert "向量库连不上" in record["error"]


async def test_knowledge_reset_is_in_every_task_record(monkeypatch):
    """每题的结果里都要留下复位痕迹（报告/排查时能看出"这道题跑在什么知识库上"）。"""
    from app.code_agent.rag import store

    async def ok_task(prompt, thread_id="eval", mode="auto", **kwargs):
        return {"ok": True, "response": "done", "tool_trace": []}

    monkeypatch.setattr(R, "run_single_task", ok_task)
    monkeypatch.setattr(R, "_clean_knowledge_root", lambda: 0)
    monkeypatch.setattr(store, "seed_knowledge_base", lambda: None)
    monkeypatch.setattr(store, "get_chunk_count", lambda: 35)

    record = await R.run_one_task(_spec(), mode="single", run_id="r1")
    assert record["knowledge_reset"]["chunks"] == 35


# ═══════════════════════════════════════════════════════════════════
# 线程隔离 / 判定器照跑
# ═══════════════════════════════════════════════════════════════════


async def test_thread_id_contains_run_and_mode(monkeypatch):
    """thread_id 必须**同时带 run-id 与 mode** —— 否则 single 与 multi 会读到彼此的 checkpoint。"""
    seen: dict = {}

    async def fake_run_single_task(prompt, thread_id="eval", mode="auto", **kwargs):
        seen["thread_id"] = thread_id
        seen["mode"] = mode
        return {"ok": True, "response": "done", "tool_trace": []}

    monkeypatch.setattr(R, "run_single_task", fake_run_single_task)
    record = await R.run_one_task(_spec(), mode="multi", run_id="v3-multi")

    assert seen["thread_id"] == "eval-v3-multi-T001-multi"
    assert record["mode"] == "multi"


async def test_verifiers_still_run_when_task_times_out(monkeypatch, tmp_path):
    """**超时 ≠ 什么都没发生**：产物可能已经写出来了，判定器必须照跑。"""
    ws = tmp_path / "ws"

    async def slow_task(prompt, thread_id="eval", mode="auto", **kwargs):
        (ws / "out.txt").write_text("超时前写下的产物", encoding="utf-8")
        await asyncio.sleep(30)  # 一定跑不完
        return {}

    monkeypatch.setattr(R, "run_single_task", slow_task)
    record = await R.run_one_task(_spec(timeout_sec=1), mode="single", run_id="r1")

    assert record["status"] == "timeout"
    assert record["timeout_hit"] is True
    assert record["score"] == 1.0, "超时前已经产出的文件应当被判过 —— 旧口径会直接记 0 分"
    assert record["passed"] is True


async def test_exception_is_recorded_not_swallowed(monkeypatch):
    async def boom(prompt, thread_id="eval", mode="auto", **kwargs):
        raise RuntimeError("模拟 runner 内部炸了")

    monkeypatch.setattr(R, "run_single_task", boom)
    record = await R.run_one_task(_spec(), mode="single", run_id="r1")

    assert record["status"] == "error"
    assert "模拟" in record["error"]


async def test_checks_that_raise_become_failures(monkeypatch):
    """判定器自身抛异常必须**看得见**（记成不过），不能静默算过。"""

    def broken_check(ctx):
        raise ValueError("判定器写错了")

    async def ok_task(prompt, thread_id="eval", mode="auto", **kwargs):
        return {"ok": True, "response": "done", "tool_trace": []}

    monkeypatch.setattr(R, "run_single_task", ok_task)
    record = await R.run_one_task(_spec(checks=(broken_check,)), mode="single", run_id="r1")

    assert record["passed"] is False
    assert "判定器自身抛异常" in record["checks"][0]["detail"]


async def test_permission_stats_are_recorded(monkeypatch):
    """报告里那句"N 次写操作全部经过确认闸门"要有据可查。"""

    async def ok_task(prompt, thread_id="eval", mode="auto", **kwargs):
        return {
            "ok": True,
            "response": "done",
            "tool_trace": [],
            "permission": {
                "mode": "confirm",
                "modeLabel": "需确认",
                "approver": "eval_auto",
                "asked": 3,
                "granted": 3,
                "highRiskAsked": 1,
                "highRiskTools": ["file_delete"],
            },
        }

    monkeypatch.setattr(R, "run_single_task", ok_task)
    record = await R.run_one_task(_spec(), mode="single", run_id="r1")

    assert record["permission"]["mode"] == "confirm"
    assert record["permission"]["asked"] == 3
    assert record["permission"]["highRiskTools"] == ["file_delete"]


# ═══════════════════════════════════════════════════════════════════
# 汇总口径
# ═══════════════════════════════════════════════════════════════════


def _record(*, passed=False, unavailable=False, score=0.0, partial=False, status="completed"):
    return {
        "id": "T",
        "score": score,
        "passed": passed,
        "partial": partial,
        "unavailable": unavailable,
        "status": status,
        "token_usage": 100,
        "elapsed_ms": 1000,
        "tool_calls": 2,
        "step_count": 3,
        "tier_summary": {t: {"total": 1, "passed": 1, "failed": 0, "skipped": 0} for t in V.TIERS},
    }


def test_aggregate_excludes_unavailable_from_denominator():
    """**没测的题不许进分母**（旧口径把它当 0 分平均进总分）。"""
    records = [
        _record(passed=True, score=1.0),
        _record(passed=False, score=0.0),
        _record(unavailable=True, score=None),
    ]
    totals = R._aggregate(records)
    assert totals["tasks"] == 3
    assert totals["available"] == 2
    assert totals["unavailable"] == 1
    assert totals["pass_rate"] == 0.5, "分母必须是 2（真跑出结果的题）"
    assert totals["score_avg"] == 0.5


def test_aggregate_counts_partials_separately():
    """部分分**单独统计并披露**，不再被悄悄算成"通过"。"""
    records = [
        _record(passed=True, score=1.0),
        _record(passed=False, partial=True, score=0.5),
    ]
    totals = R._aggregate(records)
    assert totals["passed"] == 1
    assert totals["partial"] == 1
    assert totals["partial_rate"] == 0.5
    assert totals["pass_rate"] == 0.5


def test_aggregate_counts_timeouts_and_errors():
    records = [
        _record(status="timeout"),
        _record(status="error"),
        _record(passed=True, score=1.0),
    ]
    totals = R._aggregate(records)
    assert totals["timeout"] == 1
    assert totals["error"] == 1


def test_by_dimension_skips_unavailable():
    records = [
        {**_record(passed=True, score=1.0), "dimensions": ["safety"]},
        {**_record(unavailable=True, score=None), "dimensions": ["safety"]},
    ]
    by_dim = R._by_dimension(records)
    assert by_dim["safety"]["tasks"] == 2
    assert by_dim["safety"]["available"] == 1
    assert by_dim["safety"]["score_avg"] == 1.0


# ═══════════════════════════════════════════════════════════════════
# 题集自检
# ═══════════════════════════════════════════════════════════════════


def test_task_inventory_flags_task_without_state_assertion():
    """自检必须**抓得住**"只有轨迹断言"的题（否则这道闸门是摆设）。"""
    weak = _spec(checks=(V.used_tools({"write_file"}),))
    inventory = R.task_inventory([weak])
    assert inventory["without_state_assertion"] == ["T001"]


def test_task_inventory_accepts_state_assertion():
    inventory = R.task_inventory([_spec(checks=(V.file_exists("a.txt"),))])
    assert inventory["without_state_assertion"] == []
    assert inventory["total"] == 1


def test_save_run_writes_json(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RUNS_DIR", tmp_path / "runs")
    path = R.save_run({"run_id": "unit-test", "totals": {}})
    assert path.exists()
    assert "unit-test" in path.name


def test_audit_stats_summarizes_decisions(tmp_path):
    stats = R._audit_stats(
        [
            {"tool": "write_file", "decision": "allowed_by_eval_auto", "high_risk": False},
            {"tool": "file_delete", "decision": "allowed_by_eval_auto", "high_risk": True},
            {"tool": "mysql_delete_data", "decision": "denied_by_user", "high_risk": True},
        ]
    )
    assert stats["lines"] == 3
    assert stats["decisions"]["allowed_by_eval_auto"] == 2
    assert stats["decisions"]["denied_by_user"] == 1
    assert stats["highRisk"] == 2
    assert stats["tools"] == ["file_delete", "mysql_delete_data", "write_file"]


def test_audit_reads_only_new_lines(tmp_path, monkeypatch):
    log = tmp_path / "permissions.log"
    log.write_text('{"tool": "old", "decision": "x"}\n', encoding="utf-8")
    monkeypatch.setattr(R, "PERMISSIONS_LOG", log)

    offset = R._audit_offset()
    with log.open("a", encoding="utf-8") as fp:
        fp.write('{"tool": "new", "decision": "y"}\n')
        fp.write("这不是 JSON，应被跳过\n")

    entries = R._audit_since(offset)
    assert [e["tool"] for e in entries] == ["new"]


def test_audit_offset_of_missing_file_is_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "PERMISSIONS_LOG", tmp_path / "nope.log")
    assert R._audit_offset() == 0
    assert R._audit_since(0) == []


def test_default_timeout_is_documented():
    assert R.DEFAULT_TASK_TIMEOUT >= 120, "单题超时太短会把正常的长任务误判成超时"


def test_workspace_path_is_used_by_clean(tmp_path):
    """兜底：clean_workspace 真的作用在被 patch 的目录上（防止测试变假绿）。"""
    ws = tmp_path / "ws"
    (ws / "x.txt").write_text("1", encoding="utf-8")
    R.clean_workspace()
    assert not (ws / "x.txt").exists()


def test_run_single_task_signature_has_tools_param():
    """runner 依赖 `tools=` 复用它加载的 MCP 工具（省掉每题 8~16 秒启动）。"""
    import inspect

    from app.code_agent.agent.code_agent import run_single_task

    params = inspect.signature(run_single_task).parameters
    assert "tools" in params
    assert "approver" in params
    assert "permission_mode" in params


def test_paths_are_absolute_in_module():
    assert Path(R.WORKSPACE_DIR).is_absolute()


def test_effective_timeout_honours_override(monkeypatch):
    """`EVAL_TASK_TIMEOUT` 覆盖每题的墙钟上限；`0` = **不设超时（只计量）**。

    背景（2026-09-24）：要回答"multi 到底能不能做完这道题"，就不能让人为闸门先把答案掐掉
    —— multi 的 E015 当初就是被 token 上限终止的。预算与超时统一成"`<=0` = 只计量"，
    并且**两个值都写进结果快照**，让归档数据能自证口径。
    """
    monkeypatch.setattr(R, "EVAL_TIMEOUT_OVERRIDE", None)
    assert R.effective_timeout(420) == 420  # 没设覆盖 → 用题目自己的值

    monkeypatch.setattr(R, "EVAL_TIMEOUT_OVERRIDE", 0)
    assert R.effective_timeout(420) == 0  # 0 = 不限制

    monkeypatch.setattr(R, "EVAL_TIMEOUT_OVERRIDE", 1800)
    assert R.effective_timeout(420) == 1800  # 覆盖值优先

    monkeypatch.setattr(R, "EVAL_TIMEOUT_OVERRIDE", None)
    assert R.effective_timeout(0) == 0  # 题目自己写 0 也等于不设超时


def test_uncapped_round_is_recorded_in_the_snapshot(monkeypatch):
    """口径必须落进快照：归档的结果 JSON 要能自证"这一轮关没关上限"。"""
    monkeypatch.setattr(R, "EVAL_TIMEOUT_OVERRIDE", 0)

    snap = R.prepare_run(mysql_databases=(), wsl_uploads=None)

    assert snap["task_timeout_override"] == 0
    assert "task_token_budget" in snap
