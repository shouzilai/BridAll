# 接入一条新硬件（给人与 Agent 的短指南）

> 目标：把你自己的工具链、工程、设备接进这套框架，**不改 core**。
> 全程不需要真机也能做完前四步；真机只在你把声明填对之后才需要。

## 0. 一句话

你要交出的东西只有三样：一份**接入契约**（数据）、一个**装配函数**（把你的本机值接进去）、
一份**契约测试**（证明判据会红）。第一样和第三样由脚手架生成。

```powershell
python tools\hwops.py create-adapter --out my_target --id my-board --label "我的板子"
```

生成物**当场就能跑**（拿平台自带的假工具）：

```text
my_target/my_board.py                声明文件：一条 HardwareDeclaration + resolver + hwops_wirings
my_target/test_my_board_contract.py  契约测试：12 条判据（成功 / 同意闸门 / 已知失败 / 未知失败 / 零输出超时 / 身份 / 熔断）
my_target/README.md                  接入清单：还差哪几件事才算接完
my_target/project/entry.ini          **占位输入**（= 你声明的 project_entry）：让 preflight 能说出还差什么
```

⚠️ 生成目录放着不管也能跑：它在仓库里靠“往上找 `tools/`”，在仓库之外靠
`PW_PLATFORM_TOOLS`（指向 `<仓库>/tools`）。后者忘了设时它会明确告诉你。

⚠️ 两个开关别搞混：`--id` 只是**名字**，`--project`（默认 `demo`）是**设备应该自报的
project**，它写进 `identity_expectation`。只改 `--id` 不改 `--project` 的话，
设备必须自报 `project=demo` 才能过 `verify`。

## 1. 五步通路（先跑假工具，再换真工具）

```powershell
python tools\hwops.py run --adapter my-board --targets my_target\my_board.py --steps preflight --fake
```

| 步骤 | 它证明什么 | 判据来自哪 |
| --- | --- | --- |
| `preflight` | 契约自洽、输入在、工具解析得出来、台架配了 | 契约 + 台架 |
| `build` | 构建**真产出了声明的产物** | 该步骤的 `SuccessCriterion` |
| `flash` | 真写进去了（且**先要有两条显式同意**） | 同上 + 安全闸门 |
| `verify` | 设备自报的身份与**要核的字段**对得上 | `identity_expectation` |
| `debug-check` | 通信口打得开、读得到行（不写调制线，不复位） | 注入的串口实现 |

⚠️ 生成物默认拿**平台的假工具**，所以 `preflight` 会过、而 `build` 会红在"声明的产物没出现"。
那是 TODO 还没填（产物要你的工具**真**产出），不是通路不通。

## 2. 声明里必须填对的三件事

### 2.1 构建判据：退出码 0 不等于成功

```python
success=contract_mod.SuccessCriterion(
    kind="log",                                        # 要看日志就写 log
    success_markers=("Linking",),                      # 必须出现
    error_count_pattern=r"Total number of errors:\s*(\d+)",
    require_count=True,                                # 必须有这条统计行，否则"无法判定"
    min_artifacts=2,                                   # 声明的产物必须真的出现
)
```

- `kind="exit"`：只看退出码 + 产物是否出现；
- `kind="log"`：再加日志标记；`require_count=True` 表示"数不到统计行 ≠ 没事"；
- `failure_markers`：出现就判失败（例：`Fatal error`、`Connection lost`）。

### 2.2 故障知识：把"看到这个标记 ⇒ 下一步做什么"写成数据

```python
faults=contract_mod.FaultKnowledge(
    markers=(contract_mod.FaultMarker("No MCU device found", "flash_failed",
                                      "探针没连上目标（这次是零写入）"),),
    recovery=("把探针拔插一次再试，不要连着重试",),
    unknown_advice="没见过的失败：先保留原文再判断",
    step_defaults=(("build", "build_failed"), ("flash", "flash_failed")),
)
```

判据在于：**失败详情里必须出现处置建议**（不是躺在文档里等人去翻）。

### 2.3 能力四态：做不到就明说

```python
capabilities=(
    contract_mod.CapabilityResult("build", "supported"),
    contract_mod.CapabilityResult("symbolize", "unsupported", "这条链没有地址还原入口"),
)
```

`unsupported` / `skipped` / `refused` 都必须带理由。留空等于"没人知道"。

## 3. 本机事实走台架，不进代码

工具装在哪、工程根在哪、板子插在哪个口 —— 这些是**这台电脑的事实**：

```python
# 声明里只列键名（形状）
bench_keys=("my_tool_dir",)

# 装配处把值接进来（值来自 bench.local.json / 环境变量 / --bench）
contract_mod.AdapterWiring(
    declaration=decl,
    project_dir=...,              # 工程根
    tools={"my-tool": ...},       # 工具键 → 可执行文件
    bench_values={},              # 台架键 → (配上了没, 从哪读的)
    registered=ADAPTERS,          # **受控适配器登记表**（旁路检查对着它比）
    port_factory=...,             # 串口实现（不交 ⇒ debug-check 如实 unsupported）
    identity_parser=...,          # 身份回答的解析器（回答格式是那块板的事实）
)
```

坏了或没配的台架值由入口拒绝启动（退出码 `2`），不猜路径。

⚠️ **台架文件里只接受"有目标声明消费的键"**（不做"认得的用、认不得的丢掉"）。
所以你自己声明的 `bench_keys` / `tool_keys` 要能被 `bench.local.json` 接受，装配处
得看得见你的模块 —— 两个做法：

```powershell
# ① 本机登记（不改仓库）：用**模块名**登记，台架校验就会把它算进来
$env:PW_HWOPS_TARGETS = "my_board"          # 多个用逗号分隔
# ② 随仓库发布：把它加进 tools/webconsole/bench_setup.py 的 TARGET_MODULES（见 §5）
```

⚠️ **还有一环：台架键得有人"消费"。** 台架文件里允许出现哪些键，是由各目标模块的
`bench_declaration()` 拼出来的（`bench_setup.DECLARATION_FACTORY`）。模块里没有这个函数，
它的键就**没人认领**：`bench.local.json` 会被整份拒绝（退出码 `2`），报错只说
"没有目标声明消费这个工具键"。`create-adapter` 生成的骨架**不含**这个函数
（骨架的 `bench_keys` 默认是空的）—— 打算用台架键，就自己补一个：

```python
def bench_declaration() -> bench_config.BenchDeclaration:   # 装配处按这个名字找
    return bench_config.BenchDeclaration(
        owner="my-board",                                   # 谁声明的
        tool_keys=TOOL_KEYS,                                # 这份声明消费的工具键
        firmware_ids=(FIRMWARE_ID,),                        # 台架 projects 里的固件 id
        device_keys=(DEVICE_KEY,),                          # 台架 devices 里的设备键
    )
```

## 4. 机器检查（五件事一次跑完）

```powershell
python tools\hwops.py check --module my_target\my_board.py     # 单份外部模块
python tools\hwops.py check --targets my_target\my_board.py    # 带上装配（能判台架键）
```

| 检查 | 挡的是什么 |
| --- | --- |
| 契约完整性 | 声明缺项、步骤没覆盖、能力没理由 |
| 旁路厂商命令 | 步骤里的 `adapter_id` 不在你的 `ADAPTERS` 登记表里 |
| 判据重复 | 按步骤名挑判据 / 两步共用同一套判据 |
| 跨 profile 串用 | 两份固件共用产物 / 工程入口 / 适配器 ID |
| Core 泄漏 | core 里出现平台知识 |

"本机事实不许进仓库"是**第六件**，但它是独立命令：`python tools\hwops.py opensource-audit`。

`unjudged` 与 `problems` 是两回事：前者是"这一项没判"（缺材料），后者是"判了而且不过"。
**"没判"不许当成"通过"。**

## 5. 让统一入口看见你的模块

三种做法：

1. `--targets <模块名或 .py 路径>`（推荐用**路径**，裸名字在包布局下可能命中整个目录）——
   注意这一种**不会**让你的台架键被 `bench.local.json` 接受（详见 §3）；
2. 环境变量 `PW_HWOPS_TARGETS`（逗号分隔）—— 这一种**会**（模块名形式）；
3. 随仓库发布时才登记进仓库，**三处一起改**：

```text
tools/webconsole/bench_setup.py        TARGET_MODULES 加一行（模块名）
tools/webconsole/platform_boundary.py  LAYERS 加这一层的层名
                                       MODULE_LAYERS 登记这个模块属于哪一层
```

⚠️ 只改第一处的话，普通 `check`（不带 `--targets`）会报
「层名不在平台的分层表里」——那就是在提醒你后两处还没改。
⚠️ 这三处就是 core（装配处 + 边界自述），新增**随仓库发布**的目标必然要碰它；
外部工程接自己的硬件请走前两种，那两种**不必改仓库**。

## 6. 真机验收模板

你只需要补充**你的硬件的最小成功证据**：

```text
1) 工具链：装在哪（台架键 / 环境变量），版本是什么
2) 构建：命令 + 退出码 + 声明的产物确实出现了（写清产物名与大小）
3) 烧录：两条显式同意的理由；工具判据；写完之后设备是否还在
4) 身份：问身份的命令；设备回了什么；**期望值来自哪份产物**
5) 通信：读到几行；有没有写调制线（写了就等于复位了设备）
6) 失败处置：至少一条已知故障的现场输出 + 你实际做了什么
```

## 7. 已知常态（不是 bug）

- 拿假工具跑 `build` 会红在"声明的产物没出现" —— 那是 TODO 没填完；
- 没有 `bench.local.json` 时 `preflight` 报"台架键未配置" —— 那是让你去填，不是框架坏了；
- `debug-check` 报 `unsupported` + `transport_unavailable` —— 没交串口实现（`port_factory=None`），
  或者口被别的进程占着（先让占用者放手，不要当设备故障去查）；
- `verify` 报 `unsupported`/`skipped` + `identity_unavailable` —— "问不了"（没交解析器，或拿不到自报）。
  只有"口打不开"那一种才是 `transport_unavailable`；
- `doctor.py` 报没有 pyserial（WARN）—— 只做框架自检时忽略它。

## 8. 怎么算接完了

**通路层（机器可判，五条）**

1. `python my_target\test_<模块>_contract.py` 全绿；
2. `check --module` 无 `problems`；
3. `list --targets` 认得它，且能力四态齐全；
4. `run --steps preflight --fake` 通过；
5. 注入破坏（旁路 / 按步骤名挑判据 / 共用产物）时 `check` 会红。做法：把生成物复制一份，
   照下面任一改法改一下，两条命令各跑一次，应当**红**（改回来就绿）：

   | 注入 | 怎么改 | 期望 |
   | --- | --- | --- |
   | 旁路厂商命令 | 把某一步的 `adapter_id` 改成一个不在 `ADAPTERS` 里的名字 | `check` 报"用了未登记的适配器" |
   | 按步骤名挑判据 | 模块里加一段 `if step == "build": ok, note = my_judge(...)` | `check` 报"在按步骤名的分支里挑判据" |
   | 共用产物 | 让两步声明同一个 `artifacts` 项 | `check` 报"共用产物" |

   ⚠️ 带 `--targets` 时层名那一项按"还没登记"的口径检；不带时才核分层表。

**硬件层（要真工具链与真板子，三条）**

6. `run --steps preflight,build` 真构建成功，且**声明的产物真的出现**；
7. `run --steps preflight,flash,verify --confirm-flash --allow-overwrite` 真烧录 + 身份核验通过；
8. `run --steps preflight,debug-check --port <你的口>` 读到数据。

只做完通路层 = **骨架完成**，不是"这块硬件接完了"。
