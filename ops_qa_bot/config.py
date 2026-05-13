"""配置加载：TOML 文件为主，环境变量可覆盖。

优先级：环境变量（若非空） > 配置文件值 > 默认值。
环境变量保留是为了让 secret（app_secret / token）能走 secret manager 注入，
不强制写进文件。一般场景直接填 config.toml 即可。
"""

from __future__ import annotations

import os
import re
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
class HealthConfig:
    """长连接模式下的辅助 HTTP 健康检查端点。HTTP 模式不需要这个（已自带 /healthz）。"""

    enabled: bool = True
    host: str = "127.0.0.1"  # 默认只监听 localhost；接外部监控时改成 0.0.0.0
    port: int = 8001


@dataclass
class FeishuDocCollabConfig:
    """飞书文档协作（ask_feishu_doc 工具）配置。

    peer_open_id 未设 → ws_server 不注入该工具、bot 行为完全不变；
    设了就必须是合法的 open_id 格式，否则 load_config 阶段就抛——
    比等到运行时 ask 发出去、超时 60s 才发现错填了 union_id/user_id/邮箱 友好。
    """

    peer_open_id: str | None = None


@dataclass
class AppConfig:
    docs_root: Path
    feishu: FeishuConfig
    server: ServerConfig = field(default_factory=ServerConfig)
    session_idle_ttl: float = 1800.0
    admin_token: str | None = None
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    feishu_doc: FeishuDocCollabConfig = field(default_factory=FeishuDocCollabConfig)


# 飞书 open_id 严格格式：以 ou_ 起头 + 字母数字/下划线/连字符。长度上限放 64 防止
# 误把整段邮箱/JSON 塞进来导致日志被污染。
_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]{1,64}$")


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

    health_raw = data.get("health") or {}
    health_enabled_raw = _pick("HEALTH_ENABLED", health_raw.get("enabled"), True)
    health_enabled = (
        health_enabled_raw
        if isinstance(health_enabled_raw, bool)
        else str(health_enabled_raw).lower() not in ("0", "false", "no", "")
    )
    health_host = _pick("HEALTH_HOST", health_raw.get("host"), "127.0.0.1")
    health_port = int(_pick("HEALTH_PORT", health_raw.get("port"), 8001))

    feishu_doc_raw = data.get("feishu_doc") or {}
    peer_open_id_raw = _pick(
        "FEISHU_DOC_PEER_OPEN_ID", feishu_doc_raw.get("peer_open_id")
    ) or None
    if peer_open_id_raw and not _OPEN_ID_RE.match(peer_open_id_raw):
        # 启动时硬报错而不是日志 warning + 跳过：peer_open_id 配错了，工具看似启用、
        # 调用时 100% 失败，对用户和 agent 都不友好。让运维当场看到配置错误。
        source = (
            "环境变量 FEISHU_DOC_PEER_OPEN_ID"
            if os.environ.get("FEISHU_DOC_PEER_OPEN_ID")
            else f"{path} [feishu_doc].peer_open_id"
        )
        raise RuntimeError(
            f"feishu_doc.peer_open_id 格式不合法（来自 {source}）："
            f"'{peer_open_id_raw}'。\n"
            f"必须是飞书 open_id，形如 ou_xxxxxxxx（不是 union_id / user_id / "
            f"邮箱 / 手机号）。让 lark-copilot 那个 user 给本 bot 发一条消息，"
            f"在日志里抓 sender.open_id 即是本 app 视角下对应的 open_id。"
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
        health=HealthConfig(
            enabled=health_enabled,
            host=health_host,
            port=health_port,
        ),
        feishu_doc=FeishuDocCollabConfig(peer_open_id=peer_open_id_raw),
    )
