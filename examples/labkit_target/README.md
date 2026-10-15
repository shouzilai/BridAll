# LabKit demo board —— 接入清单

这份目录是 `hwops create-adapter` 生成的**骨架**。它现在就能跑（拿假工具），
你的任务是把 `TODO` 换成自己工具链的真实声明。

生成的**四**份东西：

| 文件 | 作用 |
| --- | --- |
| `<模块>.py` | 声明文件：一条 `HardwareDeclaration` + 解析器 + `hwops_wirings()` |
| `test_<模块>_contract.py` | 契约测试：12 条判据（成功 / 同意闸门 / 已知失败 / 未知失败 / 零输出超时 / 身份 / 熔断） |
| `README.md` | 本文件：接入清单 |
| `project/entry.ini` | **占位输入**（默认 `project/entry.ini`）：只为让 `preflight` 能告诉你还差什么 |

## 一秒跑通

```powershell
$env:PW_PLATFORM_TOOLS = "<仓库路径>/tools"
python test_labkit_demo_contract.py
```

生成物里的假工具来自平台（`webconsole/hwops_fake.py`）：它按适配器 ID 给反应，
所以**不需要任何硬件**就能把下面六件事各跑一遍：

| 场景 | 它证明什么 |
| --- | --- |
| 正常路径 | 三步按声明的判据各自通过 |
| 没有显式同意 | 安全闸门在 Core 就挡住，**一个字节都不写** |
| 已知失败标记 | 稳定故障码 + 处置出现在失败详情里 |
| 零输出超时 | 与"超时但有输出"分开报 |
| 身份不符 / 拿不到 | `mismatch` 与"未核验"分开，都不冒充通过 |
| 连续两次熔断 | 第三次在调工具之前被拒 |

## 接入清单（做完一条勾一条）

1. [ ] **工程入口**：`project_entry` / `build_config` 指向你的工程（不是别人机器上的路径）。
2. [ ] **工具键**：`tools` 里列出要用的工具；参数里只写占位符（`{project}` / `{config}` /
       `{port}` / `{tools}`），不写绝对路径。
3. [ ] **构建判据**：退出码 0 不等于成功就写 `kind="log"`，并给出必须出现的标记、
       必须为零的统计行、以及**必须出现**的产物。
4. [ ] **烧录判据**：写清"这次算不算真写进去了"。失败时工具可能只打 Warning 而返回 0。
5. [ ] **故障知识**：把"看到这个标记 ⇒ 下一步做什么"写成 `FaultMarker` + `recovery`。
       别只写在文档里 —— 用到它的那一刻没人会去翻文档。
6. [ ] **能力四态**：做不到的写 `unsupported` 并给理由；留空等于"没人知道"。
7. [ ] **身份核验**：`identity.query` 与要比对的字段；拿不到就如实"未核验"。
       ⚠️ 骨架的期望值是 `values={"project": "demo"}`（默认 `demo`）—— 两个开关都
       要跟着改：`--project` 决定**设备应该自报什么**，`--id` 只决定它叫什么名字。
8. [ ] **台架键**：本机事实（装在哪、插在哪个口）写进台架文件，`bench_keys` 只列键名。
9. [ ] **机器检查**：`python tools/hwops.py check --module <你的模块>`
       —— 契约完整性、旁路命令、判据重复、跨 profile 串用、Core 泄漏一次跑完。
10. [ ] **让统一入口看见它**：`hwops.py list --targets <到 .py 的路径>`（也可以设
        `PW_HWOPS_TARGETS`，模块名与 `.py` 路径都行）。
        ⚠️ **台架键**（你声明的 `bench_keys`）要能被 `bench.local.json` 接受，装配处
        得看得见你的模块：用 `PW_HWOPS_TARGETS=<模块名>` 登记，或把它加进
        `bench_setup.py` 的 `TARGET_MODULES`（**随仓库发布**时才这么做）。
        ⚠️ 随仓库发布要**同时**改两处：`bench_setup.TARGET_MODULES` 加一行 +
        `platform_boundary.LAYERS` / `MODULE_LAYERS` 登记这个层与模块，否则普通
        `check`（不带 `--targets`）会报「层名不在分层表里」。
        能看到之后：`hwops.py run --adapter labkit-demo --targets <到 .py 的路径> --steps preflight,build --fake`。
        ⚠️ **`--fake` 是必需的**：这份装配把工具键指向平台的假工具（一个 `.py`），
        不加 `--fake` 会得到“命令起不来（WinError 193）· 工具不可用”。
        ⚠️ 给 `--targets` 用**路径**而不是模块名：裸名字在子目录布局下会命中那个目录。
        ⚠️ 生成物默认拿**平台的假工具**，所以 `preflight` 会通过、而 `build` 会红在“声明的产物
        没出现”—— 那是 TODO 还没填（产物要你的工具**真**产出），不是通路不通。
11. [ ] **真机证据**：`hwops.py run --adapter <id> --steps preflight,build` →
        接上工具 → `--steps flash --confirm-flash --allow-overwrite` → `verify` → `debug-check`。
        每一步的结论都带稳定故障码与耗时，落在 `artifacts/hwops/` 里。

## 边界（别踩）

- ⚠️ 改了两样东西要**同步改生成物自带的契约测试**：① 步骤的 `adapter_id`（测试里绑着
  `demo.*`，包括 `executor.called("demo.build")` 这种**不在断言句子**里的地方；改了之后
  那几条断言会静默退化成“恒真”）；② 要核的身份字段名（测试里绑着 `project` / `build_id`；
  改了之后 `身份不符判失败` 那条会红）。改它们是为了让测试跟着**你的声明**走，
  不是放松断言。
- **不许**把本机路径、串口名、序列号、厂商私有工程或受限二进制放进仓库：
  `hwops.py opensource-audit` 会逐行报出来。
- **不许**绕开登记表直接调厂商命令：`check` 的旁路检查会红。
- **不许**按步骤名挑判据：判据只能来自声明（那条规矩的由来）。
- Core 里**不许**出现你的平台名：声明放目标/项目层，Core 只按形状调。
