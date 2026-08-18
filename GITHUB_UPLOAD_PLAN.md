# GitHub 远端仓库上传计划

## 目标

把 `school-notice-assistant` 作为独立开源项目推到 GitHub，同时确保本地数据库、
登录态、API Key、虚拟环境和测试缓存不会进入 Git 历史。

本计划假设工作区根目录是 `D:\Study\codex`，本项目位于其下的
`school-notice-assistant\` 子目录，**所有 Git 操作只能在子目录内执行**。
若在 `D:\Study\codex` 根目录误执行 `git init`，会把整个工作区当成仓库提交，
立即 `rm -rf .git` 并在子目录重新初始化。

## 执行前需要用户确认

1. GitHub 仓库名称，默认 `school-notice-assistant`。
2. 仓库可见性：公开或私有；如要开源，默认公开。
3. 开源许可证：已选定 Apache-2.0（`LICENSE` 已就位，Copyright 2026 袁朗），上传前无需再改。
4. GitHub 账号已通过 `gh auth login` 登录，或已有可用的远端仓库地址。

## 标准流程

### 1. 发布前检查

1. 在项目目录运行完整离线测试：
   ```powershell
   Set-Location D:\Study\codex\school-notice-assistant
   .\.venv\Scripts\python.exe -m pytest -q
   ```
   全部通过后再继续；任意失败先修复源码或测试。
2. 双击 `启动学校公告助手.vbs`，确认服务和默认浏览器能正常启动、UI 可访问。
3. 通读 `README.md`，确认安装、启动、可选 AI 配置和隐私说明适合公开发布。
4. 确认源码中不含真实 Key、Cookie、学号/账号、个人路径截图或数据库副本。
5. 确认本机没有其它项目的 `.venv` 或临时调试文件被误放进本目录。

### 2. 敏感文件审计

`.gitignore` 必须覆盖下列路径，缺一不可：

| 路径 / 模式        | 保护内容                           |
| ------------------ | ---------------------------------- |
| `data/`            | SQLite 数据库、JSON 配置、会话文件 |
| `.venv/`           | 本地虚拟环境                       |
| `*.key`            | DPAPI 加密的 API Key 桶            |
| `jwc-session.bin`  | 教务处登录会话（DPAPI 加密）       |

辅助规则同样保留：`__pycache__/`、`*.py[cod]`、`*.egg-info/`、`.pytest_cache/`、
`*.bak`、`*.log`、`.coverage`、`.codex-scratch/`。

初始化仓库后立即复核暂存范围：

```powershell
git -c core.quotePath=false status --short
git ls-files
git grep -n -I -E "api[_-]?key|Bearer |sk-[A-Za-z0-9]"
```

复核要点：

- `data/`、`*.key`、`jwc-session.bin` 必须不出现在 `git ls-files` 中。
- `git grep` 命中的可疑字符串只允许出现在源码/测试的示例占位中，绝不能是真凭据。
- 若发现误跟踪，立即 `git rm --cached <path>` 并在 `.gitignore` 中补齐规则后重新
  `git add .`；**不要用 `--force` 改写已经提交的历史来掩盖已泄漏的秘密**。

### 3. 初始化独立仓库

只在本项目目录执行，**不要在 `D:\Study\codex` 工作区根目录执行**：

```powershell
Set-Location D:\Study\codex\school-notice-assistant
git init
git branch -M main
git add .
git -c core.quotePath=false status --short
git commit -m "Initial open-source release"
```

提交前再确认一次：

- `.venv/`、`data/`、`*.key`、`jwc-session.bin` 都不在暂存区。
- 没有 `.pyc`、`__pycache__/`、`.pytest_cache/` 等临时文件被误添加。
- 根目录只有源码、测试、模板、静态资源、配置与说明类文件。

### 4. 创建并推送 GitHub 仓库

首选 GitHub CLI 一键创建公开仓库：

```powershell
gh auth status
gh repo create school-notice-assistant --public --source . --remote origin --push
```

若用户已经手工创建了空远端仓库（公开或私有），改用：

```powershell
git remote add origin <用户提供的远端地址>
git push -u origin main
```

注意事项：

- 不得猜测 GitHub 用户名或远端地址。
- 在用户未确认仓库可见性前，不要替用户创建公开仓库。
- 推送前若 GitHub 提示「main 分支被保护」，按用户提供的策略再决定是否提 PR。

### 5. 上传后核验

1. 在 GitHub 网页确认 `README.md` 正常渲染、中文文件名（含 `启动学校公告助手.vbs`）显示正常。
2. 在网页文件树中确认：`data/`、`.venv/`、数据库、Cookie、`*.key`、`jwc-session.bin`、
   本机绝对路径产物均不存在。
3. 在一个临时空目录克隆仓库，按 `README.md` 完成安装并跑离线测试，确认新用户
   流程通畅。
4. 验证双击 `启动学校公告助手.vbs` 在全新克隆并安装依赖后仍能启动服务和浏览器。
5. 若公开发布，创建首个版本标签前再做一次凭据扫描和许可证检查。

## 完成标准

- GitHub 远端 `main` 与本地首个发布提交一致。
- 离线测试全绿。
- `data/`、`.venv/`、`*.key`、`jwc-session.bin` 全部未被 Git 跟踪。
- 新用户仅依赖 `README.md` 即可完成安装、启动和可选模型配置。