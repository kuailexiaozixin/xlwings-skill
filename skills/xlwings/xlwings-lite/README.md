# xlwings Lite 官方教程文档

> 本目录为 https://lite.xlwings.org/ 官方文档的完整离线镜像（36 页 + 42 张插图），已整理为 Markdown 格式。
> 抓取/更新：2026-09-15 首版（至 1.0.0.103）；2026-11-06 由 `scripts/sync_upstream.py`（web 源，`scripts/web_mirror.py`）重建于 1.0.0.105，新增 wingman / settings / beta 三页与 5 张 Wingman 截图；2026-11-20 重建至 1.0.0.112（新增 plugins 页 + 2 张插图，共 35 页）；2026-11-25 重建至 1.0.0.113（新增 custom-app 页 + 1 张插图，共 36 页）；2026-11-26 重建至 1.0.0.114（页面结构不变、无新增图）。原站为 Sphinx 文档站，本文档已去除导航/页脚等模板噪声，站内链接互转相对路径，图片存放于 `_images/`。

## 内容索引

| 文档 | 内容 |
|------|------|
| [index（xlwings Lite）](index.md) | xlwings Lite 总览（为什么选择 Lite、能做什么、开发者体验、隐私安全） |
| [quickstart（快速入门）](quickstart.md) | 快速入门（安装、卸载、首个函数/脚本） |
| [custom-functions（自定义函数）](custom-functions.md) | 自定义函数（@func/@arg/@ret、pandas/Polars、Object handles、绘图、流式函数） |
| [custom-scripts（自定义脚本）](custom-scripts.md) | 自定义脚本（@script、按钮触发、App Mode 表单字段） |
| [notebooks](notebooks.md) | Notebooks（类 Jupyter 单元格执行、Markdown 单元格、LaTeX、绘图库） |
| [xlwings Wingman](wingman.md) | xlwings Wingman（内置 AI 编码智能体：直接改写工作簿并生成可复用的 Python 脚本/函数/笔记本，可连自托管 LLM；1.0.0.105 起） |
| [Settings](settings.md) | 设置（xlwings Lite 菜单 > Settings 的行为配置） |
| [xlwings Wingman Beta](beta.md) | xlwings Wingman Beta（Beta 内测说明） |
| [plugins（插件）](plugins.md) | 插件（Python 包自动注册脚本 / 自定义函数，1.0.0.112 起） |
| [dependencies](dependencies.md) | 依赖安装（requirements.txt、第三方库） |
| [databases](databases.md) | 数据库（SQLite/PostgreSQL/MySQL/DuckDB 等） |
| [web-requests](web-requests.md) | Web 请求（fetch 外部 API） |
| [async-api（异步 API）](async-api.md) | 异步 API（await 语法） |
| [plotting](plotting.md) | 绘图（Matplotlib/Plotly/Bokeh/Altair） |
| [app-mode（App Mode）](app-mode.md) | App Mode（将工作簿变为面向终端用户的应用） |
| [custom-app（自定义应用）](custom-app.md) | 自定义应用（用自有 HTML/CSS/JS 页替换 App Mode 默认按钮/表单，`xlwings.runScript()` 调 Python；1.0.0.113 起；1.0.0.114 起亦支持以插件形式分发） |
| [personal-modules（个人模块）](personal-modules.md) | 个人模块（跨工作簿共享代码，类似 Personal.xlsb） |
| [files](files.md) | 文件（导入导出、云盘挂载） |
| [editor](editor.md) | 编辑器（VS Code 内核、快捷键、自动补全） |
| [tests](tests.md) | 测试（内置 pytest 运行器） |
| [environment-variables（环境变量）](environment-variables.md) | 环境变量 |
| [configuration](configuration.md) | 配置 |
| [security](security.md) | 安全（本地优先、代码审批、分发） |
| [limitations](limitations.md) | 限制（WebAssembly 边界） |
| [troubleshooting](troubleshooting.md) | 故障排查 |
| [faq](faq.md) | 常见问题 |
| [cleanup-missing-addin-refs](cleanup-missing-addin-refs.md) | 清理缺失的加载项引用 |
| [migrating-to-self-hosted](migrating-to-self-hosted.md) | 迁移到自托管 |
| [self-hosting](self-hosting.md) | 自托管 |
| [hosting-solutions（托管解决方案）](hosting-solutions.md) | 托管解决方案总览 |
| [linux-vm（Linux VM）](linux-vm.md) | Linux VM 自托管 |
| [azure-container-apps（Azure Container Apps）](azure-container-apps.md) | Azure Container Apps 自托管 |
| [azure-container-apps-cli（Azure CLI）](azure-container-apps-cli.md) | Azure Container Apps（CLI 方式） |
| [aws-ecs-express（AWS ECS Express）](aws-ecs-express.md) | AWS ECS Express 自托管 |
| [about（关于）](about.md) | About（项目与作者信息） |
| [changelog](changelog.md) | 更新日志 |

## 建议阅读顺序

1. **入门**：quickstart → custom-functions → custom-scripts → notebooks → wingman
2. **进阶**：dependencies → databases → web-requests → async-api → plotting → settings
3. **分发**：app-mode → personal-modules → files → security → limitations
4. **自托管**：self-hosting → hosting-solutions → linux-vm / azure-container-apps / aws-ecs-express
5. **参考**：tests → editor → configuration → environment-variables → troubleshooting → faq → changelog
