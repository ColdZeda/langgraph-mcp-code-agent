"""跑评估前的环境预检（阶段 6 · T6.3 ④）—— **把"跑了两小时才发现白跑"挡在前面**。

为什么要有它：一次全量两轮是 40~100 分钟 ×2 + token。而下面这些条件只要缺一条，
那一轮的**部分数据就是废的**（而且往往不会报错，只会安静地记成"未测"或"假过"）：

| 缺什么 | 后果（不是"跑不动"，是"数据废了却不报错"） |
|---|---|
| MySQL 容器没起 | 6 道 MySQL 题记 `unavailable`，不进分母 —— 报告里的样本量白白缩水 |
| WSL 不可用 / 上传目录清不掉 | E014 的产物断言要么 `skip`、要么被上一轮残留**蒙过（假阳性）** |
| `.env` 的 key 失效 | 全部 30 题一起失败；**CLI/evals 不读界面设置**，界面里那个 key 帮不上忙 |
| 8123 端口被占 | E016 要起 uvicorn，会直接失败 |
| 知识库不是 35 块 / 根目录有散文件 | 检索结果被上一轮自学习写进去的内容污染 |
| run-id 与上一轮重名 | thread_id 复用 → **第二轮读到第一轮的 checkpoint** |
| reranker 加载不出来 | 不阻塞，但 RAG 指标退化成纯向量召回（报告里必须披露） |

用法：

    uv run python evals/preflight.py                       # 跑全部检查
    uv run python evals/preflight.py --run-id v3-single    # 顺便检查 run-id 有没有重名
    uv run python evals/preflight.py --json runtime/runs/preflight.json

退出码：有**阻塞项**（❌）返回 1；只有警告（⚠️）返回 0。**不修任何东西** ——
该起容器就打印命令让人自己起（评估前的环境属于"人要知情"的事）。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.config import (  # noqa: E402
    KNOWLEDGE_DIR,
    MODEL_API_KEY,
    MODEL_BASE_URL,
    MODEL_NAME,
    RUNS_DIR,
    VM_UPLOADS_DIR,
    WORKSPACE_DIR,
)
from evals import verifiers as V  # noqa: E402

#: 期望的知识库块数（7 个文件 × 5 条原子）—— 与 `AGENTS.md` 的数字来源速查一致
EXPECTED_CHUNKS = 35

#: E016 会在这个端口起 uvicorn
HTTP_TEST_PORT = 8123

#: docker-compose 与 WSL 里那 4 个依赖容器
DEPENDENCY_CONTAINERS = ("agent-mysql", "searxng", "redis-stack-server", "my-nginx")

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "✅", WARN: "⚠️", FAIL: "❌"}


def _check(name: str, status: str, detail: str, *, fix: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


# ═══════════════════════════════════════════════════════════════════
# 各项检查
# ═══════════════════════════════════════════════════════════════════


def _authed_get(url: str, key: str, timeout: float) -> tuple[bool, int, str]:
    """带 `Authorization: Bearer <key>` 的 GET。

    ⚠️ **别用 `verifiers.http_reachable`**：它刻意不带任何头（判定器只打本机接口），
    拿它去请求模型 API 一定吃 401 —— 第一版就是这么写的，
    结果把一把**好 key** 报成了失效（查了半天才发现是探针自己的错）。
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 —— 地址来自配置
            return True, int(resp.status), resp.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return True, int(exc.code), exc.read(500).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return False, 0, f"{type(exc).__name__}: {exc}"


def check_model_key(timeout: float = 15.0) -> dict:
    """**最要紧的一条**：`.env` 的 key 必须真的能用。

    只打 `GET /models`（**不烧 token**）—— 验证的是"这把 key 能用"，
    不是"某个模型一定能答对题"（那是评估本身要测的）。
    顺带核对配置里的 `MODEL_NAME` **是否还在服务端的模型列表里**
    （2026-09-10 那次 `deepseek-v4-flash` 下线就是这种坑：名字不在了、请求却还能"暂时路由"过去）。
    """
    if not MODEL_API_KEY:
        return _check(
            ".env 的模型 key",
            FAIL,
            "MODEL_API_KEY 是空的",
            fix="把有效 key 写进 .env 的 MODEL_API_KEY（界面里填的 key 对评估无效）",
        )
    url = MODEL_BASE_URL.rstrip("/") + "/models"
    ok, code, body = _authed_get(url, MODEL_API_KEY, timeout)
    tail = MODEL_API_KEY[-4:]
    if not ok or code != 200:
        return _check(
            ".env 的模型 key",
            FAIL,
            f"GET {url} → {code if ok else '连不上'}（key 尾号 {tail}）：{body[:120]}",
            fix="换一把有效的 key 写进 .env；**改完必须重启进程**（load_dotenv 只在 import 时跑）",
        )

    try:
        listed = [m.get("id") for m in json.loads(body).get("data", [])]
    except ValueError:
        listed = []
    if listed and MODEL_NAME not in listed:
        return _check(
            ".env 的模型 key",
            WARN,
            f"key 有效（尾号 {tail}），但服务端的模型列表里没有 `{MODEL_NAME}`：{listed}",
            fix="核对 .env 的 MODEL_NAME 是否已被官方改名/下线（旧名可能被「暂时路由」，别靠它）",
        )
    return _check(
        ".env 的模型 key",
        OK,
        f"GET {url} → 200（key 尾号 {tail}）；模型名 `{MODEL_NAME}` 在服务端列表里",
    )


def check_containers(timeout: float = 20.0) -> dict:
    """4 个依赖容器是否在跑（`restart: unless-stopped` 不会自动拉起**手动停过**的容器）。"""
    import shutil
    import subprocess

    exe = shutil.which("docker")
    if not exe:
        return _check("依赖容器", WARN, "找不到 docker 命令，无法检查", fix="手动确认 4 个容器在跑")
    try:
        proc = subprocess.run(
            [exe, "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return _check("依赖容器", WARN, f"docker ps 调不动：{type(exc).__name__}: {exc}")
    running = {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}
    missing = [name for name in DEPENDENCY_CONTAINERS if name not in running]
    if not missing:
        return _check("依赖容器", OK, f"4 个都在跑：{'、'.join(DEPENDENCY_CONTAINERS)}")
    return _check(
        "依赖容器",
        FAIL,
        f"没在跑：{'、'.join(missing)}（当前在跑：{'、'.join(sorted(running)) or '无'}）",
        fix=r".\scripts\run\start-deps.ps1",
    )


def check_mysql() -> dict:
    ok, why = V.mysql_available()
    if ok:
        return _check("MySQL 端口可连", OK, "连接成功")
    return _check("MySQL 端口可连", FAIL, why, fix="先起容器；还不行就看 docker logs agent-mysql")


def check_wsl() -> dict:
    ok, why = V.wsl_available()
    if ok:
        return _check("WSL 可用", OK, "wsl.exe 有响应")
    return _check("WSL 可用", FAIL, why, fix="确认 WSL 发行版能起来：wsl -l -v")


def check_wsl_uploads() -> dict:
    """**顺手验证清理链路真的能跑通**（订正 #33：它曾经整轮都没被调用过）。"""
    from evals.runner import _clean_wsl_uploads

    record = _clean_wsl_uploads(VM_UPLOADS_DIR)
    if record.get("ok"):
        return _check(
            "WSL 上传目录可清",
            OK,
            f"{VM_UPLOADS_DIR}：删了 {record.get('removed')} 个，复核剩余 {record.get('left')}",
        )
    if record.get("skipped"):
        return _check(
            "WSL 上传目录可清", FAIL, "清理被跳过了（没传路径）—— 这正是订正 #33 那个 bug"
        )
    return _check(
        "WSL 上传目录可清",
        FAIL,
        f"清理失败：{record.get('error', '?')}",
        fix=f"手动确认 WSL 里 {VM_UPLOADS_DIR} 存在且可写",
    )


def check_http_port(port: int = HTTP_TEST_PORT) -> dict:
    """E016 要在这个端口起 uvicorn —— 被占了它就会失败（与模型能力无关的假失败）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        return _check(
            f"端口 {port} 空闲",
            FAIL,
            f"绑定失败：{exc}",
            fix=f"先腾出 {port}：netstat -ano | findstr :{port}",
        )
    finally:
        sock.close()
    return _check(f"端口 {port} 空闲", OK, f"127.0.0.1:{port} 可以绑定")


def check_knowledge() -> dict:
    """知识库块数与根目录散文件（后者是上一轮 Agent 自学习写进去的，会污染检索）。"""
    from app.code_agent.rag import store

    stray = (
        [p.name for p in KNOWLEDGE_DIR.iterdir() if p.is_file() and p.name != ".gitkeep"]
        if KNOWLEDGE_DIR.exists()
        else []
    )
    try:
        store.ensure_seeded()
        chunks = store.get_chunk_count()
        rerank = store.get_reranker() is not None
    except Exception as exc:  # noqa: BLE001
        return _check("知识库与精排", FAIL, f"{type(exc).__name__}: {exc}")

    detail = f"{chunks} 块（期望 {EXPECTED_CHUNKS}）；精排{'启用' if rerank else '未启用'}"
    if stray:
        return _check(
            "知识库与精排",
            WARN,
            f"{detail}；根目录有 {len(stray)} 个散文件：{'、'.join(stray[:5])}",
            fix="下一轮开跑时 prepare_run 会清掉它们（不影响正式数据，但别在跑之前手工往里加文件）",
        )
    if chunks != EXPECTED_CHUNKS:
        return _check(
            "知识库与精排",
            WARN,
            f"{detail} —— 与期望不符，检索指标会跟历史数字不可比",
            fix="确认 data/knowledge/ 没被改动（4 篇正解 + 3 篇干扰）",
        )
    if not rerank:
        return _check(
            "知识库与精排",
            WARN,
            f"{detail} —— reranker 没加载，RAG 指标会退化成纯向量召回",
            fix="检查 CODE_AGENT_RERANKER_PATH 指向的目录里有 config.json",
        )
    return _check("知识库与精排", OK, detail)


def check_run_id(run_id: str | None) -> dict:
    """run-id 重名 = 第二轮会读到第一轮的 checkpoint（两轮**必须**换 run-id）。"""
    if not run_id:
        return _check("run-id 未重复", WARN, "没给 --run-id，无法检查", fix="两轮各用一个新 run-id")
    existing = sorted(RUNS_DIR.glob(f"{run_id}.json")) if RUNS_DIR.exists() else []
    if existing:
        return _check(
            "run-id 未重复",
            FAIL,
            f"{run_id} 已经有结果文件：{existing[0].name}",
            fix="换一个没用过的 run-id（复用会让第二轮读到第一轮的 checkpoint）",
        )
    return _check("run-id 未重复", OK, f"{run_id} 是新的")


def check_task_suite() -> dict:
    """题集自检（30 题 / 每题都有状态断言）—— 题集被改坏时先在这里报出来。"""
    from evals.runner import task_inventory
    from evals.tasks import TASKS

    inventory = task_inventory(TASKS)
    if inventory["without_state_assertion"]:
        return _check(
            "题集自检",
            FAIL,
            f"有题缺状态断言：{inventory['without_state_assertion']}",
            fix="状态断言是「真产物」的唯一保证，缺了就是软尺子",
        )
    return _check(
        "题集自检",
        OK,
        f"{inventory['total']} 题；类型 {inventory['kinds']}；"
        f"{len(inventory['dimensions'])} 个维度覆盖",
    )


def check_workspace_writable() -> dict:
    """`runtime/workspace/` 可写（每题前都会被清空，写不了就全盘皆输）。"""
    probe = WORKSPACE_DIR / ".preflight_probe"
    try:
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return _check("工作目录可写", FAIL, f"{type(exc).__name__}: {exc}")
    return _check("工作目录可写", OK, str(WORKSPACE_DIR))


# ═══════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════


def run_all_checks(*, run_id: str | None = None, skip_rag: bool = False) -> dict:
    checks: list[dict] = []
    # 便宜的、与模型/容器无关的先跑（失败也快）
    checks.append(check_run_id(run_id))
    checks.append(check_task_suite())
    checks.append(check_workspace_writable())
    checks.append(check_http_port())
    checks.append(check_model_key())
    checks.append(check_containers())
    checks.append(check_mysql())
    checks.append(check_wsl())
    checks.append(check_wsl_uploads())
    if not skip_rag:
        checks.append(check_knowledge())

    blockers = [c for c in checks if c["status"] == FAIL]
    warnings = [c for c in checks if c["status"] == WARN]
    return {
        "kind": "preflight",
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "run_id": run_id or "",
        "model": {"name": MODEL_NAME, "base_url": MODEL_BASE_URL},
        "ready": not blockers,
        "blockers": len(blockers),
        "warnings": len(warnings),
        "checks": checks,
    }


def _print(payload: dict) -> None:
    print("=" * 78)
    print("阶段 6 · 跑评估前的环境预检")
    print("=" * 78)
    width = max(len(c["name"]) for c in payload["checks"]) + 2
    for c in payload["checks"]:
        print(f"{MARK[c['status']]} {c['name']:<{width}}{c['detail']}")
        if c["status"] != OK and c.get("fix"):
            print(f"   {'':<{width}}↳ 怎么办：{c['fix']}")
    print("-" * 78)
    if payload["ready"]:
        print(f"✅ 可以开跑（{payload['warnings']} 条警告，不阻塞）")
    else:
        print(f"❌ 有 {payload['blockers']} 条阻塞项 —— **先修再跑**，否则那一轮的部分数据是废的")
    print("提醒：两轮之间必须换 --run-id；跑完用 evals/report.py 生成报告。")


def main() -> int:
    parser = argparse.ArgumentParser(description="跑评估前的环境预检（不修任何东西）")
    parser.add_argument("--run-id", default=None, help="本次要用的 run-id（检查有没有重名）")
    parser.add_argument("--skip-rag", action="store_true", help="跳过知识库/精排检查（省十几秒）")
    parser.add_argument("--json", type=Path, default=None, help="把结果写进 JSON")
    args = parser.parse_args()

    started = time.perf_counter()
    payload = run_all_checks(run_id=args.run_id, skip_rag=args.skip_rag)
    payload["elapsed_sec"] = round(time.perf_counter() - started, 1)
    _print(payload)
    print(f"（预检耗时 {payload['elapsed_sec']}s）")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已保存: {args.json}")
    return 0 if payload["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
