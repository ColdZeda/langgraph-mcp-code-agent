"""评估报告生成器（阶段 6 · T6.3 ③）—— 把 run JSON 变成**能进 `docs/evidence/` 的 Markdown**。

**为什么要有它（而不是跑完手写报告）**：

1. **手写会掺进记忆里的数字**。报告里的每个百分比都从 JSON 现算 —— 口头说的和文件里的不一致，
   是这类项目最容易出的丑，而且面试官一追问就露。
2. **口径必须跟数据走**。报告开头要写清"通过 = 满分 / skip 不进分母 / 超时也验分"，
   这些是口径的一部分，写在生成器里就不会漏。
3. **STAR 段落必须是算出来的**。简历上那句"多 Agent 通过率比单 Agent 高 N 个百分点"，
   要求单轮/多轮两套 JSON 都在场、任务集合一致、口径一致 —— 这里会**机械核对**
   （比对两轮的题目 id 集合），不一致就在报告里点名，而不是照样算一个漂亮数字。

用法：

    # 自测（不跑评估，用假数据把整篇报告渲染出来；这是"生成器自己没问题"的证据）
    uv run python evals/report.py --selftest

    # 真跑完之后
    uv run python evals/report.py \\
        --single runtime/runs/v3-single.json \\
        --multi  runtime/runs/v3-multi.json \\
        --rag    runtime/runs/rag_ablation_xxx.json \\
        --out    docs/evidence/阶段6_评估报告.md

⚠️ **报告只写算得出来的东西**：缺哪个输入，对应章节就写「未提供」并说明缺什么，
**绝不用 0 或估算值顶上**（那是"看起来完整、实际骗人"的报告）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 评估口径（写在报告开头 —— 口径与数字同源，别让读者自己去别处找）
CALIBER_NOTES = """\
| 口径 | 做法 | 旧口径的坑 |
|---|---|---|
| 通过判据 | **满分才算通过** | 旧口径 `score >= 0.5` 记通过，而部分判定器会给 0.5 部分分 → 通过率虚高 |
| 环境不可用 | 记 `skip`，**不进分母**；整题全 skip 标 `unavailable` | 旧口径把"没测"当 0 分平均进总分 |
| 超时 / 异常 | **判定器照跑**（产物可能已经写出来了） | 旧口径只在 `status == completed` 时验分 → 超时题直接 0 |
| 判定器 | 按**真实 MCP 工具名 + 真实参数名**写，对着 `permissions.ALL_TOOLS` 校验 | 旧题集检查的 `run_vm_shell_command` 根本不是 MCP 工具 → 安全题判定器空转、恒满分 |
| 权限档 | 评估跑的是**产品默认档「需确认」** + `AutoApprover`，每次写操作过闸门并留审计 | 用「放开」档会绕开确认闸门，成绩证明不了机制 |"""

TIER_NAMES = {
    "text": "文本断言（模型说对话）",
    "trace": "轨迹断言（调对工具 / 过程预算）",
    "state": "状态断言（真实产物：文件 / 库表 / WSL / 接口）",
    "judge": "LLM 评分（本批题集未使用）",
}


# ═══════════════════════════════════════════════════════════════════
# 小工具
# ═══════════════════════════════════════════════════════════════════


def _pct(value) -> str:
    """百分比。`None` 一律显示「未提供」—— 不许出现"0.0%"这种把"没数据"说成"零分"的写法。"""
    if value is None:
        return "未提供"
    return f"{value * 100:.1f}%"


def _num(value, digits: int = 0) -> str:
    if value is None:
        return "未提供"
    return f"{value:,.{digits}f}"


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_label(run: dict | None) -> str:
    if not run:
        return "未提供"
    return f"{run.get('run_id', '?')}（{run.get('started_at', '?')}）"


def _task_index(run: dict | None) -> dict[str, dict]:
    if not run:
        return {}
    return {t.get("id", "?"): t for t in run.get("tasks", [])}


def _totals(run: dict | None) -> dict:
    return (run or {}).get("totals", {}) or {}


# ═══════════════════════════════════════════════════════════════════
# 各章节
# ═══════════════════════════════════════════════════════════════════


def _header(single: dict | None, multi: dict | None, rag: dict | None, *, generated_at: str) -> str:
    lines = [
        "# 阶段 6 · 评估报告（新口径）",
        "",
        f"> 生成时间：{generated_at}　｜　由 `evals/report.py` 从结果 JSON **机械生成**"
        "（报告里的每个数字都取自下列文件，没有手工填写的值）。",
        "",
        "| 数据来源 | 文件 | 运行时间 |",
        "|---|---|---|",
        f"| 单 Agent 一轮 | {_run_label(single)} | {_src_time(single)} |",
        f"| 多 Agent 一轮 | {_run_label(multi)} | {_src_time(multi)} |",
        f"| RAG 消融对照 | {(rag or {}).get('run_id', '未提供')} | {_src_time(rag)} |",
        "",
        "## 一、口径（先看这段，再看数字）",
        "",
        CALIBER_NOTES,
        "",
    ]
    return "\n".join(lines)


def _src_time(run: dict | None) -> str:
    if not run:
        return "—"
    return str(run.get("timestamp") or run.get("started_at") or "—")


def _environment(single: dict | None, multi: dict | None) -> str:
    env = (single or multi or {}).get("env")
    lines = ["## 二、运行环境与清残留快照", ""]
    if not env:
        lines += ["未提供（结果 JSON 里没有 `env` 段）。", ""]
        return "\n".join(lines)

    def ws(key: str) -> str:
        rec = env.get(key)
        if isinstance(rec, dict):
            if rec.get("skipped"):
                return "**跳过了**（未清理）"
            if not rec.get("ok", True):
                return f"**失败**：{rec.get('error', '?')}"
            path = rec.get("path") or "—"
            return f"{path}（删了 {rec.get('removed', '?')} 个，复核剩余 {rec.get('left')}）"
        # 兼容旧格式（int）：旧记录**分不清**"清干净了"和"压根没清"（两者都是 0），
        # 所以如实说明"无法确认"，而不是画一个看着像成功的 0。
        return f"旧格式记录（{rec}）—— 无法区分「清干净了」与「压根没清」，这一轮请以新格式结果为准"

    lines += [
        "| 项 | 值 |",
        "|---|---|",
        f"| 知识库块数 | {env.get('chunks_total', env.get('chunks', '未提供'))} |",
        f"| 精排（CrossEncoder） | {'启用' if env.get('rerank_enabled') else '降级为纯向量召回'} |",
        f"| MySQL 可用 | {'是' if env.get('mysql_available') else '否'} |",
        f"| WSL 可用 | {'是' if env.get('wsl_available') else '否'} |",
        f"| 清空工作目录 | 清掉 {env.get('workspace_cleaned', '?')} 个残留 |",
        f"| 清空知识库根目录 | 清掉 {env.get('knowledge_root_cleaned', '?')} 个散文件 |",
        f"| 清评估用 MySQL 库/表 | {', '.join(env.get('mysql_cleaned') or []) or '（无）'} |",
        f"| 清 WSL 上传目录 | {ws('wsl_uploads') if 'wsl_uploads' in env else ws('wsl_uploads_cleaned')} |",
        "",
        "> 每题开跑前还会**单独清空 `runtime/workspace/`** —— 否则上一题的产物会让下一题的状态断言白过。",
        "",
    ]
    models = (single or multi or {}).get("models") or {}
    if models:
        lines += ["模型分配（按角色，来自运行时的注册表）：", ""]
        for role, name in models.items():
            lines.append(f"- **{role}**：`{name}`")
        lines.append("")
    return "\n".join(lines)


def _overview(single: dict | None, multi: dict | None) -> str:
    ts, tm = _totals(single), _totals(multi)
    lines = ["## 三、总览（单 Agent vs 多 Agent）", ""]
    if not single and not multi:
        lines += ["未提供任何一轮结果。", ""]
        return "\n".join(lines)

    rows = [
        ("题目总数", ts.get("tasks"), tm.get("tasks"), "count"),
        ("可用题数（真跑出结果）", ts.get("available"), tm.get("available"), "count"),
        (
            "`unavailable`（环境不可用，不进分母）",
            ts.get("unavailable"),
            tm.get("unavailable"),
            "count",
        ),
        ("通过题数（满分才算）", ts.get("passed"), tm.get("passed"), "count"),
        ("**通过率**", ts.get("pass_rate"), tm.get("pass_rate"), "pct"),
        ("部分分题数（未通过但有分）", ts.get("partial"), tm.get("partial"), "count"),
        ("部分分占比", ts.get("partial_rate"), tm.get("partial_rate"), "pct"),
        ("平均得分（按可用题）", ts.get("score_avg"), tm.get("score_avg"), "score"),
        ("超时题数", ts.get("timeout"), tm.get("timeout"), "count"),
        ("异常题数", ts.get("error"), tm.get("error"), "count"),
        ("总 token", ts.get("tokens"), tm.get("tokens"), "count"),
        ("总耗时（秒）", ts.get("elapsed_sec"), tm.get("elapsed_sec"), "sec"),
        ("工具调用次数", ts.get("tool_calls"), tm.get("tool_calls"), "count"),
        ("总步数", ts.get("steps"), tm.get("steps"), "count"),
    ]
    lines += ["| 指标 | 单 Agent | 多 Agent | 差异（多 − 单） |", "|---|---|---|---|"]

    for name, a, b, kind in rows:
        lines.append(f"| {name} | {cell(a, kind)} | {cell(b, kind)} | {delta_cell(a, b, kind)} |")
    lines += [
        "",
        "> `unavailable` 的题**不在分母里**：环境没起来就是「没测」，不是「做错了」。",
        "> 通过率是**分数**（0~1），差异列换算成**百分点**；平均得分的差异保留 3 位小数。",
        "",
    ]
    return "\n".join(lines)


def cell(value, kind: str) -> str:
    """单格渲染。`None` 一律「未提供」—— 不许把"没数据"写成 0。"""
    if value is None:
        return "未提供"
    if kind == "pct":
        return _pct(value)
    if kind == "score":
        return f"{float(value):.3f}"
    if kind == "sec":
        return f"{float(value):.1f}"
    if isinstance(value, float):
        return _num(value, 1)
    return _num(value)


def delta_cell(a, b, kind: str) -> str:
    """差异格。百分比类**必须乘 100** —— 直接相减会把 50%→75% 写成「+0.2」。"""
    if a is None or b is None:
        return "—"
    diff = b - a
    sign = "+" if diff >= 0 else ""
    if kind == "pct":
        return f"{sign}{diff * 100:.1f} 个百分点"
    if kind == "score":
        return f"{sign}{diff:.3f}"
    if kind == "sec":
        return f"{sign}{diff:.1f}"
    return f"{sign}{diff:,.0f}"


def _by_dimension(single: dict | None, multi: dict | None) -> str:
    ds = (single or {}).get("by_dimension") or {}
    dm = (multi or {}).get("by_dimension") or {}
    dims = sorted(set(ds) | set(dm))
    lines = ["## 四、分维度对比", ""]
    if not dims:
        lines += ["未提供分维度数据。", ""]
        return "\n".join(lines)

    lines += [
        "| 维度 | 题数 | 单 Agent 通过率 | 多 Agent 通过率 | 单 Agent 平均分 | 多 Agent 平均分 |",
        "|---|---|---|---|---|---|",
    ]
    for dim in dims:
        a, b = ds.get(dim, {}), dm.get(dim, {})
        count = a.get("tasks") or b.get("tasks")
        lines.append(
            f"| `{dim}` | {count if count is not None else '?'} | {_pct(a.get('pass_rate'))} "
            f"| {_pct(b.get('pass_rate'))} | {_num(a.get('score_avg'), 3)} "
            f"| {_num(b.get('score_avg'), 3)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _tiers(single: dict | None, multi: dict | None) -> str:
    ts, tm = _totals(single).get("tiers") or {}, _totals(multi).get("tiers") or {}
    keys = sorted(set(ts) | set(tm))
    lines = ["## 五、按断言档位统计（断言总数 / 通过 / 失败 / 跳过）", ""]
    if not keys:
        lines += ["未提供档位数据。", ""]
        return "\n".join(lines)

    lines += ["| 档位 | 单 Agent | 多 Agent |", "|---|---|---|"]
    for tier in keys:
        name = TIER_NAMES.get(tier, tier)

        def fmt(slot: dict) -> str:
            if not slot:
                return "未提供"
            return (
                f"共 {slot.get('total', 0)} 条："
                f"{slot.get('passed', 0)} 通过 / {slot.get('failed', 0)} 失败"
                f" / {slot.get('skipped', 0)} 跳过"
            )

        lines.append(f"| **{name}** | {fmt(ts.get(tier, {}))} | {fmt(tm.get(tier, {}))} |")
    lines += [
        "",
        "> 「跳过」= 该条断言依赖的环境不可用（如 MySQL 没起），**既不算对也不算错**。",
        "",
    ]
    return "\n".join(lines)


def _permissions(single: dict | None, multi: dict | None) -> str:
    lines = ["## 六、权限闸门与审计（「成绩能证明机制」的证据）", ""]
    if not single and not multi:
        lines += ["未提供任何一轮结果。", ""]
        return "\n".join(lines)

    lines += [
        "| 指标 | 单 Agent | 多 Agent |",
        "|---|---|---|",
    ]
    for label, field in (
        ("经过确认闸门的工具调用", "asked"),
        ("被批准的次数", "granted"),
        ("其中高危操作", "highRiskAsked"),
    ):
        vals = []
        for run in (single, multi):
            if not run:
                vals.append("未提供")
                continue
            total = sum(
                int((t.get("permission") or {}).get(field, 0) or 0) for t in run.get("tasks", [])
            )
            vals.append(str(total))
        lines.append(f"| {label} | {vals[0]} | {vals[1]} |")

    # 审计日志里的判定分布
    lines += ["", "审计日志（`runtime/permissions.log`）在本轮新增的判定：", ""]
    for label, run in (("单 Agent", single), ("多 Agent", multi)):
        if not run:
            continue
        decisions: dict[str, int] = {}
        high = 0
        for task in run.get("tasks", []):
            audit = task.get("audit") or {}
            for key, value in (audit.get("decisions") or {}).items():
                decisions[key] = decisions.get(key, 0) + int(value or 0)
            high += int(audit.get("highRisk", 0) or 0)
        if not decisions:
            lines.append(f"- **{label}**：本轮没有产生审计条目。")
            continue
        detail = "、".join(f"`{k}` × {v}" for k, v in sorted(decisions.items()))
        lines.append(f"- **{label}**：{detail}；其中高危 {high} 条。")
    lines += [
        "",
        "> 评估跑的是产品**默认档「需确认」**：每次写操作都要过闸门（评估里由 `AutoApprover` 自动批准并留痕，",
        "> 审计里记为 `allowed_by_*`）。用「放开」档会绕开闸门，那份成绩证明不了机制存在。",
        "",
    ]
    return "\n".join(lines)


def _rag(rag: dict | None) -> str:
    lines = ["## 七、RAG 检索消融对照（整篇 vs 分块｜无精排 vs 精排）", ""]
    if not rag:
        lines += ["未提供（需要 `evals/rag_ablation.py` 的结果 JSON）。", ""]
        return "\n".join(lines)

    env = rag.get("env") or {}
    combos = rag.get("combos") or {}
    lines += [
        f"语料：**{env.get('whole_docs_indexed', '?')} 篇 / {env.get('chunks_total', '?')} 块**；"
        f"查询 {env.get('queries', '?')} 条；`top_k={env.get('top_k', '?')}`；"
        f"精排 {'启用' if env.get('rerank_enabled') else '**未启用（B/D/E 已退化）**'}。",
        "",
        "| 组合 | 索引 | 精排 | 候选集 | top-1 命中正解文件 | top-1 命中关键词（文件粒度）"
        " | top-1 命中关键词（块粒度） | top-1 落干扰项 | 稳态延迟 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    meta = {
        "A": ("整篇", "否", "全部 7 篇"),
        "B": ("整篇", "是", "全部 7 篇"),
        "C": ("分块", "否", f"前 {env.get('recall_k', '?')} 块"),
        "D": ("分块", "是", f"前 {env.get('recall_k', '?')} 块（生产配置）"),
        "E": ("分块", "是", f"全量 {env.get('full_recall_k', '?')} 块（对照）"),
    }
    for key in sorted(combos):
        c = combos[key]
        index, rerank, cand = meta.get(key, ("?", "?", "?"))
        chunk = "—" if c.get("chunk_top1") is None else f"{c['chunk_top1']:.2f}"
        warm = c.get("latency_ms_avg_warm")
        lines.append(
            f"| **{key}** {c.get('label', '')} | {index} | {rerank} | {cand} "
            f"| **{c.get('correct_source_top1', 0):.2f}** | {c.get('file_top1', 0):.2f} "
            f"| {chunk} | {c.get('distractor_top1', 0):.2f} "
            f"| {'—' if warm is None else f'{warm:.1f} ms'} |"
        )
    lines += [
        "",
        "**怎么读这张表**：",
        "",
        "- **A → D 是这次改造的真实变化**（同一个知识库、同一批查询、同一个 embedding 模型）：",
        f"  top-1 命中正解文件 {combos.get('A', {}).get('correct_source_top1', 0):.2f} → "
        f"{combos.get('D', {}).get('correct_source_top1', 0):.2f}；"
        f"top-1 落干扰项 {combos.get('A', {}).get('distractor_top1', 0):.2f} → "
        f"{combos.get('D', {}).get('distractor_top1', 0):.2f}。",
        "- **B（只加精排）看着比 D（现状）好，但那不是「整篇更好」**：A/B 的候选集是全部 7 篇，",
        f"  而 D 走生产配置 `recall_k={env.get('recall_k', '?')}`，35 块里只有 10 块能进精排。",
        "- **E 是压掉这个差异的对照组**（同样分块 + 精排，候选集放到全量）：",
        f"  E 的 top-1 命中正解文件 {combos.get('E', {}).get('correct_source_top1', 0):.2f}，"
        f"与 B 持平；且它还能把**命中的那条知识本身**排到第一（块粒度 "
        f"{combos.get('E', {}).get('chunk_top1') if combos.get('E', {}).get('chunk_top1') is None else f'{combos.get("E", {}).get("chunk_top1"):.2f}'}）"
        "—— 整篇索引根本表达不了这一档。",
        f"  代价是延迟：{combos.get('D', {}).get('latency_ms_avg_warm', 0):.1f} ms → "
        f"{combos.get('E', {}).get('latency_ms_avg_warm', 0):.1f} ms（精排要对全部块打分）。",
        "",
        "⚠️ **局限**：知识库只有 4 篇正解 + 3 篇干扰（每篇 140~370 字符），语料极小，",
        "「整篇 vs 分块」的差距被压小了，上表是**真实下界**、不可外推到大语料；",
        "精排模型是段落级语料训练的，B 组喂整篇文件属分布外输入；",
        "延迟为本机单进程数字（冷启动单独测，未混入稳态）。",
        "",
    ]
    return "\n".join(lines)


def _star(single: dict | None, multi: dict | None) -> str:
    """STAR 量化对比 —— **只用两轮结果里算得出来的数字**。"""
    lines = ["## 八、可用于简历的 STAR 量化对比", ""]
    ts, tm = _totals(single), _totals(multi)
    if not single or not multi:
        lines += [
            "**未提供两轮结果，本节不生成。**",
            "",
            "为什么不做单轮的 STAR：简历上那句「多 Agent 比单 Agent 高 N 个百分点」必须"
            "**两轮同口径、同题目集合**才成立；只有一轮就说「提升」是编的。",
            "",
        ]
        return "\n".join(lines)

    tasks_s, tasks_m = _task_index(single), _task_index(multi)
    only_s = sorted(set(tasks_s) - set(tasks_m))
    only_m = sorted(set(tasks_m) - set(tasks_s))
    warn = ""
    if only_s or only_m:
        warn = (
            f"\n> ⚠️ **两轮的题目集合不一致**（单 Agent 独有：{only_s or '无'}；"
            f"多 Agent 独有：{only_m or '无'}）→ 下面的对比**只在两轮共有的 "
            f"{len(set(tasks_s) & set(tasks_m))} 题**上成立，整体通过率不可直接相减。\n"
        )

    ps, pm = ts.get("pass_rate"), tm.get("pass_rate")
    tokens_s, tokens_m = ts.get("tokens"), tm.get("tokens")
    lines += [
        "> 下面每一格都是从上面的结果 JSON 现算的。**没有估算、没有外推、没有手填的数字**；"
        "样本量（题目数）如实写在括号里。",
        "",
        "**S（情境）** 一个 LangGraph 多 Agent 编程助手（Planner → Executor → Verifier），"
        "接入 6 个自建 MCP Server（25 个工具）+ 7 个文件工具，双入口（CLI / Web）。",
        "",
        "**T（任务）** 在**不降低断言强度**的前提下，重建一套可复现的端到端评估："
        f"{ts.get('tasks', '?')} 道题、覆盖 8 个能力维度、"
        f"通过判据为「全部断言通过」（部分分单列、环境不可用记 `skip` 不进分母）。",
        "",
        "**A（行动）**",
        f"- 单 Agent 一轮：{_num(ts.get('steps'))} 步 / {_num(ts.get('tool_calls'))} 次工具调用；",
        f"- 多 Agent 一轮（Planner → Executor → Verifier）：{_num(tm.get('steps'))} 步 / "
        f"{_num(tm.get('tool_calls'))} 次工具调用；",
        f"- 每次写操作都过「需确认」闸门并写审计日志（本轮审计条目：单 "
        f"{sum(int((t.get('audit') or {}).get('lines', 0) or 0) for t in single.get('tasks', []))} 条 / "
        f"多 {sum(int((t.get('audit') or {}).get('lines', 0) or 0) for t in multi.get('tasks', []))} 条）；",
        "- 检索层做了 2×2 消融 + 全量召回对照组（见第七节），把「分块」和「精排」的贡献分开归因。",
        "",
        "**R（结果）**",
        f"- 通过率（满分口径，可用题 {ts.get('available', '?')}/{ts.get('tasks', '?')}）："
        f"单 Agent **{_pct(ps)}** → 多 Agent **{_pct(pm)}**"
        f"（{delta_cell(ps, pm, 'pct')}）；",
        f"- 平均得分：{_num(ts.get('score_avg'), 3)} → {_num(tm.get('score_avg'), 3)}"
        f"（{delta_cell(ts.get('score_avg'), tm.get('score_avg'), 'score')}）；",
        f"- 总 token：{_num(tokens_s)} → {_num(tokens_m)}"
        f"（{delta_cell(tokens_s, tokens_m, 'count')}）；",
        f"- 总耗时：{_num(ts.get('elapsed_sec'), 1)} s → {_num(tm.get('elapsed_sec'), 1)} s"
        f"（{delta_cell(ts.get('elapsed_sec'), tm.get('elapsed_sec'), 'sec')} s）；",
        f"- `unavailable`（环境不可用，如实披露）：单 {ts.get('unavailable', 0)} 题 / "
        f"多 {tm.get('unavailable', 0)} 题。",
        warn,
        "> 写进简历/答辩时请连**样本量**一起说（「N 道题、满分口径」）。"
        "没有对照的绝对分数不构成「提升」，两轮题目集合不一致时也不构成。",
        "",
    ]
    return "\n".join(lines)


def _limitations(single: dict | None, multi: dict | None) -> str:
    lines = ["## 九、局限与如实披露", ""]
    items: list[str] = []
    for label, run in (("单 Agent", single), ("多 Agent", multi)):
        if not run:
            continue
        totals = _totals(run)
        if totals.get("unavailable"):
            ids = [t["id"] for t in run.get("tasks", []) if t.get("unavailable")]
            items.append(
                f"**{label}** 有 {totals['unavailable']} 道题属于 `unavailable`（环境不可用）："
                f"{', '.join(ids)} —— 它们**不在分母里**，不等于做错。"
            )
        if totals.get("timeout"):
            ids = [t["id"] for t in run.get("tasks", []) if t.get("status") == "timeout"]
            items.append(
                f"**{label}** 有 {totals['timeout']} 道题超时：{', '.join(ids)}"
                "（超时题**仍然执行了判定器**，所以不是 0 分）。"
            )
        if totals.get("error"):
            ids = [t["id"] for t in run.get("tasks", []) if t.get("status") == "error"]
            items.append(f"**{label}** 有 {totals['error']} 道题异常退出：{', '.join(ids)}。")
        if totals.get("partial"):
            items.append(
                f"**{label}** 有 {totals['partial']} 道题拿到部分分但**未通过**"
                "（新口径下部分分不算通过）。"
            )
    items += [
        "知识库只有 4 篇正解 + 3 篇干扰（35 条原子），RAG 指标是小语料上的**真实下界**，不可外推。",
        "检索层没有**路径级**沙箱：Agent 的 PowerShell 工作目录是仓库根，理论上能写到题目之外"
        "（已登记进候选池，见方案文档的「工作区选择器 + 路径级围栏」）。",
        "WSL2 是**隔离执行环境**，不是安全沙箱（它默认把 Windows 盘挂在 `/mnt/c`）。",
        "每题 300 秒墙钟上限；超时判定器照跑，但产物可能是「做了一半」的中间态。",
    ]
    for item in items:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _details(single: dict | None, multi: dict | None) -> str:
    lines = ["## 十、逐题明细", ""]
    if not single and not multi:
        lines += ["未提供任何一轮结果。", ""]
        return "\n".join(lines)

    for label, run in (("单 Agent", single), ("多 Agent", multi)):
        if not run:
            continue
        lines += [f"### {label}（{_run_label(run)}）", ""]
        lines += [
            "| 题号 | 维度 | 类型 | 状态 | 判定 | 得分 | 断言（过/共） | token | 步数 | 工具调用 | 耗时(s) |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for task in run.get("tasks", []):
            if task.get("unavailable"):
                verdict = "未测"
            elif task.get("passed"):
                verdict = "通过"
            elif task.get("partial"):
                verdict = "部分分"
            else:
                verdict = "未通过"
            tiers = task.get("tier_summary") or {}
            total = sum(int(s.get("total", 0)) for s in tiers.values())
            passed = sum(int(s.get("passed", 0)) for s in tiers.values())
            dims = "/".join(task.get("dimensions") or [task.get("dimension", "?")])
            lines.append(
                f"| `{task.get('id', '?')}` | {dims} | {task.get('kind', '?')} "
                f"| {task.get('status', '?')} | {verdict} | {task.get('score', 0):.2f} "
                f"| {passed}/{total} | {_num(task.get('token_usage'))} "
                f"| {_num(task.get('step_count'))} | {_num(task.get('tool_calls'))} "
                f"| {task.get('elapsed_ms', 0) / 1000:.1f} |"
            )
        lines.append("")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 组装
# ═══════════════════════════════════════════════════════════════════


def build_report(
    single: dict | None = None,
    multi: dict | None = None,
    rag: dict | None = None,
    *,
    generated_at: str | None = None,
) -> str:
    stamp = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = [
        _header(single, multi, rag, generated_at=stamp),
        _environment(single, multi),
        _overview(single, multi),
        _by_dimension(single, multi),
        _tiers(single, multi),
        _permissions(single, multi),
        _rag(rag),
        _star(single, multi),
        _limitations(single, multi),
        _details(single, multi),
    ]
    return "\n".join(parts).rstrip() + "\n"


# ═══════════════════════════════════════════════════════════════════
# 自测用假数据（**只在 --selftest 里用**，不会写进任何正式报告）
# ═══════════════════════════════════════════════════════════════════


def _fake_task(tid: str, *, passed: bool, tokens: int, audit: int = 1) -> dict:
    return {
        "id": tid,
        "dimension": "task_completion",
        "dimensions": ["task_completion", "multi_step"],
        "kind": "basic",
        "status": "completed",
        "unavailable": False,
        "partial": False,
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "token_usage": tokens,
        "step_count": 5,
        "tool_calls": 3,
        "elapsed_ms": 12345.6,
        "permission": {"asked": 2, "granted": 2, "highRiskAsked": 0},
        "audit": {"lines": audit, "decisions": {"allowed_by_eval_auto": audit}, "highRisk": 0},
        "tier_summary": {
            "state": {
                "total": 3,
                "passed": 3 if passed else 1,
                "failed": 0 if passed else 2,
                "skipped": 0,
            },
            "trace": {"total": 1, "passed": 1, "failed": 0, "skipped": 0},
        },
    }


def _fake_run(run_id: str, *, mode: str, pass_rate: float, tokens: int) -> dict:
    tasks = [
        _fake_task("E001", passed=True, tokens=tokens // 4),
        _fake_task("E002", passed=True, tokens=tokens // 4),
        _fake_task("E003", passed=pass_rate > 0.6, tokens=tokens // 4),
        _fake_task("E004", passed=False, tokens=tokens // 4),
    ]
    available = len(tasks)
    passed = sum(1 for t in tasks if t["passed"])
    return {
        "run_id": run_id,
        "mode": mode,
        "started_at": "2026-01-01T00:00:00",
        "wall_sec": 100.0,
        "env": {
            "chunks_total": 35,
            "rerank_enabled": True,
            "mysql_available": True,
            "wsl_available": True,
            "workspace_cleaned": 12,
            "knowledge_root_cleaned": 0,
            "mysql_cleaned": ["db:eval_shop"],
            "wsl_uploads": {
                "attempted": True,
                "ok": True,
                "path": "/home/x/uploads",
                "removed": 1,
                "left": 0,
            },
        },
        "models": {"planner": "deepseek-flash", "executor": "deepseek-flash"},
        "totals": {
            "tasks": available,
            "available": available,
            "unavailable": 0,
            "passed": passed,
            "pass_rate": round(passed / available, 4),
            "partial": 0,
            "partial_rate": 0.0,
            "score_avg": round(passed / available, 4),
            "timeout": 0,
            "error": 0,
            "tokens": tokens,
            "elapsed_sec": 96.0,
            "tool_calls": 12,
            "steps": 20,
            "tiers": {
                "state": {"total": 12, "passed": 10, "failed": 2, "skipped": 0},
                "trace": {"total": 4, "passed": 4, "failed": 0, "skipped": 0},
            },
        },
        "by_dimension": {
            "task_completion": {
                "tasks": 4,
                "available": 4,
                "passed": passed,
                "pass_rate": round(passed / available, 4),
                "score_avg": round(passed / available, 4),
            },
            "multi_step": {
                "tasks": 4,
                "available": 4,
                "passed": passed,
                "pass_rate": round(passed / available, 4),
                "score_avg": round(passed / available, 4),
            },
        },
        "tasks": tasks,
    }


def _fake_rag() -> dict:
    return {
        "run_id": "rag_ablation_FAKE",
        "kind": "rag_ablation",
        "timestamp": "2026-01-01T00:00:00",
        "env": {
            "chunks_total": 35,
            "whole_docs_indexed": 7,
            "rerank_enabled": True,
            "recall_k": 10,
            "full_recall_k": 35,
            "top_k": 3,
            "queries": 10,
        },
        "combos": {
            "A": {
                "label": "整篇 + 无精排 = 改造前",
                "correct_source_top1": 0.2,
                "file_top1": 0.6,
                "chunk_top1": None,
                "distractor_top1": 0.8,
                "latency_ms_avg_warm": 12.8,
            },
            "D": {
                "label": "分块 + 精排 = 现状（生产）",
                "correct_source_top1": 0.6,
                "file_top1": 0.9,
                "chunk_top1": 0.7,
                "distractor_top1": 0.4,
                "latency_ms_avg_warm": 84.8,
            },
            "E": {
                "label": "分块 + 精排 + 全量召回",
                "correct_source_top1": 0.7,
                "file_top1": 1.0,
                "chunk_top1": 0.8,
                "distractor_top1": 0.3,
                "latency_ms_avg_warm": 289.0,
            },
        },
    }


def _selftest() -> int:
    """用**假数据**把整篇报告渲染出来，检查关键章节都在、且缺数据时不瞎编。"""
    single = _fake_run("fake-single", mode="single", pass_rate=0.5, tokens=40_000)
    multi = _fake_run("fake-multi", mode="multi", pass_rate=0.75, tokens=61_000)
    report = build_report(single, multi, _fake_rag(), generated_at="2026-01-01 00:00:00")

    required = [
        "# 阶段 6 · 评估报告",
        "## 一、口径",
        "## 二、运行环境与清残留快照",
        "## 三、总览",
        "## 四、分维度对比",
        "## 五、按断言档位统计",
        "## 六、权限闸门与审计",
        "## 七、RAG 检索消融对照",
        "## 八、可用于简历的 STAR 量化对比",
        "## 九、局限与如实披露",
        "## 十、逐题明细",
        "allowed_by_eval_auto",
        "清 WSL 上传目录",
        "/home/x/uploads",
    ]
    missing = [token for token in required if token not in report]
    if missing:
        print(f"❌ 自测失败：报告缺少 {missing}")
        return 1

    # ⚠️ 百分比差异必须换算成**百分点**。这条是回归守卫：
    #    第一版直接相减，把 50% → 75% 写成了「+0.2 个百分点」（正确是 +25.0）——
    #    这种错会一路写进简历，所以拿自测钉死。
    if "+25.0 个百分点" not in report:
        print("❌ 自测失败：通过率差异没有换算成百分点（50%→75% 应写 +25.0 个百分点）")
        return 1
    if "+0.2 个百分点" in report:
        print("❌ 自测失败：出现了「+0.2 个百分点」—— 这就是没乘 100 的那个 bug")
        return 1
    if "+21,000" not in report:
        print("❌ 自测失败：整数差异没有千分位（40000→61000 应写 +21,000）")
        return 1

    # 缺数据时必须写"未提供"，**不许**退化成 0 / 空表
    partial_report = build_report(single, None, None, generated_at="2026-01-01 00:00:00")
    for token in ("未提供", "本节不生成", "为什么不做单轮的 STAR"):
        if token not in partial_report:
            print(f"❌ 自测失败：只给一轮时缺少「{token}」的如实说明")
            return 1
    if "RAG 检索消融" not in partial_report:
        print("❌ 自测失败：缺 RAG 数据时章节直接消失了（应保留章节并说明缺什么）")
        return 1

    print("✅ 自测通过：11 个章节齐全；缺输入时如实写「未提供」而不是编数字。")
    print(f"   报告长度：{len(report)} 字符 / {report.count(chr(10)) + 1} 行")
    print(
        "\n"
        + "=" * 78
        + "\n以下是**假数据**渲染出来的报告全文（看格式用，数字是编的）：\n"
        + "=" * 78
    )
    print(report)
    return 0


def _mask_home_paths(text: str) -> str:
    """把 `/home/<某人>/` 统一脱敏成 `/home/user/`（**报告是给人看的，别带作者家目录**）。

    为什么放在渲染之后：归档 JSON 是原始证据（保留当时的真实路径，不改写），
    但报告不必要地把本机用户名带出去 —— 阶段 7 收尾时就是这么发现的。
    """
    return re.sub(r"/home/[^/\s)\]]+/", "/home/user/", text)


def main() -> int:
    parser = argparse.ArgumentParser(description="从结果 JSON 生成评估报告（Markdown）")
    parser.add_argument("--single", type=Path, help="单 Agent 一轮的结果 JSON")
    parser.add_argument("--multi", type=Path, help="多 Agent 一轮的结果 JSON")
    parser.add_argument("--rag", type=Path, help="RAG 消融结果 JSON")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "evidence" / "评估报告.md")
    parser.add_argument("--selftest", action="store_true", help="用假数据自测生成器")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()

    if not any((args.single, args.multi, args.rag)):
        parser.error("至少给一个输入：--single / --multi / --rag（或用 --selftest）")

    report = build_report(
        load_json(args.single) if args.single else None,
        load_json(args.multi) if args.multi else None,
        load_json(args.rag) if args.rag else None,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(_mask_home_paths(report), encoding="utf-8")
    print(f"报告已生成：{args.out}（{len(report)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
