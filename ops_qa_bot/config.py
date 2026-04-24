"""配置加载：TOML 文件为主，环境变量可覆盖。

优先级：环境变量（若非空） > 配置文件值 > 默认值。
环境变量保留是为了让 secret（app_secret / token）能走 secret manager 注入，
不强制写进文件。一般场景直接填 config.toml 即可。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str
    verify_token: str | None = None
    card_verify_token: str | None = None
    encrypt_key: str | None = None  # 设置后启用 AES 解密 + 签名校验


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class LoggingConfig:
    main_log: Path = field(default_factory=lambda: Path("./logs/ops_qa_bot.log"))
    feedback_log: Path = field(default_factory=lambda: Path("./logs/feedback.log"))


@dataclass
class AppConfig:
    docs_root: Path
    feishu: FeishuConfig
    server: ServerConfig = field(default_factory=ServerConfig)
    session_idle_ttl: float = 1800.0
    admin_token: str | None = None
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _pick(env_key: str, cfg_value: Any, default: Any = None) -> Any:
    """env var 优先（非空），其次 config 文件值，最后 default。"""
    env_val = os.environ.get(env_key)
    if env_val not in (None, ""):
        return env_val
    if cfg_value not in (None, ""):
        return cfg_value
    return default


def load_config(path: Path) -> AppConfig:
    """从 TOML 文件加载配置。文件不存在不会报错（可纯靠环境变量），
    但 `feishu.app_id` / `feishu.app_secret` 两个必填项缺失时会抛出。"""
    data: dict[str, Any] = {}
    if path.is_file():
        with open(path, "rb") as f:
            data = tomllib.load(f)

    feishu_raw = data.get("feishu") or {}
    app_id = _pick("FEISHU_APP_ID", feishu_raw.get("app_id"))
    app_secret = _pick("FEISHU_APP_SECRET", feishu_raw.get("app_secret"))
    if not app_id or not app_secret:
        raise RuntimeError(
            f"feishu.app_id / feishu.app_secret 必须在 {path} 里配置，"
            "或通过环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET 提供"
        )

    verify_token = _pick("FEISHU_VERIFY_TOKEN", feishu_raw.get("verify_token")) or None
    card_verify_token = (
        _pick("FEISHU_CARD_VERIFY_TOKEN", feishu_raw.get("card_verify_token"))
        or verify_token
    )
    encrypt_key = _pick("FEISHU_ENCRYPT_KEY", feishu_raw.get("encrypt_key")) or None

    docs_root = Path(
        _pick("DOCS_ROOT", data.get("docs_root"), "./docs")
    ).resolve()

    server_raw = data.get("server") or {}
    host = _pick("HOST", server_raw.get("host"), "0.0.0.0")
    port = int(_pick("PORT", server_raw.get("port"), 8000))

    session_raw = data.get("session") or {}
    idle_ttl = float(_pick("SESSION_IDLE_TTL", session_raw.get("idle_ttl"), 1800))

    admin_raw = data.get("admin") or {}
    admin_token = _pick("ADMIN_TOKEN", admin_raw.get("token")) or None

    logging_raw = data.get("logging") or {}
    main_log = Path(
        _pick("LOG_FILE", logging_raw.get("main_log"), "./logs/ops_qa_bot.log")
    )
    feedback_log = Path(
        _pick("FEEDBACK_LOG", logging_raw.get("feedback_log"), "./logs/feedback.log")
    )

    return AppConfig(
        docs_root=docs_root,
        feishu=FeishuConfig(
            app_id=app_id,
            app_secret=app_secret,
            verify_token=verify_token,
            card_verify_token=card_verify_token,
            encrypt_key=encrypt_key,
        ),
        server=ServerConfig(host=host, port=port),
        session_idle_ttl=idle_ttl,
        admin_token=admin_token,
        logging=LoggingConfig(main_log=main_log, feedback_log=feedback_log),
    )
