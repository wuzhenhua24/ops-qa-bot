"""外部「飞书文档问答」服务接入：注册表解析 + HTTP 客户端 + SDK MCP 工具。

部分组件负责人用飞书文档维护运维知识，另一个系统提供 `POST /doc_qa`：传一组
飞书 doc token + 问题，返回 markdown 答案（内部自带看文档/图的 agent）。本模块把
它包成主 `OpsQABot` 的一个进程内工具 `query_feishu_doc`，让主 agent 像查本地文档
一样查飞书文档——升级 / 归档 / 追问 / 反馈那套现有管线一行不用改。

设计要点：
- **只依赖 stdlib + httpx + claude_agent_sdk**，不引入 feishu_core / bot（被它们
  反向 import，避免循环）。
- **agent 只传组件名，token 服务端解析**：doc token 是登记在 INDEX.md 里的权威
  数据，agent 不碰原始 token——防写错、防被文档内容注入诱导去拉任意飞书文档
  （和 `_validate_image_path` 不信任 LLM 给的路径同一套防御姿态）。
- 工具失败**返回文字结果而不是抛异常**：抛异常会打断 agent 这一轮；返回一段
  「取不到飞书文档，请按升级规则通知负责人」的提示，让 agent 自己决定 ESCALATE。
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from .config import DocQAConfig

logger = logging.getLogger("ops_qa_bot.doc_qa")

# 工具名（INDEX.md 的 prompt 和 allowed_tools 都引用这个）。MCP 工具在 agent
# 侧的全名是 mcp__<server_key>__<tool_name>，server_key 见 build_doc_qa_server。
TOOL_NAME = "query_feishu_doc"
SERVER_KEY = "docqa"
FULL_TOOL_NAME = f"mcp__{SERVER_KEY}__{TOOL_NAME}"

# 飞书文档 token 单元格分隔符：英文/中文逗号、顿号、分号、空白都当分隔
_DOC_SPLIT_RE = re.compile(r"[,，、;；\s]+")


@dataclass(frozen=True)
class FeishuDocComponent:
    """INDEX.md 里登记为 feishu 来源的一个组件。"""

    name: str  # 原始「组件」列文字（用于回显 / 来源标注）
    docs: tuple[str, ...]  # 飞书 doc token / url 列表


# INDEX.md 飞书注册表解析缓存：路径 → (mtime, {lookup_key: FeishuDocComponent})
# lookup_key 是小写归一化后的组件名 / 目录名（两者都建别名，agent 传哪个都认）。
_registry_cache: dict[Path, tuple[float, dict[str, FeishuDocComponent]]] = {}


def _norm_key(s: str) -> str:
    """组件名 / 目录名归一化成查找 key：去 backtick、去首尾斜杠和空白、转小写。"""
    return s.strip().strip("`").strip("/").strip().lower()


def parse_feishu_registry(docs_root: Path) -> dict[str, FeishuDocComponent]:
    """解析 docs_root/INDEX.md 的组件表 → {归一化 key: FeishuDocComponent}。

    只收 `来源` 列值为 `feishu`（大小写不敏感）且「飞书文档」列非空的行。
    每个组件同时以「组件」列和「目录」列的归一化值建别名，agent 传组件名或
    目录名都能命中。表头按列名定位（与 `_index_owner_to_dirs` 同思路），加列 /
    调序不影响；缺「来源」或「飞书文档」列时返回空（视作没有飞书组件）。

    mtime 缓存：文件没改直接返回上次结果。解析失败回退到上次缓存（或空表）。
    """
    index_path = docs_root / "INDEX.md"
    try:
        mtime = index_path.stat().st_mtime
    except FileNotFoundError:
        return {}
    cached = _registry_cache.get(index_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    registry: dict[str, FeishuDocComponent] = {}
    in_table = False
    name_idx = src_idx = docs_idx = dir_idx = -1
    try:
        with index_path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line.startswith("|"):
                    if in_table:
                        break
                    continue
                cols = [c.strip() for c in line.strip("|").split("|")]
                if not in_table:
                    if "来源" in cols and "组件" in cols and "飞书文档" in cols:
                        name_idx = cols.index("组件")
                        src_idx = cols.index("来源")
                        docs_idx = cols.index("飞书文档")
                        dir_idx = cols.index("目录") if "目录" in cols else -1
                        in_table = True
                    continue
                # 分隔行 |---|---|
                if all(set(c) <= set("-: ") and c for c in cols):
                    continue
                if max(name_idx, src_idx, docs_idx) >= len(cols):
                    continue
                if cols[src_idx].strip("`").strip().lower() != "feishu":
                    continue
                name = cols[name_idx].strip("`").strip()
                docs_cell = cols[docs_idx].strip("`").strip()
                tokens = tuple(t for t in _DOC_SPLIT_RE.split(docs_cell) if t)
                if not name or not tokens:
                    continue
                comp = FeishuDocComponent(name=name, docs=tokens)
                registry[_norm_key(name)] = comp
                if 0 <= dir_idx < len(cols):
                    dir_key = _norm_key(cols[dir_idx])
                    if dir_key:
                        registry.setdefault(dir_key, comp)
    except OSError as e:
        logger.warning("read INDEX.md for feishu registry failed: %s", e)
        return _registry_cache.get(index_path, (0.0, {}))[1]

    _registry_cache[index_path] = (mtime, registry)
    return registry


class DocQAError(Exception):
    """/doc_qa 调用失败。`agent_hint` 是给主 agent 看的引导文字。"""

    def __init__(self, log_detail: str, agent_hint: str):
        super().__init__(log_detail)
        self.agent_hint = agent_hint


# 给 agent 的兜底引导：让它按 system prompt 的「升级规则」回找不到 + 通知负责人，
# 而不是干等或编答案。措辞 agent-facing，不是给终端用户看的。
_ESCALATE_HINT = (
    "未能取得该组件的飞书文档内容。请按 system prompt 的「升级规则」处理："
    "回复未找到相关内容并通知该组件负责人（输出 <<ESCALATE:ou_xxx:目录>>），"
    "不要凭常识编答案。"
)


class DocQAClient:
    """`POST /doc_qa` 的轻量 httpx 客户端。

    `transport` 仅供测试注入 httpx.MockTransport；生产留 None 走默认网络。
    """

    def __init__(
        self,
        config: DocQAConfig,
        transport: "httpx.AsyncBaseTransport | None" = None,
    ):
        if not config.base_url:
            raise ValueError("DocQAClient 需要非空 base_url")
        self._url = f"{config.base_url}/doc_qa"
        self._token = config.token
        self._timeout = config.timeout
        self._transport = transport

    async def ask(
        self, docs: list[str], question: str, req_id: str | None = None
    ) -> str:
        """调 /doc_qa 拿 markdown 答案。失败抛 DocQAError（带 agent_hint）。"""
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        payload: dict = {"docs": docs, "q": question}
        if req_id:
            payload["req_id"] = req_id
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(self._url, json=payload, headers=headers)
        except httpx.TimeoutException as e:
            raise DocQAError(f"doc_qa timeout: {e!r}", _ESCALATE_HINT) from e
        except httpx.HTTPError as e:
            raise DocQAError(f"doc_qa connect error: {e!r}", _ESCALATE_HINT) from e

        body: dict = {}
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}

        if resp.status_code == 200 and body.get("ok"):
            answer = body.get("answer")
            if isinstance(answer, str) and answer.strip():
                return answer
            raise DocQAError(
                f"doc_qa 200 but empty answer: req_id={body.get('req_id')}",
                _ESCALATE_HINT,
            )

        # 失败分类。401/422 是配置错（鉴权 / docs 为空），打 error 提醒运维核对；
        # 504/500/其它运行期错误打 warning。两类给 agent 的 hint 都是「按升级规则走」。
        detail = (
            f"doc_qa failed: status={resp.status_code} "
            f"req_id={body.get('req_id')} error={body.get('error')}"
        )
        if resp.status_code in (401, 422):
            logger.error("%s (检查 [doc_qa] 配置 / INDEX.md 飞书文档登记)", detail)
        else:
            logger.warning(detail)
        raise DocQAError(detail, _ESCALATE_HINT)


_TOOL_DESC = (
    "查询用飞书文档维护的组件的运维知识。当 INDEX.md 里某组件的『来源』列是 "
    "feishu 时，用本工具代替 Read/Glob（这类组件没有本地 md 文件）。"
    "component 传 INDEX.md 里该组件的『组件』列名（如 Nginx）；question 传一个"
    "自包含的完整问题——本服务无对话记忆，追问时要把上下文折进 question。"
    "返回该组件飞书文档里的答案 markdown；取不到内容时返回升级提示。"
)


def _text_result(text: str, *, is_error: bool = False) -> dict:
    res: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        res["is_error"] = True
    return res


def make_query_feishu_doc_handler(docs_root: Path, client: "DocQAClient"):
    """造 query_feishu_doc 的 async handler（与 SDK 解耦，便于单测直接 await）。

    注册表每次调用都重新解析（mtime 缓存，开销很小），保证 INDEX.md 改了登记
    后无需重启 session。失败一律返回 is_error 文字结果而不抛——抛会打断 agent
    这一轮，返回提示则让 agent 自己按升级规则处理。
    """

    async def handler(args: dict) -> dict:
        component = str(args.get("component") or "").strip()
        question = str(args.get("question") or "").strip()
        if not question:
            return _text_result("调用缺少 question 参数，无法查询。", is_error=True)
        registry = parse_feishu_registry(docs_root)
        entry = registry.get(_norm_key(component))
        if entry is None:
            known = "、".join(sorted({c.name for c in registry.values()})) or "（无）"
            return _text_result(
                f"组件「{component}」未登记为飞书文档来源。"
                f"当前飞书来源组件：{known}。"
                "请确认组件名，或改用本地文档工具 / 按归属判断处理。",
                is_error=True,
            )

        req_id = uuid.uuid4().hex[:12]
        logger.info(
            "doc_qa call: component=%s req_id=%s docs=%d q_len=%d",
            entry.name,
            req_id,
            len(entry.docs),
            len(question),
        )
        try:
            answer = await client.ask(list(entry.docs), question, req_id=req_id)
        except DocQAError as e:
            logger.warning("doc_qa call failed: req_id=%s %s", req_id, e)
            return _text_result(e.agent_hint, is_error=True)
        logger.info(
            "doc_qa ok: component=%s req_id=%s ans_len=%d",
            entry.name,
            req_id,
            len(answer),
        )
        return _text_result(answer)

    return handler


def build_doc_qa_server(
    docs_root: Path, config: DocQAConfig
) -> McpSdkServerConfig:
    """构建挂飞书文档问答工具的进程内 MCP server。

    工具 `query_feishu_doc(component, question)`：按组件名查 INDEX.md 注册表拿
    飞书 doc token，调 /doc_qa，把答案 markdown 返给主 agent。
    """
    handler = make_query_feishu_doc_handler(docs_root, DocQAClient(config))
    decorated = tool(TOOL_NAME, _TOOL_DESC, {"component": str, "question": str})(
        handler
    )
    return create_sdk_mcp_server(
        name="ops-qa-doc-qa", version="1.0.0", tools=[decorated]
    )
