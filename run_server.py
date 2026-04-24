#!/usr/bin/env python3
"""启动飞书 webhook 服务。

用法：
    # 1. 复制配置模板并填值
    cp config.example.toml config.toml
    # 2. 启动
    uv run python run_server.py                   # 默认读 ./config.toml
    uv run python run_server.py --config /etc/ops-qa-bot/config.toml

任何字段也可以用同名环境变量覆盖（见 config.example.toml 中注释）。

安全：未包含签名校验，请在飞书开放平台配置 IP 白名单限制来源。
"""

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from ops_qa_bot.config import load_config
from ops_qa_bot.feishu_server import create_app
from ops_qa_bot.logging_config import setup_feedback_logger, setup_logging

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="飞书运维问答 bot 服务")
    parser.add_argument(
        "--config",
        default=os.environ.get("CONFIG_FILE", "./config.toml"),
        help="配置文件路径（默认 ./config.toml，也可用 CONFIG_FILE 环境变量指定）",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))

    setup_logging(config.logging.main_log)
    setup_feedback_logger(config.logging.feedback_log)

    logging.getLogger(__name__).info(
        "config loaded from %s; main_log=%s feedback_log=%s",
        args.config,
        config.logging.main_log,
        config.logging.feedback_log,
    )

    app = create_app(config)
    uvicorn.run(app, host=config.server.host, port=config.server.port)
