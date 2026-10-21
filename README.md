[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB.svg?logo=python&logoColor=white)](pyproject.toml)
[![CI](https://img.shields.io/badge/CI-pytest-44cc11.svg?logo=githubactions&logoColor=white)](.github/workflows/python-tests.yml)
[![Tests](https://img.shields.io/badge/tests-343%20passed-44cc11.svg)](#%E6%B5%8B%E8%AF%95)

# 学校公告助手

面向苏州大学未来科学与工程学院的公告归档、搜索和群分享工具，仅在本机运行。更多实现细节见 [`AGENTS.md`](AGENTS.md) 与 [`GITHUB_UPLOAD_PLAN.md`](GITHUB_UPLOAD_PLAN.md)。

当前版本 **v0.2.0**：删除教务处登录态持久化，每次同步在弹出窗口手动登录；完善搜索空状态、
取消提示与同步账号展示；修复若干一致性问题。
下载见 [GitHub Releases](https://github.com/tobi-qiang/school-notice-assistant/releases)，
完整变更和验证范围见 [`CHANGELOG.md`](CHANGELOG.md)。

## 功能

- **三来源抓取**：未来科学与工程学院·通知公告 / 奖惩助贷、苏州大学教务处·通知公告（需登录）
- **本地优先存储**：所有抓到的公告、AI 富化结果、群分享批次都落在 SQLite，可永久回看
- **AI 富化（可选）**：标题精简、10 字段结构化摘要（受众/截止/操作/时间/材料/地点/联系方式/外部系统/不确定项/标签）
- **混合搜索**：关键词默认开启，embedding 语义搜索与 rerank 重排可独立开启
- **群分享**：单篇 10–20 字极简摘要 + 原文链接；按时间窗批量复制
- **受控后台任务**：同步、AI 富化、搜索都可中断、可恢复；单实例锁避免重复启动
- **凭据安全**：API key 用 Windows DPAPI 加密，机器外不可解；教务处登录每次同步现场完成，Cookie 不落盘

## 系统要求

- Windows（用 Windows DPAPI 加密本地 API key）
- Python **3.13**（3.13 之前的版本会被 `install.bat` 拒绝）
- 仅当你需要同步「苏州大学教务处·通知公告」时才需要安装 Google Chrome；
  其他两个来源（未来科学与工程学院·通知公告、未来科学与工程学院·奖惩助贷）
  不需要浏览器

## 安装

### 推荐：双击 `install.bat`

首次使用前双击项目根目录里的 `install.bat`。它会：

1. 自动检测 `py -3.13` 或系统里 ≥3.13 的 `python`
2. 在当前目录创建 `.venv`
3. 用 pip 安装项目本体与开发依赖

完成后保持窗口打开会有"Install complete"提示，按任意键关闭。

### 手动安装

不想用 `install.bat` 时，可手动执行等价命令：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 从旧版本更新

先退出正在运行的助手，并备份原目录中的 `data/`。使用 Git 安装时，在原项目目录执行
`git pull --ff-only`，然后重新运行 `install.bat`。使用 Release 源码 ZIP 安装时，将新版本
解压到新目录，复制原来的 `data/` 到新目录，再运行 `install.bat`。

在同一台 Windows 电脑、同一用户下保留完整 `data/`，可继续使用已有公告和配置。
`data/` 含本机凭据，不要上传到仓库；旧版留下的 `data/jwc-session.bin` 已不再使用，可以删除。

## 启动

依赖装好之后任选一种方式：

- 双击 `启动学校公告助手.vbs`（隐藏命令行窗口，在后台启动服务并打开默认浏览器）
- 或在 PowerShell 里跑 `.\.venv\Scripts\school-notice-assistant.exe`

服务只监听 `127.0.0.1:8765`，启动或打开网页时**不会**访问学校网站；只有点击
"同步公告" 才会抓取。首次同步回溯至 `2025-07-01`，之后按 WebPlus 文章编号增量同步。

AI 配置完全可选。未配置或服务失效时，公告同步、原文阅读、关键词搜索和分享仍可使用。
Embedding 与 Rerank 默认关闭。`summary_*` 是为单篇群分享生成 10–20 字摘要的小模型槽位。
使用 MiniMax-M3 时，结构化富化、查询扩展和短摘要会关闭 thinking；基于公告的问答使用
adaptive thinking，并通过 `reasoning_split` 只向页面返回最终答案。各任务有独立输出上限，
达到上限的半截结果不会入库或显示为成功。

## 许可

本项目以 [Apache License 2.0](LICENSE) 发布。Copyright 2026 袁朗。

## 数据存放

所有运行期状态都落在项目下的 `data/` 目录，**整目录已经在 `.gitignore` 里**，不会进
Git 仓库，新用户首次启动时会自动创建。文件清单：

| 路径 | 用途 | 是否含凭据 |
| --- | --- | --- |
| `data/notices.db` | SQLite：所有抓到的公告正文、AI 富化、同步历史、群分享批次 | 否（只是正文和元数据） |
| `data/config.json` | 明文配置：模型 URL / 名称 / 开关 / 版本号 | 否，**API key 不会写在这里** |
| `data/config.key` | DPAPI 加密的 chat 模型 API key | 是 |
| `data/config.embedding.key` | DPAPI 加密的 embedding API key | 是 |
| `data/config.rerank.key` | DPAPI 加密的 rerank API key | 是 |
| `data/config.summary.key` | DPAPI 加密的 summary API key | 是 |

四个 `*.key` 都是 Windows DPAPI 加密，只能用同一台机器、同一用户解出来；搬迁到别的
机器或换账号登录会失效，需要重新填写。教务处登录不产生任何本地文件。

`data/config.json` 与 `*.key` 通过 Web 界面的"设置"页面维护。

## 教务处登录

教务处公告正文需要登录才能读取。首次同步遇到受保护正文时，程序会自动打开 Chrome，
进入苏大官方统一认证页面；你自己完成登录（账号密码只输给学校页面），程序等页面能
读到正文后继续本次同步。

登录产生的 Cookie 只保存在这一次同步的内存里，程序退出或本次同步结束后即失效：
**每次触发同步都需要重新登录一次**。设置页不再提供登录态管理，v0.1.1 之前保存的
`data/jwc-session.bin` 不会被读取，可以随时删除。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/frontend_exit.test.cjs
```

测试使用离线 HTML、假传输层和独立临时目录，不调用学校网站或 AI 接口。
前端交互测试需要 Node.js，仅测试开发需要，运行助手不依赖 Node.js。

API Key 不写入 SQLite 或普通 JSON；Windows 上通过当前用户的 DPAPI 加密保存。点击"退出程序"会取消受控后台任务、关闭本地服务器和数据库并释放单实例锁，同时尝试关闭当前标签页；若浏览器安全策略拒绝自动关页，会显示可安全关闭的退出页面。直接关闭最后一个程序标签页也可以，后台会在短暂宽限后自动退出。

退出页面会等待服务确认退出请求，再检查本地服务是否断开；失败或超时会显示“尚未确认
退出”，可点击“重试退出”。退出时会关闭已接受的 HTTP 长连接，取消教务处登录窗口，
并清理启动中途失败的资源。`data/app.lock` 文件可在退出后保留；是否正在运行取决于
操作系统文件锁，不能通过文件是否存在判断。后台任务的退出等待有时间上限，超时会记入日志。
