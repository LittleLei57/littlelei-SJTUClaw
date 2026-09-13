# 第三方资源与发布范围

## 离线 KaTeX

`web/vendor/katex/` 包含 KaTeX 0.16.47 的前端脚本、样式和字体。
原始项目：https://github.com/KaTeX/KaTeX

版权与许可见 [`web/vendor/katex/LICENSE`](web/vendor/katex/LICENSE)。
许可证原文来自对应版本：
https://raw.githubusercontent.com/KaTeX/KaTeX/v0.16.47/LICENSE

## Python 与 Node.js 依赖

Python 依赖通过 `requirements.txt` 和 `requirements-lock.txt` 声明；桌宠的 Node.js
依赖通过 `desktop_pet/package.json` 和 `desktop_pet/package-lock.json` 声明。它们均按自身许可安装和使用。
公开源码不包含 `node_modules/` 或本机虚拟环境。

## Skills

公开源码只包含 [`skills/README.md`](skills/README.md) 列出的六个项目内置 Skills。
`agent-browser`、`guizang-ppt-skill`、`pptx`、`xlsx` 及其他第三方安装项均不分发。
Git 忽略规则、Docker 忽略规则和打包脚本均执行此范围。

## 桌宠素材

桌宠精灵图的生成来源见 [`web/assets/pet/README.md`](web/assets/pet/README.md)。

## 主项目许可

作者暂不授予主项目统一的开源许可，保留相关权利；上述第三方资源按各自许可使用。
如需复用主项目代码，请先联系作者确认授权。
