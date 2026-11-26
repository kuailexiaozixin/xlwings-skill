# xlwings-sqlookup（占位）

本目录**未随本仓库分发**，仅作占位说明。

## 内容
xlwings Lite 官方插件 `xlwings-sqlookup` 的完整本地副本：`src/xlwings_sqlookup/`（`SQLOOKUP` 自定义函数实现）、`pyproject.toml`（`[project.entry-points.xlwings_lite]` 插件声明）、`tests/`、`README.md`（完整用法文档）。

## 官方来源
- 官方仓库：<https://github.com/xlwings/xlwings-sqlookup>
- 功能说明：单元格内用 SQL 查询 Excel 范围/表格（SQLite 内存引擎），结果溢出到相邻单元格

## 在技能中的用途
xlwings Lite 插件机制的最小完整样例（`@func` + `xlwings_lite` entry point + `requirements.txt` 一行启用），并自带一个 Lite **App Mode 自定义页**（`index.html`/`app.js`/`style.css` + `scripts.py`，`xlwings.runScript` 插入示例表）。用法与源码研读见 `references/15-xlwings-lite-guidance.md` 3.5「官方 Lite 插件 xlwings-sqlookup」；开发自有 Lite 插件（含 App Mode 页）时照此扩展。
