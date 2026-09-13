# SJTUClaw 详细设计与使用手册

本手册介绍本地 Agent 的配置、运行机制、工具、数据维护和可选接入。
首次安装与快速启动见 [README](../README.md)，发布范围见
[GitHub 发布说明](GITHUB_PUBLISHING.md)。

## 模块导航

- `runtime.py`、`llm_client.py`、`tool_protocol.py`：模型调用、事件流与工具协议；
- `session_store.py`、`context_builder.py`、`memory_store.py`、`compaction.py`：上下文与持久化；
- `goal_state.py`、`task_planner.py`、`execution_evidence.py`：任务计划与执行证据；
- `tools.py`、`advanced_tools.py`、`approval_store.py`：工具与审批；
- `gateway.py`、`channels/`、`web/`、`desktop_pet/`：服务与交互入口；
- `test/`：按功能组织的回归测试；`scripts/check.py`：隔离自检；

## QQ 与微信通道（文本 MVP）

两个通道都复用现有 `ChannelService`、Session 映射、事件去重、Memory、Tool 与 Approval，不会创建另一套 Agent Runtime。

QQ 使用腾讯官方 `qqbot-agent-sdk` 的 WebSocket Gateway，不需要公网回调地址：

1. 在 [QQ 开放平台](https://q.qq.com/) 创建机器人并取得 AppID、AppSecret。
2. 在 `.env` 填写 `QQBOT_APP_ID` 与 `QQBOT_CLIENT_SECRET`。

QQ 回复优先通过官方 Bot API 的 Markdown 消息发送，标题、强调、链接、代码和表格由
支持该能力的客户端原生渲染；若平台拒绝某条 Markdown 消息，Gateway 会自动重试一次
纯文本版本，并把表格按“主项 + 字段：值”展开，避免直接显示竖线和分隔行。发送前仍会
剥离模型偶发输出的 `type=final` JSON 协议外壳。QQ 首次发送会保留原始 LaTeX，让支持
公式的客户端直接排版；只有进入纯文本兜底时才转换为 Unicode 可读公式。
3. 重启 `python gateway.py`。私聊、群聊和频道文本消息会映射到彼此隔离的 SJTUClaw Session。

微信使用腾讯 `@tencent-weixin/openclaw-weixin` 所公开的 iLink Bot 协议设计，采用扫码授权和长轮询，同样不需要公网地址。首次连接运行：

```powershell
python -m channels.weixin_login_cli
```

使用手机微信扫码并确认后，凭证只保存在本机 `data/weixin-account.json`；重启 Gateway 即开始接收微信私聊。也可调用 `POST /api/channels/weixin/login/start` 和 `POST /api/channels/weixin/login/{sessionId}/poll` 自行制作扫码 UI。当前微信 MVP 仅处理私聊文本；出站回复会保留客户端可识别的常用 Markdown，群聊、图片、语音和文件尚未接入。

`GET /api/health` 中的 `qqbot`、`weixin` 字段可以查看配置、运行、连接和错误状态。

## 飞书机器人与桌宠（MVP）

飞书和桌宠都复用同一个 Gateway、Agent Runtime、Tool、Memory 与 Approval 系统，不会各自维护一套 Agent。外部会话映射与事件去重记录统一保存在 `data/state.sqlite3`；飞书消息不会切换 Web/CLI 当前 Session。

飞书默认使用官方 SDK 的 WebSocket 长连接，不需要公网 IP、域名、内网穿透或 Verification Token。开放平台配置：

1. 创建企业自建应用，启用机器人能力，并订阅 `im.message.receive_v1` 事件。
2. 在“事件与回调”中选择“使用长连接接收事件”。
3. 在 `.env` 配置 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 和 `FEISHU_CONNECTION_MODE=websocket`，再重启 Gateway。
4. 飞书支持文本、文件/图片等消息附件，QQ 支持 SDK 可下载的消息附件；附件统一进入当前 Session 的 Attachment Store。飞书聊天里直接发送 `wiki`、`docx`、`docs` 或 `doc` 链接时，Gateway 会把云文档解析为文本附件后再交给 `read_attachment`，读取失败会直接返回权限/链接错误，不让模型猜测内容。需要审批时，飞书会发送带“批准执行/拒绝”按钮的原生交互卡片，也可在原渠道发送 `/approve <approvalId>` 或 `/reject <approvalId> [原因]` 作为兜底；卡片回调仍复用同一 ApprovalStore、权限校验和幂等流程。
5. Scheduler 会沿用 Session 最近一次出现的飞书、QQ 或微信路由主动推送执行结果；任务触发审批时只推送等待审批通知，不会同时误报执行失败。

`GET /api/health` 的 `feishu` 字段可查看是否已配置及长连接线程是否运行。原 Webhook 路由仍作为兼容选项保留；只有将 `FEISHU_CONNECTION_MODE=webhook` 时才需要配置公网回调地址与 `FEISHU_VERIFICATION_TOKEN` 或 `FEISHU_ENCRYPT_KEY`。配置 `FEISHU_ENCRYPT_KEY` 后，Gateway 会校验 `X-Lark-Request-Timestamp`、`X-Lark-Request-Nonce`、`X-Lark-Signature`，拒绝签名不匹配、超出 `FEISHU_EVENT_MAX_AGE_SECONDS`（默认 300 秒）的请求，并按 AES-256-CBC/PKCS7 解密事件；解密依赖由 `requirements.txt` 提供。重复事件仍由持久化 EventDeduplicator 幂等拦截。长连接模式不需要这些 Webhook 配置。
飞书回复默认使用文本消息；当回复包含标题、列表、代码、链接或表格等 Markdown 时，Gateway 会自动改用官方 interactive card 的 `markdown` 元素发送，手机端和桌面端均可直接渲染，不需要额外安装插件。包含 GFM 表格时默认使用 JSON 2.0 卡片并保留原始 Markdown，飞书新版客户端会渲染为网格表格；如需兼容不支持 JSON 2.0 的旧客户端，可在 `.env` 设置 `FEISHU_CARD_VERSION=legacy`，此时表格会降级为“表头 + 字段行”。

飞书和微信目前没有与 Web 端 KaTeX 等价且稳定的原生 LaTeX 组件，因此发送层会把常见公式降级为可读 Unicode/文本形式，例如 `x²`、`θ`、`√(...)`、`(a)/(b)` 和 `≤`，同时保留平台能够原生显示的 Markdown。QQ 优先保留原始 LaTeX 交给原生 Markdown 渲染器，失败后的纯文本重试才使用相同降级；复杂矩阵、分段公式或严格排版仍以 Web 端最稳定。

飞书云文档读取需要额外配置权限：在开放平台为应用开通 `wiki:node:read`（查看知识空间节点信息；旧后台可选 `wiki:wiki:readonly`）、`docx:document:readonly`（查看新版文档）和 `im:resource`（获取消息资源）等只读能力；若还要兼容旧版 `/docs` 或 `/doc`，再开 `docs:doc:readonly`/“查看云文档内容”。同时把目标文档或知识库授权给应用，并发布权限变更后的应用版本。Wiki URL 中的 token 是知识库节点 token，Gateway 会先调用 Wiki 节点接口换取实际文档 token，再读取正文；没有权限时会明确返回飞书 API 的权限错误。普通聊天文件仍使用 `im/v1/messages/{message_id}/resources/{file_key}` 下载。

桌宠使用 Electron 提供透明置顶窗口、托盘、状态动画和简洁输入气泡。先启动 Gateway，再另开终端运行：

```powershell
cd desktop_pet
npm install
npm start
```

默认连接 `http://127.0.0.1:8000`。如 Gateway 位于其他地址，可先设置 `SJTUCLAW_GATEWAY` 环境变量。桌宠拥有独立 Session，并通过现有 SSE 接口显示思考、工具调用和流式回复状态。Gateway 暂时不可用时，桌宠会自动重试连接。

桌宠还提供可重复的 Windows 发布脚本（不影响上面的开发启动方式）：

```powershell
cd desktop_pet
npm run check           # 语法检查
npm run dist:dir        # 未安装目录，快速验收
npm run dist:portable   # 便携版 exe
npm run dist            # NSIS 安装包
```

发布产物写入 `desktop_pet/dist/`，安装器默认允许修改安装目录，卸载时不删除用户数据。桌宠主进程每 5 秒探测 Gateway 的 `/api/health`；检测到 Gateway 重启并恢复后会自动重新加载桌宠页面。

桌宠托盘菜单提供可选的“开机启动”（默认关闭）；桌宠不在前台时，回复完成会发送系统通知。渲染进程异常退出会自动重载，启动器也会对非正常退出进行有限重连，不会改变用户现有的 `npm start` 启动方式。

桌宠交互：平时显示项目原创的“打工喵”本体，拖动猫咪即可移动；单击会从招手、伸懒腰、思考、庆祝、摸摸、蹦跶、探头、转圈、摇尾巴、击掌、打卡、鞠躬、受惊弹跳、点头、节拍摇摆、短冲刺、爱心律动和歪头观察等 18 种动作中随机反应，并配合对应粒子效果，双击进入聊天。待命时会有轻微呼吸动效，连续 20 秒无操作后进入小睡；模型思考、调用工具、回复、离线和错误均有独立状态。开始交互或发送消息后会自动恢复待命计时。右键猫咪可以直接关闭，或最小化成带拖动、展开和关闭按钮的控制条。展开状态下拖动顶部标题栏即可移动，输入框、按钮、消息内容和链接保持原本交互；窗口会限制在屏幕工作区内，避免拖到顶部或边缘后无法找回。聊天框支持从左右边、底边或四个角缩放，内容区会自适应，且会记住上次使用的聊天尺寸；顶部中段只用于移动，两个顶角保留缩放能力，收起后恢复紧凑的桌宠尺寸。缩放期间允许光标短暂停顿后继续微调，不会立即丢失跟随。`−` 收起聊天气泡，`×` 隐藏到系统托盘。

桌宠输入框使用聊天软件常见快捷键：`Enter` 发送，`Ctrl+Enter`（macOS 为 `⌘+Enter`）换行，`Shift+Enter` 也可换行；中文输入法组合期间不会拦截回车。

各入口共享 LLM 调用、Session、Memory、Scheduler、Workspace、Approval 和 Skill System。

## 运行

CLI 启动时会显示与网页侧栏相同的每日欢迎语；Agent Turn 通过事件流增量输出 assistant 正文，并用分割线和 🦞 标识区分每轮回答。Tool 调用、结果、审批等待和自动 Compaction 会以简洁状态行即时显示，避免把第三方 API 的大段 JSON 淹没对话。交互式终端会根据能力启用低饱和 ANSI 主题；Windows 旧版控制台、`TERM=dumb`、`NO_COLOR` 或重定向输出会自动降级为纯文本/ASCII，不影响内容。非事件流适配器仍优先使用 Rich 渲染 Markdown；若运行环境未安装 Rich，也会使用内置终端渲染器整理常用 Markdown。需要检查原始 Tool 输入/输出时可使用 `python main.py --verbose-tools`。

输入 `/help` 可查看分组命令；`/history [条数]` 只展示当前 Session 最近的用户与最终回答，不泄露 Tool/Approval 协议记录；`/paste` 可输入保留换行的多行内容，以单独一行 `.` 结束；`/clear` 清理当前终端视图。支持 `readline` 的交互终端还会自动启用本进程内的历史浏览与 `/` 命令 Tab 补全，不写入额外历史文件。拼错或未知的 `/命令` 会在本地拦截，不会误发给模型。

网页顶栏和 CLI `/model` 命令会列出当前 OpenAI-compatible API 配置下可用的模型。使用项目默认的 SJTU API 地址时，会提供 DS-V4 常规、DS-V4 思考、MiniMax-M2.7 与 Qwen3.6-27B；使用自己的 API 地址时，默认只显示 `LLM_MODEL` 指定的模型，不再错误地展示交大专属选项。CLI 使用 `/model use <model-id>` 完成切换；选择结果持久化在本地 SQLite，Gateway/CLI 下次启动会继续使用。

如果同一个 API 地址和 API Key 可以调用多个模型，可通过 `LLM_MODELS=model-a,model-b` 快速配置，或用 `LLM_MODELS_JSON` 设置模型 ID、显示名称、短名称、模式、能力说明、上下文长度和视觉能力。只有一个可用模型时，网页会自动隐藏没有实际用途的切换器。模型菜单只接收经过筛选的公开元数据，API Key 始终保留在 Gateway 后端，不会发送到浏览器。模型切换目前限定在同一 API 地址与凭据下；修改 `.env` 后需重启 Gateway。每个模型的原生 Tool、视觉、流式与 JSON 协议能力仍按模型分别记录；网页在当前 Turn 尚未结束时会拒绝切换，避免一次回复混用两套模型状态。

推荐使用统一启动器（它只依赖 Python 标准库，因此缺依赖时也能正常诊断）：

```powershell
# 检查 Python、API Key、网页和桌宠环境
python sjtuclaw_launcher.py doctor --pet

# 首次安装 Python 与桌宠依赖
python sjtuclaw_launcher.py install

# 启动 Gateway 并打开网页
python sjtuclaw_launcher.py start

# 同时启动桌宠
python sjtuclaw_launcher.py start --pet
```

Windows 也可以直接双击 `install.cmd` 完成首次安装，之后双击 `start.cmd` 启动网页与桌宠。如果 8000 端口已有健康的 Gateway，启动器会复用它，不会再创建重复进程。

传统方式仍然保留：复制 `.env.example` 为 `.env` 并填写 `LLM_API_KEY`，安装 `requirements.txt` 后运行 `python main.py`（CLI）或 `python gateway.py`（网页）。若只想测试一次模型调用，可运行 `python main.py --once`，程序会发送一条默认消息并打印 assistant 回复。

### 自检与数据维护

安装 `requirements.txt` 后，运行项目自检：

```powershell
python scripts/check.py             # 环境、Gateway、核心回归和前端语法
python scripts/check.py --full      # 完整 unittest
python scripts/check.py --browser   # 额外运行真实浏览器 E2E
```

自检使用临时数据目录和占位 Key，不调用真实模型。浏览器测试需要
`requirements-dev.txt` 中的 Playwright 及 Chromium，安装步骤见 README。

运行数据可独立备份、校验和恢复；备份使用 SQLite 一致性快照并附带 SHA-256
清单，默认写入已被 Git 忽略的 `backups/`：

```powershell
python scripts\maintenance.py backup --keep 5
python scripts\maintenance.py verify
python scripts\maintenance.py verify backups\<备份文件>.zip

# 恢复前必须停止 Gateway；程序还会先生成恢复前安全快照
python scripts\maintenance.py restore backups\<备份文件>.zip

# 生成不含 API Key、会话正文和附件正文的脱敏诊断包
python scripts\maintenance.py diagnostic

# 只读预览可清理的旧 Turn/Trace 与已完成 Tool 记录
python scripts\maintenance.py cleanup-history --older-than-days 30 --keep-turns-per-session 100

# 关闭 Gateway 并确认预览后才执行；执行前自动创建 SQLite 安全快照
python scripts\maintenance.py cleanup-history --older-than-days 30 --keep-turns-per-session 100 --apply
```

历史清理默认只做预览，不会修改数据。它只处理超过保留期且超出每个 Session
最低保留数量的终态 Turn、对应 Trace，以及过期的已完成 Tool 幂等记录；会话正文、
Summary、附件、Memory、Scheduler、Approval、渠道投递状态和任何运行中记录均不在
清理范围内。

### 安全边界

- Gateway 默认只接受本机回环地址的请求；不要把 `SJTUCLAW_ALLOW_REMOTE` 改为 `true`，除非它位于可信反向代理和访问控制之后。
- 浏览器的写操作执行同源校验，响应包含 CSP、禁止嵌入、MIME 嗅探保护等安全头。飞书 Webhook 是远程访问的唯一例外，并且必须配置 `FEISHU_VERIFICATION_TOKEN`；默认长连接模式不需要开放端口。
- `.env`、运行数据和渠道凭证已被 `.gitignore` 排除。健康检查会脱敏渠道错误，未捕获异常只写入 Gateway 日志，不向网页或外部渠道返回内部细节。
- 文件读写被限制在当前 Session 的 Workspace；写文件、附件复制和 Shell 操作必须人工审批。附件上限为 10 MB，Office/PDF/OCR 解析另有解压体积、页数、像素和输出长度限制。
- 如果 API Key 曾经出现在聊天记录、截图、提交历史或公开日志中，应立即去对应平台轮换；仅从本地 `.env` 注入新 Key。

CLI 对空输入直接忽略；`/exit`、`/quit` 和 Ctrl+C 会友好退出或中断本轮。若 VS Code 在切换解释器时把 `conda.EXE activate ...` 注入正在运行的输入循环，CLI 会识别并忽略它，不会误交给 Agent 当作 Shell 任务。LLM 调用失败以及 `None`、空字符串、纯空白 assistant 回复都不会追加 user/assistant messages，用户可以继续下一轮输入。失败会作为 `turn_failed` Activity 留在 Timeline 中用于审计，但不会进入模型的 conversation context。

CLI 启动帮助按“会话/状态、上下文操作、附件/诊断”分组显示；Tool 执行也使用独立活动块，避免把系统状态、审批提示和模型正文挤在同一行。

连续输入消息即可对话，使用 `/exit` 或 `/quit` 退出。Session 数据保存在 `data/sessions/`，程序重启后会恢复当前会话。

网页与 CLI 的 Session 消息数均按用户可见的 user/assistant 消息统计；Tool Call、Tool Result、Approval 和协议重试等内部记录仍会保留在 Session 与 Tool Trace 中用于恢复、压缩和审计，但不会把对话条数虚增。通道消息在模型失败、暂停或仍处理中时可能暂时只有 user 没有 assistant，因此网页会同时显示“问/答”分项和待处理提示，而不会错误地删掉这条真实消息。

## Session 命令

```text
/session new [标题]
/session list
/session show [sessionId]
/session switch <sessionId>
/session rename <sessionId> <标题>
/session delete <sessionId>
```

`/session show` 不带参数时查看当前会话，带 `sessionId` 时查看指定会话的基本信息和最近消息；这些内部命令由 CLI/Runtime 处理，不会作为普通用户消息发送给模型。

CLI 还提供几个轻量交互命令：`/context`（查看当前 Session 的消息、Summary 和 Workspace 快照）、`/goal`（查看当前任务目标、验收条件和进度）、`/retry` 或 `/regenerate`（基于最近一条可见用户消息重新生成，自动丢弃原回答后的历史）、`/stop`（说明当前停止方式）。模型生成期间直接按 `Ctrl+C` 会中断本轮且不写入半截 assistant；在输入提示处按 `Ctrl+C` 只取消当前输入，不会退出 CLI。

附件与导出命令：`/attachment list`、`/attachment show <attachmentId>` 查看当前 Session 已上传附件；`/export` 默认将可见对话导出到 `data/exports/<sessionId>.md`（例如 `session_9.md`），也支持 `/export json [相对路径]` 导出完整 Session JSON。相对导出路径限制在 `data/exports/`，绝对路径只有在当前 Workspace 内才允许。开发者排查性能时仍可使用 `/metrics` 或启动时加 `--metrics`，查看模型调用、Tool 数量、耗时和 Token 估算；它不再出现在普通启动帮助中。

## Stable Context 与 Memory

Memory、候选记忆、Approval、Scheduler Task、渠道会话映射和事件去重统一存储在
`data/state.sqlite3` 中。数据库启用 WAL、事务写入和 10 秒 busy timeout；首次启动时会自动
导入对应的旧 JSON，并将原文件保留为 `*.json.legacy.bak`。Session 正文与附件目录仍采用
独立文件保存，便于人工检查和按会话备份。

`state.sqlite3` 与 `turns.sqlite3` 使用有序的 `PRAGMA user_version` schema migration。
旧版本数据库升级前会通过 SQLite backup API 创建包含 WAL 内容的一致性快照
`*.pre-migration-v<旧>-to-v<新>.bak`；一批迁移中的任一步失败都会整体回滚。若数据版本
高于当前程序支持范围，Gateway 会拒绝用旧代码打开，而不会静默降级或覆盖新结构。

Stable Context 由 `System Prompt + Soul + Retrieved Memory` 组成，再由 `ContextBuilder`
与当前 Session 的 conversation messages 组合成模型输入。普通用户消息不能覆盖 System
Prompt 或 Soul；Memory 是跨 Session 的长期上下文，不属于某个单独会话。

普通对话不会直接写入长期 Memory。系统先用保守规则筛出明确的偏好、身份、课程和长期
项目信息，再在后台调用模型将候选整理成稳定表述，因此不会阻塞当前回答。候选面板会展示
整理原因、用户原话和与既有 Memory 的冲突；用户可以编辑、忽略或确认。确认冲突候选时会
更新旧 Memory，避免互相矛盾的偏好同时进入上下文。

系统规则和交互风格分别位于 `prompts/system_prompt.md` 与 `prompts/soul.md`，修改后重启生效。

```text
/memory add <长期信息>
/memory list
/memory find <关键词>
/memory update <memoryId> <新内容>
/memory delete <memoryId>
```

Memory 保存在 `data/state.sqlite3`，对所有 Session 可见；只有上述内部命令或用户在候选记忆面板中的显式确认能够修改它。

### Goal / Spec 任务状态

对包含实现、检查、读取、整理、生成、验收等明确动作的请求，Runtime 会在 Session 中创建一个轻量 `goalState`，记录目标、来源、当前步骤、验收条件、已完成步骤、下一步以及 `active/awaiting_approval/completed/blocked` 状态。它不是额外的对话模式，也不会为普通寒暄增加模型调用；工具审批、工具结果和最终回答会更新同一状态。Goal 状态会进入下一轮 Context 和 Compaction Prompt，避免压缩后丢失“要完成什么、怎样算完成”。CLI 可用 `/goal` 查看详情。

多步骤、包含两类以上外部动作或带明确先后顺序的请求还会由 `task_planner.py` 建立有界结构化计划。计划固定分为“计划—执行—验证”，每一步都声明预期证据；调用开始只能把步骤标为执行中，只有真实 Tool Result 才能标记为已验证，失败结果和待审批也会进入同一状态机。Planner 是确定性 Runtime 编排，不增加额外 LLM 调用，也不保存或展示模型私有思维链。Web 会在当前对话中显示一张可折叠的低干扰计划卡，并通过 `goal_state` SSE 事件实时刷新；页面重载或 Turn 重连后可由持久化 `goalState.plan` 恢复。

Tool 输出、附件正文/OCR、网页和 GitHub 仓库内容会在 Context 中明确标记为不可信证据：它们只能支持当前问题的事实判断，不能改写 System Prompt、Soul、权限、审批要求或长期 Memory，也不能把外部文字中的指令当作 Runtime 指令执行。

## Compaction

当当前 Session 超过 20 条语义消息、12,000 个语义字符，或默认估算超过 8,000 个语义 token 时，程序会用 `prompts/compact_prompt.md` 将较旧消息合并进当前 Session 的 `summary`，并保留最近 8 条正文消息。token 估算是无额外依赖的保守估算（中日韩字符按单 token、拉丁文字按字符块估算），也可用 `SJTUCLAW_COMPACT_MAX_TOKENS=0` 关闭 token 阈值。语义消息只计算用户正文与最终 assistant 回答；已完成 Turn 的 Tool Call、Tool Result、Approval 和协议重试不占消息槽位，也不会重复进入后续模型上下文。尚未完成的当前 Tool 链仍完整保留，工具审计记录继续存放在 `toolTrace` 与 Timeline。System Prompt、Soul 和 Memory 是 Stable Context，不参与压缩，也不会被摘要模型改写。

摘要 Prompt 要求输出固定 Markdown 栏目：当前任务、已完成、用户偏好与约束、关键事实与证据、附件与文件来源、待解决问题、下一步、不应记住。超长历史会同时按字符和 token 预算切成多个 chunks 分别摘要，再合并成最终 summary；单条超长消息也会先分片，避免压缩请求本身超过模型上下文。即使当前只有一轮对话，只要用户正文已经超过字符或 token 阈值，自动压缩也会在回答完成后整理该正文并保留最新回答；手动 `/compact` 还允许直接整理单条超长消息。单个摘要分片默认限制约 6,000 个估算 token，可用 `SJTUCLAW_COMPACT_CHUNK_TOKENS` 调整。

也可以使用 `/compact` 手动触发。压缩成功后 CLI 会展示消息数量、Summary 版本、覆盖语义消息范围和经 Markdown 渲染的 Summary 预览；Web 自动压缩则会在对话流中显示一张“上下文已压缩”卡片，包含旧消息数、保留消息数、分片数和可展开的 Summary 预览。摘要元数据会记录版本、覆盖范围、分片数、Token 统计和质量提醒；质量检查会发现固定栏目缺失/为空，以及旧消息中的 `att_...`、PDF/DOCX/PPTX 等来源标记没有进入摘要，便于回溯但不会因格式问题破坏性丢弃历史。摘要生成失败、摘要为空、摘要对有实质历史产出“全无”占位内容或保存失败时，原始 messages 和原 summary 都会保留，不会因为 compaction 丢失历史。

自动压缩作为一次 Agent Turn 的后置步骤触发：先完成并落盘当前回答，再整理旧消息，避免摘要模型调用占用本轮回答上下文或把压缩卡片插到回答之前。开始、成功和失败都会写入 Timeline；成功结果通过 CLI 输出或 Web 对话卡片展示，网页刷新不会把历史卡片重复追加到对话底部。压缩只替换旧消息为 `session.summary`，不会把摘要请求本身或工具审计记录再次塞回普通 conversation messages；下一轮 Context Builder 会使用新的 Summary 加上最近正文消息继续工作。只有模型输出达到硬上限时，才会在同一轮重试前执行必要的紧急压缩。

## Read-only Tools 与 Agent Loop

CLI 默认以分区式活动块展示 Tool 执行：调用中、审批、调用完成和最终回答分别留出层级与空白；工具区会在助手正文开始前闭合。流式 Markdown 会增量处理粗体、代码、标题、引用和 GFM 表格，表格会转换为终端对齐布局；结果只显示一行摘要，避免把第三方 API 的大段 JSON 淹没对话。需要检查原始 Tool 输入/输出时可使用 `python main.py --verbose-tools`，此时参数和结果会分栏显示并限制长度。

模型调用会记录 OpenAI 兼容接口返回的 `finish_reason`。当返回 `length`、`max_tokens` 或 `max_output_tokens` 时，Runtime 不会把半截 JSON/Markdown 当作成功答案：会先清理临时流式输出，按观测到的输出长度提高 `max_tokens` 后有限重试；如果本轮上下文也超过 Compaction 阈值，会在重试前尽力压缩旧消息。连续重试仍触发上限时，才落盘并展示可重答提示，同时在 Timeline 记录原因和重试次数。

模型未执行却承诺“稍后搜索/读取”时，Runtime 会写入内部 `system` 协议反馈并继续本轮 Loop。为兼容部分只接受首位 system message 的 OpenAI 兼容网关，Context Builder 会在发送模型前将这类内部反馈合并到首个 system context；Session 中的原始角色仍保持为 `system`，且不会在网页中显示为用户消息。

基础 `read_only` Tool 包括：

- `current_time`：读取当前本地时间和时区。
- `calculate`：安全科学计算器，支持四则运算、幂/根式、三角与对数、阶乘与组合、常用统计、变量代入、复数 `i/j` 与角度/弧度制；负数开方和复数结果会以 JSON 安全的 `real`/`imag` 结构返回。不使用任意 `eval`，并限制表达式复杂度与结果规模。
- `symbolic_math`：基于受限 SymPy 表达式解析的符号数学工具，支持化简、展开、因式分解、方程、求导、积分、极限和小型矩阵运算。
- `wolfram_query`（可选）：接入 Wolfram|Alpha LLM API，处理自然语言数学、单位换算和跨领域计算知识。本地 `calculate`/`symbolic_math` 仍是默认；只有在 `.env` 配置 `WOLFRAM_APP_ID` 后才注册该 Tool。Wolfram 官方当前为非商业开发提供每月 2,000 次免费调用额度，结果不会在 SJTUClaw 中缓存，并会附带 `Computed by Wolfram|Alpha` 归属信息。
- `symbolic_math`：基于受限 SymPy 解析环境的符号数学工具，支持方程求解、化简、展开、因式分解、求导、定/不定积分、极限，以及小型矩阵的行列式、求逆和线性方程组；同时返回精确结果、数值近似和 LaTeX。它不会开放任意 Python `eval`，表达式、变量数、矩阵规模和运算复杂度均有限制。

模型调用数学 Tool 时，参数仍使用工具规定的纯文本表达式；面向用户的最终回复则默认把数学表达式排版为 LaTeX：行内使用 `\(...\)`，独立公式使用 `\[...\]`。Web 端由本地 KaTeX 渲染，避免把 `x^2 + sin(x)`、裸积分符号或 HTML 实体直接展示给用户。
- `list_dir`：列出目录，最多返回 200 项。
- `read_file`：读取 UTF-8 文本，超过 100,000 字节时截断。
- `web_search`：通过 Tavily 查询最新公开信息与来源。
- Web 来源会在写回 Session 前统一分配本 Session 内唯一的 `[Wn]` 标签，并保留一个有上限的 citation index；即使 compact 移除了旧的 Tool 消息，网页端仍能把后续回答中的旧 `[Wn]` 渲染成可点击链接。
- 其他来源标签按类型区分：`[Pn]` PDF 页、`[Sn]` PPTX 幻灯片、`[Dn]` DOCX、`[An]` 文本附件、`[On]` 图片 OCR 块；`[On]` 始终指向产生 OCR 结果的图片附件或 Workspace 图片，不代表 Skill 或 `SKILL.md`。
- `weather_forecast`：通过 Open-Meteo 查询当前天气及未来 1-7 天预报，可选未来 48 小时逐小时数据；地点缓存 24 小时，预报缓存 5 分钟。
- `github_read`：读取 GitHub 公开仓库中的文本文件或目录；仅允许 GitHub 官方 HTTPS 域名，并限制文件数量、单文件大小和总字节数，不执行仓库代码。Contents API 触发公开限流时，会自动切换到受大小限制的官方 codeload ZIP 读取；如果整仓库归档本身超过 20 MB，会提示改用具体 `path`、关闭 `recursive` 或分目录读取。返回内容标记为外部不可信资料。

普通天气优先使用 `weather_forecast`；台风路径、灾害预警、停课通知和气象新闻仍使用 `web_search` 查询权威来源。天气预报不等同于官方灾害预警。

模型使用结构化 JSON 协议请求单个 `tool_call` 或一批 `tool_calls`，每批最多 5 个；最终答案推荐使用 `{"type":"final","content":"..."}`。Gateway/CLI 默认采用宽松协议模式：只有真正解析到结构化 Tool Call 才执行工具，普通自然语言中的“我先去查一下”不会被误判为协议错误、不会自动吞掉当前回答，用户可以直接再次要求。仍可通过 `AgentRuntime(strict_tool_protocol=True)` 为专门的协议验收或兼容旧行为的调用启用严格模式。对于明确的结构化 Tool JSON、原生 Function Calling、附件确定性读取和审批恢复，宽松模式不改变其执行、校验、审计和上下文回灌。原始异常输出仅保留在执行审计中，不展示给用户、也不回灌为下一轮对话内容：

```json
{"type":"tool_call","tool":"read_file","args":{"path":"README.md"}}
{"type":"tool_calls","calls":[{"tool":"current_time","args":{}},{"tool":"list_dir","args":{"path":"."}}]}
{"type":"final","content":"最终回答正文"}
```

对于 `LLMClient`，Runtime 会把 Tool Registry 转换为 OpenAI-compatible 的
`tools` 定义，并在明确需要工具时使用 `tool_choice="required"`；模型返回的原生
`tool_calls` 会被统一转换为上面的内部协议，再进入同一个执行循环。若当前模型或
网关拒绝 `tools`/`tool_choice` 字段，客户端会记录该能力并自动退回 JSON 协议，
不会让一次兼容性差异中断会话。这样既能使用 SJTU API（如 `deepseek-chat`）的原生
Function Calling，也能继续运行仅支持文本协议的测试模型。部分兼容模型会把强制
函数调用放在 assistant 正文中，返回 `[{ "name": "...", "parameters": {...} }]`
数组；客户端会在流式缓冲区内将其规范化为 `tool_calls`，不会把原始 JSON 当作回答
显示给用户。

Runtime 会从模型输出中抽取合法协议 JSON，严格校验 tool name 与参数；未知 tool、参数错误或 handler 异常都会作为失败 observation 写回当前 Session，而不是让程序崩溃。执行结果以 `[tool_results] ...` 消息进入 session messages，随后 Runtime 再次构造上下文调用模型，直到模型输出 final。对“读取文件/附件”或“查看 Workspace/目录”等明确观察请求，若本轮没有对应 Tool Result，普通寒暄或空泛 final 会被自动撤回并重试，不能提前结束。每个 Agent Turn 默认最多进行 24 轮“模型调用 → Tool 执行”（可用 `SJTUCLAW_MAX_AGENT_STEPS` 调整），达到上限会写入审计事件并返回可重答提示，避免模型异常重复调用时无限占用资源；单批工具调用仍限制为 5 个。

一次完整的内部 Agent Loop 是：`用户消息 → buildContext → callLLM → 解析 final/tool_call(s) → 执行本轮 Tool（必要时暂停等待 Approval）→ 写入 Tool Result → 重新 buildContext → 再次 callLLM`，直到得到有效 `final` 或明确失败。模型可以在多轮工具之间输出一条简短进度说明，但进度说明不会替代 Tool Call；工具结果始终先回灌上下文，避免模型只“承诺要做”却直接结束。

Runtime 还维护一份结构化的 **Execution Evidence（执行证据）**：读取、联网搜索、写入、运行命令、安装 Skill 和创建定时任务等动作，只有对应 Tool 返回成功结果后才会被记为已完成。模型自然语言中的“已经读取/已经创建/请点批准”不构成证据，也不能凭空生成 Approval；Approval 只会由真实的副作用 Tool Call 和 ApprovalStore 记录产生。若 final 声称完成但缺少证据，Runtime 会把已经展示且有实质内容的分析落盘为只读过程片段，剥离协议 JSON 后再通过原生 `tool_choice="required"` 有界纠正；因此重试、刷新或断线恢复不会把用户已经看到的有效正文整段吞掉。过程片段不会冒充最终完成证据；仍无法执行时会返回如实的未完成说明。

Execution Evidence 会随轻量 Goal/Spec 状态持久化。用户用“继续吧”“开始吧”等短句续接任务时，Runtime 会恢复尚未满足的动作要求，并只补做缺失步骤；审批暂停和恢复也沿用同一份证据。该机制是对现有 Agent Loop 的结果校验，不额外暴露模型私有思维过程，也不会把某一句固定话术误当成执行事实。

每个 Tool 定义都带有 `parallel_safe`、`side_effect`、`retryable`、`max_attempts`、`timeout_seconds` 和 `idempotent` 元数据。连续的只读调用会在最多 4 个线程内有界并发，写入、下载和审批 Tool 始终串行；可重试只对明确声明的 Tool 生效，副作用 Tool 默认不会自动重试。每个结果还会记录 `errorCode`、`retryable`、`attempts` 和 `durationMs`，错误文本会限制长度并脱敏。时间敏感请求仍强制先执行 `current_time`，再用真实日期重写 `web_search`/天气查询。

Runtime 会在同一 Turn 内按 `tool + 规范化 args` 为调用去重：模型重复请求已经成功执行过的 Tool 时，会回灌首个结果而不再次执行。该机制也覆盖同一批中的重复审批请求，避免副作用 Tool 被重复执行或生成多个审批卡片；去重结果会在 Tool Trace 中标记 `deduplicated`。

每次调用及结果会显示在 CLI，并持久化到 Session 的 `toolTrace` 中。持久化的审计副本会递归脱敏凭据字段、限制嵌套深度和大字段长度；当前轮传给模型的 Tool Result 仍保留完整内容。只读工具注册表不包含写文件、Shell 或其他有副作用的 Tool。

## Gateway 与 Web UI

Web 顶部操作会随视口自适应：窄屏仅保留高频入口，其余导出、运行记录、诊断、记忆和
Skills 收进“更多”菜单。对话中的连续同类 Tool 事件会按工具名折叠成一组，并汇总调用、
完成和失败数量；单条事件仍保留完整参数、结果与重试入口。

浏览器通过 `/api/events/stream` 接收 Session、Scheduler 和 Approval 的统一 Gateway
事件流，因此 QQ、飞书、微信或 Scheduler 写入当前 Session 后无需等待固定轮询周期。
SSE 不可用或断线时会自动退回低频轮询，重新连接后再关闭轮询，保证旧浏览器和短暂网络
抖动下仍能恢复。

上传附件采用 SHA-256 内容寻址，文件保存在 `data/attachments/blobs/`。不同 Session 上传
相同内容时只保存一份 Blob，各 Session 保留独立的附件名称和引用；删除最后一个引用时才
删除 Blob。`GET /api/attachments/audit` 可检查缺失引用和孤立 Blob，
`POST /api/attachments/cleanup` 会清理没有任何 Session 引用的孤立 Blob。旧版 Session
附件会在 Gateway 启动时自动迁移到 Blob 仓库。

启动本地 Gateway：

```bash
python gateway.py
```

然后访问 `http://127.0.0.1:8000`。Web UI 支持发送消息、查看历史、创建/列出/切换 Session、显示错误，以及从输入框一次上传多个附件。新对话首先是浏览器本地草稿，可在标题下预选 Workspace、暂存附件；这些操作不会创建空 Session，直到发送第一条消息才会把标题、Workspace 和附件一并保存。输入 `/` 会弹出可键盘筛选的快捷命令菜单，可直接新建会话、手动压缩上下文或打开附件、Workspace、Skill、Memory、任务、Timeline、诊断、模型与导出入口；这些本地命令不会作为普通消息发送给模型。输入栏左侧的“＋”是本轮快捷入口：可上传图片/文件、定位已有附件，或选择一个 Skill 作为本轮专业工作流；已选 Skill 会以可移除标签显示，真正发送时才随同消息进入同一个流式 Agent Turn。浏览器端不保存、接收或暴露 LLM API Key。

Gateway API：

```text
GET  /api/health
GET  /api/daily-quote
GET  /api/sessions
POST /api/sessions
GET  /api/sessions/{sessionId}
PATCH /api/sessions/{sessionId}
DELETE /api/sessions/{sessionId}
POST /api/chat
POST /api/chat/stream
POST /api/chat/replay/stream
GET  /api/turns/active
GET  /api/turns/history
GET  /api/turns/{turnId}
GET  /api/turns/{turnId}/events
GET  /api/turns/{turnId}/trace
GET  /api/turns/{turnId}/stream
POST /api/turns/{turnId}/cancel
GET  /api/sessions/{sessionId}/attachments
POST /api/sessions/{sessionId}/attachments
DELETE /api/sessions/{sessionId}/attachments/{attachmentId}
GET  /api/sessions/{sessionId}/attachments/{attachmentId}/preview
GET  /api/sessions/{sessionId}/attachments/{attachmentId}/content
GET  /api/sessions/{sessionId}/attachments/{attachmentId}/raw
GET  /api/sessions/{sessionId}/attachments/{attachmentId}/pdf-pages
GET  /api/attachments/audit
POST /api/attachments/cleanup
GET  /api/tasks
POST /api/tasks
GET  /api/tasks/{taskId}
POST /api/tasks/{taskId}/cancel
GET  /api/sessions/{sessionId}/workspace
PUT  /api/sessions/{sessionId}/workspace
GET  /api/approvals
POST /api/approvals/{approvalId}/decision
GET  /api/downloads/{downloadId}
GET  /api/skills
GET  /api/skills/doctor
GET  /api/skills/{skillName}
GET  /api/skills/{skillName}/doctor
POST /api/skills/{skillName}/run
GET  /api/sessions/{sessionId}/skill-usage
```

取消接口的请求体可省略，或使用 `{ "mode": "graceful" }` / `{ "mode": "immediate" }`。重复取消会返回持久化的当前状态；未知 `turnId` 仍返回 404。

`POST /api/chat` 中提供 `sessionId` 时严格路由到该 Session；缺失时使用 CLI 与 Gateway 共享的当前 Session，不存在则返回 404。Gateway 只调用 `AgentRuntime.run()`，继续复用 Context Builder、Memory、Compaction 和 Tool Registry。

附件最大 10 MiB，正文按 SHA-256 保存在 `data/attachments/blobs/`，metadata 写入对应 Session JSON 并进入该 Session 上下文。相同内容跨 Session 只保存一份 Blob，但每个 Session 只保留自己的附件引用与文件名；不同 Session 无法通过 API 查看彼此的附件 metadata。附件不是 Workspace 文件；读取附件不会自动授予文件修改或 Shell 权限。

附件读取支持 UTF-8/GB18030 文本、代码、Markdown、CSV、JSON、DOCX、PPTX 和 PDF。PDF 优先提取原生文本，扫描页自动在本地渲染并执行 OCR；无需复制到 Workspace、启动 Shell 或请求审批。对于 PNG/JPEG/WEBP/GIF/BMP：用户本轮明确选中图片且模型支持视觉时，Runtime 会压缩原图并以临时多模态内容块随模型请求发送，不把 base64 写入 Session；文本模型或不兼容接口则使用 `rapidocr` 在本机提取文字。`ocr_image` 既可接收当前 Session 的 `attachment_id`，也可接收当前 Workspace 内图片的相对 `path`，两者必须二选一；Workspace 路径仍经过不可逃逸的边界检查。OCR 只能识别图片中的文字，原生视觉还可理解图形、人物、场景与布局。可用 `LLM_VISION=auto|on|off` 控制该行为，默认对 Qwen 开启自动判断。

## Scheduler

Scheduler 与 Gateway 同进程运行，每秒检查一次任务，到期后复用同一个 `AgentRuntime` 的内部 Agent Loop。任务状态和历史持久化在 `data/state.sqlite3`，重启后会恢复未完成任务。

Web UI 的“定时任务”面板和 Agent 的 Scheduler Tools 都可以创建任务：

- 一次性任务使用带时区的未来 `runAt`；
- 周期任务使用 `intervalSeconds`，可选 `startAt`、`endAt` 和 `maxRuns`；
- Cron 任务使用 5 段表达式（分、时、日、月、星期），可指定 IANA 时区，默认 `Asia/Shanghai`；
- 待执行或失败任务可以点击“立即执行”试跑；周期任务的试跑会写入历史，但不会改变下一次计划或消耗次数；
- 任务支持暂停、恢复和取消；
- 列表展示 `nextRunAt`、边界、`runCount/maxRuns`、状态和执行历史。

任务状态包括 `pending`、`running`、`waiting_approval`、`paused`、`completed`、`failed`、`expired` 和 `cancelled`。审批中的任务不会被记成失败，也不会重复消耗执行次数；用户批准后 Runtime 会继续原 Agent Loop，完成后再写入同一条 Task History。周期任务遇到失败会保留下一次触发时间，超过 `endAt` 或达到 `maxRuns` 后自动结束。

Scheduler 触发的用户文本仍会写入 Session 以便审计，但模型上下文会额外收到不可伪造的 Scheduler Trigger Context，明确这是后台任务而不是普通聊天；结果通过持久化 `OutboundDelivery` 投递，按 `deliveryId` 去重，渠道暂时离线时指数退避重试，不会重复执行 Agent。Task 同时持久化 `executionContext` 与 `deliveryMode`（默认 `main`/`session`）：`current` 在触发时使用当前活动 Session，`isolated` 为任务建立并复用独立执行 Session，`none` 只保留任务历史而不主动投递；`session` 使用 Session 最近一次出现的渠道路由，`channel` 可指定一个 `deliveryChannel`，也可指定 `deliveryChannels` 数组同时投递到飞书、QQ、微信，未指定时保持兼容的最近路由行为。广播父记录会汇总为全部成功、部分成功、等待重试、全部失败或结果未知，子渠道仍独立幂等与重试；飞书主动通知使用原生卡片状态标题，QQ/微信使用一致的状态前缀。配置 `SJTUCLAW_SCHEDULER_WEBHOOK_URL` 后，`webhook` 会向该地址 POST JSON 事件；未配置时会明确失败，不会悄悄按默认模式执行。Gateway 在“已发出但尚未收到渠道确认”的窗口重启时，会把记录标为 `unknown` 并写入 Timeline，不自动猜测性重发，避免用户收到重复通知；需要时可重新运行对应任务。创建、暂停、恢复、取消和立即执行均需要 Approval（网页端显式按钮除外），`schedule_list`/`schedule_get` 为只读 Tool。

## Workspace、Advanced Tools 与 Approval

Workspace 按 Session 保存。CLI 使用：

```text
/workspace show
/workspace set <目录>
/workspace migrate
```

Web UI 顶部的 Workspace 按钮提供相同能力。所有 Tool 文件路径必须是相对于 Workspace 的路径；绝对路径、`../`、解析后位于 Workspace 外的符号链接路径都会被拒绝。
如果项目文件夹被重命名导致已保存的绝对路径失效，CLI 的 `/workspace migrate` 或 Web UI 点击失效的 Workspace 芯片会先征得确认，再把当前 Session 的 Workspace 指向当前项目目录；迁移只更新 Session 元数据，不移动或删除任何用户文件，并保留迁移历史。

Advanced Tools：

- `create_file`：创建文件；
- `overwrite_file`：覆盖已有文件；
- `edit_file`：精确替换文件内容；
- `apply_patch`：使用 `*** Begin Patch` 协议对一个或多个 UTF-8 文本文件执行增量更新、新建、删除或移动；标准头为 `*** Update File: <相对路径>`。兼容模型偶发输出的“操作行与 `*** File:` 路径分行”格式；单文件调用若省略补丁内路径，可通过 Tool 的 `path` 参数指定，仍缺失时只会采用当前 Session 最近一次成功 `read_file` 的路径作为受控回退。全部路径和 hunk 上下文会先校验，再通过临时文件原子提交，任一冲突都会使整批修改保持不变；执行前需要审批；
- `copy_attachment_to_workspace`：只把当前 Session 附件拷贝进 Workspace；
- `copy_file`：把 Workspace 内的任意文件复制到 Workspace 内的指定文件或目录（已有目录或以 `/` 结尾时保留原文件名）；默认不覆盖已有文件，执行前需要审批；
- `new_shell`：在 Workspace 中启动或替换持久 PowerShell；
- `run_command`：复用当前 Shell，返回 cwd、退出码、stdout、stderr、超时和截断状态；
- `create_download`：为 Workspace 中此前已存在的文件生成 15 分钟有效的下载链接。

`create_file`、`overwrite_file`、`edit_file`、`copy_file` 与附件复制在写入成功后，会直接在
Tool Result 中附带 `downloadUrl`、`downloadId`、文件名和过期时间。因此综合 Case 的标准
链路是“选中并读取附件 → 在当前 Workspace 写入产物 → Approval → 直接展示下载入口”，
不再依赖模型额外记住一次 `create_download` 调用；只有下载本轮之前已存在的文件时才需要
单独调用它。

Update、附件拷贝和 Shell Tools 的 safety level 为 `approval_required`。模型请求后 Agent Turn 会持久化到 `data/state.sqlite3` 并暂停；CLI 或 Web UI 展示 tool 和完整 args，用户批准后 Runtime 才执行，拒绝原因也会作为 observation 写回 Session 并继续 Agent Loop。Gateway 与前端没有直接文件写入或 Shell 执行接口。

`create_download` 不需要显式 Approval；用户通过 Web UI 点击短期下载链接时获取文件。Shell 每次命令执行前后都会确认 cwd 位于 Workspace，超时或越界会终止当前 Shell。服务关闭时会清理所有持久 Shell。

## Skill System

`SkillRegistry` 扫描项目的 `skills/` 目录。普通请求只把 Skill 的 name/description 轻量索引加入上下文；Skill 被选中后加载完整 `SKILL.md`，模板、脚本和参考资料则只生成资源目录，不会整包塞进模型上下文。

Active Skill 使用渐进式资源加载：`read_skill_resource` 只能读取当前 Skill 的 `references/`、`scripts/` 或 `assets/`，支持 `offset`/`nextOffset` 分段，并把读取结果保存在所属 Session 的 Tool trace。这样复杂 Skill 可以跨多轮 Tool 调用维持“准备—执行—验证—交付”，同时避免长模板或参考资料挤占真实对话。Skill 明确要求运行自带校验器或生成器时，可调用 `run_skill_script`；它仅接受当前 Active Skill `scripts/` 下的 Python/Node 文件和参数数组，不经过 Shell、拒绝绝对路径/路径穿越、隐藏凭据环境变量，并且每次都要用户 Approval。Runtime 仍以成功的 Tool Result 作为步骤完成证据；模型自然语言中的“已经完成”不会推进执行状态。

项目内置并预装了一组核心 Skill；通过 `install_skill` 安装的社区 Skill（例如 `agent-browser`）也会作为额外条目出现在索引中：

- `course-report`：课程小论文、学习总结、读书报告和实验报告 Markdown 草稿；
- `material-summary`：汇总 workspace 中的学习材料、课堂笔记与调研资料；
- `presentation-outline`：生成课堂展示逐页结构、讲稿提示和时间计划。
- `pptx`：Anthropic 官方可编辑 PowerPoint 生成、渲染与逐页验证工作流；
- `xlsx`：Anthropic 官方表格创建、编辑、清洗与分析工作流；Windows 可复用本机 Excel 完成公式回算。
- `repository-briefing`：渐进读取 GitHub 仓库或 Workspace，生成带证据路径的架构、运行、执行链和风险导读；
- `document-reader`：读取并整理 DOCX/PPTX 等 Office 文档；
- `pdf-reader`：读取 PDF，必要时结合本地 OCR 处理扫描页。

CLI：

```text
/skill list
/skill show <skill-name>
/skill <skill-name> <task>
/skill usage
```

显式调用会直接加载指定 Skill。网页输入栏“＋ → 使用 Skill”允许先为本轮选择一个 Skill，再与用户文本和所选附件一起发送；这不会额外制造一条占位消息，也不会绕开流式输出。普通聊天中，模型可以根据轻量索引调用 `use_skill` 自主选择，但必须先生成 Approval；批准后才加载完整 Skill。Skill 仍复用原有 Session、Context Builder、Memory、Tool Registry、Workspace、Compaction 和 Approval。生成文件时必须继续通过 Update Tool 获得文件写入审批。

新增的 `install_skill` 也必须经过 Approval，可从 GitHub 仓库/压缩包或 ClawHub Skill 下载。安装器仅接受 HTTPS 白名单来源，限制压缩包、解压后总大小、文件数和单文件大小，拒绝路径穿越、符号链接、脚本/可执行文件、插件包和缺少合法 `SKILL.md` frontmatter 的内容；安装完成后只刷新本地 Skill 索引，不会自动执行 Skill 中的代码。对于包含多个 Skill 的大仓库，可传入 GitHub `.../tree/<ref>/<skill-subdir>` URL 只安装目标子目录；ClawHub 的公开下载接口可能返回 GitHub 来源交接信息，仍会重新经过同样的 GitHub 和压缩包校验。若 ClawHub 对未固定的 `latest` 返回 HTTP 409，安装器会查询具体版本并只重试一次；仍失败时会保留服务端错误说明，并提示切换到对应 GitHub 子目录，避免把“审批成功”误报成“安装成功”。GitHub 返回 404 时会显示原始下载地址；Agent 不得仅凭 Skill 名称猜仓库，也不得把一个猜错的 404 地址解释成 Skill 已被删除。

每次使用记录保存在所属 Session 的 `skillUsage` 中，包括 Skill、任务、`explicit`/`auto` 来源、自动选择原因、时间、状态、最终输出和保存路径。Web UI 的 Skills 面板支持查看、显式调用和检查当前 Session 的使用记录。

Skill Doctor 会在不执行第三方脚本的前提下被动检查 `SKILL.md`、本地资源、当前平台，以及 Skill 声明的命令、Python 包和环境变量。内置纯提示型 Skill 标记为“可用”；缺少命令或平台不兼容时标记为“不可用”，缺少环境变量时标记为“需配置”，未声明运行依赖的第三方 Skill 标记为“未验证”。不可用或未配置完成的 Skill 不会进入 Agent Turn。Skill 可用性可以通过 Skills 面板或 `/api/skills/doctor` 查看。

模型能力不再只依赖模型名称猜测。SJTUClaw 会按 `API base URL + model`
持久化真实请求中观察到的原生 Tool Calling、视觉、流式输出和 JSON
协议能力；重启后会复用已有结论，网页左下角以紧凑状态显示“实测 /
推测 / 待检测”。可通过 `GET /api/model/capabilities` 查看详情，或调用
`POST /api/model/capabilities/reset` 清除当前模型记录，让后续请求重新检测。

## SSE 流式事件

Web UI 默认通过 `POST /api/chat/stream` 接收 `text/event-stream`。事件包括：

```text
status             分析、模型请求、协议重试、完成等可审计状态
tool_call          模型提出的 Tool 与参数
tool_result        Runtime 的真实执行结果
approval_required  等待用户审批的 Tool 请求
assistant_delta    最终回答的文本增量
done               Agent Turn 的最终状态和 pending approvals
error              单次请求错误；不会终止 Gateway
```

这里展示的是 Agent 执行轨迹，不是模型隐藏思维链。工具调用帧会在 Runtime 内部缓冲并解析，不会把 Tool JSON 当作回答流到聊天框；确认输出进入 `final` 内容后，正文可以通过 `assistant_delta` 增量发送。Tool、状态和正文事件都在 Agent Loop 中实时发送。事件回调属于旁路遥测，浏览器断开不会破坏 Session 执行与持久化。

每个 SSE 事件还带有统一的 `agentEvent` 信封：`runId`、`turnId`、`seq`、`step`、`type`、`payload` 和 `timestamp`。工具事件与结果使用同一个 `callId`；原生 Function Calling 的 ID 会被保留，旧式文本协议缺少 ID 时由 Runtime 按 Turn/步骤/批次位置生成稳定回退值。事件同时带 `parentSeq`、`traceId`、`traceType`，可把一次 Turn 中的模型调用、Tool、结果、审批、重试和 Compaction 还原成父子链。现有顶层字段继续保留，旧版 Web/CLI 不需要同时升级。

每个 Gateway Turn 同时写入 `data/turns.sqlite3`：`turns` 表保存 Session、类型、阶段、结束状态和最后事件序号，`turn_events` 表保存带序号及父子 Trace 元数据的 SSE 事件。Gateway 重启时只会把仍处于运行阶段的记录标记为 `interrupted`；最终回答一旦落盘就立即锁定 `completed`，即使后续还在做 Compaction，也不会误报“Turn 未完成”。`/api/turns/history`、`/api/turns/{turnId}`、`/api/turns/{turnId}/events` 和 `/api/turns/{turnId}/trace` 可供恢复界面与诊断使用。终态写入是幂等的，晚到的清理不会把 `completed/cancelled` 覆盖成错误。

## 可观测性、Markdown 与取消

Web UI 的 Timeline 面板统一展示 Turn、模型调用、Tool、Approval、Skill、Compaction 和 Scheduler 事件。每次模型调用记录耗时和 input/output/total tokens；API 返回 usage 时使用精确值，否则明确标记为估算值。旧 Session 没有 `activity` 字段时会自动按空列表兼容。

Assistant 消息支持安全的 Markdown 标题、列表、粗体、行内代码、代码块和 `http/https` 链接。渲染前先转义模型文本，不执行模型生成的任意 HTML 或脚本。

Runtime 对兼容接口的协议污染做了兜底：如果模型把自然语言前缀、`final` 包装和实际 `tool_call` 混在同一段，解析器会优先恢复真实 Tool Call；如果 Tool JSON 被嵌在 `final.content` 中，也会在执行前提取并清除临时流式正文，不把正确结果误判成 `protocol_error`。Web UI 的“Session 健康”面板会先给出当前会话的整体结论，再展示上下文、工具、附件、存储和协议错误检查项；该检查只读，不调用模型。

发送过程中可点击 Stop。前端通过 `turnId` 调用 `POST /api/turns/{turnId}/cancel`，可传 `{ "mode": "graceful" }` 或 `{ "mode": "immediate" }`；两种模式都使用 cooperative cancellation，立即模式先停止前端等待，后台仍在当前不可中断的 HTTP/Tool 调用返回后安全退出。Runtime 会在模型调用前后、流式 chunk、每个 Tool 前后和审批恢复前检查取消状态。取消请求与已结束 Turn 都是幂等的，重复点击会返回当前终态而不是 404。

审批决策同样以 `approvalId` 为幂等键。重复批准/拒绝只返回已保存的 `approved/rejected` 结果，不会再次运行副作用 Tool；流式审批恢复会在 `done` 事件中带 `alreadyResolved` 标志，方便前端刷新状态。

飞书、QQ、微信入口和 Web 一样限制单条用户消息最多 100,000 个字符；超限渠道消息会返回结构化 `message_too_large` 错误，不进入模型上下文。渠道服务按映射后的 `sessionId` 串行执行同一会话，跨渠道重复事件由持久化 Deduplicator 去重，最终回复仍通过统一的 `OutboundEvent` 投递。

### Web 端到端测试

真实浏览器测试使用临时 Gateway、临时数据库和本地假模型，不会调用外部 LLM，也不会修改
现有 `data/`。首次运行先安装开发依赖和 Chromium：

```powershell
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
$env:RUN_E2E="1"
python -m unittest test.test_web_e2e
```

测试覆盖 SSE 流式回复、Session 重命名、文件拖放、附件选择、Approval 操作，以及错误提示
出现时输入框不发生布局偏移。普通 `python -m unittest discover` 会跳过真实浏览器测试。

## Docker 一键启动

`.dockerignore` 会排除 `.env`、运行数据和本地虚拟环境，密钥不会写入镜像。使用 Compose 在运行时注入 `.env`，并把 `data/` 挂载为持久卷：

```bash
docker compose up --build
```

打开 `http://127.0.0.1:8000`。后台运行与停止：

```bash
docker compose up --build -d
docker compose down
```

镜像包含 `/api/health` 健康检查。生产部署时应通过平台 Secret 注入 `LLM_API_KEY`，不要把 `.env` 提交到仓库。
