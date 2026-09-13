# GitHub 发布

公开范围：源码、测试、Markdown 文档、必要静态资源与六个项目内置 Skills。
主项目暂不授予开源许可；第三方资源的许可见 `THIRD_PARTY_NOTICES.md`。

## 1. 本机检查

在项目根目录、已安装依赖的 Python 环境中运行：

```powershell
python scripts/github_preflight.py
python scripts/check.py --full
```

无 Git 的下载副本需先运行 `git init -b main`。预检查要求项目自身是仓库根目录，
不会在 Git 不可用时返回假通过。脚本分别检查当前源码与索引中的文件内容，
即使工作区里已擦除密钥，也能发现暂存区中尚未更新的版本。

它会拒绝已跟踪的 `.env`、私人数据、构建产物和非内置 Skills。
`.gitignore` 不能自动移除已跟踪文件；若报告这类问题，需要先从索引移除，
确认本机副本保留，再重新检查。不要用 `git add -f` 绕过发布排除规则。

脚本不输出密钥值，也不验证凭证是否有效；检查范围不包括已有提交历史与图片内容。
如果曾提交真实凭证，应先撤销或轮换，并另行清理历史。

## 2. 生成干净目录

```powershell
python scripts/package_github.py
python scripts/github_preflight.py --root release/github --export
```

默认输出 `release/github/`，只复制检查通过的 Git 候选文件。
导出目录不包含 `.git`、`.env`、运行数据、依赖、第三方 Skills 或本地二进制文档。
`--export` 会直接检查整个输出目录；不会因为忽略规则而跳过混入的私人文件。

目标已存在时不会删除或覆盖。再次生成请使用新目录：

```powershell
python scripts/package_github.py --target release/github-v2
```

可在这份目录内按 README 从零创建环境和配置 `.env`，验证启动。
填写过真实凭证、运行过应用的副本不再是干净发布目录，应重新导出。

## 3. 提交到自己的仓库

推荐在 GitHub 创建空仓库 `littlelei-SJTUClaw`，不要自动生成另一份 README 或许可证。
然后在本项目根目录执行：

```powershell
git add .
git status --short
python scripts/github_preflight.py
git diff --cached --stat
git commit -m "Initial public release"
git remote add origin https://github.com/<你的用户名>/littlelei-SJTUClaw.git
git push -u origin main
```

提交前确认 `.env`、`data/`、`Workspace/`、`node_modules/`、本机第三方 Skills 和
`release/` 没有出现在暂存清单中。预检查中的“暂不授予开源许可”是作者的选择，
不是检查失败。

如果使用 GitHub 网页上传，选择 `release/github/` 中的内容，并确认 `.github/`、
`.gitignore`、`.gitattributes` 和 `.env.example` 等隐藏项一起上传。
不要上传外层开发目录或包含多个项目的上级文件夹。

## 4. CI 与浏览器测试

GitHub Actions 使用 Python 3.13、`requirements.txt`、测试占位 Key 和临时数据目录，
运行发布检查及 `check.py --full`。不需要配置真实 API Key Secret。

CI 不运行真实模型和浏览器 E2E。后者可在本机安装 Playwright 与 Chromium 后执行：

```powershell
python scripts/check.py --full --browser
```

显式请求 E2E 时，浏览器无法启动或测试全部跳过均返回失败。

## 发布内容来源

第三方 `agent-browser`、`guizang-ppt-skill`、`pptx`、`xlsx` 在开发目录保留，
不进入 Git、Docker 或发布打包脚本的输出。后续安装的第三方 Skills 默认也不发布。
KaTeX 的对应版本许可证与项目内桌宠素材的来源说明保留在源码中。
