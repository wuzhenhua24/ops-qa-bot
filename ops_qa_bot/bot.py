import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from .prompt import build_system_prompt

logger = logging.getLogger(__name__)


@dataclass
class AnswerResult:
    """`OpsQABot.answer()` 的返回值：答案文本 + 模型用量。

    用量字段直接转发自 Claude Agent SDK 的 ResultMessage，便于上层按自定的
    单价（比如对接第三方 Claude 兼容代理时）算实际成本，而不只看 SDK
    给的 total_cost_usd。
    """

    text: str
    cost_usd: float | None = None
    usage: dict[str, Any] | None = None  # input/output/cache_read/cache_creation tokens
    num_turns: int | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None


def format_tool_call(name: str, tool_input: dict) -> str:
    """紧凑展示工具调用，用于日志和 CLI 输出。"""
    if name == "Read":
        return f"Read {tool_input.get('file_path', '?')}"
    if name == "Glob":
        return f"Glob {tool_input.get('pattern', '?')}"
    if name == "Grep":
        pattern = tool_input.get("pattern", "?")
        path = tool_input.get("path", "")
        return f"Grep '{pattern}'" + (f" in {path}" if path else "")
    return f"{name}({tool_input})"


class OpsQABot:
    """运维文档问答机器人。

    用法（流式，适合 CLI）：
        async with OpsQABot(docs_root="./docs") as bot:
            async for event in bot.ask("Redis 内存告警怎么处理？"):
                ...

    用法（一次性拿完整答案，适合飞书/Slack 接入）：
        async with OpsQABot(docs_root="./docs") as bot:
            text = await bot.answer("Redis 内存告警怎么处理？")
    """

    def __init__(self, docs_root: str | Path):
        self.docs_root = Path(docs_root).resolve()
        if not self.docs_root.is_dir():
            raise ValueError(f"docs_root 不存在或不是目录: {self.docs_root}")
        if not (self.docs_root / "INDEX.md").is_file():
            raise ValueError(
                f"docs_root 下缺少 INDEX.md 路由表: {self.docs_root / 'INDEX.md'}"
            )

        # 显式收窄工具集是产品约束 + 安全约束，不是能力约束：
        # - 飞书群是半开放入口，任何成员都能给 bot 发文本。默认工具集里的
        #   Bash / Write / WebFetch 在提示注入下会变成武器。
        # - 本任务是只读地导航 markdown 文档，Read/Glob/Grep 是完备集。
        # - WebFetch 会让 agent 在文档找不到答案时上网搜，与"只基于本地文档
        #   回答、否则说找不到"的防幻觉约束相冲突。
        # 未来若要支持"查实时状态"（如读 Redis 内存、查 Grafana），应当加
        # 一个受限的自定义 SDK 工具（白名单命令），而不是放开 Bash。
        self._options = ClaudeAgentOptions(
            system_prompt=build_system_prompt(self.docs_root),
            tools=["Read", "Glob", "Grep"],
            cwd=str(self.docs_root),
            permission_mode="acceptEdits",
        )
        self._client: ClaudeSDKClient | None = None

    async def __aenter__(self) -> "OpsQABot":
        self._client = ClaudeSDKClient(options=self._options)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self._client is not None
        await self._client.__aexit__(exc_type, exc, tb)
        self._client = None

    async def ask(self, question: str) -> AsyncIterator[dict]:
        """向 bot 提问。流式返回事件字典：

        - {"type": "tool", "name": str, "input": dict}        —— agent 调用的工具
        - {"type": "text", "text": str}                       —— 回答文本片段
        - {"type": "done", "cost_usd": float | None,
          "usage": dict | None, "num_turns": int | None,
          "duration_ms": int | None,
          "duration_api_ms": int | None}                       —— 本轮结束
        """
        if self._client is None:
            raise RuntimeError("OpsQABot 必须在 async with 块内使用")

        await self._client.query(question)

        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        yield {
                            "type": "tool",
                            "name": block.name,
                            "input": block.input,
                        }
                    elif isinstance(block, TextBlock):
                        yield {"type": "text", "text": block.text}
            elif isinstance(msg, ResultMessage):
                yield {
                    "type": "done",
                    "cost_usd": msg.total_cost_usd,
                    "usage": msg.usage,
                    "num_turns": msg.num_turns,
                    "duration_ms": msg.duration_ms,
                    "duration_api_ms": msg.duration_api_ms,
                }

    async def answer(self, question: str) -> AnswerResult:
        """一次性返回完整答案 + 用量元数据。

        工具调用和成本写入 logger（INFO 级，不进入返回值）；token 用量等
        结构化字段塞进 AnswerResult，让上层（如飞书反馈日志）按需落库。
        """
        logger.info("question: %s", question)
        chunks: list[str] = []
        cost_usd: float | None = None
        usage: dict | None = None
        num_turns: int | None = None
        duration_ms: int | None = None
        duration_api_ms: int | None = None
        async for event in self.ask(question):
            if event["type"] == "tool":
                logger.info("  tool: %s", format_tool_call(event["name"], event["input"]))
            elif event["type"] == "text":
                chunks.append(event["text"])
            elif event["type"] == "done":
                cost_usd = event.get("cost_usd")
                usage = event.get("usage")
                num_turns = event.get("num_turns")
                duration_ms = event.get("duration_ms")
                duration_api_ms = event.get("duration_api_ms")
                if cost_usd is not None:
                    logger.info("  done, cost=$%.4f", cost_usd)
        return AnswerResult(
            text="".join(chunks).strip(),
            cost_usd=cost_usd,
            usage=usage,
            num_turns=num_turns,
            duration_ms=duration_ms,
            duration_api_ms=duration_api_ms,
        )
