# 贡献规范

本文件是本仓库贡献规则的唯一权威来源。

## 1. 提交原则

- 一个提交只表达一个完整意图，并保持可独立评审、可独立回退；
- 功能、测试、文档可以一起形成一个完整意图，无关清理另行提交；
- 提交前只暂存本次变更，不用 `git add -A` 带入本机配置、日志、真实证据或别人的未提交修改；
- **不提交**：真实设备证据（`runs/`、会话目录）、本机绝对路径、COM 口名、内部事项编号、开发过程复盘。

## 2. 提交信息

首行格式：

```text
<类型>(<范围>): <中文动作说明>
```

允许的类型：`feat`（用户可见能力）、`fix`（缺陷修复）、`docs`（只改文档）、`test`（只改测试）、
`refactor`（不改外部行为的重构）、`chore`（日常维护）、`build`（构建、依赖、门禁与开发工具）。

首行必须：

- 用中文说明具体动作，**结尾不加句号**；
- **整条只有这一行**，不写正文；
- 整行不超过 **60 个 Unicode 字符**（中文、字母、数字、标点各算一个）；
- 不使用 `update`、`misc`、`WIP` 这类说不清意图的词。

无法在 60 个字符内说清时，应缩小并拆分提交。

Git 自动生成的合并与回滚提交保留标准格式，不受上述单行限制。

## 3. 门禁

```powershell
python tools\check_commit_message.py <message-file>        # 单条
python tools\check_commit_message.py --range main..HEAD    # 一段区间
git config core.hooksPath .githooks                        # 安装本地 commit-msg 钩子（每个工作树一次）
```

钩子只提供快速反馈，可以被 `--no-verify` 绕过；**合并前以 `--range` 的检查结果为准**。

**范围必填**：`(<范围>)` 不是可选项，`feat: xxx` 这类缺范围的写法会被判为不合格
（`tests/test_commit_messages.py` 有对应判据）。范围写受影响的部分，例如
`web`、`tools`、`tests`、`docs`、`repo`。

远端门禁在 `.github/workflows/gates.yml`：push / PR 到 `main` 时跑全量
`python tests\run_all.py`（先装好 playwright + chromium，**跳过不算通过**）
再跑一段区间的提交信息检查。

## 4. 验证

```powershell
python tests\run_all.py
```

这一条命令覆盖：核心契约套件、通用夹具自检、参考适配契约、Web 验收、仓库卫生、
真实浏览器 E2E（缺 node/playwright 会记 `SKIP`，**跳过不算通过**）、机器检查与开源边界审计。

- 按变更影响面执行相邻测试；涉及共享契约或装配关系时扩大范围；
- 不用修改测试、降低断言或隐藏失败来换取绿灯；
- 文档变更至少跑 `python tests\test_repo_hygiene.py` 与 `git diff --check`。

## 5. 硬件相关约束

- 本仓库只定义接入契约，不代替你自己的固件工程；
- 任何会写串口、复位或烧录的动作都必须由使用者**显式**发起，并且要两条明确同意（见 README）；
- 测试只用仓库自带的假工具与假串口，不连接、不改写真实设备。
