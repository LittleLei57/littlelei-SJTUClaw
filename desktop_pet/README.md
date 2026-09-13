# SJTUClaw Desktop Pet

桌宠是 Gateway 的轻量 Electron 客户端。开发启动方式保持不变：

```powershell
npm install
npm start
```

## Windows 发布

在 Windows、Node.js 22+ 环境中执行：

```powershell
# 只校验源码语法
npm run check

# 生成未安装目录，适合快速验收
npm run dist:dir

# 生成便携版 exe
npm run dist:portable

# 生成可修改安装位置的 NSIS 安装包
npm run dist
```

产物位于 `desktop_pet/dist/`，文件名包含版本号和 CPU 架构。安装器不会删除
用户的 Gateway 数据、Session 或附件；桌宠自身只保存浏览器端的 Session 标识。

桌宠默认探测 `http://127.0.0.1:8000/api/health`。可通过
`SJTUCLAW_GATEWAY` 指向其他 Gateway；Gateway 重启后，健康检查恢复时桌宠会自动
重新加载页面。开发启动和打包启动都复用 `main.js`、`preload.js` 与 Gateway 的
`/pet.html`，避免出现两套行为。

当前使用项目原创的“打工喵”主题，提供待机呼吸、随机招手/思考/小睡、点击庆祝、
模型思考、工具执行、回复、离线和错误等状态。主题精灵图及替换说明位于
`web/assets/pet/`。它不包含“月薪喵”等第三方商业 IP 素材；未来取得授权后可按同一
2×3 精灵图协议直接换肤。

托盘菜单中的“开机启动”默认关闭，用户可随时打开或关闭；安装器不会替用户开启
自启动。桌宠不在前台时，完成一轮回复会通过 Windows 系统通知提醒。Electron 渲染
进程异常退出时会自动重载页面，开发启动器也会在非正常退出后尝试重新拉起一次。
