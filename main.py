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
    args = parser.parse_args()

    thread_id = str(uuid.uuid4())[:8] if args.new_session else args.thread_id
    if args.new_session:
        print(f"已开启新会话，thread_id = {thread_id}")
        print(f"下次回到该会话：uv run python main.py --thread-id {thread_id}")

    run_agent_main(thread_id=thread_id, debug=args.debug)
