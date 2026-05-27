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
class DocQAConfig:
    """外部「飞书文档问答」服务（POST /doc_qa）的接入配置。

    部分组件负责人用飞书文档而非本地 markdown 维护运维知识；这个服务把
    feishu doc token + 问题翻成 markdown 答案。bot 把它包成主 agent 的一个
    工具 `query_feishu_doc`，按 INDEX.md 里登记为 `feishu` 来源的组件调用。

    `base_url` 为空时整个特性关闭（不挂工具、prompt 不加来源节），让没有飞书
    文档需求的部署零感知。`token` 为空表示对端没开鉴权（仅可信内网测试）。
    `timeout` 必须 ≥ 对端最坏耗时——/doc_qa 内部还要跑 agent + 拉文档图，
    比纯文本接口慢，留足余量避免外层 agent 等不到结果误判失败。
    """

    base_url: str | None = None
    token: str | None = None
    timeout: float = 60.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)


@dataclass
class AppConfig:
    docs_root: Path
    feishu: FeishuConfig
    server: ServerConfig = field(default_factory=ServerConfig)
    session_idle_ttl: float = 1800.0
    admin_token: str | None = None
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    doc_qa: DocQAConfig = field(default_factory=DocQAConfig)


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

    doc_qa_raw = data.get("doc_qa") or {}
    doc_qa_base_url = (
        _pick("DOC_QA_BASE_URL", doc_qa_raw.get("base_url")) or None
    )
    # 末尾斜杠归一化：拼 /doc_qa 时不想撞出 //doc_qa
    if doc_qa_base_url:
        doc_qa_base_url = doc_qa_base_url.rstrip("/")
    doc_qa_token = _pick("DOC_QA_TOKEN", doc_qa_raw.get("token")) or None
    doc_qa_timeout = float(
        _pick("DOC_QA_TIMEOUT", doc_qa_raw.get("timeout"), 60)
    )

    return AppConfig(
        docs_root=docs_root,
        feishu=FeishuConfig(
            app_id=app_id,
            app_secret=app_secret,
            verify_token=verify_token,
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
        doc_qa=DocQAConfig(
            base_url=doc_qa_base_url,
            token=doc_qa_token,
            timeout=doc_qa_timeout,
        ),
    )
