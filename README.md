# SJTUClaw

SJTUClaw 是一个本地 AI Agent 个人项目。
它将对话、记忆、工具调用、文件审批和定时任务接入同一个 Python Runtime，
提供 Web、CLI、Electron 桌宠以及飞书、QQ、微信等可选入口。

项目默认在本机运行。完成模型配置后即可使用 Web 和 CLI，无需部署网站。

## 功能

- 多 Session 对话、历史持久化、长期 Memory 与上下文压缩；
- 流式输出、Tool Call 循环、执行证据与可恢复的 Turn 状态；
- Workspace 文件边界、写操作审批、局部 Patch 和产物下载；
- 附件解析、PDF/OCR、多模态输入、联网搜索、天气与数学工具；
- 一次性与周期定时任务，复用同一套 Runtime；
- 六个内置 Skills，以及按需安装第三方 Skills 的扩展机制；
- Web Timeline、健康诊断、桌宠和渠道接入。

## 运行环境

| 项目 | 说明 |
| --- | --- |
| 已验证的主要环境 | Windows 11、Python 3.13 |
| 模型服务 | OpenAI-compatible API；需要自己的 API Key |
| Web 前端 | 已包含静态 HTML/CSS/JS 和离线 KaTeX；启动 Web 不需要 npm 安装 |
| Node.js | 可选；完整自检和 Electron 桌宠需要，建议使用 22 |
| 存储 | 本地 `data/`；首次运行自动创建 |

Linux/macOS 可尝试运行 Python 核心功能；Windows 专用的 Shell、OCR 与桌宠路径
未承诺跨平台等价。Docker 配置为可选入口，见下方说明。

## 快速开始

在项目根目录打开 PowerShell。使用 Conda 创建一个新环境：

```powershell
conda create -n sjtuclaw python=3.13 -y
conda activate sjtuclaw
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

没有 Conda 时，可使用 Python 3.13 的虚拟环境：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

采用后一种方式时，下文的 `python` 替换为 `.\.venv\Scripts\python.exe`。
新环境优先使用 `requirements.txt`；`requirements-lock.txt` 是历史环境的直接依赖
版本记录，并非包含全部间接依赖的跨平台锁文件。

编辑本机 `.env`，至少填写：

```dotenv
LLM_API_KEY=你的_API_KEY
LLM_BASE_URL=https://models.sjtu.edu.cn/api/v1
LLM_MODEL=deepseek-chat
```

默认示例使用 SJTU 模型服务，需要有该服务的访问权限。使用其他兼容服务时，
将地址和模型名称替换为服务商提供的值；自定义模型列表等选项见 `.env.example`。
不要提交填写后的 `.env`。

启动 Web：

```powershell
python gateway.py
```

浏览器打开 <http://127.0.0.1:8000>。停止服务时在终端按 `Ctrl+C`。
也可以使用 CLI：

```powershell
python main.py
```

诊断和启动器：

```powershell
python sjtuclaw_launcher.py doctor
python sjtuclaw_launcher.py start
```

## 先试这些操作

1. 创建 Session，连续追问上一条消息，再刷新页面检查历史记录。
2. 上传一份自己的测试文档，让 Agent 读取并总结。
3. 将一个专用测试目录设为 Workspace，请 Agent 创建 Markdown 文件；
   在审批面板批准后，查看写入结果和下载入口。
4. 使用 `/skill list` 查看内置 Skills，或使用 `/compact` 压缩对话。
5. 创建一次性任务，保持 Gateway 运行并观察执行记录。

## 自动化验证

安装 Node.js 后，可以运行隔离自检：

```powershell
python scripts/check.py --full
```

该入口设置临时数据目录和测试占位 Key，运行 Gateway 冒烟、完整 unittest、
Web 与桌宠脚本语法检查，不调用真实模型。CI 使用同一入口。

真实浏览器测试需额外安装 Playwright 和 Chromium：

```powershell
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python scripts/check.py --full --browser
```

普通自检跳过浏览器测试；显式指定 `--browser` 时，缺少浏览器或测试被跳过都会
返回失败，避免把“未执行”误报为“通过”。

## 可选入口与扩展

- Electron 桌宠：安装和启动方式见 [`desktop_pet/README.md`](desktop_pet/README.md)。
- 飞书、QQ、微信：需要各平台的凭证与接入配置，见
  [`docs/DETAILED_GUIDE.md`](docs/DETAILED_GUIDE.md)。未配置时不影响 Web/CLI。
- Skills：公开版只含六个内置 Skills，见 [`skills/README.md`](skills/README.md)。
  第三方 Skills 及其脚本、资源、依赖不随此仓库分发。
- Docker：镜像只复制发布范围内的源码；容器网络与本机回环不同，默认 Gateway
  访问限制仍生效。如需容器访问，应单独配置访问边界，勿直接将完整 Agent 暴露到公网。

## 系统架构

| 模块 | 主要能力 | 代码入口 |
| --- | --- | --- |
| 模型与对话 | 模型配置、多轮对话与统一 Agent Loop | `config.py`、`llm_client.py`、`runtime.py` |
| 上下文与记忆 | Session、长期 Memory、摘要与压缩 | `session_store.py`、`context_builder.py`、`memory_store.py`、`compaction.py` |
| 工具与审批 | 工具协议、Workspace 边界、文件操作与审批 | `tools.py`、`advanced_tools.py`、`workspace.py`、`approval_store.py` |
| 服务与渠道 | Web Gateway、流式事件、附件与多渠道接入 | `gateway.py`、`attachment_store.py`、`channels/` |
| 调度与扩展 | 周期任务、内置 Skills 与按需资源加载 | `scheduler.py`、`scheduler_tools.py`、`skill_system.py` |
| 交互界面 | Web UI 与 Electron 桌宠 | `web/`、`desktop_pet/` |

`runtime.py` 是统一 Agent Loop。CLI、Gateway、Scheduler 和外部渠道最终都进入
这条路径：

```text
用户输入
→ Context Builder
→ LLM
→ Tool Call
→ Runtime 执行 Tool
→ Tool Result 写回 Session
→ 重新构造 Context
→ 继续调用 LLM，直到得到 Final Answer
```


## 数据与发布

本机 `.env`、`data/`、`Workspace/`、渠道凭证、依赖、缓存、第三方 Skills 和
本地二进制文档均排除在发布范围外。保留 Markdown 文档作为项目说明。
写入和 Shell 审批是应用层保护，运行权限仍取决于本机用户。

生成无本机数据的 GitHub 发布目录：

```powershell
python scripts/github_preflight.py
python scripts/package_github.py
python scripts/github_preflight.py --root release/github --export
```

首次获取无 Git 的源码压缩包时，可先 `git init -b main`；若只检查现有发布目录，
使用 `--export` 即可。默认输出 `release/github/`，不会覆盖已有目录。
完整提交步骤见 [`docs/GITHUB_PUBLISHING.md`](docs/GITHUB_PUBLISHING.md)。

## 进一步阅读

- [`docs/DETAILED_GUIDE.md`](docs/DETAILED_GUIDE.md)：配置与功能细节；
- [`docs/TURN_LIFECYCLE.md`](docs/TURN_LIFECYCLE.md)：Turn、审批与取消状态机；
- [`ROADMAP.md`](ROADMAP.md)：后续计划；
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)：第三方资源来源与许可。

## 许可

本仓库作为个人项目公开展示，作者暂不授予主项目开源许可，保留相关权利。
第三方资源按各自许可使用；如需复用主项目代码，请先联系作者确认授权。
