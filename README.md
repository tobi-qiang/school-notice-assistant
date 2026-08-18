[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB.svg?logo=python&logoColor=white)](pyproject.toml)
[![CI](https://img.shields.io/badge/CI-pytest-44cc11.svg?logo=githubactions&logoColor=white)](.github/workflows/python-tests.yml)
[![Tests](https://img.shields.io/badge/tests-300%20passed-44cc11.svg)](#%E6%B5%8B%E8%AF%95)

# 学校公告助手

仅在本机运行的苏州大学公告归档、搜索和群分享工具。更多实现细节见 [`AGENTS.md`](AGENTS.md) 与 [`GITHUB_UPLOAD_PLAN.md`](GITHUB_UPLOAD_PLAN.md)。

## 功能

- **三来源抓取**：未来科学与工程学院·通知公告 / 奖惩助贷、苏州大学教务处·通知公告（需登录）
- **本地优先存储**：所有抓到的公告、AI 富化结果、群分享批次都落在 SQLite，可永久回看
- **AI 富化（可选）**：标题精简、10 字段结构化摘要（受众/截止/操作/时间/材料/地点/联系方式/外部系统/不确定项/标签）
- **混合搜索**：关键词默认开启，embedding 语义搜索与 rerank 重排可独立开启
- **群分享**：单篇 10–20 字极简摘要 + 原文链接；按时间窗批量复制
- **受控后台任务**：同步、AI 富化、搜索都可中断、可恢复；单实例锁避免重复启动
- **凭据安全**：所有 API key 与教务处会话都用 Windows DPAPI 加密，机器外不可解

## 系统要求

- Windows（用了 Windows DPAPI 加密本地凭据，教务处登录态只在本机可解密）
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

## 启动

依赖装好之后任选一种方式：

- 双击 `启动学校公告助手.vbs`（隐藏命令行窗口，在后台启动服务并打开默认浏览器）
- 或在 PowerShell 里跑 `.\.venv\Scripts\school-notice-assistant.exe`

服务只监听 `127.0.0.1:8765`，启动或打开网页时**不会**访问学校网站；只有点击
"同步公告" 才会抓取。首次同步回溯至 `2025-07-01`，之后按 WebPlus 文章编号增量同步。

AI 配置完全可选。未配置或服务失效时，公告同步、原文阅读、关键词搜索和分享仍可使用。
Embedding 与 Rerank 默认关闭。`summary_*` 是为单篇群分享生成 10–20 字摘要的小模型槽位。

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
| `data/jwc-session.bin` | DPAPI 加密的教务处 CAS 登录态 cookie | 是 |

四个 `*.key` 与 `jwc-session.bin` 都是 Windows DPAPI 加密，只能用同一台机器、同一
用户解出来；搬迁到别的机器或换账号登录会失效，需要重新填或重新登录教务处。

`data/config.json`、`*.key` 通过 Web 界面的"设置"页面维护；`data/jwc-session.bin`
通过"教务处登录"按钮走一次浏览器 CAS 登录后写入。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试使用离线 HTML 与假传输层，不调用学校网站或 AI 接口。运行产生的数据库、配置和日志都位于 `data/`，该目录不会提交到代码仓库。

API Key 不写入 SQLite 或普通 JSON；Windows 上通过当前用户的 DPAPI 加密保存。点击"退出程序"会取消受控后台任务、关闭本地服务器和数据库并释放单实例锁，同时尝试关闭当前标签页；若浏览器安全策略拒绝自动关页，会显示可安全关闭的退出页面。直接关闭最后一个程序标签页也可以，后台会在短暂宽限后自动退出。