"""飞书卡片 / post 构造层：纯 builder，从 feishu_core 拆出（行为零变化）。

全部是"参数 → 卡片 / post dict"的无状态纯函数：不发请求、不读会话/缓存。
发送时机与上下文组装（qid 关联、pending 登记、cooldown 判定）仍在
feishu_core 编排层。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .feishu_format import markdown_to_feishu_post
from .markers import _FOLLOWUP_LIBRARY, _excerpt

if TYPE_CHECKING:
    from .db_query import DbChangeRequest

POST_TITLE = "测试环境助手"

def _mention_post(user_id: str, answer_markdown: str, title: str = POST_TITLE) -> dict:
    """在答案开头插入 `@用户` 提醒，让群里一眼看出回的是谁。"""
    post = markdown_to_feishu_post(answer_markdown, title)
    mention_paragraph = [
        {"tag": "at", "user_id": user_id},
        {"tag": "text", "text": " "},
    ]
    post["zh_cn"]["content"].insert(0, mention_paragraph)
    return post




def _feedback_card(qid: str, user_id: str) -> dict:
    """问答结束后附带的反馈卡片：纯 👍 / 👎。

    追问按钮拆到独立的 `_followup_card`，避免点追问把整张反馈卡顶掉、用户失去打分入口。

    用 v2 schema：👎 后要替换成带 form 的原因表单（form 是 v2 才有），原卡和替换卡
    schema 不一致飞书侧渲染会失败。v2 不再支持 `tag:action` 容器，按钮直接放进
    column_set 并排，回调走 `behaviors:[{type:"callback", value:...}]`。
    """
    btn_up = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "👍 有帮助"},
        "type": "primary",
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "action": "feedback",
                    "qid": qid,
                    "rating": "up",
                    "asker_id": user_id,
                },
            }
        ],
    }
    btn_down = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "👎 待改进"},
        "type": "default",
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "action": "feedback",
                    "qid": qid,
                    "rating": "down",
                    "asker_id": user_id,
                },
            }
        ],
    }
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": "这次回答是否有帮助？"},
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [btn_up],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [btn_down],
                        },
                    ],
                },
            ]
        },
    }


def _followup_card(
    qid: str,
    user_id: str,
    chat_id: str,
    parent_msg_id: str | None,
    followup_keys: list[str],
) -> dict:
    """问答结束后附带的追问按钮卡：独立卡片，与反馈卡解耦。

    `followup_keys` 来自 LLM 输出的 `<<FOLLOWUPS:...>>`，已过滤到白名单内、最多 3 个。
    每个 button.behaviors.callback.value 自带 chat_id 和 parent_msg_id：parent_msg_id
    是用户原始问题的 message_id，回调时透传给新一轮 `handle_question`，让追问的
    占位/答案/卡片继续引用回到原问题，线程感不断。

    v2 schema：与同模式的 `_feedback_card` / `_clarify_giveup_card` 对齐。原卡 v1 +
    替换卡 v2 的跨版本切换在飞书上不是官方推荐做法，统一到 v2 后未来 SDK / 飞书侧
    schema 校验收紧也不会突然炸。
    """
    columns: list[dict] = []
    for k in followup_keys:
        label, _ = _FOLLOWUP_LIBRARY[k]
        btn = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": "default",
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "action": "followup",
                        "qid": qid,
                        "key": k,
                        "asker_id": user_id,
                        "chat_id": chat_id,
                        "parent_msg_id": parent_msg_id,
                    },
                }
            ],
        }
        columns.append(
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [btn],
            }
        )
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": "**想再深入？**"},
                {"tag": "column_set", "columns": columns},
            ]
        },
    }



def _followup_ack_card(label: str) -> dict:
    """点完追问按钮后用来替换原反馈卡的状态卡（v2）。

    UI 层面：原卡（含 👍/👎 + 追问按钮）整张被替换；用户想反馈得在点追问前点。
    """
    return {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"✅ 已发起追问：**{label}** —— 正在生成…",
                }
            ]
        },
    }


def _followup_error_card(message: str) -> dict:
    return {
        "schema": "2.0",
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"⚠️ {message}"},
            ]
        },
    }


def _clarify_giveup_card(
    qid: str,
    user_id: str,
    chat_id: str,
    parent_msg_id: str | None,
) -> dict:
    """反问轮卡片：单按钮 "🤷 说不清楚，按常见情况直接答"。

    用 v2 schema 单按钮（与反馈卡 / 反馈原因表单卡一致），点击触发 clarify_giveup
    回调，后台跑新一轮 handle_question 喂 _CLARIFY_GIVEUP_PROMPT。chat_id /
    parent_msg_id 透过 button value 带回，新一轮按原问题引用回复维持线程。
    asker-only 校验在 handle_clarify_giveup_click 里做。
    """
    btn = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "🤷 说不清楚，按常见情况直接答"},
        "type": "default",
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "action": "clarify_giveup",
                    "qid": qid,
                    "asker_id": user_id,
                    "chat_id": chat_id,
                    "parent_msg_id": parent_msg_id,
                },
            }
        ],
    }
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "答不上来？点下面这颗按钮让 bot 按最常见情况假设直接答，"
                        "答案会标 ⚠️ 假设。"
                    ),
                },
                {"tag": "column_set", "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [btn],
                    }
                ]},
            ]
        },
    }


def _clarify_giveup_ack_card() -> dict:
    """点完"说不清"按钮替换原卡：v2 简单 ack。"""
    return {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "✅ 收到，按常见情况重新作答中…",
                }
            ]
        },
    }



def _feedback_ack_card(rating: str, clicker_name: str | None = None) -> dict:
    """点击后用来替换原卡片的"已收到反馈"提示。

    v2 schema，对齐 `_feedback_card` / `_feedback_reason_form_card`。
    """
    msg = "✅ 感谢反馈！" if rating == "up" else "🙏 已收到，我们会持续改进。"
    if clicker_name:
        msg = f"{msg}（by {clicker_name}）"
    return {
        "schema": "2.0",
        "body": {
            "elements": [
                {"tag": "markdown", "content": msg},
            ]
        },
    }


# 👎 后弹出的原因枚举：覆盖最常见的几类可执行抓手（更新文档 / 调 prompt）
_FEEDBACK_REASONS: dict[str, str] = {
    "outdated": "文档过时",
    "incomplete": "步骤不完整",
    "incorrect": "事实错误",
    "verbose": "答案啰嗦 / 没重点",
    "other": "其他",
}


def _feedback_reason_form_card(qid: str, asker_id: str | None) -> dict:
    """👎 后替换原卡的原因收集表单（card v2 form）。

    multi_select 多选原因 + 多行 input 写备注（可选）+ 提交按钮，跳过按钮放 form 外
    （form 内放无 form_action_type 的纯 callback button 行为不明确，官方 demo 没这种
    写法）。submit 不挂 behaviors callback，仅靠 form_action_type:"submit" + button.value
    触发提交回调；事件里 action.value 带 payload，action.form_value 带字段值（多选返回
    数组）。qid / asker_id 透过按钮 value 带回，不依赖服务端状态。
    """
    options = [
        {"text": {"tag": "plain_text", "content": label}, "value": value}
        for value, label in _FEEDBACK_REASONS.items()
    ]
    btn_common = {"qid": qid, "asker_id": asker_id}
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "想了解一下这次回答哪里需要改进，方便我们补文档 / 调 prompt："
                    ),
                },
                {
                    "tag": "form",
                    "name": "feedback_reason_form",
                    "elements": [
                        {
                            "tag": "multi_select_static",
                            "name": "reasons",
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "可多选（如过时 + 不完整）",
                            },
                            "required": True,
                            "options": options,
                        },
                        {
                            "tag": "input",
                            "name": "comment",
                            "input_type": "multiline_text",
                            "rows": 3,
                            "max_length": 500,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "可选：举例哪步错了 / 哪条步骤少了 / 哪段过时了",
                            },
                        },
                        {
                            "tag": "button",
                            "name": "submit_btn",
                            "text": {"tag": "plain_text", "content": "提交"},
                            "type": "primary",
                            "form_action_type": "submit",
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": {
                                        "action": "feedback_reason_submit",
                                        **btn_common,
                                    },
                                }
                            ],
                        },
                    ],
                },
                {
                    "tag": "button",
                    "name": "skip_btn",
                    "text": {"tag": "plain_text", "content": "跳过"},
                    "type": "default",
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "action": "feedback_reason_skip",
                                **btn_common,
                            },
                        }
                    ],
                },
            ]
        },
    }



def _append_escalate_at(
    post: dict,
    owner_id: str,
    archive_path: str,
    *,
    is_ticket: bool = False,
    is_feishu: bool = False,
) -> None:
    """在 post 末尾追加 "📣 已通知负责人 @xxx" 行（+ 普通升级时再加归档去向）。

    is_ticket=False（默认，"文档没答案"升级）：追加两行——@ + 归档去向告知。

    本地组件（is_feishu=False）：archive_path 是相对 docs_root 的路径（如
    "redis/qa-archive.md"），与紧随其后发出的归档表单卡一致。告诉 asker 答案最终
    会落到哪、下次类似问题 bot 能从哪里直接答，避免"通知完就没下文"的预期空白。

    飞书来源组件（is_feishu=True）：这类组件的知识维护在飞书文档里，bot 不会回读
    本地 qa-archive.md（见 [[project_feishu_doc_qa_integration]] 的归档回读缺口），
    所以**不能**承诺"下次我能直接从这里答"——那是假的。改成告诉 asker 答案会同步
    给他，并点明知识维护在飞书文档（第一信号源是负责人维护的飞书文档）。本地仍会
    静默写一份 archive 作留档 + 为将来万一要切"兼读本地"预热数据，但不对外宣称。

    is_ticket=True（工单类升级，"加权限/开账号"操作请求）：只 @ 不提归档；动词
    用"协助处理"而不是"协助回答"——工单 ≠ 知识答疑。archive_path 在 ticket 模式
    下被忽略，调用方可以传空串。
    """
    post["zh_cn"]["content"].append([{"tag": "text", "text": ""}])  # 空行隔开
    verb = "协助处理" if is_ticket else "协助回答"
    post["zh_cn"]["content"].append(
        [
            {"tag": "text", "text": "📣 已通知负责人 "},
            {"tag": "at", "user_id": owner_id},
            {"tag": "text", "text": f" {verb} 🙏"},
        ]
    )
    if not is_ticket:
        if is_feishu:
            line = (
                "📁 答案整理后会同步给你；这个组件的运维知识维护在飞书文档里，"
                "已请负责人补充进去。"
            )
        else:
            line = (
                f"📁 负责人填写后会归档到 {archive_path}，"
                "下次类似问题我能直接从这里答。"
            )
        post["zh_cn"]["content"].append([{"tag": "text", "text": line}])



def _archive_form_card(
    qid: str,
    question_default: str,
    owner_id: str,
    archive_path_repr: str,
    *,
    is_feishu: bool = False,
) -> dict:
    """归档表单卡（card v2 form）：可编辑问题标题 + 多行答案输入框 + 提交按钮。

    question_default：预填进"问题"输入框的标题——优先是答题那轮 LLM 给的归一化
    标题，否则是用户原话。负责人可在框里改成更通用的说法再提交；最终写盘用框里的值。
    archive_path_repr：展示给 owner 的相对路径（如 "redis/qa-archive.md"），
    让他知道答案会落到哪个文件再决定写多详细。

    is_feishu=True（飞书来源组件）：bot 不回读本地 archive，所以引导文案不提"追加进
    xxx.md / 检索关键词"那套（会误导负责人以为填表单 bot 就学会了），改成"答案会
    同步给提问者 + 请维护飞书文档"。表单照常提交、本地照常静默留档（见
    [[project_feishu_doc_qa_integration]]）。
    """
    if is_feishu:
        intro = (
            f"<at id={owner_id}></at> 下面的「问题」是系统整理的，可改成更通用的"
            "说法；把整理过的答案填进答案框，提交后会同步给提问者。"
            "这个组件的运维知识维护在飞书文档，请把答案一并补充进去。"
        )
    else:
        intro = (
            f"<at id={owner_id}></at> 下面的「问题」是系统自动整理的，"
            "可改成更通用的说法（它会作为归档标题和以后的检索关键词）；"
            "把整理过的答案填进答案框，"
            f"提交后会追加进 `{archive_path_repr}`。"
        )
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📝 问答归档"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": intro,
                },
                {
                    "tag": "form",
                    "name": "archive_form",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "question",
                            "default_value": _excerpt(question_default, 100),
                            "max_length": 120,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "归档用的问题标题（可修订）…",
                            },
                            "required": True,
                        },
                        {
                            "tag": "input",
                            "name": "answer",
                            "input_type": "multiline_text",
                            "rows": 6,
                            "max_length": 1000,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "粘贴整理后的答案文本（最多 1000 字）…",
                            },
                            "required": True,
                        },
                        {
                            "tag": "button",
                            "name": "submit_btn",
                            "text": {"tag": "plain_text", "content": "提交并归档"},
                            "type": "primary",
                            "form_action_type": "submit",
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": {
                                        "action": "archive_submit",
                                        "qid": qid,
                                    },
                                }
                            ],
                        },
                    ],
                },
            ]
        },
    }


def _archive_ack_card(icon: str, message: str) -> dict:
    """提交后用来替换原表单卡的提示卡（card v2，纯文本）。"""
    return {
        "schema": "2.0",
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"{icon} {message}"},
            ]
        },
    }


def _archive_answer_notify_post(
    asker_id: str,
    owner_id: str,
    question: str,
    answer_markdown: str,
    archive_rel: str,
    *,
    is_feishu: bool = False,
) -> dict:
    """构造"负责人答完 → 通知 asker"的 feishu post。

    asker_id 放在第一段以 @ 推送（asker 才会收到飞书侧消息提醒，不然写到归档
    文件里 asker 永远不知道有答案）；owner_id 内嵌作为"谁答的"标记。
    answer_markdown 走 markdown_to_feishu_post，保留答案原本的列表/代码块/换行
    结构。末尾补一行收尾——闭环交付。

    is_feishu=False：本地组件，告知归档路径 + 承诺"下次直接答"（agent 后续轮
    Read 本地 archive 能命中）。

    is_feishu=True：飞书来源组件，bot 不回读本地 archive（见
    [[project_feishu_doc_qa_integration]]），**不能**承诺"下次直接答"——那是
    假的。改成告诉 asker 答案已同步 + 请负责人补充进飞书文档。与
    `_append_escalate_at` / `_archive_form_card` 的 is_feishu 分支保持一致姿态。
    """
    post = markdown_to_feishu_post(answer_markdown, POST_TITLE)
    # 截短 question 防止特别长的标题撑爆头部一行；归档时已经做了 200 字上限但
    # 这里再保险一道（头部行越短越好读，详情看下面的答案 body）。
    q_short = question if len(question) <= 60 else question[:60].rstrip() + "…"
    intro_paragraph = [
        {"tag": "at", "user_id": asker_id},
        {"tag": "text", "text": f" 你之前问的「{q_short}」，"},
        {"tag": "at", "user_id": owner_id},
        {"tag": "text", "text": " 已答复 👇"},
    ]
    post["zh_cn"]["content"].insert(0, intro_paragraph)
    post["zh_cn"]["content"].append([{"tag": "text", "text": ""}])  # 空行隔开
    if is_feishu:
        tail = (
            "📁 答案已同步给你；这个组件的运维知识维护在飞书文档，"
            "已请负责人补充进去。"
        )
    else:
        tail = f"📁 已归档到 {archive_rel}，下次类似问题我能直接从这里答。"
    post["zh_cn"]["content"].append([{"tag": "text", "text": tail}])
    return post



def _fmt_db_instance(req: "DbChangeRequest") -> str:
    """卡片/通知里展示的实例标识：host:port（OceanBase 再带 租户#集群 + 模式）。"""
    base = f"{req.host}:{req.port}"
    if req.kind in ("ob_mysql", "ob_oracle") and req.tenant:
        return f"{base} · 租户 {req.tenant}#{req.cluster}（{req.db_type}/{req.mode}）"
    return f"{base}（{req.db_type}）"


def _db_change_card(
    change_id: str,
    req: "DbChangeRequest",
    asker_id: str | None,
    admin_ids: Iterable[str] | None = None,
) -> dict:
    """参数变更确认卡（card v2）：实例 / 参数现值→新值 / 待执行 SQL / 申请人 +
    「确认执行」「驳回」两个按钮。两个按钮都仅 `admin_open_ids` 名单里的人点有效
    （校验在 handler，非授权点击保持卡片可见）。所见即所执行：卡上的 SQL 就是确认
    时原样要跑的那条。

    `admin_ids` 非空时在卡顶加一行 @ 管理员（飞书 `<at id=..></at>` 渲染 @姓名、
    不暴露 open_id，且会给被 @ 的管理员推通知），让审批方知道有单子待处理。
    """
    current = req.current_value or "（未取到，请确认前自行核对）"
    admin_at = " ".join(f"<at id={a}></at>" for a in (admin_ids or []))
    notify_md = f"📣 请管理员审批：{admin_at}\n" if admin_at else ""
    asker_at = f"\n- 申请人：<at id={asker_id}></at>" if asker_id else ""
    detail_md = (
        f"{notify_md}"
        "**🛠 数据库参数变更申请**（需管理员确认后执行）\n"
        f"- 实例：{_fmt_db_instance(req)}\n"
        f"- 参数：`{req.param}`\n"
        f"- 变更：{current}  →  **{req.new_value}**"
        f"{asker_at}"
    )
    sql_md = f"待执行 SQL（管理员确认后由 bot 执行）：\n```sql\n{req.sql}\n```"
    confirm_btn = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "✅ 确认执行"},
        "type": "primary",
        "behaviors": [
            {
                "type": "callback",
                "value": {"action": "db_change_confirm", "change_id": change_id},
            }
        ],
    }
    reject_btn = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "❌ 驳回"},
        "type": "danger",
        "behaviors": [
            {
                "type": "callback",
                "value": {"action": "db_change_reject", "change_id": change_id},
            }
        ],
    }
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "数据库参数变更确认"},
            "template": "orange",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": detail_md},
                {"tag": "markdown", "content": sql_md},
                {"tag": "markdown", "content": "*仅管理员可执行；其他人点击无效。*"},
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [confirm_btn],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [reject_btn],
                        },
                    ],
                },
            ]
        },
    }


