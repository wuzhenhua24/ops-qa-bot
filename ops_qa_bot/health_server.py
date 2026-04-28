"""极小的 asyncio HTTP 服务，专门给监控/健康检查用。

只支持 GET，路由由调用方注册。响应固定 application/json。约 100 行，
不引入新依赖（fastapi/aiohttp 都不要），足够给 prometheus blackbox /
k8s liveness probe / 内部监控脚本调用。

刻意不实现：
- 非 GET 方法（运维探活用不到）
- HTTP/1.1 keep-alive（每次新连接，简化处理）
- 流式响应、文件下载等
- HTTPS（监控调用走内网/localhost，不需要 TLS）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("ops_qa_bot.health")

# 路由处理器：无参数 async 函数，返回 (HTTP 状态码, JSON-able 字典)
HandlerFn = Callable[[], Awaitable[tuple[int, dict[str, Any]]]]


_REASON: dict[int, str] = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class HealthServer:
    def __init__(
        self,
        host: str,
        port: int,
        routes: dict[str, HandlerFn],
    ):
        self.host = host
        self.port = port
        self._routes = routes
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        logger.info(
            "health server listening on http://%s:%d (routes: %s)",
            self.host,
            self.port,
            ", ".join(sorted(self._routes.keys())),
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None
        logger.info("health server stopped")

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
            except asyncio.TimeoutError:
                return
            if not line:
                return

            try:
                method, path, _ = line.decode("ascii", "replace").rstrip().split(" ", 2)
            except ValueError:
                await self._send(writer, 400, {"error": "bad request"})
                return

            # 把 headers 读完丢掉，等到空行
            for _ in range(64):  # 上限防恶意请求
                try:
                    h = await asyncio.wait_for(reader.readline(), timeout=5)
                except asyncio.TimeoutError:
                    return
                if h in (b"\r\n", b"\n", b""):
                    break

            if method != "GET":
                await self._send(writer, 405, {"error": "method not allowed"})
                return

            path_only = path.split("?", 1)[0]
            handler = self._routes.get(path_only)
            if handler is None:
                await self._send(writer, 404, {"error": "not found", "path": path_only})
                return

            try:
                status, body = await handler()
            except Exception:
                logger.exception("health handler crashed: %s", path_only)
                await self._send(writer, 500, {"error": "internal"})
                return

            await self._send(writer, status, body)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _send(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: dict[str, Any],
    ) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {_REASON.get(status, '')}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            writer.write(head + payload)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
