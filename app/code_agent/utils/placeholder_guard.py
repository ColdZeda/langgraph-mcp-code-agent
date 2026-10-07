"""阶段 8 · P1.5：**"模板没替换"这一类**的识别与拦截（候选池 §十五B）。

**要解决什么**（探索测试第 2 题的现场）：用户把提示词模板里的 `<你的WSL用户名>` **原样粘进来**，
模型一句没问、直接四处翻文件去猜，烧了 20.8 万 token（账本 R1/R2）。

**两道口子**（同一套判据，两处调用）：
1. **任务入口**（`multi_agent.run_multi_agent`）：命中 ⇒ **只回问、不进图**（0 次模型调用、0 次工具调用）；
2. **工具层**（`utils/tool_wrap._process`）：**路径类参数**命中 ⇒ 直接拒并给示例，
   让模型自己改（异常做成普通 `Exception`，LangGraph 的 ToolNode 会把它变成一条 ToolMessage 交给模型）。

⚠️ **判据必须是"形状 + 占位词"双条件**，不能见到尖括号/花括号就拦 —— 实测**两道评估题**会被误伤：
  · `E016` 的任务里写着 `GET /health 返回 {"status": "ok"}`（JSON 例子，单花括号）；
  · `E029` 的任务里写着 `每个文件里有一行 TOTAL=<数字>`（`<数字>` 是**文件内容的描述**，不是占位符）。
所以：
  · **单花括号 `{…}` 一律不管**（编程任务里 JSON / 字典 / f-string 太常见）；
  · `<…>` / `{{…}}` / `${…}` 只看**内层**：是全大写标识符（`YOUR_API_KEY`）或含中文占位词
    （`用户名 / 密码 / 路径 / 目录 / 地址 …`，见 `_CJK_WORDS`）才算；`<数字>`、`<h1>`、`${name}`、
    `{{ name }}` 都**不算**（前者无占位词、后者是小写标识符）；
  · 另有 `你的XXX` / `我的XXX` 一种（XXX 里必须含中文占位词，"你的代码"不算）。
回归护栏：`tests/test_placeholder_guard.py` 会把**全部 30 道评估题的 prompt** 过一遍
（以后谁写了一道带真占位符的题，会当场变红，而不是等评估跑崩）。
"""

from __future__ import annotations

import re
from typing import Any

# ── 内层"像占位符"的判据 ────────────────────────────────────────────

#: 全大写标识符：`YOUR_API_KEY` / `WSL_USERNAME` / `XXX`（≥3 个字符，避免 `<T>` 这类泛型参数被误判）
_UPPER_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_CJK = re.compile(r"[\u4e00-\u9fff]")
#: 中文占位词（内层含其中之一才算"让人填真实值"）
_CJK_WORDS = (
    "用户名",
    "密码",
    "密钥",
    "口令",
    "路径",
    "目录",
    "地址",
    "邮箱",
    "手机号",
    "账号",
    "名字",
    "项目名",
    "替换",
    "填写",
    "填上",
    "你的",
    "我的",
)
#: 英文占位词（出现在内层即可）
_ASCII_WORDS = (
    "YOUR",
    "MY_",
    "XXX",
    "PLACEHOLDER",
    "TODO",
    "USERNAME",
    "PASSWORD",
    "API_KEY",
    "TOKEN",
    "SECRET",
    "WSL_",
)

_BRACKETS = (
    re.compile(r"<([^<>\n]{1,40})>"),
    re.compile(r"\{\{([^{}\n]{1,40})\}\}"),  # Jinja 双花括号
    re.compile(r"\$\{([^{}\n]{1,40})\}"),  # shell / JS 模板
)
#: `你的XXX` / `我的XXX`：XXX 里必须含中文占位词（"你的代码" 不算）。
#: ⚠️ 尾部字符集**刻意排掉**尖括号/花括号/引号/斜杠 —— 否则 `<你的WSL用户名>` 里会再套出一个
#: `你的WSL用户名>/ngin` 这种噪声片段（第一版就是这样，靠下面的"重叠丢弃"兜住）。
_YOURS = re.compile(r"[你我]的([^<>{}\[\]()\s，。；：、,.;:!?/\\]{1,12})")

#: 明确要求"按字面处理"时**放行**（避免守卫把用户卡死 —— §十五A 的"绝不卡住不动"）
_LITERAL_HINTS = ("按字面", "原样处理", "不要替换", "字面处理", "别替换")


def _inner_looks_like_placeholder(inner: str) -> bool:
    text = inner.strip()
    if not text:
        return False
    if _UPPER_TOKEN.match(text):
        return True
    upper = text.upper()
    if any(word in upper for word in _ASCII_WORDS):
        return True
    return bool(_CJK.search(text)) and any(word in text for word in _CJK_WORDS)


def find_placeholders(text: str) -> list[str]:
    """找出文本里**看起来是没替换的模板占位符**的片段（去重、保持出现顺序）。

    ⚠️ 要去**重叠**：`<你的WSL用户名>` 既命中尖括号规则、内层又命中 `你的XXX` 规则；
    只保留最外层那一个（否则回问文案里会出现 `你的WSL用户名>/ngin` 这种噪声片段）。
    """
    spans: list[tuple[int, int]] = []
    found: list[str] = []
    for start, end, whole in sorted(_iter_candidates(text or "")):
        if any(s < end and start < e for s, e in spans):
            continue  # 与外层/已记录的片段重叠 ⇒ 丢掉
        spans.append((start, end))
        if whole not in found:
            found.append(whole)
    return found


def _iter_candidates(text: str):
    """产出 (start, end, 片段) —— 三种括号形状 + `你的XXX`。"""
    for pattern in _BRACKETS:
        for match in pattern.finditer(text):
            if _inner_looks_like_placeholder(match.group(1)):
                yield match.start(), match.end(), match.group(0)
    for match in _YOURS.finditer(text):
        if any(word in match.group(1) for word in _CJK_WORDS):
            yield match.start(), match.end(), match.group(0)


def looks_like_placeholder(value: Any) -> bool:
    """单个值（通常是一个路径参数）是不是模板占位符。"""
    return bool(isinstance(value, str) and find_placeholders(value))


def is_explicitly_literal(text: str) -> bool:
    """用户是否明确说了"按字面处理"（命中就**不拦**）。"""
    return any(hint in (text or "") for hint in _LITERAL_HINTS)


# ── 给模型/用户看的文案（中文；含"默认建议"，不是空问）────────────────


def clarify_reply(placeholders: list[str]) -> str:
    """入口拦截时的回问文案（§十五A：提问必须带默认建议 + 给出路）。"""
    shown = "、".join(f"「{p}」" for p in placeholders[:3])
    return (
        f"【需要你先确认】任务里出现了看起来**没有替换的模板占位符**：{shown}\n"
        "这类尖括号 / 花括号里的词，本意是让你填上真实值的（比如用户名、路径、密钥）。\n"
        "**我没有直接开跑**：猜错会白跑一轮，代价比问一句大。\n"
        "请把占位符换成真实值再发一次（例如把 `<你的WSL用户名>` 写成 `/home/zhangsan`）。\n"
        "如果你确实想让我**按字面**处理这些字符，就在任务里加一句「按字面处理」，我照做。"
    )


#: 参数名里出现这些片段就当成"路径类参数"（只查这些参数，避免误伤正文内容）
PATH_ARG_HINTS = (
    "path",
    "file",
    "dir",
    "folder",
    "destination",
    "source",
    "target",
    "root",
    "cwd",
    "workdir",
)


class PlaceholderArgError(Exception):
    """工具参数的路径里出现了没替换的模板占位符。

    ⚠️ 刻意做成**普通 `Exception`**（不是 `BaseException`）：我们要 LangGraph 的 `ToolNode`
    把它变成一条 `ToolMessage` 交给模型 —— 模型看到"这个参数是占位符，请用真实路径"就能自己改，
    而不是把整个任务打死。
    """

    def __init__(self, tool_name: str, arg_name: str, value: str, found: list[str]) -> None:
        shown = "、".join(f"「{p}」" for p in found[:3])
        super().__init__(
            f"[参数无效] 工具 {tool_name} 的参数 {arg_name} 看起来是**没替换的模板占位符**：{shown}"
            f"（原值：{value[:80]}）。请把占位符换成真实路径后重试"
            "（例如 /home/zhangsan/nginx/uploads 或 runtime/workspace/xxx）；"
            "不要重复调用同一个工具。"
        )
        self.tool_name = tool_name
        self.arg_name = arg_name
        self.found = found


def check_tool_args(tool_name: str, args: dict) -> None:
    """工具层入口：**路径类参数**里出现占位符就抛 `PlaceholderArgError`。

    只查名字像路径的参数（`file_path` / `dir_path` / `vm_dest_dir` / …）——
    正文类参数（`text` / `content` / `command`）里的尖括号是内容，**不查**
    （写一个模板文件是正当需求，例如让模型生成 `<h1>标题</h1>`）。
    """
    for name, value in (args or {}).items():
        key = str(name).lower()
        if not any(hint in key for hint in PATH_ARG_HINTS):
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            found = find_placeholders(item) if isinstance(item, str) else []
            if found:
                raise PlaceholderArgError(tool_name, str(name), str(item), found)


__all__ = [
    "PATH_ARG_HINTS",
    "PlaceholderArgError",
    "check_tool_args",
    "clarify_reply",
    "find_placeholders",
    "is_explicitly_literal",
    "looks_like_placeholder",
]
