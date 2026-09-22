"""阶段 6 · 题集的**机械验收**（方案 T6.1/T6.2 的验收项，全部可脚本统计）。

这些断言为什么必须自动化：题集是"活的"（以后还要加题），而"每道题至少一个状态断言"
这种事**靠自觉一定会漂**——旧 30 题集就是漂成了 14/30 题没有产物级断言。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import verifiers as V  # noqa: E402
from evals.runner import task_inventory  # noqa: E402
from evals.tasks import MYSQL_DATABASES, TASKS  # noqa: E402

DIMENSIONS = {
    "tool_selection",
    "task_completion",
    "multi_step",
    "cross_tool",
    "error_recovery",
    "safety",
    "context_management",
    "efficiency",
}


def _tiers_of(task) -> list[str]:
    return [getattr(f, "__tier__", "?") for f in task.checks]


def test_task_count_within_plan_range():
    """方案要求 **≥24 且 ≤30**（第六版的"24-30"与"8×3"要能同时满足）。"""
    assert 24 <= len(TASKS) <= 30, f"实际 {len(TASKS)} 题"


def test_ids_unique_and_well_formed():
    ids = [t.id for t in TASKS]
    assert len(ids) == len(set(ids)), "题号重复"
    for task in TASKS:
        assert task.id.startswith("E") and len(task.id) == 4, task.id


def test_every_dimension_has_at_least_three_tasks():
    counts: dict[str, int] = {}
    for task in TASKS:
        for dim in task.all_dimensions:
            counts[dim] = counts.get(dim, 0) + 1
    assert set(counts) == DIMENSIONS, f"维度对不上：{sorted(set(counts) ^ DIMENSIONS)}"
    thin = {d: n for d, n in counts.items() if n < 3}
    assert not thin, f"这些维度不足 3 题（没有统计意义）：{thin}"


def test_every_task_has_at_least_one_state_assertion():
    """**方案 T6.1 的头号验收项**：每道题至少一个"③ 状态断言"。"""
    missing = [t.id for t in TASKS if V.TIER_STATE not in _tiers_of(t)]
    assert not missing, f"这些题没有产物级断言（模型说对话就能过）：{missing}"


def test_every_task_has_at_least_two_checks():
    thin = [t.id for t in TASKS if len(t.checks) < 2]
    assert not thin, f"这些题只有一条断言，太脆：{thin}"


def test_kinds_cover_basic_long_and_adversarial():
    kinds: dict[str, int] = {}
    for task in TASKS:
        kinds[task.kind] = kinds.get(task.kind, 0) + 1
    assert kinds.get("basic", 0) >= 8, "基础题（对照组）至少要 8 题"
    assert kinds.get("long", 0) >= 8, "长任务题至少要 8 题，否则体现不出多 Agent 价值"
    assert kinds.get("adversarial", 0) >= 6, "对抗题至少要 6 题"


def test_prompts_are_non_trivial():
    for task in TASKS:
        assert len(task.prompt.strip()) >= 15, f"{task.id} 的题面太短"


def test_timeouts_are_sane():
    for task in TASKS:
        assert 60 <= task.timeout_sec <= 900, f"{task.id} 的超时 {task.timeout_sec}s 不合理"


def test_inventory_reports_no_missing_state_assertion():
    """`evals/run_e2e.py --list` 用的就是这条口径，两边必须一致。"""
    assert task_inventory(TASKS)["without_state_assertion"] == []


def test_all_check_factories_are_registered():
    for task in TASKS:
        for factory in task.checks:
            tier = getattr(factory, "__tier__", None)
            assert tier in V.TIERS, f"{task.id} 里有判定器没登记档位：{factory}"


def test_no_llm_judge_as_the_only_gate():
    """LLM 评分**只能当参考分**：它一抖，通过率就跟着抖，没法复现。"""
    for task in TASKS:
        gating = [f for f in task.checks if getattr(f, "__tier__", None) != V.TIER_JUDGE]
        assert gating, f"{task.id} 只有 LLM 评分断言"


def test_mysql_tasks_declare_their_databases():
    """用了 MySQL 判定器的题必须声明库名 —— 整轮开始前按这份清单清残留。"""
    for task in TASKS:
        uses_mysql = any(
            getattr(f, "__tier__", None) == V.TIER_STATE and "mysql" in getattr(f, "__name__", "")
            for f in task.checks
        )
        if uses_mysql:
            assert task.mysql_databases, f"{task.id} 用了 MySQL 判定器却没声明 mysql_databases"


def test_declared_databases_are_collected():
    assert "eval_shop" in MYSQL_DATABASES
    assert "eval_lib" in MYSQL_DATABASES
    assert "eval_decoy" in MYSQL_DATABASES


def test_readonly_task_really_uses_readonly_mode():
    readonly_tasks = [t for t in TASKS if t.permission_mode]
    assert readonly_tasks, "至少要有一道题固定「只读」档来测拦截"
    for task in readonly_tasks:
        assert task.permission_mode == "readonly"


def test_adversarial_tasks_never_ask_for_real_destructive_commands():
    """**安全红线**：题面里不许出现"真的去执行 rm -rf /"这类要求。

    危险命令拦截由 `tests/test_dangerous_commands.py`（打桩 subprocess）验证；
    在端到端评测里放一条真命令，等于"防线一旦失效就把机器删了"。
    """
    banned = ("rm -rf /", "Remove-Item -Recurse -Force C:\\", "mkfs", "--no-preserve-root")
    for task in TASKS:
        for pattern in banned:
            assert pattern not in task.prompt, f"{task.id} 的题面里有真危险命令：{pattern}"


def test_high_risk_task_uses_restricted_approver():
    """高危对抗题必须换批准器（只拦高危），否则自动批准器会一路放行。"""
    restricted = [t for t in TASKS if t.approver_factory is not None]
    assert restricted, "至少要有一道题限制批准器（只拦高危）"
    for task in restricted:
        approver = task.approver_factory()
        assert approver.deny_high_risk is True


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_each_task_is_self_consistent(task):
    """逐题体检：题号/维度/类型/判定器齐备，且题面里没有未替换的占位符。"""
    assert task.dimension in DIMENSIONS
    assert task.kind in {"basic", "long", "adversarial"}
    assert task.checks
    # 只查**我们自己用**的占位符（题面里合法地出现 JSON 花括号，不能一刀切查 "{"）
    assert "{_WS}" not in task.prompt, f"{task.id} 的题面里 _WS 占位符没替换"
