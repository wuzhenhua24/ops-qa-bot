# 部署到 Linux 服务器（systemd 方案）

适用：长连接模式，单机部署，systemd 管控（绝大多数 Linux 发行版都自带）。
HTTP 模式部署同理，只需把 `ExecStart` 里的 `run_ws.py` 换成 `run_server.py`。

## 一次性准备

```bash
# 1. 装 uv 和 Node.js + claude CLI（非 root 即可）
curl -LsSf https://astral.sh/uv/install.sh | sh
# Node.js 18+ 装法：
#   - apt:    apt-get install -y nodejs npm
#   - 或用 nvm / fnm 装 LTS
sudo npm install -g @anthropic-ai/claude-code

# 2. 建专用运行用户（不要用 root 跑）
sudo useradd --system --create-home --shell /bin/bash ops-bot

# 3. 准备代码目录
sudo mkdir -p /opt/ops-qa-bot
sudo chown ops-bot:ops-bot /opt/ops-qa-bot
sudo -u ops-bot git clone <仓库地址> /opt/ops-qa-bot
cd /opt/ops-qa-bot
sudo -u ops-bot git checkout feat/long-connection   # 或对应分支

# 4. 装依赖（用 ops-bot 身份装到项目本地 .venv/）
sudo -u ops-bot bash -c '
    cd /opt/ops-qa-bot
    uv sync --extra ws
'

# 5. claude CLI 登录（首次需要交互；选 ANTHROPIC_API_KEY 路径就 export 后跳过）
sudo -u ops-bot bash -lc 'cd /opt/ops-qa-bot && claude'
# 走完登录流程后 Ctrl+C 退出

# 6. 准备日志目录
sudo -u ops-bot mkdir -p /opt/ops-qa-bot/logs
```

## 配置文件

把 secret 放到 `/etc/`，权限收紧到只有 ops-bot 能读：

```bash
sudo mkdir -p /etc/ops-qa-bot
sudo cp /opt/ops-qa-bot/config.example.toml /etc/ops-qa-bot/config.toml

# 编辑，至少填 feishu.app_id / feishu.app_secret
sudo vim /etc/ops-qa-bot/config.toml

# 收权限（含 app_secret，不能让其他用户看到）
sudo chown root:ops-bot /etc/ops-qa-bot/config.toml
sudo chmod 640 /etc/ops-qa-bot/config.toml
```

## 安装并启动 systemd 服务

```bash
sudo cp /opt/ops-qa-bot/deploy/ops-qa-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ops-qa-bot

# 检查
sudo systemctl status ops-qa-bot
sudo journalctl -u ops-qa-bot -f       # 实时日志，等价于 tail
```

## 常用运维操作

```bash
# 查状态
sudo systemctl status ops-qa-bot
sudo systemctl is-active ops-qa-bot      # 给监控脚本用

# 重启 / 停止
sudo systemctl restart ops-qa-bot
sudo systemctl stop ops-qa-bot

# 看日志
sudo journalctl -u ops-qa-bot -f                 # 实时
sudo journalctl -u ops-qa-bot --since "1h ago"   # 最近 1 小时
sudo journalctl -u ops-qa-bot -p err             # 仅 ERROR 级
# 业务日志（含 token 用量等结构化字段）：
sudo tail -f /opt/ops-qa-bot/logs/ops_qa_bot.log
sudo tail -f /opt/ops-qa-bot/logs/feedback.log

# 升级代码
sudo -u ops-bot bash -c '
    cd /opt/ops-qa-bot
    git pull
    uv sync --extra ws
'
sudo systemctl restart ops-qa-bot
```

## 排错

**服务起不来（`systemctl status` 显示 failed）**
- 看 `journalctl -u ops-qa-bot --no-pager` 最后 50 行
- 常见：config.toml 路径错、app_id/app_secret 没填、claude CLI 没登录

**反复重启（`StartLimit` 触发后停手）**
- `journalctl -u ops-qa-bot -p err --since "10m ago"` 找根因
- 修完之后：`sudo systemctl reset-failed ops-qa-bot && sudo systemctl restart ops-qa-bot`

**子进程没清理干净**
- 应该不会发生（unit 配了 `KillMode=mixed`），但如果看到一堆孤儿 `claude` 进程：
  `sudo systemctl stop ops-qa-bot && sleep 2 && sudo pkill -u ops-bot claude`

**升级后行为没变化**
- 改了代码忘了 restart：`sudo systemctl restart ops-qa-bot`
- 改了 unit 文件忘了 reload：`sudo systemctl daemon-reload && sudo systemctl restart ops-qa-bot`

## 路径不一致时改哪几行

如果你想换路径（比如装到 `/srv/...` 而不是 `/opt/...`），只改 `ops-qa-bot.service` 里这几个字段：

- `User=` / `Group=`（运行身份）
- `WorkingDirectory=`（项目根）
- `ExecStart=`（venv 里的 python + run_ws.py 的绝对路径 + --config 路径）
- `ReadWritePaths=`（允许写入的目录，至少要包含日志目录）

改完 `daemon-reload` + `restart` 即可。
