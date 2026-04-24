#!/usr/bin/env python3
"""长连接模式启动入口（替代 HTTP 模式的 run_server.py）。

用法：
    cp config.example.toml config.toml   # 只需填 feishu.app_id / feishu.app_secret
    uv sync --extra ws
    uv run python run_ws.py

相对 HTTP 模式的好处：
- 不需要公网 HTTPS 入口、TLS 证书、反向代理、IP 白名单
- 不需要 encrypt_key / verify_token / FastAPI / uvicorn
- 只要服务能出站访问 open.feishu.cn / api.anthropic.com 即可

飞书开放平台需要做的调整：
- "事件订阅" 方式改成长连接
- "消息卡片的接收方式" 改成 事件订阅（card.action.trigger 新版）
- 订阅事件：`im.message.receive_v1` 和 `card.action.trigger`
"""

import argparse
import asyncio
import logging
import os
from pathlib import Path

from ops_qa_bot.config import load_config
from ops_qa_bot.logging_config import setup_feedback_logger, setup_logging
from ops_qa_bot.ws_server import WsRunner


async def _main(config) -> None:
    await WsRunner(config).run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="飞书运维问答 bot（长连接模式）")
    parser.add_argument(
        "--config",
        default=os.environ.get("CONFIG_FILE", "./config.toml"),
        help="配置文件路径（默认 ./config.toml）",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))

    setup_logging(config.logging.main_log)
    setup_feedback_logger(config.logging.feedback_log)

    logging.getLogger(__name__).info(
        "starting WS mode with config from %s (main_log=%s feedback_log=%s)",
        args.config,
        config.logging.main_log,
        config.logging.feedback_log,
    )

    try:
        asyncio.run(_main(config))
    except KeyboardInterrupt:
        pass
