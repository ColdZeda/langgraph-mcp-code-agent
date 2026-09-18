"""Code Agent 入口 — CLI 参数解析。"""

import argparse
import uuid

from app.code_agent.agent.code_agent import main as run_agent_main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="轻量化编程智能体 (Devin-like Code Agent)")
    parser.add_argument(
        "--thread-id",
        default=str(uuid.uuid4())[:8],
        help="会话 ID（默认自动生成 UUID 前8位）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="开启 DEBUG 日志级别（等价于 LOG_LEVEL=DEBUG）",
    )
    args = parser.parse_args()

    run_agent_main(thread_id=args.thread_id, debug=args.debug)
