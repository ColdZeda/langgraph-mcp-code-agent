"""阶段 6 · 跑前预检的守卫测试（**不连网、不起容器**，全部打桩）。

守的是"预检自己别说谎"这件事 —— 一个会说谎的预检比没有预检更糟：
它给人"我检查过了"的错觉，然后把废数据放进正式结果里。

| 风险 | 对应测试 |
|---|---|
| 探针不带鉴权头，把好 key 报成失效 | `test_key_probe_sends_bearer_header` |
| 端口被占却报空闲（E016 会假失败） | `test_port_check_detects_occupied_port` |
| run-id 重名（第二轮会读到第一轮 checkpoint）却放行 | `test_run_id_collision_is_a_blocker` |
| 有阻塞项却报"可以开跑" | `test_ready_flag_follows_blockers` |
"""

import json
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import preflight as P  # noqa: E402

# ═══════════════════════════════════════════════════════════════════
# 模型 key 探针：必须带 Authorization 头
# ═══════════════════════════════════════════════════════════════════


class _FakeResponse:
    def __init__(self, payload: str, status: int = 200):
        self._payload = payload.encode("utf-8")
        self.status = status

    def read(self, _n: int = -1) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_key_probe_sends_bearer_header(monkeypatch):
    """**回归守卫**：第一版拿不带头的 `http_reachable` 去问 `/models`，
    结果把一把好 key 报成 401。探针必须自己带上 `Authorization: Bearer …`。
    """
    seen: dict = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return _FakeResponse(json.dumps({"data": [{"id": "deepseek-flash"}]}))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(P, "MODEL_API_KEY", "sk-test-1234")
    monkeypatch.setattr(P, "MODEL_BASE_URL", "https://api.example.com")
    monkeypatch.setattr(P, "MODEL_NAME", "deepseek-flash")

    result = P.check_model_key()
    assert result["status"] == P.OK
    assert seen["url"] == "https://api.example.com/models"
    assert seen["auth"] == "Bearer sk-test-1234", (
        "不带鉴权头就必然吃 401，那是探针的错不是 key 的错"
    )
    assert "1234" in result["detail"], "只回显尾号，不打印整把 key"


def test_key_probe_warns_when_model_name_is_gone(monkeypatch):
    """key 有效但模型名不在服务端列表里 → 警告（`deepseek-v4-flash` 下线就是这种坑）。"""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout=None: _FakeResponse(
            json.dumps({"data": [{"id": "deepseek-flash"}]})
        ),
    )
    monkeypatch.setattr(P, "MODEL_API_KEY", "sk-test-1234")
    monkeypatch.setattr(P, "MODEL_BASE_URL", "https://api.example.com")
    monkeypatch.setattr(P, "MODEL_NAME", "deepseek-v4-flash")

    result = P.check_model_key()
    assert result["status"] == P.WARN
    assert "deepseek-v4-flash" in result["detail"]


def test_missing_key_is_a_blocker(monkeypatch):
    monkeypatch.setattr(P, "MODEL_API_KEY", "")
    result = P.check_model_key()
    assert result["status"] == P.FAIL
    assert "MODEL_API_KEY" in result["detail"]


def test_key_probe_failure_is_a_blocker(monkeypatch):
    def boom(request, timeout=None):
        raise OSError("网络不可达")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(P, "MODEL_API_KEY", "sk-test-1234")
    result = P.check_model_key()
    assert result["status"] == P.FAIL
    assert "连不上" in result["detail"]


# ═══════════════════════════════════════════════════════════════════
# 端口 / run-id / 工作目录
# ═══════════════════════════════════════════════════════════════════


def test_port_check_passes_when_free():
    assert P.check_http_port(0)["status"] == P.OK, "端口 0 = 让系统随便给一个，必然空闲"


def test_port_check_detects_occupied_port():
    """E016 要起 uvicorn；端口被占 → 与模型能力无关的假失败，必须挡在前面。"""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        result = P.check_http_port(port)
        assert result["status"] == P.FAIL
        assert "绑定失败" in result["detail"]
    finally:
        holder.close()


def test_run_id_collision_is_a_blocker(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "RUNS_DIR", tmp_path)
    (tmp_path / "v3-single.json").write_text("{}", encoding="utf-8")

    collision = P.check_run_id("v3-single")
    assert collision["status"] == P.FAIL, "重名会让第二轮读到第一轮的 checkpoint，必须拦住"

    fresh = P.check_run_id("v3-single-2")
    assert fresh["status"] == P.OK

    missing = P.check_run_id(None)
    assert missing["status"] == P.WARN, "没给 run-id 就提醒一下，不算阻塞"


def test_workspace_probe_cleans_up_after_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "WORKSPACE_DIR", tmp_path / "ws")
    result = P.check_workspace_writable()
    assert result["status"] == P.OK
    assert (tmp_path / "ws").exists()
    assert list((tmp_path / "ws").iterdir()) == [], "探针文件必须被删掉，别给评估留垃圾"


def test_task_suite_check_passes_on_real_suite():
    result = P.check_task_suite()
    assert result["status"] == P.OK
    assert "30 题" in result["detail"]


def test_task_suite_check_flags_missing_state_assertion(monkeypatch):
    from evals import runner as R

    monkeypatch.setattr(
        R,
        "task_inventory",
        lambda _tasks: {
            "total": 1,
            "dimensions": {},
            "kinds": {},
            "without_state_assertion": ["E999"],
        },
    )
    result = P.check_task_suite()
    assert result["status"] == P.FAIL
    assert "E999" in result["detail"]


# ═══════════════════════════════════════════════════════════════════
# 汇总口径
# ═══════════════════════════════════════════════════════════════════


def _stub_all(monkeypatch, status: str, *, record: list[str] | None = None):
    """把所有检查换成打桩（可顺带记录"谁被调用了"）。"""

    def make(name: str):
        def stub(*_args, **_kwargs):
            if record is not None:
                record.append(name)
            return P._check(name, status, "打桩")

        return stub

    for name in (
        "check_model_key",
        "check_containers",
        "check_mysql",
        "check_wsl",
        "check_wsl_uploads",
        "check_http_port",
        "check_knowledge",
        "check_run_id",
        "check_task_suite",
        "check_workspace_writable",
    ):
        monkeypatch.setattr(P, name, make(name))


def test_ready_flag_follows_blockers(monkeypatch):
    _stub_all(monkeypatch, P.OK)
    payload = P.run_all_checks(run_id="r1")
    assert payload["ready"] is True
    assert payload["blockers"] == 0

    _stub_all(monkeypatch, P.WARN)
    payload = P.run_all_checks(run_id="r1")
    assert payload["ready"] is True, "警告不阻塞（例如 reranker 降级）"
    assert payload["warnings"] > 0

    _stub_all(monkeypatch, P.FAIL)
    payload = P.run_all_checks(run_id="r1")
    assert payload["ready"] is False
    assert payload["blockers"] > 0, "有阻塞项就必须 ready=False"


def test_skip_rag_drops_the_knowledge_check(monkeypatch):
    """`--skip-rag` 必须**真的不调用**知识库检查（而不是调用了但把结果丢掉）。"""
    called: list[str] = []
    _stub_all(monkeypatch, P.OK, record=called)

    P.run_all_checks(skip_rag=True)
    assert "check_knowledge" not in called

    called.clear()
    P.run_all_checks(skip_rag=False)
    assert "check_knowledge" in called


def test_all_checks_are_actually_wired_into_run_all(monkeypatch):
    """防"写了检查函数却忘了挂上去" —— 预检漏一项就等于没查。"""
    called: list[str] = []
    _stub_all(monkeypatch, P.OK, record=called)
    P.run_all_checks(run_id="r1")

    assert set(called) == {
        "check_model_key",
        "check_containers",
        "check_mysql",
        "check_wsl",
        "check_wsl_uploads",
        "check_http_port",
        "check_knowledge",
        "check_run_id",
        "check_task_suite",
        "check_workspace_writable",
    }


def test_check_records_carry_a_fix_hint_when_not_ok():
    """非 OK 的检查必须给"怎么办" —— 否则人只看到红叉不知道下一步。"""
    record = P._check("x", P.FAIL, "坏了", fix="重启容器")
    assert record["fix"] == "重启容器"
    assert record["status"] in (P.OK, P.WARN, P.FAIL)


@pytest.mark.parametrize("status", [P.OK, P.WARN, P.FAIL])
def test_markers_exist_for_every_status(status):
    assert status in P.MARK
