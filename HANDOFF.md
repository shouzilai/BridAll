# 接手这个仓库

> 职责：接手顺序、执行方法、安全与关单规则。
> 不写阶段流水账，也不写具体的历史 run。

## 1. 先读什么（按顺序，不要跳）

1. [README.md](README.md) —— 这是什么、四条硬规矩、一条命令能做什么；
2. [INSTALL.md](INSTALL.md) —— 怎么跑起来、退出码是什么意思；
3. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) —— 边界在哪（core / target，谁能 import 谁）；
4. [docs/adapter-guide.md](docs/adapter-guide.md) —— 要接硬件时才读。

**不要**从代码开始猜设计意图：每份核心模块的模块 docstring 都写了"为什么这样"，
而且那些理由都对应一条可执行的判据。

## 2. 执行方法

```powershell
python tools\doctor.py                     # 环境体检（只读）
python tests\test_hwops_contract.py        # 核心契约套件（不接硬件）
python tools\hwops.py check                # 机器检查
python tools\hwops.py opensource-audit     # 本机事实扫描
python tools\webconsole\platform_boundary.py   # 边界自述与检查
```

改动后的**最低验收**：上面五条全绿（`doctor.py` 允许有 `WARN`，但不允许 `FAIL`）。

## 3. 安全规则（不可协商）

- **烧录永远要两条显式同意**：`--confirm-flash --allow-overwrite`。没有它们，一个字节都不写。
- **同一故障码连续两次之后熔断**：第三次在调用厂商工具之前被拒。人工处置之后才
  `hwops.py fuse --clear`。不要为了"再试一次"绕过它。
- **只读优先**：`doctor.py` 与 `debug-check` 不写调制线（不复位设备）；`--hardware` 也是只读枚举。
- **不猜**：工具装在哪、板子插在哪个口、串口后面的板子是谁 —— 都从本机台架文件或设备自报来，
  不按枚举顺序猜，也不留本机默认值在代码里。
- **能力四态**：做不到就写 `unsupported` + 理由，不要留空、不要用空结果冒充成功。

## 4. 改代码时的边界

```text
core       tools/ + tools/webconsole/     不认识任何芯片、厂商工具、串口名
target     targets/                       平台差异只住在这里
```

- core 里**不许**出现目标层字面量（工具名、芯片族名）——`platform_boundary.token_hit_problems()` 会红；
- core 里**不许** import 目标模块，唯一口子是 `COMPOSITION_MODULES`（装配处）——`scope_problems()` 会红；
- 新模块必须在 `platform_boundary.MODULE_LAYERS` 里登记，否则 `module_layer_problems()` 会红。

这三条不是文档承诺，是**会红的判据**：`python tools\hwops.py check` 一次跑完。

**新增一个随仓库发布的目标**要同时改三处（只改第一处会被普通 `check` 拦下）：

```text
tools/webconsole/bench_setup.py        TARGET_MODULES 加一行
tools/webconsole/platform_boundary.py  LAYERS + MODULE_LAYERS 登记层名与模块名
```
（外部工程接自己的硬件用 `--targets` / `PW_HWOPS_TARGETS`，**不必改仓库**。）

## 5. 关单规则

一件事算做完，要有**三类证据**（缺一类就只能写"部分完成"）：

1. **机器判据**：`test_hwops_contract.py` / `hwops.py check` 通过；
2. **可复核的运行记录**：命令、退出码、关键输出（写进提交信息或 `docs/RELEASE-INFO.md`）；
3. **无硬件可复现**：不接任何板子也能把通路跑一遍（假工具 + 假串口）。

真机结论必须写清三件事：**期望值来自哪里**（声明、还是设备自报、还是本次构建的产物）、
**判据是什么**、**失败时的处置**。只写"跑通了"不算证据。

## 6. 不要做的事

- 不要把本机事实（绝对路径、串口名、序列号）写进代码或文档；
- 不要把私有工程、厂商受限二进制、真实证据提交进仓库；
- 不要为"让测试变绿"放松判据 —— 判据是交付物本身；
- 不要在 core 里加 `if <某块板>`。
