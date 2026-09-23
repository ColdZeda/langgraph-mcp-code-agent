"""`evals/merge_runs.py`（逐题分片 → 合并成一轮）的测试。

它守的是一个**真实踩过的坑**：一轮 30 题原本只在整轮结束时写一次 JSON，
于是 E016 把评估进程杀掉之后，前 15 题的成绩**全部丢失**。
改成逐题分片跑之后数据是分批落盘的，这个脚本负责合回去 ——
所以它必须：① 口径与整轮跑一致（复用 `runner._aggregate`）；
② **缺题时拒绝写出**（"残轮"和"整轮"文件名一样，靠人记靠不住）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import merge_runs as M  # noqa: E402
from evals import verifiers as V  # noqa: E402


def _record(tid: str, *, passed: bool = True, mode: str = "single") -> dict:
    return {
        "id": tid,
        "dimension": "task_completion",
        "dimensions": ["task_completion"],
        "mode": mode,
        "status": "completed",
        "unavailable": False,
        "passed": passed,
        "partial": False,
        "score": 1.0 if passed else 0.0,
        "token_usage": 100,
        "elapsed_ms": 1000.0,
        "tool_calls": 2,
        "step_count": 3,
        "tier_summary": {
            t: {"total": 1, "passed": int(passed), "failed": int(not passed), "skipped": 0}
            for t in V.TIERS
        },
    }


def _shard(runs_dir: Path, run_id: str, record: dict, *, mode: str = "single") -> Path:
    path = runs_dir / f"{run_id}.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "mode": mode,
                "started_at": "2026-09-24T00:00:00",
                "wall_sec": 12.5,
                "env": {"chunks": 35},
                "models": {"executor": "m"},
                "totals": {},
                "by_dimension": {},
                "tasks": [record],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def runs_dir(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setattr(M, "RUNS_DIR", d)
    return d


def test_merges_shards_and_reuses_runner_aggregation(runs_dir, monkeypatch, tmp_path):
    """两道题的分片合起来 → 通过率 / token / 耗时都按 runner 的口径算。"""
    _shard(runs_dir, "v3-single-E001", _record("E001"))
    _shard(runs_dir, "v3-single-E002", _record("E002", passed=False))
    out = tmp_path / "v3-single.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merge_runs",
            "--prefix",
            "v3-single",
            "--mode",
            "single",
            "--allow-partial",
            "--out",
            str(out),
        ],
    )

    assert M.main() == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["run_id"] == "v3-single"
    assert [t["id"] for t in payload["tasks"]] == ["E001", "E002"]
    assert payload["totals"]["tasks"] == 2
    assert payload["totals"]["passed"] == 1
    assert payload["totals"]["pass_rate"] == 0.5
    assert payload["totals"]["tokens"] == 200
    assert payload["totals"]["elapsed_sec"] == 2.0
    # 合并来源必须写清楚 —— 别让读报告的人以为这是"一次进程跑完的"
    assert payload["merged_from"]["shards"] == 2
    assert payload["merged_from"]["partial"] is True


def test_refuses_to_write_a_partial_round(runs_dir, monkeypatch, tmp_path, capsys):
    """**缺题就拒绝**：残轮和整轮文件名一样，不能靠人记。"""
    _shard(runs_dir, "v3-single-E001", _record("E001"))
    out = tmp_path / "v3-single.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["merge_runs", "--prefix", "v3-single", "--mode", "single", "--out", str(out)],
    )

    assert M.main() == 1
    assert "拒绝写出不完整的一轮" in capsys.readouterr().out
    assert not out.exists(), "拒绝时不该留下半成品"


def test_mode_mismatch_is_rejected(runs_dir, monkeypatch, tmp_path):
    _shard(runs_dir, "v3-single-E001", _record("E001", mode="multi"), mode="multi")
    monkeypatch.setattr(
        sys,
        "argv",
        ["merge_runs", "--prefix", "v3-single", "--mode", "single", "--allow-partial"],
    )

    assert M.main() == 1


def test_duplicate_shards_keep_the_newest(runs_dir, monkeypatch, tmp_path, capsys):
    """同一题跑了两遍 → 取最新那份，并在输出里报出来。"""
    import os
    import time

    now = time.time()
    older = _shard(runs_dir, "v3-single-E001-aaa", _record("E001", passed=False))
    newer = _shard(runs_dir, "v3-single-E001-bbb", _record("E001", passed=True))
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))

    out = tmp_path / "v3-single.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merge_runs",
            "--prefix",
            "v3-single",
            "--mode",
            "single",
            "--allow-partial",
            "--out",
            str(out),
        ],
    )

    assert M.main() == 0
    assert "已按文件修改时间取最新" in capsys.readouterr().out
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["totals"]["passed"] == 1, "应该取到新的那份"
    assert len(payload["tasks"]) == 1, "同一题只保留一份"


def test_no_shards_returns_error(runs_dir, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["merge_runs", "--prefix", "nope", "--mode", "single"])
    assert M.main() == 1
