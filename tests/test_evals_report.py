"""阶段 6 · 报告生成器的守卫测试。

守的是**报告不能骗人**这件事：

| 风险 | 对应测试 |
|---|---|
| 百分比差异算错（0.5→0.75 写成「+0.2」而不是「+25.0 个百分点」） | `test_pct_delta_*` |
| 缺数据时被写成 0 / 空表（看起来完整、实际是编的） | `test_missing_input_*` |
| 只有一轮却算出"多 Agent 提升 N 个百分点" | `test_star_*` |
| 两轮题目集合不一致还照样相减 | `test_mismatched_task_sets_*` |
| 环境不可用 / 超时 / 部分分被藏起来不说 | `test_limitations_*` |
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import report as R  # noqa: E402


@pytest.fixture
def single():
    return R._fake_run("fake-single", mode="single", pass_rate=0.5, tokens=40_000)


@pytest.fixture
def multi():
    return R._fake_run("fake-multi", mode="multi", pass_rate=0.75, tokens=61_000)


# ═══════════════════════════════════════════════════════════════════
# 数字格式（最容易出错、也最丢人的一块）
# ═══════════════════════════════════════════════════════════════════


def test_pct_delta_is_percentage_points():
    """0.5 → 0.75 是 **+25.0 个百分点**，不是 +0.2。

    第一版就是直接相减，写成了「+0.2 个百分点」——这种错会一路写进简历。
    """
    assert R.delta_cell(0.5, 0.75, "pct") == "+25.0 个百分点"
    assert R.delta_cell(0.75, 0.5, "pct") == "-25.0 个百分点"
    assert R.delta_cell(1.0, 1.0, "pct") == "+0.0 个百分点"


def test_score_and_count_delta_format():
    assert R.delta_cell(0.5, 0.75, "score") == "+0.250"
    assert R.delta_cell(40_000, 61_000, "count") == "+21,000"
    assert R.delta_cell(96.0, 120.5, "sec") == "+24.5"


def test_delta_of_missing_data_is_dash():
    """缺一侧数据就写「—」，**不许**把 None 当 0 算出一个假差值。"""
    for kind in ("pct", "score", "sec", "count"):
        assert R.delta_cell(None, 1.0, kind) == "—"
        assert R.delta_cell(1.0, None, kind) == "—"


def test_cell_never_renders_missing_as_zero():
    for kind in ("pct", "score", "sec", "count"):
        assert R.cell(None, kind) == "未提供"
    assert R.cell(0.0, "pct") == "0.0%", "真的是 0 才画 0（与「未提供」区分开）"


def test_numbers_use_thousands_separator():
    assert R.cell(123_456, "count") == "123,456"
    assert R.cell(2049, "count") == "2,049"


# ═══════════════════════════════════════════════════════════════════
# 章节完整性
# ═══════════════════════════════════════════════════════════════════


def test_report_contains_all_sections(single, multi):
    text = R.build_report(single, multi, R._fake_rag(), generated_at="2026-01-01 00:00:00")
    for section in (
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
    ):
        assert section in text, f"报告缺少章节：{section}"


def test_report_shows_wsl_cleanup_evidence(single):
    """清理 WSL 上传目录这件事必须在报告里看得见（它是订正 #33 的修复证据）。"""
    text = R.build_report(single, None, None, generated_at="t")
    assert "清 WSL 上传目录" in text
    assert "/home/x/uploads" in text
    assert "删了 1 个，复核剩余 0" in text, "要写出「删了几个 + 复核后剩几个」，不是只说一句清了"


def test_wsl_cleanup_skipped_is_visible(single):
    """跳过清理必须写出来 —— 旧代码里它和"清干净了"都返回 0，看不出区别。"""
    single["env"]["wsl_uploads"] = {"attempted": False, "skipped": True, "path": "", "removed": 0}
    text = R.build_report(single, None, None, generated_at="t")
    assert "跳过了" in text


def test_legacy_int_format_is_not_dressed_up_as_success(single):
    """旧格式的 `wsl_uploads_cleaned: 0` **不能**被写成"清理了 0 处"那种像成功的句子。

    那正是订正 #33 那个 bug 的显示层版本：0 既可能是"本来就干净"，
    也可能是"一整轮都没清"，报告必须说"无法确认"。
    """
    del single["env"]["wsl_uploads"]
    single["env"]["wsl_uploads_cleaned"] = 0
    text = R.build_report(single, None, None, generated_at="t")
    assert "旧格式记录" in text
    assert "无法区分" in text
    assert "清理了 0 处" not in text


def test_report_is_deterministic_for_a_given_timestamp(single, multi):
    a = R.build_report(single, multi, None, generated_at="2026-01-01 00:00:00")
    b = R.build_report(single, multi, None, generated_at="2026-01-01 00:00:00")
    assert a == b


# ═══════════════════════════════════════════════════════════════════
# 缺数据时不瞎编
# ═══════════════════════════════════════════════════════════════════


def test_missing_input_keeps_section_and_says_undelivered(single):
    text = R.build_report(single, None, None, generated_at="t")
    assert "## 七、RAG 检索消融对照" in text, "缺 RAG 数据也要保留章节"
    assert "未提供" in text
    assert "未提供任何一轮结果" not in text, "至少给了一轮，不该说「任何一轮都没有」"


def test_empty_report_is_honest():
    text = R.build_report(None, None, None, generated_at="t")
    assert "未提供任何一轮结果" in text
    assert "未提供" in text
    # 一个数字都不许编出来
    assert "0.0%" not in text


def test_star_requires_both_rounds(single):
    """只有一轮就说「多 Agent 提升 N 个百分点」是编的 → 本节必须拒绝生成。"""
    text = R.build_report(single, None, None, generated_at="t")
    assert "本节不生成" in text
    assert "为什么不做单轮的 STAR" in text


def test_star_appears_with_two_rounds(single, multi):
    text = R.build_report(single, multi, None, generated_at="t")
    assert "**S（情境）**" in text
    assert "**T（任务）**" in text
    assert "**A（行动）**" in text
    assert "**R（结果）**" in text
    assert "+25.0 个百分点" in text
    assert "样本量" in text, "必须提醒连样本量一起说"


def test_mismatched_task_sets_are_flagged(single, multi):
    """两轮题目集合不一致 → 报告要点名，并说明对比只在交集上成立。"""
    multi["tasks"] = multi["tasks"][:3]
    multi["totals"]["tasks"] = 3
    text = R.build_report(single, multi, None, generated_at="t")
    assert "两轮的题目集合不一致" in text
    assert "共有的" in text


def test_limitations_disclose_environment_gaps(single, multi):
    single["tasks"][0]["unavailable"] = True
    single["totals"]["unavailable"] = 1
    single["tasks"][1]["status"] = "timeout"
    single["totals"]["timeout"] = 1
    single["totals"]["partial"] = 1
    single["tasks"][2]["partial"] = True
    text = R.build_report(single, multi, None, generated_at="t")
    assert "`unavailable`（环境不可用）" in text or "属于 `unavailable`" in text
    assert "超时" in text
    assert "部分分" in text


def test_limitations_mention_no_path_sandbox_and_wsl_not_a_sandbox(single):
    text = R.build_report(single, None, None, generated_at="t")
    assert "路径级" in text
    assert "不是安全沙箱" in text


# ═══════════════════════════════════════════════════════════════════
# 自测入口本身
# ═══════════════════════════════════════════════════════════════════


def test_selftest_passes(capsys):
    assert R._selftest() == 0
    out = capsys.readouterr().out
    assert "自测通过" in out
