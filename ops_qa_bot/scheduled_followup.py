"""定时跟进任务接入：让 agent 能"过 N 分钟后自动再查一次"。

典型场景：用户发起一个变更（如跑了一条 ALTER），让 bot "20 分钟后帮我看看执行完没"。
单轮答题是同步跑完就返回的——agent 没法真的 sleep 20 分钟，所以以前只能老实回
"我没法定时"。本模块把"登记一个未来动作"包成主 `OpsQABot` 的一个进程内工具
`schedule_followup`：agent 只负责**提议**（把"过多久、查什么"登记下来、当轮立刻回复
"好，到点帮你看"），真正的查询发生在**到点的回调里**——届时 bot 用存好的 task 跑一轮
全新答题（实打实调 query_database 等工具），再把结果主动 @ 用户推回群里。

设计要点（与 gateway_trace.py / db_query.py 的参数变更审批同姿态）：
- **只依赖 stdlib + claude_agent_sdk**，不引入 bot / feishu_core（避免循环）。工具只做
  "校验入参 + 交给注入的 submitter 登记"，定时器/回调执行的实现住在 feishu_core
  （`FollowupScheduler`），通过 `FollowupSubmitter` Protocol 解耦。
- **工具天生改不成写操作**：它唯一的副作用是"安排一次未来的提问"，到点跑的还是受
  同样只读约束（写命令 hook + 只读账号）的答题流程。
- **MVP 是纯内存定时器**：进程重启会丢掉未触发的任务（20 分钟级场景一般够用）。真有
  "重启丢任务"痛点了再补持久化（落 sqlite / 文件、启动时重载）。
- 入参边界（最小/最大延时、每人挂起上限）由 `ScheduledFollowupConfig` 给，
  delay/task 的合法性在本模块校验，每人上限由 submitter 侧按 (chat, asker) 维度兜。
- 失败**返回文字结果而不是抛异常**：抛会打断 agent 这一轮；返回引导文字让 agent
  据此如实告诉用户（比如"超出可登记数量，请精简或过会儿手动再问"）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from .config import ScheduledFollowupConfig

logger = logging.getLogger("ops_qa_bot.scheduled_followup")

# 工具名（allowed_tools 引用）。agent 侧全名是 mcp__<server_key>__<tool_name>。
TOOL_NAME = "schedule_followup"
SERVER_KEY = "followup"
FULL_TOOL_NAME = f"mcp__{SERVER_KEY}__{TOOL_NAME}"

# task 文本上限：自包含的一句跟进指令远不到这个量级，超长当异常拒。
_MAX_TASK_LEN = 1000


@dataclass
class ScheduledFollowupRequest:
    """一笔待登记的定时跟进——由 `schedule_followup` 工具校验后组装，交给
    `FollowupSubmitter` 落成"一个 N 分钟后触发的后台任务"。

    `task` 是 agent 写的**自包含**跟进指令（到点时作为新一轮的问题原样喂回答题
    流程），所以必须把"查哪个实例/表、查什么、判断标准"都写进去，不能依赖上文。
    """

    delay_minutes: int
    task: str


class FollowupSubmitter(Protocol):
    """把一笔 ScheduledFollowupRequest 落成"一个到点触发的后台跟进任务"。

    由 feishu_core 实现并按 (chat_id, asker) 注入进 `schedule_followup` 工具；本模块
    只依赖这个 Protocol，不 import feishu_core（避免循环依赖）。返回给 agent 看的
    文字（如"已登记，20 分钟后帮你看"或"超出可登记数量"），工具原样转给 LLM。
    """

    async def __call__(self, req: ScheduledFollowupRequest) -> str: ...


_TOOL_DESC = (
    "登记一个**定时跟进任务**：过 N 分钟后，由 bot 自动执行一次你现在指定的检查，"
    "并把结果主动发到群里 @ 用户。用于用户明确要求「过一会儿/X 分钟后再帮我看看 Y」"
    "这类**需要等待再复查**的场景——典型如用户跑了个耗时变更（ALTER、迁移、扩容、"
    "重启后预热等），想让你过一段时间确认它完成了没 / 现在状态如何。\n"
    "**你只是登记，不会也不需要在这一轮真的等待**：工具立刻返回，到点时系统会自动"
    "用你写的 task 跑一轮全新的检查（那时你才会实际去查数据库/机器/链路）。所以调用"
    "成功后把返回的确认语如实转达用户即可（告诉他到点会自动来看并 @ 他），不要谎称"
    "现在就查到了结果。\n"
    "参数：\n"
    "- delay_minutes：等待分钟数（整数）。用用户说的时间；没明说就按场景给个合理值"
    "（变更类常见 5~30 分钟）。有上下限，越界工具会返回提示。\n"
    "- task：到点要执行的**自包含**指令。必须把复查所需的一切都写进去——查哪个实例"
    "（IP/端口/租户/库/表）、具体查什么、怎么判断完成或异常。**不要依赖上文**（到点是"
    "全新一轮，没有现在的对话记忆），例如别写「看看它好了没」，要写「连 10.1.2.3 的"
    "OB 租户 xxx，检查表 ps_index_flow_6 的 ALTER 是否已执行完成（DDL 进度/表结构），"
    "完成则告知最终结构、未完成则说明当前进度」。\n"
    "什么时候**不要**用：用户只是现在问一个能立刻答的问题（直接答，别绕成定时）；"
    "或要求的是 bot 不能做的事（持续盯着、做写操作等）——定时任务到点跑的仍受只读约束。"
)

_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "delay_minutes": {
            "type": "integer",
            "description": "等待多少分钟后执行（整数，有上下限）",
        },
        "task": {
            "type": "string",
            "description": "到点要执行的自包含检查指令（不能依赖上文）",
        },
    },
    "required": ["delay_minutes", "task"],
    "additionalProperties": False,
}


def _text_result(text: str, *, is_error: bool = False) -> dict:
    res: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        res["is_error"] = True
    return res


def make_schedule_followup_handler(
    config: ScheduledFollowupConfig, submitter: FollowupSubmitter
):
    """造 schedule_followup 的 async handler（与 SDK 解耦，便于单测直接 await）。

    校验 delay 边界 + task 非空/长度后交给 submitter 登记。失败一律返回 is_error
    文字（不抛），让 agent 据引导处理（修正参数 / 如实告诉用户登记不了）。
    """

    async def handler(args: dict) -> dict:
        delay_raw = args.get("delay_minutes")
        try:
            delay = int(delay_raw)
        except (TypeError, ValueError):
            return _text_result(
                "delay_minutes 必须是整数分钟数。请确认用户要等多久后再让我看。",
                is_error=True,
            )
        if delay < config.min_delay_minutes or delay > config.max_delay_minutes:
            return _text_result(
                f"等待时间需在 {config.min_delay_minutes}~{config.max_delay_minutes} "
                f"分钟之间（你传的是 {delay}）。请在范围内取值；"
                "需要更久的跟进就告诉用户过会儿手动再问我。",
                is_error=True,
            )

        task = str(args.get("task") or "").strip()
        if not task:
            return _text_result(
                "缺少 task（到点要执行的检查指令）。请写一条自包含的指令再登记。",
                is_error=True,
            )
        if len(task) > _MAX_TASK_LEN:
            return _text_result(
                "task 过长，疑似异常。请精简成一条聚焦的检查指令后重试。",
                is_error=True,
            )

        req = ScheduledFollowupRequest(delay_minutes=delay, task=task)
        try:
            text = await submitter(req)
        except Exception:
            logger.exception("submit scheduled followup failed")
            return _text_result(
                "登记定时跟进时出错。请如实告诉用户暂时没能登记，让他过会儿手动再问我。",
                is_error=True,
            )
        return _text_result(text)

    return handler


def build_followup_server(
    config: ScheduledFollowupConfig, submitter: FollowupSubmitter
) -> McpSdkServerConfig:
    """构建挂定时跟进工具的进程内 MCP server。

    工具 `schedule_followup(delay_minutes, task)`：校验后把一笔跟进交给 submitter
    登记成一个到点触发的后台任务（到点在飞书侧跑一轮答题并把结果推回群，不在本
    工具调用里执行）。只在 submitter 注入（即飞书侧定时器链路已接好）时构建。
    """
    handler = make_schedule_followup_handler(config, submitter)
    decorated = tool(TOOL_NAME, _TOOL_DESC, _TOOL_SCHEMA)(handler)
    return create_sdk_mcp_server(
        name="ops-qa-followup", version="1.0.0", tools=[decorated]
    )
