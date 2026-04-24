import contextvars
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 每个 webhook 请求一个短 id，后续该请求链路的所有日志都会带上。
# ContextVar 在 asyncio.Task 间自动传递（FastAPI BackgroundTasks 也继承调用方 context），
# 所以只需在入口处 set 一次，不用在函数签名里手动传。
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class _RequestIdFilter(logging.Filter):
    """把 request_id_var 注入到每条 LogRecord 的 `request_id` 字段，供 format 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def setup_logging(
    log_file: Path,
    level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """配置根 logger：文件（滚动） + stdout 同时输出。

    日志格式：时间 logger名 级别 消息
    文件滚动：默认单文件 10MB，保留 5 份历史。
    """
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(request_id)s] %(name)s %(levelname)s %(message)s"
    )
    rid_filter = _RequestIdFilter()

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(rid_filter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler.addFilter(rid_filter)

    root = logging.getLogger()
    root.setLevel(level)
    # 清掉已有 handler，避免 hot reload 时重复输出
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    # uvicorn 接入同一套格式
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    # access log 噪音较大，只看 warning 以上
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def setup_feedback_logger(
    log_file: Path,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 10,
) -> logging.Logger:
    """配置独立的反馈日志（`ops_qa_bot.feedback`）。

    每行一条 JSON，便于离线 grep/统计（问答+反馈用 qid 关联）：

        {"event": "qa", "qid": "...", "chat_id": "...", "user_id": "...", "question": "...", "answer_excerpt": "..."}
        {"event": "feedback", "qid": "...", "user_id": "...", "rating": "up|down"}
    """
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("ops_qa_bot.feedback")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 不要混到通用日志里
    logger.handlers = []

    handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    # 保留时间戳前缀，其余就是 JSON 原文
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    return logger
