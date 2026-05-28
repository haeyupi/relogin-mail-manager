# Relogin Mail Manager

Relogin Mail Manager 是一个本地化的 OpenAI/Codex 重新登录工具。它从你导出的邮箱资料 ZIP 中导入 Outlook/Hotmail 邮箱、邮箱 refresh token、OpenAI 密码和手机号，然后在本项目内完成邮箱验证码读取、重新登录、token 保存和可选的远端上传。

项目不依赖外部邮箱池服务。所有账号资料都保存在本地 SQLite 数据库中。

## 功能

- 从 ZIP 导入邮箱资料，保存在本地 SQLite。
- 直接读取 Outlook/Hotmail 邮箱验证码：优先 Microsoft Graph，失败后回退 IMAP。
- 重新登录 OpenAI/Codex，并保存新的 token 文件。
- 支持 CPA 或 Sub2API 作为远端目标，重登成功后自动上传。
- Web 控制台采用深色侧栏、白色顶栏、状态卡片、账号列表和任务日志双栏布局。
- Web 控制台支持搜索、状态筛选、单邮箱重登、批量重登、最近任务恢复和日志自动刷新。
- 批量重登使用队列，重登核心流程固定 1 并发，避免多个浏览器/Sentinel 会话互相影响。
- 全量同步需要手动触发，不会自动定时扫描远端。

## 不要提交隐私文件

这些文件和目录包含邮箱、密码、refresh token 或 OpenAI token，已经在 `.gitignore` 和 `.dockerignore` 中排除：

- `config.json`
- `.env`
- `data/`
- `tokens/`
- `success/`
- `fail/`

公开仓库中只应包含代码、示例配置和文档。不要把真实 ZIP、数据库、token、日志或管理密钥提交到 GitHub。

## 准备 ZIP 文件

导入 ZIP 至少需要包含：

```text
mail/mail_accounts.txt
mail/gpt_passwords.txt
mail/phone_numbers.txt
```

文件格式：

```text
mail/mail_accounts.txt: email----邮箱密码----client_id----refresh_token
mail/gpt_passwords.txt: email----OpenAI密码
mail/phone_numbers.txt: email----手机号
```

ZIP 中如果包含 `cpa/*.json` 或 `sub2api/accounts.json`，导入时会尽量补充远端名称和账号 ID，方便后续同步匹配。

## 一键部署到服务器

服务器只需要 Docker 和 Git。以 Ubuntu/Debian 为例：

```bash
sudo apt update
sudo apt install -y git docker.io
sudo systemctl enable --now docker
```

克隆仓库：

```bash
git clone https://github.com/haeyupi/relogin-mail-manager.git relogin-mail-manager
cd relogin-mail-manager
```

生成配置：

```bash
cp .env.example .env
nano .env
```

至少修改：

```env
WEB_PASSWORD=change-me-to-a-long-random-password
```

如果你要通过反向代理访问，推荐保持：

```env
BIND_IP=127.0.0.1
HOST_PORT=8787
WEB_HOST=0.0.0.0
WEB_PORT=8787
```

启动或更新容器：

```bash
bash scripts/deploy.sh
```

脚本会自动完成：

- 根据 `.env` 生成本地 `config.json`。
- 创建 `data/`、`tokens/`、`success/`、`fail/`。
- 构建 Docker 镜像。
- 删除旧容器并启动新容器。
- 挂载持久化目录，重启容器不会丢数据。

默认访问地址：

```text
http://127.0.0.1:8787/
```

如果你使用 Nginx、OpenResty、Caddy 或云服务器端口转发，把外部域名代理到服务器的 `127.0.0.1:8787` 即可。

## 配置远端目标

远端目标是可选的。不配置时，重登成功只会保存本地 token。

`.env` 中配置 CPA：

```env
TARGET_PROVIDER=cpa
TARGET_BASE_URL=https://your-cpa.example.com
TARGET_MANAGEMENT_KEY=your-management-key
```

`.env` 中配置 Sub2API：

```env
TARGET_PROVIDER=sub2api
TARGET_BASE_URL=https://your-sub2api.example.com
TARGET_MANAGEMENT_KEY=your-management-key
SUB2API_CONCURRENCY=3
SUB2API_PRIORITY=50
```

修改 `.env` 后重新运行：

```bash
bash scripts/deploy.sh
```

也可以在 Web 控制台中修改目标配置。管理密钥在页面上只会脱敏显示。

## 使用 Web 控制台

1. 打开 Web 控制台并输入 Basic Auth 密码。
2. 在 **导入与同步** 中上传原项目导出的 ZIP。
3. 在 **邮箱列表** 中搜索邮箱、手机号或失败原因。
4. 单个邮箱点击 **重登**。
5. 多个邮箱先勾选，再点击 **批量重登**。
6. 查看 **任务日志** 区域的进度条和日志；刷新页面后可以点击 **恢复最近日志**。

批量重登说明：

- 可以使用搜索框筛选后点击 **选择当前结果**。
- 可以点击表头复选框选择或取消当前结果。
- 队列固定 1 并发，会逐个处理邮箱。
- 一个邮箱失败不会中断后面的邮箱。

## 手动全量同步

点击 **全量更新** 后，程序会从当前目标 provider 拉取远端账号列表：

- 远端存在：标记为 `normal`。
- 远端不存在：标记为 `dropped`，并自动进入重登队列。
- 重登失败：标记为 `unavailable`，保存失败原因。

全量同步只会在你手动点击时运行。

## 状态含义

- `unknown`：刚导入，还没有同步或重登。
- `normal`：远端存在，或重登成功。
- `dropped`：远端不存在，准备自动修复。
- `unavailable`：重登或上传失败。

## 本地开发

安装依赖：

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

启动 Web：

```bash
python app.py serve --host 127.0.0.1 --port 8787
```

单邮箱 CLI 重登：

```bash
python relogin.py --email user@hotmail.com
```

默认登录策略会优先复现浏览器密码页的 **使用一次性验证码登录** 流程。如果发送一次性验证码失败，再回退到本地保存的 OpenAI 密码。

## 测试

运行：

```bash
python -m py_compile app.py relogin.py config_loader.py mail_reader.py mail_store.py providers.py cpa_provider.py sentinel.py
python -m unittest discover -s tests
```

## 目录结构

```text
.
├── app.py                 # Flask Web 控制台和后台任务
├── relogin.py             # OpenAI/Codex 重新登录流程
├── mail_reader.py         # Graph/IMAP 邮箱验证码读取
├── mail_store.py          # SQLite 邮箱库和 ZIP 导入
├── providers.py           # CPA/Sub2API 远端适配
├── cpa_provider.py        # CPA OAuth callback 上传
├── sentinel.py            # Sentinel token 提取
├── templates/index.html   # Web 控制台页面模板
├── scripts/deploy.sh      # 服务器一键部署脚本
├── config.example.json    # JSON 配置示例
├── .env.example           # 一键部署环境变量示例
└── tests/                 # 单元测试
```

## 安全建议

- Web 暴露到公网时必须设置强 `WEB_PASSWORD`。
- 建议把容器绑定到 `127.0.0.1`，再由反向代理提供 HTTPS。
- 定期备份 `data/` 和 `tokens/`，但不要上传到公开仓库。
- 不要在 issue、日志或截图中公开邮箱 refresh token、OpenAI token、管理密钥或真实账号密码。

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=haeyupi/relogin-mail-manager&type=Date)](https://www.star-history.com/#haeyupi/relogin-mail-manager&Date)

## 友情链接

- [Linux.do](https://linux.do/)
