# Changelog

## 2026-05-29

- 导入与同步区域新增大尺寸 ZIP 拖拽上传区，支持拖入高亮、点击选择、文件名和大小显示。
- Web 控制台彻底移除左侧栏，主工作区改为全宽布局。
- 新增太阳/月亮图标式日间/夜间模式切换，显示偏好保存在浏览器本地。
- 目标配置中新增 Web 登录密码修改项，留空保存时不会覆盖现有密码。

## 2026-05-28

- Web 首页改为独立模板，并按 `重新登陆 Icloud分支` 的控制台结构复刻：深色侧栏、白色顶栏、统计卡片、账号列表和任务日志双栏布局。
- 借鉴 `重新登陆 Icloud分支` 最近提交，重构 Web 任务面板：支持恢复最近任务、刷新/聚焦自动同步任务日志、搜索防抖，以及表格中完整展示邮箱和失败原因。
- Web 控制台按管理后台风格重新设计，新增左侧导航、顶栏、分区卡片、状态徽标、表格工具栏、进度条和终端风格任务日志。
- 重写 README，补充公开仓库说明、服务器一键部署、Web 操作流程、批量队列说明和隐私文件清单。
- 新增 `.env.example`、`scripts/deploy.sh` 和 MIT License，支持服务器上用一条脚本构建、配置并重启 Docker 容器。
- Web 邮箱列表新增勾选、按当前搜索结果选择、清空选择和批量重登；批量重登使用 1 并发队列逐个执行。
- 重新登录默认优先走浏览器密码页“使用一次性验证码登录”的 passwordless 发信链路，发送失败时再回退 GPT 密码。
- 重新登录在密码验证后进入 `email_otp_verification` 时，会主动调用邮箱验证码 resend，再开始轮询邮箱。
- Web 后台任务日志接入重新登录内部步骤，显示步骤开始/完成、邮箱验证码等待和 resend 进度，减少长时间等待时“卡住”的错觉。
- 将邮箱运行期依赖从外部 `outlook_rt` API 改为项目内置 SQLite 邮箱库。
- 新增 ZIP 导入：读取 `mail/mail_accounts.txt`、`mail/gpt_passwords.txt`、`mail/phone_numbers.txt`，并兼容 CPA/Sub2API 导出文件中的远端匹配线索。
- 新增本地邮箱验证码读取：Microsoft refresh token 换取 Graph access token，Graph 失败后自动尝试 Outlook IMAP。
- 新增 CPA/Sub2API 二选一 provider 配置：
  - CPA 使用 `/v0/management/auth-files` 判断远端存在性，重登成功后沿用 OAuth callback 上传。
  - Sub2API 使用 `/api/v1/admin/accounts` 判断远端存在性，重登成功后更新已有账号或新建 OpenAI OAuth 账号。
- 新增 Flask Web 控制台：配置目标、上传 ZIP、邮箱搜索/筛选、手动全量同步、单邮箱重新登录、后台任务进度和日志。
- 新增本地状态流转：`unknown`、`normal`、`dropped`、`unavailable`，失败原因保存在 SQLite。
- Web 默认监听 `127.0.0.1`；监听非 localhost 时要求配置 `web.password`。
- 新增 Dockerfile 和 `.dockerignore`，便于部署到 Home 服务器并通过 Azure 反代暴露。
- 删除旧的 `outlook_rt.py` 外部邮箱 API 客户端，运行期不再调用外部邮箱池项目。

- 新建独立项目 `重新登录`，从 `sms_out_re` 精简为单功能重新登录工具。
- 删除注册、HeroSMS、动态代理、批量并发和冷却熔断等无关流程。
- `relogin.py` 保留邮箱登录、密码失败后一次性验证码登录、CPA OAuth callback 上传等核心链路。
- Sentinel 提取固定走本地 Playwright Chromium，不读取动态代理配置。
- token 保存兼容原项目文件名：`tokens/auth_<phone-or-email>.json` 和 `tokens/codex-<phone-or-email>.json`。
- 重新登录默认使用邮箱作为 OpenAI 登录标识；保留 `--phone-login` 参数用于回退到手机号登录。
- 邮箱登录时先尝试保存的 GPT 密码；若 OpenAI 返回 `invalid_username_or_password`，自动切换为邮箱一次性验证码登录。
- 邮箱一次性验证码登录改为使用登录页真实接口 `POST /api/accounts/passwordless/send-otp`；当 `authorize/continue` 已返回 `email_otp_verification` 时直接跳过密码步骤。
