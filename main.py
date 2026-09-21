"""Code Agent 入口 — CLI 参数解析。"""

import argparse
import uuid

from app.code_agent.agent.code_agent import main as run_agent_main
from app.code_agent.config import THREAD_ID

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="轻量化编程智能体 (Devin-like Code Agent)")
    parser.add_argument(
        "--thread-id",
        default=THREAD_ID,
        help=(
            "会话 ID（默认取 .env 的 CODE_AGENT_THREAD_ID，默认 'default'）。"
            "同一个 ID 可跨重启继续对话"
        ),
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="开一个新会话（生成随机 ID 并打印；之后可用 --thread-id <id> 回到该会话）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="开启 DEBUG 日志级别（等价于 LOG_LEVEL=DEBUG）",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "single", "multi"],
        default="auto",
        help=(
            "执行模式：auto=按任务复杂度自动选（默认）；"
            "single=只用 Executor（快，适合查询类）；multi=完整 Planner→Executor→Verifier"
        ),
    )
    parser.add_argument(
        "--permission",
        choices=["readonly", "confirm", "open"],
        default=None,
        help=(
            "权限模式（阶段 5）：readonly=只读（写/执行类工具直接拒绝，不弹框）；"
            "confirm=需确认（默认，写/执行类先问你，高危的显示影响面）；"
            "open=放开（不再逐次确认，但**危险命令黑名单仍然生效**）。"
            "默认取 .env 的 CODE_AGENT_PERMISSION_MODE（不设就是 confirm）"
        ),
    )
    args = parser.parse_args()

    thread_id = str(uuid.uuid4())[:8] if args.new_session else args.thread_id
    if args.new_session:
        print(f"已开启新会话，thread_id = {thread_id}")
        print(f"下次回到该会话：uv run python main.py --thread-id {thread_id}")

    print(f"执行模式：{args.mode}")
    run_agent_main(
        thread_id=thread_id,
        debug=args.debug,
        mode=args.mode,
        permission=args.permission,
    )
