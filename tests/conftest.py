"""pytest 全局前置：把「阶段 4 的重活」在单元测试里关掉。

**为什么需要**：
- 「自动注入 / 自动沉淀」会 import RAG store → 加载 sentence-transformers + CrossEncoder
  （近百 MB 权重、数秒启动），单元测试不该为它买单，也不该去读写真实的 ChromaDB；
- 「工具结果缓存」会连真实 Redis，让"工具到底执行了几次"变得不确定。

⚠️ 这些开关**必须在 `app.code_agent.config` 被 import 之前**设好
—— 配置是在 import 时读环境变量的，晚一步就不生效了。
（conftest.py 由 pytest 在收集测试模块之前导入，正好满足这个顺序。）

⚠️ 这里用**强制赋值**而不是 `setdefault`：单测要确定性，
不能因为 shell 里恰好设了 `CODE_AGENT_RAG_AUTO_INJECT=1` 就变成慢测。
需要测这些开关**打开时**的行为时，用 `monkeypatch.setattr` 改模块级常量
（见 tests/test_memory.py）。
"""

import os

import pytest

os.environ["CODE_AGENT_RAG_AUTO_INJECT"] = "0"
os.environ["CODE_AGENT_RAG_AUTO_DEPOSIT"] = "0"
os.environ["CODE_AGENT_TOOL_CACHE"] = "0"


@pytest.fixture(autouse=True)
def _permission_open_by_default(tmp_path, monkeypatch):
    """单测默认把权限模式设为「放开」，并把审计日志引到 tmp。

    **为什么**：阶段 5 起，工具调用要先过权限层（`security/permissions.py`）。
    默认档位是「需确认」，而单测里**没有人可以问** —— 按 B2 的"无人应答 → 自动拒绝"，
    `write_file` 之类的工具会被统统拒掉，`test_tool_wrap.py` 那批既有测试会集体失败。
    那批测试要验的是"外置 + 缓存"的行为，不该被权限层干扰，所以这里统一放开。

    权限层自身的测试（`tests/test_permissions.py`）用 `use_mode()` / 显式
    `bind_session()` 把档位切回去，**不受这个 fixture 影响**；
    另有一条测试专门断言"配置里的默认档位是「需确认」"，防止默认值被悄悄改掉。

    ⚠️ 审计日志也必须指到 tmp：否则单测会往真实的 `runtime/permissions.log` 里灌垃圾。
    """
    from app.code_agent.security import permissions as perm

    monkeypatch.setattr(perm, "PERMISSIONS_LOG", tmp_path / "permissions.log")
    with perm.bind_session(perm.Session(mode=perm.MODE_OPEN, scope="pytest", timeout=1.0)):
        yield
