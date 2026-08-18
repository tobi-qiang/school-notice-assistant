# 更新日志

本项目的所有显著变更都会记录在此文件。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-08-18

### 新增

- 首次开源发布。
- 三个抓取来源：未来科学与工程学院·通知公告、未来科学与工程学院·奖惩助贷、苏州大学教务处·通知公告。
- 教务处登录态在 Windows 下使用 DPAPI 加密保存到本地 `data/jwc-session.bin`。
- 四套独立 AI 配置：chat / embedding / rerank / summary；全部可选。
- AI 富化（10 字段结构）与 share 用的极简短摘要生成走 `summary_*` 小模型槽位。
- 关键词搜索 + 可选 embedding 语义搜索 + 可选 rerank 混合排序。
- 单篇极简群分享（10–20 字摘要 + 空格 + 原文链接）与按时间窗批量分享。
- Web UI：单页 + 暗色风格 + 同步状态滚动卡 + 自绘来源筛选下拉。
- 退出程序时受控后台任务、单实例锁、心跳均安全关闭。
- 离线测试 300 用例，使用 fixture + 假传输层，不调用学校网站或真实 AI。

### 安全

- `data/` 目录、`*.key`、`jwc-session.bin` 全部在 `.gitignore` 中。
- API Key 通过 Windows DPAPI 加密保存，不进入 JSON 或 SQLite。
- 本地服务只监听 `127.0.0.1:8765`。