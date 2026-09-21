"""阶段 5 · T5.1 / T5.2 权限层的测试。

守的是**四条不可违背的约束**（B1+），不是"表里写了什么"：

1. **权限判定在缓存查询之前** —— 否则"曾经允许过"的缓存值会让**已被拒绝**的调用照样返回结果；
2. **被拒绝的调用不写缓存、也不读缓存**；
3. **拒绝用独立异常类型** —— 不能被"写失败 → 清缓存"分支误当成执行失败；
4. **拒绝信息要说清原因和出路** —— 否则模型会反复重试烧 token。

另外守两条"表本身"的性质：

- **32 个工具一个不多一个不少**（从源码解析工具名，新增工具漏归类 → 这里立刻红）；
- **未归类工具在三个模式下都被拒绝**（白名单语义，给将来新增的工具兜底）。

⚠️ `tests/conftest.py` 有一个 autouse 的 fixture 把全局默认档位设成「放开」，
好让阶段 4 那批"外置 + 缓存"的测试不被权限层干扰；**本文件一律显式
`bind_session`/`use_mode` 切回要测的档位**，所以两者互不干扰。
"""

import ast
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.security import permissions as perm  # noqa: E402
from app.code_agent.tools.file_tools import file_tools  # noqa: E402
from app.code_agent.utils import tool_wrap as tw  # noqa: E402
from app.code_agent.utils.tool_cache import ToolCache  # noqa: E402
from tests.test_tool_cache import _FakeRedis  # noqa: E402
from tests.test_tool_wrap import _Tool  # noqa: E402

# ═══════════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════════


def _session(mode, *, approver=None, sync_approver=None, timeout=1.0, always=None):
    return perm.Session(
        mode=mode,
        scope="test",
        approver=approver,
        sync_approver=sync_approver,
        timeout=timeout,
        always_allow=always if always is not None else set(),
    )


def _mcp_tool_names() -> set[str]:
    """从 MCP server 源码里**解析**工具名（不是硬编码一份名单）。

    为什么要解析：这条测试的目的是"将来新增工具、忘了归类时会红"。
    硬编码一份名单的话，加工具时得同时改代码和测试，等于没守。

    规则与 FastMCP 一致：`@mcp.tool(name="x")` 取 `x`；`@mcp.tool(...)` 没写 name 时用函数名。
    """
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "app/code_agent/mcp_servers").glob("*.py"))
    files.append(root / "app/code_agent/rag/rag.py")

    names: set[str] = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                target = call.func if call else decorator
                if not (isinstance(target, ast.Attribute) and target.attr == "tool"):
                    continue
                explicit = None
                for keyword in call.keywords if call else []:
                    if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                        explicit = keyword.value.value
                names.add(explicit or node.name)
    return names


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def audit_log(monkeypatch, tmp_path):
    """把审计日志引到 tmp（conftest 已经指过一次，这里再显式来一遍便于断言）。"""
    path = tmp_path / "permissions.log"
    monkeypatch.setattr(perm, "PERMISSIONS_LOG", path)
    return path


@pytest.fixture
def fake_cache(monkeypatch):
    cache = ToolCache(scope="perm", enabled=True)
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(cache, "_get_client", _client)
    cache._fake = fake
    return cache


# ═══════════════════════════════════════════════════════════════════
# 一、档位表本身
# ═══════════════════════════════════════════════════════════════════


def test_tier_table_covers_exactly_the_32_real_tools():
    """**完整性**：源码里 25 个 MCP 工具 + 7 个文件工具，一个不多一个不少地归了类。

    漏归类不是"少一条记录"，而是**默认拒绝**（白名单语义）—— 功能会静默坏掉，
    所以必须让测试红，而不是等运行时报"工具不在表里"。
    """
    actual = _mcp_tool_names() | {t.name for t in file_tools}

    assert len(actual) == 32, f"实际工具数变了（{len(actual)}），请同步档位表：{sorted(actual)}"
    assert actual == perm.ALL_TOOLS, (
        f"没归类：{sorted(actual - perm.ALL_TOOLS)}；"
        f"表里有但源码里没有：{sorted(perm.ALL_TOOLS - actual)}"
    )
    assert not (perm.READONLY_TOOLS & perm.WRITE_TOOLS), "同一个工具不能既只读又写"


def test_tier_counts_match_the_approved_table():
    """数量与已审核的候选表一致（14 + 18 = 32，其中高危 6）。"""
    assert len(perm.READONLY_TOOLS) == 14
    assert len(perm.WRITE_TOOLS) == 18
    assert len(perm.HIGH_RISK_TOOLS) == 6
    assert perm.HIGH_RISK_TOOLS <= perm.WRITE_TOOLS, "高危标记只能加在写/执行类上"
    # 确认框要给高危操作显示"影响面说明"，所以每个高危工具都必须有文案
    assert set(perm.HIGH_RISK_NOTES) == set(perm.HIGH_RISK_TOOLS)


def test_default_mode_constant_is_confirm():
    """默认档位是「需确认」（D4）。"""
    assert perm.DEFAULT_MODE == perm.MODE_CONFIRM
    assert perm.MODE_LABELS[perm.DEFAULT_MODE] == "需确认"
    # 无论 .env 怎么设，兜底出来的档位都必须是合法三档之一
    assert perm.Session.from_config().mode in perm.MODES


def test_classify_and_high_risk():
    assert perm.classify("read_file") == "readonly"
    assert perm.classify("write_file") == "write"
    assert perm.classify("nope") == "unknown"
    assert perm.is_high_risk("file_delete") is True
    assert perm.is_high_risk("write_file") is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("readonly", perm.MODE_READONLY),
        ("READONLY", perm.MODE_READONLY),
        (" 需确认 ", perm.MODE_CONFIRM),
        ("open", perm.MODE_OPEN),
        ("放开", perm.MODE_OPEN),
        ("bogus", perm.DEFAULT_MODE),
        ("", perm.DEFAULT_MODE),
        (None, perm.DEFAULT_MODE),
    ],
)
def test_normalize_mode(raw, expected):
    assert perm.normalize_mode(raw) == expected


# ═══════════════════════════════════════════════════════════════════
# 二、档位矩阵（纯函数 decide）
# ═══════════════════════════════════════════════════════════════════


def test_readonly_tools_allowed_in_every_mode():
    for mode in perm.MODES:
        for name in perm.READONLY_TOOLS:
            assert perm.decide(name, mode) == perm.ALLOW, f"{mode} 下 {name} 应放行"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (perm.MODE_READONLY, perm.DENY),
        (perm.MODE_CONFIRM, perm.ASK),
        (perm.MODE_OPEN, perm.ALLOW),
    ],
)
def test_write_tools_matrix(mode, expected):
    for name in perm.WRITE_TOOLS:
        assert perm.decide(name, mode) == expected, f"{mode} 下 {name} 判定错了"


@pytest.mark.parametrize("mode", perm.MODES)
def test_unknown_tool_denied_in_every_mode(mode):
    """白名单语义：没归类的工具在**任何**档位下都拒绝（含「放开」）。"""
    assert perm.decide("brand_new_tool_nobody_classified", mode) == perm.DENY


def test_garbage_mode_is_treated_as_most_restrictive():
    """配置被手改成脏值时，绝不因为"读不懂"而放行写操作。"""
    assert perm.decide("write_file", "随便写的") == perm.DENY
    assert perm.decide("read_file", "随便写的") == perm.ALLOW


# ═══════════════════════════════════════════════════════════════════
# 三、拒绝的文案与异常类型（约束 3、4）
# ═══════════════════════════════════════════════════════════════════


async def test_rejection_message_has_reason_and_way_out(audit_log):
    """拒绝信息必须让模型知道"为什么"和"怎么办"，否则它会反复重试烧 token。"""
    with perm.bind_session(_session(perm.MODE_READONLY)):
        with pytest.raises(perm.PermissionDenied) as excinfo:
            await perm.enforce("write_file", {"path": "a.py"})

    message = str(excinfo.value)
    assert "只读" in message, "要说清当前档位（原因）"
    assert "write_file" in message, "要说清是哪个工具"
    assert "需确认" in message, "要给出出路（切到哪一档）"
    assert "未执行" in message and "未缓存" in message


async def test_unknown_tool_message_points_at_the_table(audit_log):
    with perm.bind_session(_session(perm.MODE_OPEN)):
        with pytest.raises(perm.PermissionDenied) as excinfo:
            await perm.enforce("mystery_tool", {})

    assert excinfo.value.decision == "deny_unknown_tool"
    assert "permissions.py" in str(excinfo.value), "要告诉人去哪补档位表"


def test_permission_denied_is_a_dedicated_type():
    """独立异常类型：**不是**工具自身的异常，也不是 ValueError/OSError 之类会被顺手捕获的东西。"""
    assert issubclass(perm.PermissionDenied, Exception)
    assert not issubclass(perm.PermissionDenied, (ValueError, OSError, TimeoutError))
    err = perm.PermissionDenied(
        "说明", tool_name="t", mode=perm.MODE_CONFIRM, decision="denied_by_user"
    )
    assert str(err) == "说明", "args[0] 就是给模型看的那句话（ToolNode 会原样放进 ToolMessage）"


# ═══════════════════════════════════════════════════════════════════
# 四、顺序约束（**这一节是阶段 5 的核心**）
# ═══════════════════════════════════════════════════════════════════


async def test_permission_check_runs_before_cache_lookup(monkeypatch):
    """**约束 1**：被拒绝的调用绝不能拿到缓存里的旧结果。

    怎么构造出来的：把 `write_file` 临时塞进可缓存名单。
    现实里可缓存的只有只读工具（那就永远放行），所以要**人为**造出
    "同一工具先被允许、后被拒绝"的场景 —— 这正是这条顺序约束要防的事：
    只要两张表里任何一张将来变了，顺序写错就会让"拒绝"被缓存旁路掉。
    """
    monkeypatch.setattr(tw, "CACHEABLE_TOOL_NAMES", {"write_file"})
    calls = {"n": 0}

    async def call_tool(**kwargs):
        calls["n"] += 1
        return (f"结果{calls['n']}", None)

    cache = ToolCache(scope="order", enabled=True)
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(cache, "_get_client", _client)

    tool = tw.wrap_tool(_Tool("write_file", coroutine=call_tool), cache)

    # 第一次：「放开」档 → 真实执行并被缓存
    with perm.bind_session(_session(perm.MODE_OPEN)):
        assert (await tool.coroutine(path="a"))[0] == "结果1"

    # 第二次：切到「只读」档 → 必须**拒绝**，而不是命中缓存返回 "结果1"
    with perm.bind_session(_session(perm.MODE_READONLY)):
        with pytest.raises(perm.PermissionDenied):
            await tool.coroutine(path="a")

    assert calls["n"] == 1, "被拒绝的调用绝不能真的执行"
    assert fake.set_calls == 1, "被拒绝的调用绝不能写缓存"


async def test_denied_call_does_not_invalidate_cache(fake_cache, audit_log):
    """**约束 3 的回归**：拒绝不能走"写失败 → 清缓存"那条分支。

    如果 `PermissionDenied` 不是独立类型、或者判定被放进了执行 try 块里，
    一次"被拒绝的写"就会把整个会话的缓存清空 —— 那是纯粹的误伤。
    """

    async def read_tool(**kwargs):
        return ("只读结果", None)

    async def write_tool(**kwargs):
        raise AssertionError("被拒绝的调用不该走到这里")

    read = tw.wrap_tool(_Tool("read_file_range", coroutine=read_tool), fake_cache)
    write = tw.wrap_tool(_Tool("write_file", coroutine=write_tool), fake_cache)

    with perm.bind_session(_session(perm.MODE_OPEN)):
        await read.coroutine(path="a")
    assert fake_cache._fake.data, "先确认缓存里确实有东西"

    with perm.bind_session(_session(perm.MODE_READONLY)):
        with pytest.raises(perm.PermissionDenied):
            await write.coroutine(path="a")

    assert fake_cache._fake.data, "被拒绝的调用绝不能顺手清空缓存"


async def test_denied_tool_never_reaches_the_underlying_call():
    executed = []

    async def call_tool(**kwargs):
        executed.append(kwargs)
        return ("写了", None)

    tool = tw.wrap_tool(_Tool("write_file", coroutine=call_tool))
    with perm.bind_session(_session(perm.MODE_READONLY)):
        with pytest.raises(perm.PermissionDenied):
            await tool.coroutine(path="a", content="x")

    assert executed == [], "工具函数本身一次都不能被调到"


# ═══════════════════════════════════════════════════════════════════
# 五、人工确认（T5.3 的判定侧：允许 / 否决 / 超时 / 无通道）
# ═══════════════════════════════════════════════════════════════════


async def test_user_allow_executes(audit_log):
    async def approver(request):
        assert request.tool_name == "write_file"
        assert request.high_risk is False
        return True

    with perm.bind_session(_session(perm.MODE_CONFIRM, approver=approver)):
        await perm.enforce("write_file", {"path": "a"})  # 不抛 = 放行

    assert [e["decision"] for e in _read_log(audit_log)] == ["allowed_by_user"]


async def test_user_reject_denies(audit_log):
    async def approver(request):
        return False

    with perm.bind_session(_session(perm.MODE_CONFIRM, approver=approver)):
        with pytest.raises(perm.PermissionDenied) as excinfo:
            await perm.enforce("write_file", {})

    assert excinfo.value.decision == "denied_by_user"
    assert "拒绝" in str(excinfo.value)
    assert [e["decision"] for e in _read_log(audit_log)] == ["denied_by_user"]


async def test_no_answer_times_out_into_rejection(audit_log):
    """**B2：超时 / 无人应答 → 自动拒绝**（安全侧，绝不能超时放行）。"""

    async def never_answers(request):
        await asyncio.sleep(30)
        return True

    with perm.bind_session(_session(perm.MODE_CONFIRM, approver=never_answers, timeout=0.05)):
        with pytest.raises(perm.PermissionDenied) as excinfo:
            await perm.enforce("write_file", {})

    assert excinfo.value.decision == "deny_timeout"
    assert "无人应答" in str(excinfo.value)
    assert [e["decision"] for e in _read_log(audit_log)] == ["deny_timeout"]


async def test_confirm_without_any_channel_fails_closed(audit_log):
    """无人值守入口（没有确认通道）：「需确认」档必须**拒绝**，不能默默放行。"""
    with perm.bind_session(_session(perm.MODE_CONFIRM)):  # approver=None
        with pytest.raises(perm.PermissionDenied) as excinfo:
            await perm.enforce("write_file", {})

    assert excinfo.value.decision == "deny_no_channel"
    assert "没有可用的人工确认通道" in str(excinfo.value)


async def test_broken_approver_also_fails_closed(audit_log):
    """确认通道自己抛异常时也必须拒绝（不能"问不到人就当同意"）。"""

    async def broken(request):
        raise RuntimeError("ws 断了")

    with perm.bind_session(_session(perm.MODE_CONFIRM, approver=broken)):
        with pytest.raises(perm.PermissionDenied):
            await perm.enforce("write_file", {})


async def test_high_risk_request_carries_impact_note():
    seen = {}

    async def approver(request):
        seen.update(request.to_payload("rid-1", 120))
        return True

    with perm.bind_session(_session(perm.MODE_CONFIRM, approver=approver)):
        await perm.enforce("mysql_execute_command", {"sql": "DROP TABLE t"})

    assert seen["highRisk"] is True
    assert "MySQL" in seen["note"], "高危操作要带影响面说明"
    assert seen["type"] == "permission_request" and seen["requestId"] == "rid-1"


# ═══════════════════════════════════════════════════════════════════
# 六、「本会话内对该工具总是允许」（B7）
# ═══════════════════════════════════════════════════════════════════


async def test_always_allow_asks_only_once(audit_log):
    asked = []

    async def approver(request):
        asked.append(request.tool_name)
        perm.grant_always(request.tool_name)  # 模拟用户勾了"本会话内总是允许"
        return True

    with perm.bind_session(_session(perm.MODE_CONFIRM, approver=approver)):
        await perm.enforce("write_file", {})
        await perm.enforce("write_file", {})
        await perm.enforce("write_file", {})

    assert asked == ["write_file"], "勾了「本会话内总是允许」之后不应再问"
    decisions = [e["decision"] for e in _read_log(audit_log)]
    assert decisions == [
        "allowed_by_user",
        "allowed_always_in_session",
        "allowed_always_in_session",
    ]


def test_always_allow_is_keyed_by_mode_so_switching_mode_drops_it():
    """B7：授权只在**当前会话 + 当前权限模式**内有效 —— 换档位即失效（不用额外清理）。"""
    with perm.bind_session(_session(perm.MODE_CONFIRM)) as session:
        perm.grant_always("write_file")
        assert ("confirm", "write_file") in session.always_allow
        assert ("open", "write_file") not in session.always_allow
        assert ("readonly", "write_file") not in session.always_allow


async def test_always_allow_does_not_leak_across_sessions():
    """换会话（新的 Session 对象）→ 上一个会话的授权不带过去。"""

    async def approver(request):
        perm.grant_always(request.tool_name)
        return True

    session_a = _session(perm.MODE_CONFIRM, approver=approver)
    with perm.bind_session(session_a):
        await perm.enforce("write_file", {})
    assert session_a.always_allow

    with perm.bind_session(_session(perm.MODE_CONFIRM, approver=approver)):
        assert perm.current_session().always_allow == set()


def test_revoke_all_always_clears_grants():
    with perm.bind_session(_session(perm.MODE_CONFIRM)) as session:
        perm.grant_always("write_file")
        perm.grant_always("file_delete")
        perm.revoke_all_always()
    assert session.always_allow == set()


# ═══════════════════════════════════════════════════════════════════
# 七、同步路径（只有同步 `_run` 的工具）
# ═══════════════════════════════════════════════════════════════════


def test_sync_path_fails_closed_without_sync_approver(audit_log):
    with perm.bind_session(_session(perm.MODE_CONFIRM)):  # 只绑了异步通道
        with pytest.raises(perm.PermissionDenied) as excinfo:
            perm.enforce_sync("write_file", {})

    assert excinfo.value.decision == "deny_sync_no_channel"


def test_sync_path_uses_sync_approver(audit_log):
    asked = []

    def sync_approver(request):
        asked.append(request.tool_name)
        return True

    with perm.bind_session(_session(perm.MODE_CONFIRM, sync_approver=sync_approver)):
        perm.enforce_sync("write_file", {})

    assert asked == ["write_file"]


def test_sync_proxy_goes_through_permission_layer(audit_log):
    """只有同步 `_run` 的工具被换成代理后，**同步入口也要过权限**（不能有旁路）。"""
    wrapped = tw.wrap_tool(_Tool("write_file", run=lambda path: f"写了{path}"))

    with perm.bind_session(_session(perm.MODE_READONLY)):
        with pytest.raises(perm.PermissionDenied):
            wrapped.invoke({"path": "a"})

    with perm.bind_session(_session(perm.MODE_OPEN)):
        assert wrapped.invoke({"path": "a"}) == "写了a"


# ═══════════════════════════════════════════════════════════════════
# 八、审计留痕（T5.4 的判定侧）
# ═══════════════════════════════════════════════════════════════════


def test_audit_appends_one_json_line_per_event(monkeypatch, tmp_path):
    path = tmp_path / "permissions.log"
    monkeypatch.setattr(perm, "PERMISSIONS_LOG", path)

    perm.audit({"scope": "s", "tool": "write_file", "decision": "denied_by_user"})
    perm.audit({"scope": "s", "tool": "file_delete", "decision": "allowed_by_user"})

    lines = _read_log(path)
    assert [line["tool"] for line in lines] == ["write_file", "file_delete"]
    assert all("ts" in line for line in lines), "每条都要有时间戳"


def test_audit_never_raises_even_if_unwritable(monkeypatch, tmp_path):
    """审计是旁路：写不进去也不能让工具调用失败。"""
    monkeypatch.setattr(perm, "PERMISSIONS_LOG", tmp_path)  # 指向一个**目录** → 打开必失败
    perm.audit({"tool": "x"})  # 不抛就算过


async def test_open_mode_still_audits_high_risk(audit_log):
    """候选表 §一：「放开」档与「需确认」档的第二处差异 —— **高危仍然强制留痕**。"""
    with perm.bind_session(_session(perm.MODE_OPEN)):
        await perm.enforce("execute_powershell_command", {"command": "echo hi"})
        await perm.enforce("write_file", {"path": "a"})  # 非高危 → 不记（否则日志全是噪音）

    entries = _read_log(audit_log)
    assert len(entries) == 1, f"只该记高危那一条，实际：{[e['tool'] for e in entries]}"
    assert entries[0]["decision"] == "allowed_open_high_risk"
    assert entries[0]["high_risk"] is True
    assert "PowerShell" in entries[0]["note"]


async def test_mode_denials_are_audited(audit_log):
    with perm.bind_session(_session(perm.MODE_READONLY)):
        with pytest.raises(perm.PermissionDenied):
            await perm.enforce("mysql_delete_data", {"table": "t"})

    entry = _read_log(audit_log)[0]
    assert entry["decision"] == "deny_mode"
    assert entry["tier"] == "write"
    assert entry["high_risk"] is True
    assert entry["mode"] == perm.MODE_READONLY


def test_audit_truncates_huge_args(monkeypatch, tmp_path):
    """审计里不能塞进整个文件内容（`write_file` 的 content 可能几十 KB）。"""
    path = tmp_path / "permissions.log"
    monkeypatch.setattr(perm, "PERMISSIONS_LOG", path)

    perm.audit({"tool": "write_file", "args": perm._brief_args({"content": "x" * 5000})})

    entry = _read_log(path)[0]
    assert len(entry["args"]) < 500, "审计里的参数必须被截断"
    assert entry["args"].endswith("字符)"), "截断后要标出原文有多长"
