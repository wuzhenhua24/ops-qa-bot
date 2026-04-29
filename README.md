# Ops QA Bot

基于 [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) 的内部运维文档问答机器人。

核心思路：让 agent 通过 `Read`/`Glob`/`Grep` 按需检索 `docs/` 下的 markdown 文档，用 `docs/INDEX.md` 作为路由表定位到对应组件目录，基于真实文档内容回答问题。

## 目录结构

```
ops-qa-bot/
├── docs/                 # 运维文档根目录（按组件划分）
│   ├── INDEX.md          # 路由表：列出每个组件目录的职责
│   ├── redis/
│   ├── mysql/
│   └── kafka/
├── ops_qa_bot/
│   ├── prompt.py         # system prompt 构造
│   ├── bot.py            # OpsQABot（ClaudeSDKClient 封装）
│   └── cli.py            # 交互式 REPL
└── run.py                # 启动入口
```

## 使用

前置：已安装 [uv](https://docs.astral.sh/uv/) 和 Claude Code CLI（`claude` 命令）。

```bash
# 同步依赖（首次运行会自动创建 .venv）
uv sync

# 启动交互式问答
uv run python run.py

# 或指定文档目录
uv run python run.py --docs /path/to/your/docs

# 隐藏 agent 的工具调用日志
uv run python run.py --hide-tools
```

启动后直接输入问题，空行或 Ctrl+C 退出。

## 飞书接入

> ⚠️ 注意：**群自定义机器人 webhook**（`open.feishu.cn/open-apis/bot/v2/hook/xxx`）是单向入站通道，只能让你把消息推进群，**收不到用户消息**，无法用于问答机器人。本项目必须走"飞书自建应用 + 事件订阅"路线。

### 两种接入模式，任选其一

| 模式 | 入口 | 适用场景 |
|------|------|----------|
| **HTTP 模式**（`run_server.py`） | 公网 HTTPS Webhook | 已有 Nginx/Ingress 基建、公网入站无审批阻力 |
| **长连接模式**（`run_ws.py`） | 只出不进，飞书 SDK WebSocket | 内网部署、不想开公网端口、本地开发/调试 |

两种模式**业务逻辑完全一致**（会话隔离、占位消息、反馈收集、日志都复用同一套），差异只在"事件怎么进来"这一层。下面先讲 HTTP 模式的飞书平台配置（通用前半段），再分别给出两种启动方式。

### 前置：在飞书开放平台配置自建应用

1. 登录 [飞书开放平台](https://open.feishu.cn/)，**创建企业自建应用**，拿到 `App ID`（对应 `FEISHU_APP_ID`）和 `App Secret`（对应 `FEISHU_APP_SECRET`）。
2. **应用功能 → 机器人**：开启机器人能力。
3. **权限管理** 至少开启以下权限：
   - `im:message`（接收/读取/发送/**更新**消息——"占位→最终答案"的编辑操作也走这个权限）
   - `im:message.group_at_msg`（接收群组 @ 消息）
   - `im:message:send_as_bot`（以机器人身份发消息，包含 interactive 卡片）

   > 注：权限名称以飞书开放平台实际展示为准。如果服务启动后编辑/发送消息报权限不足错误，根据返回的 `code` 和 `msg` 对照 [权限总览](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/permission-list) 补上即可。
4. **事件与回调 → 事件订阅**：
   - 请求方式选 **HTTP**，请求地址填 `https://<your-host>/feishu/webhook`
   - 保存时飞书会打一次 `url_verification` challenge，本服务会自动回 `challenge`，一次通过
   - "Verification Token" 就是代码里的 `FEISHU_VERIFY_TOKEN`（可选，配置后强校验来源）
   - **Encrypt Key**（强烈推荐公网部署时启用）：填入配置到 `feishu.encrypt_key`。启用后飞书会对 payload 做 AES 加密，并在请求头带签名，服务端会自动解密并校验 `X-Lark-Signature`（SHA256）。篡改或伪造的请求会被 401 拒绝。
5. **事件订阅 → 添加事件**：订阅 `im.message.receive_v1`（接收消息 v2.0）。
6. **功能 → 机器人 → 消息卡片请求网址**：填 `https://<your-host>/feishu/card`。这是反馈按钮点击回调的地址（与上面的事件 webhook 是两个独立 URL），首次保存时同样会做 `url_verification` 校验。如果卡片回调的 Verification Token 与事件订阅**不同**，通过环境变量 `FEISHU_CARD_VERIFY_TOKEN` 单独配置；默认会复用 `FEISHU_VERIFY_TOKEN`。
7. **版本管理与发布**：创建版本 → 提交发布 → 等企业管理员审批通过。
8. 审批通过后，**在群里添加这个机器人**，群成员 `@机器人 问题` 即可触发。

### 启动服务

```bash
# 1. 装上 server 依赖（fastapi / uvicorn / httpx / cachetools）
uv sync --extra server

# 2. 复制配置模板并按需填写（config.toml 已被 .gitignore）
cp config.example.toml config.toml
# 编辑 config.toml：至少要填 feishu.app_id 和 feishu.app_secret

# 3. 启动
uv run python run_server.py                      # 默认读 ./config.toml
uv run python run_server.py --config /etc/ops-qa-bot/config.toml
```

**配置方式**：统一走 `config.toml`，结构见 `config.example.toml`。所有字段都可以通过**同名环境变量覆盖**（优先级：环境变量 > 配置文件 > 默认值），方便 `app_secret` 这类敏感值走 secret manager 注入而不落文件：

```bash
# 举例：配置文件里只写非敏感字段，secret 从环境变量注入
FEISHU_APP_SECRET=$(vault read -field=secret ops/feishu) \
ADMIN_TOKEN=$(vault read -field=token ops/admin) \
uv run python run_server.py
```

服务默认监听 `0.0.0.0:8000`。生产环境请用 Nginx / Caddy 反向代理加 TLS，并在飞书开放平台的"事件订阅"页配置飞书出口 **IP 白名单**限制来源。

### 启动服务（长连接模式）

```bash
# 1. 只需要 ws 这个 extra，比 server 少装 fastapi/uvicorn/cryptography 等
uv sync --extra ws

# 2. 配置文件里只填 app_id / app_secret 即可，其他字段（encrypt_key、
#    verify_token、HTTPS 入站相关）都用不上
cp config.example.toml config.toml

# 3. 启动
uv run python run_ws.py
```

**飞书开放平台配置差异**（切换到长连接模式时）：
- "**事件与回调 → 事件订阅**"：订阅方式选 **长连接**（不用填 Request URL）
- "**消息卡片 → 接收方式**"：选 **事件订阅**（对应 `card.action.trigger` 新版事件，通过同一条长连接推回）
- 订阅事件：`im.message.receive_v1` + `card.action.trigger`
- 不需要配 `encrypt_key` / `verify_token`（SDK 自己管鉴权）
- 不需要配公网 HTTPS 地址、不需要 IP 白名单

长连接模式只需要**出站 443 能访问 `open.feishu.cn`** 即可，入站无端口暴露。审核流程和安全评估通常比开公网容易很多。

### 运维接口

| 接口 | 说明 |
|------|------|
| `GET /healthz` | 健康检查，顺带返回当前活跃 session 数 |
| `GET /admin/sessions` | 列出所有活跃会话（chat_id / user_id / last_used / idle_seconds），按空闲时长升序 |

`/admin/sessions` 在未设置 `ADMIN_TOKEN` 时开放（适合内网部署）；设置后需要带 `X-Admin-Token: <token>` 请求头或 `?token=<token>` 查询参数：

```bash
curl http://localhost:8000/admin/sessions -H "X-Admin-Token: xxxxxxxx"
# {
#   "count": 2,
#   "idle_ttl_seconds": 1800.0,
#   "sessions": [
#     {"chat_id": "oc_xxx", "user_id": "ou_alice", "last_used": "2026-04-23 23:50:47", "idle_seconds": 10.0},
#     {"chat_id": "oc_xxx", "user_id": "ou_bob",   "last_used": "2026-04-23 23:48:57", "idle_seconds": 120.0}
#   ]
# }
```

### 设计要点

- **按 `(chat_id, user_id)` 隔离会话**：同一群里每个用户的对话上下文互不干扰，A 追问只带 A 自己的历史，B 的提问不会污染 A 的 context。
- **空闲回收**：会话空闲超 `SESSION_IDLE_TTL`（默认 30 分钟）自动关闭，释放 subprocess。
- **手动重置**：用户发 `/reset`、`/new`、`新对话`、`重置` 任一关键词即可清空自己的上下文开新会话，不影响别人。
- **@ 提问者**：回复消息开头会 `@` 对应用户，群里多人并行提问时一眼看出归属。
- **找不到答案 @ 负责人**：bot 在文档里查不到时，会按 `INDEX.md` 里登记的组件 `open_id` 自动 @ 对应负责人协助。同一 (群, 负责人) 30 分钟内只 @ 一次，防止刷屏。配置方式：在 `docs/INDEX.md` 的"组件目录"表里加一列 `open_id`，对应飞书用户的 `ou_xxxxxxxx`。
- **问答留档**：被升级到负责人的问题，bot 会同时发一张表单卡片。负责人在群里正常作答给提问者看到的同时，把整理过的答案填进卡片提交，bot 自动追加进 `docs/<component>/qa-archive.md`（按 `INDEX.md` 反查组件目录，查不到落到 `docs/qa-archive.md`）。下次再有人问类似问题，RAG 会从这里找到答案，文档库自然滚雪球。每条带 `qid` 字段，可通过 `logs/feedback.log` 里 `event=archive` 的记录追溯。
- **健康检查**：HTTP 模式自带 `/healthz` 和 `/admin/sessions`；长连接模式额外启动一个本地小 HTTP 服务（默认 `127.0.0.1:8001`）暴露 `/healthz` / `/readyz` / `/admin/sessions`，方便接 Prometheus、k8s 探针、内网监控脚本。`/readyz` 检查 WS 客户端线程是否还在跑，挂了返回 503；事件计数 / 上次事件时间作为观测字段返回，不参与 ready 判定。详见 `deploy/README.md`。
- **占位消息**：收到提问后**立即**发送 `🔍 正在翻文档，请稍候...` 作为占位，答案生成完后通过飞书编辑消息 API（`PUT /im/v1/messages/{mid}`）把占位替换成最终答案。用户立刻感知 bot 已接到、不会以为 @ 掉了。编辑失败时自动兜底发新消息。
- **反馈收集**：答案后紧跟一条 interactive 卡片，带 👍 / 👎 两个按钮。用户点击 → 飞书回调 `/feishu/card` → 服务侧记录 + 返回新卡片替换按钮（防重复点击）。问答和反馈都落在 `logs/feedback.log`，每行 JSON，用 `qid` 关联：

  ```
  2026-04-24 ... {"event": "qa", "qid": "abc123", "chat_id": "...", "user_id": "...", "question": "...", "answer_excerpt": "..."}
  2026-04-24 ... {"event": "feedback", "qid": "abc123", "rating": "up", "clicker_id": "...", "asker_id": "..."}
  2026-04-24 ... {"event": "archive", "qid": "abc123", "owner_id": "...", "asker_id": "...", "path": "redis/qa-archive.md", "answer_excerpt": "...", "duplicate": false}
  ```

  离线 `grep` / `jq` 即可统计满意率、定位被踩问题用于迭代 prompt 或补文档。
- Webhook 立即返回 200，实际问答在后台跑完后通过飞书 API 主动推回（飞书要求 3 秒内响应）。
- 回复使用飞书 `post` 富文本消息：markdown 的标题、粗体/斜体、链接、列表、代码块会被转成对应结构化元素渲染（详见 `ops_qa_bot/feishu_format.py`）。
- 工具调用（agent 读了哪些文档）、成本、异常堆栈**只写日志**不发给用户，方便排查 bot 是否路由正确。
- 日志默认滚动写入 `./logs/ops_qa_bot.log`（单文件 10MB，保留 5 份），同时输出到 stdout。

### 反馈日志分析

`logs/feedback.log` 每行是 `时间戳 + JSON`。用 `sed 's/^[^{]*//'` 去掉时间戳前缀后就能喂给 `jq`。

**`qa` 事件的字段**：

```json
{
  "event": "qa",
  "qid": "abc123",
  "chat_id": "oc_xxx",
  "user_id": "ou_xxx",
  "question": "...",
  "answer_excerpt": "...",
  "cost_usd": 0.0123,                    // SDK 给的官方价估算（订阅模式下是参考值）
  "usage": {
    "input_tokens": 1234,                // 净输入（不含缓存命中）
    "output_tokens": 567,                // 输出
    "cache_read_input_tokens": 8901,     // 缓存命中（约 1/10 价）
    "cache_creation_input_tokens": 23    // 写缓存（比正价贵 25% 左右）
  },
  "num_turns": 4,                        // 几轮 tool use 才答完
  "duration_ms": 8500,                   // 总耗时
  "duration_api_ms": 7200                // 模型 API 实际耗时
}
```

```bash
# 1. 查看所有被 👎 的问答（自动按 qid 关联回原题）
grep -F '"rating": "down"' logs/feedback.log \
  | sed 's/^[^{]*//' \
  | jq -r '.qid' \
  | while read qid; do
      echo "=== $qid ==="
      grep -F "\"qid\": \"$qid\"" logs/feedback.log \
        | grep -F '"event": "qa"' \
        | sed 's/^[^{]*//' \
        | jq -r '"Q: \(.question)\nA: \(.answer_excerpt)"'
    done

# 2. 总体满意率
grep -F '"event": "feedback"' logs/feedback.log \
  | sed 's/^[^{]*//' \
  | jq -r '.rating' \
  | sort | uniq -c
# 示例输出：
#    38 up
#     7 down

# 3. 按用户拆分反馈（找出哪些人常给差评 → 针对性沟通）
grep -F '"event": "feedback"' logs/feedback.log \
  | sed 's/^[^{]*//' \
  | jq -r '[.asker_id, .rating] | @tsv' \
  | sort | uniq -c | sort -rn
```

拿到高频被踩的问题后，对照检查对应组件的文档是否缺内容、`INDEX.md` 路由表是否有歧义、`prompt.py` 的 system prompt 是否需要加 few-shot 示例——这就是"反馈驱动优化"的闭环。

**按自定单价算实际成本**（对接第三方 Claude 兼容代理时尤其有用）：

```bash
# 假设单价（每 1M tokens 的美元数）：
#   input         = $3
#   output        = $15
#   cache_read    = $0.3
#   cache_create  = $3.75
# 这只是占位值，按你实际的代理/模型单价改

grep -F '"event": "qa"' logs/feedback.log \
  | sed 's/^[^{]*//' \
  | jq -r '
      .usage as $u
      | (
          ($u.input_tokens // 0)               * 3      / 1000000 +
          ($u.output_tokens // 0)              * 15     / 1000000 +
          ($u.cache_read_input_tokens // 0)    * 0.3    / 1000000 +
          ($u.cache_creation_input_tokens // 0)* 3.75   / 1000000
        ) as $cost
      | "\(.qid)  $\($cost | tostring | .[0:8])  in=\($u.input_tokens // 0) out=\($u.output_tokens // 0) cache_r=\($u.cache_read_input_tokens // 0)"
    '
```

聚合每天总开销：

```bash
grep -F '"event": "qa"' logs/feedback.log \
  | awk '{print substr($1,1,10), $0}' \
  | sed 's/^\([^ ]*\) [^{]*/\1\t/' \
  | awk -F'\t' '{print $1, $2}' \
  | while read date rest; do
      echo "$rest" | jq --arg d "$date" '
        .usage as $u
        | (
            ($u.input_tokens // 0)*3 + ($u.output_tokens // 0)*15
            + ($u.cache_read_input_tokens // 0)*0.3
            + ($u.cache_creation_input_tokens // 0)*3.75
          )/1000000 as $c
        | [$d, $c, .qid] | @tsv'
    done | awk '{day[$1]+=$2; n[$1]++} END {for (d in day) printf "%s  $%.4f  (%d 次)\n", d, day[d], n[d]}'
```

### 安全

三层防御，按安全强度递增：

1. **IP 白名单**（基础）：飞书开放平台"事件订阅"页配置，只放飞书出口 IP 段。
2. **Verification Token**（轻量）：`feishu.verify_token` + `feishu.card_verify_token`，飞书在 payload 里带的 token 字符串比对。防不了链路中间人。
3. **Encrypt Key**（推荐）：`feishu.encrypt_key`。启用后 payload AES-256-CBC 加密，请求头带 SHA256 签名，服务端在 `ops_qa_bot/feishu_crypto.py` 里做解密 + 签名校验。可以彻底防伪造/篡改，等价于飞书官方 SDK 的保护级别。公网部署建议开启。

另外：

- `/admin/*` 接口生产环境请务必设置 `ADMIN_TOKEN`，或通过反向代理限制内网访问。
- 签名校验用 `hmac.compare_digest` 做常量时间比较，防 timing attack。

## 部署到服务器（生产）

不要用 `nohup &` 跑长期服务——崩溃不会自动重启、`kill` 不走优雅停机会留下孤儿 claude 子进程、机器重启后 bot 也不会自动起来。**用 systemd**（Linux 服务器自带）能一次性解决这些问题。

完整步骤、systemd unit 文件和常用运维操作见 [`deploy/README.md`](deploy/README.md)，核心要点：

- 专用 `ops-bot` 系统用户，非 root 运行
- 项目装在 `/opt/ops-qa-bot`，配置文件 `/etc/ops-qa-bot/config.toml`（权限 `640`，含 secret 不能被其他用户读）
- `Restart=on-failure` + `StartLimitBurst=5` 自动重启不雪崩
- `KillMode=mixed` 优雅停机时把 claude CLI 子进程一并回收
- `journalctl -u ops-qa-bot -f` 看实时日志，`systemctl restart` 升级

```bash
# 一行总结：
sudo cp deploy/ops-qa-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ops-qa-bot
```

## 扩展

- **新增组件文档**：在 `docs/` 下新建组件目录，写 markdown，然后在 `docs/INDEX.md` 加一行即可。无需改代码。
- **换文档根目录**：`uv run python run.py --docs /path/to/your/docs`。
- **对接 Slack / 企业微信 / Web**：复用 `OpsQABot.answer()` 方法（一次性返回完整文本），仿照 `feishu_server.py` 包一层接入层即可。

### 文档格式要求

`docs/` 下**必须是 markdown**（`.md`）。Agent 用的 `Read`/`Glob`/`Grep` 工具对 `.docx`/`.xlsx`/`.pptx` 当二进制处理（读出乱码），对 PDF 虽能读但 `Grep` 无法跨文件搜内容，会导致关键词路由失效。

如果源文档是 Word / PDF / PPT 等格式，**先手工转成 markdown 再放进 `docs/`**。推荐用微软的 [markitdown](https://github.com/microsoft/markitdown)，一个工具覆盖 docx / xlsx / pptx / pdf / 图像(OCR) / html：

```bash
# 通过 uvx 临时拉起，不污染环境
uvx markitdown path/to/runbook.docx > docs/redis/troubleshooting.md
uvx markitdown path/to/cluster.pdf  > docs/kafka/operations.md
```

转完人工过一眼（尤其表格、公式、代码块可能错位），再在 `docs/INDEX.md` 加条目即可。如果文档量后续变大，再考虑把转换步骤脚本化。
