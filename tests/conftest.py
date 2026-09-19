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

os.environ["CODE_AGENT_RAG_AUTO_INJECT"] = "0"
os.environ["CODE_AGENT_RAG_AUTO_DEPOSIT"] = "0"
os.environ["CODE_AGENT_TOOL_CACHE"] = "0"
