# 开源嵌入式工作平台

一套**与芯片、工具链无关**的硬件接入框架：把新硬件平台从"每个 Agent 各自摸索工具链"变成
一条受约束、可验证、可复核的固定流程。

```text
发现环境 -> 创建适配 -> Doctor -> 编译 -> 烧录 -> 程序身份验证 -> 调试/通信验证 -> 形成证据
```

框架不提供你的硬件工程和厂商工具，它提供**适配器应当声明什么、怎么执行、怎样判成功、
失败何时停止、如何证明适配完成**。

## 一分钟上手（不需要任何硬件）

```powershell
python tools\doctor.py                    # 只读体检：这台电脑能不能跑这个框架
python tools\hwops.py list                # 这台机器上装配了哪些接入契约
python tools\hwops.py check               # 机器检查（完整性 / 旁路 / 判据 / 串用 / Core 泄漏）
python tests\test_hwops_contract.py       # 核心契约套件（假工具 + 假串口，全程不接硬件）
```

生成一条新的适配器骨架，并**当场**用假工具把它跑通：

```powershell
python tools\hwops.py create-adapter --out my_target --id my-board --label "我的板子"
```

`create-adapter` 会生成**四**份东西，并且立刻跑一遍生成物自带的契约测试：

| 生成物 | 作用 |
| --- | --- |
| `my_target/<module>.py` | 接入契约：工具、步骤、判据、故障、能力（数据，不是分支） |
| `my_target/test_<module>_contract.py` | 契约测试：拿假工具跑成功/同意闸门/已知失败/未知失败/超时/身份/熔断 |
| `my_target/README.md` | 接入清单：还差哪几件事才算接完 |
| `my_target/<工程入口>` | 占位输入（默认 `project/entry.ini`）：让 `preflight` 能说出还差什么 |

## 在浏览器里用（Web 工作台）

框架自带一个本地 Web 工作台，三种工作模式共用同一份证据模型与资源闸门：

| 模式 | 做什么 |
| --- | --- |
| 自动验证 | 按声明的步骤跑一轮，把每一步的判据、耗时、证据与结论落成可复核的记录 |
| 实时观察 | 只读地看设备在说什么（原始字节、结构化事件、指标趋势），并可在会话上跑受控用例 |
| 离线诊断 | 回看历史 run 与会话、检索整份证据、导入导出诊断包 |

### 一、先看不接硬件的演示（30 秒）

```powershell
python tools\webconsole\server.py --demo
# 然后用浏览器打开 http://127.0.0.1:8770/
```

`--demo` 把证据目录与会话目录指向仓库内的 `demo/`（**合成演示数据**，不是任何真实台架记录：
时间是 2026-01-01、端口是 `FAKE0`）。演示目标是只读的（core 层假目标没有构建/烧录链，
能力表里如实写着 `unsupported`）：它能验证"三种模式、工作台与回放"，
**不能**替你验证真机。

### 二、接自己的设备（普通模式，只改一份本机配置）

不需要改任何 Python / JavaScript，也不需要写装配代码 —— 只把台架示例抄一份、填上**你的**通信口：

```powershell
Copy-Item bench.example.json bench.local.json
# 编辑 bench.local.json：填 devices.stm32a.port / devices.esp32a.port（要用的那个）
python tools\webconsole\server.py
```

启动后：点「嗅探」→ 从卡片里选一台设备 → 点「连接」。身份未知的设备**也允许先连上**，
连上之后会话自己去问身份（问不出来就按原始串口收字节）。**嗅探不抢占已连接的口**：
被本平台自己握着的口不会重开、也不会多发一个字节。

嗅探**一次一个通信口**地走：页面上有一个进度条与一句「正在嗅探 <口> · <已完成>/<总数>」，
轮到哪个口就先显示哪个口；一轮搭完（或出错）就收起来，不会永远停在“正在嗅探”。
同一时刻只允许一轮 —— 同时发了第二个请求的那个会拿到 **409**，而且**一个字节都不发**。

- **不填任何东西也能用**：候选口 = **系统串口枚举**（基础源） + `bench.local.json` 点名的那些口
  （合并去重，台架点名的排前面）；没有 pyserial 时枚举不出来，那句“先 pip install pyserial”会写在
  候选口说明里。平台**不会**按枚举顺序或“唯一在线”替你猜设备；
- 通信口也可以写成 **pyserial 的 URL**：`loop://`（写出去的回显回来）或
  `socket://主机:端口`（连一台自己的虚拟板）—— 于是“没有板子也把真实串口栈跑一遍”是可能的；
- 未适配、身份问不到的口**照样能选、能连、能收发**（“通用串口”，没有身份与能力声明）——
  适配只让能力**变多**，不是使用前置条件；
- 普通模式是**只读**的：不构建、不烧录、不动调制线（打开口不写 DTR/RTS，所以不复位设备）。
  要跑自动验证（构建 / 烧录 / 断言）请按 [docs/adapter-guide.md](docs/adapter-guide.md)
  登记带构建链的目标。

其余几条：

- 端口用 `--port` 换；默认只监听回环地址（`--host` 可改）；
- 服务端是**串口与厂商工具的唯一持有者**：浏览器只发意图、只读证据，自己从不打开串口；
- 真正写设备的两步（烧录 / 复位）要**两条显式同意**；同一故障码连续两次后，第三次在调用工具
  **之前**熔断，人工处置后用 `python tools\hwops.py fuse --clear` 恢复；
- 不带 `--demo` 时证据落在仓库根的 `runs/`、`observe-sessions/`、`sniff/` ——
  那几个目录只存在于本机，不进版本库;
- 公开的必绿入口 `python tests\run_all.py` 里有一条 **Web 验收**（页面 / 静态资源 / 三种模式 /
  历史回放 / 嗅探 / 嗅探进度与并发闸门 / 连接生命周期 / 空候选口 / 未知身份 / 串口参数 /
  公共传输实现与失败面），它不需要硬件、也不需要 pyserial；同一入口里还有一条 **真实浏览器 E2E**
  （桌面 1280×800 + 窄屏 390×844：嗅探 / 逐口进度 / 选择 / 连接 / 终端收发 / 断开重连 / 三种模式），
  它需要本机有 `node` + `playwright` —— 没有就如实记成**跳过**，不算通过。


## 参考适配

`targets/` 下有随发布的参考适配，**工具与工程位置一律从本机台架取**（不写死在代码里）：

| 适配器 | 目标 | 接什么工具 |
| --- | --- | --- |
| `stm32f103-baremetal` / `stm32f103-rtthread` | STM32F103 | IAR `IarBuild` + C-SPY；RT-Thread 那条用 GNU `make` |
| `esp32-generic-fw` | ESP32 | PlatformIO + esptool（串口与烧录共用一个口） |

```powershell
python tests\test_stm32_target.py        # 不接硬件：假工具 + 假串口
python tests\test_esp32_target.py
python tests\run_all.py                  # 全部无需硬件的检查
```

细节见 [targets/README-stm32.md](targets/README-stm32.md) 与
[targets/README-esp32.md](targets/README-esp32.md)。

## 框架的四条硬规矩

1. **判据是数据**：某一步算不算成功，由该步骤声明的 `SuccessCriterion` 决定 ——
   "退出码 0"不等于成功（有些工具失败时也返回 0）；"按步骤名挑判据"会被机器检查挡住。
2. **失败自己带着下一步**：已知失败标记映射到**稳定故障码**与处置建议；不认得的标记如实报
   `unknown_failure` 并保留原文，不编原因。
3. **能力四态，不许含糊**：`supported` / `unsupported` / `skipped` / `refused` ——
   后三者必须写理由，且都不等于"通过"。
4. **熔断在动手之前**：同一目标 + 同一故障码连续两次之后，第三次在调用厂商工具**之前**被拒，
   并且告诉你先做什么人工处置、再怎样显式恢复。

烧录永远要**两条显式同意**（`--confirm-flash --allow-overwrite`）。没有它们，
`flash` 这一步以 `refused` 收场，并且**一个字节都不写**。

## 目录

```text
tools/hwops.py              统一操作入口（list / check / run / create-adapter / fuse / opensource-audit）
tools/doctor.py             只读体检（平台能力 + 目标依赖 + 可选硬件枚举）
tools/webconsole/           核心框架包（契约、编排、检查、假工具、脚手架、台架、体检模型、边界自述）
targets/                    参考适配器（随发布），见 docs/adapter-guide.md
tests/                      契约测试（不接硬件）
examples/                   最小示例
docs/                       架构、接入指南、版本与发布信息
bench.example.json          本机台架文件的形状（抄成 bench.local.json 再填）
```

## 文档

| 文档 | 什么时候读 |
| --- | --- |
| [INSTALL.md](INSTALL.md) | 第一次装、第一次跑 |
| [HANDOFF.md](HANDOFF.md) | 接手这个仓库（人或 Agent） |
| [AGENT_WORKFLOW.md](AGENT_WORKFLOW.md) | AI Agent 的短入口：命令 + 判据 + 停止条件 |
| [docs/USING-WITH-AI.md](docs/USING-WITH-AI.md) | **用 AI 上手**：按场景可复制的提示词 + 别让 AI 做的事 |
| [docs/adapter-guide.md](docs/adapter-guide.md) | 接入一条新硬件 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 想改核心 / 想知道边界在哪 |
| [docs/COMMANDS.md](docs/COMMANDS.md) | 记不住某个开关是什么意思 |
| [docs/RELEASE-INFO.md](docs/RELEASE-INFO.md) | 版本、生成方式、校验和、已知限制 |

## 不做什么（明确边界）

- 不托管或分发 IAR、ST-Link、PlatformIO、esptool 等第三方工具；
- 不替开发者提供硬件工程，也不猜本机路径（工具装在哪、板子插在哪个口由**本机台架文件**给）；
- 不提供任意 shell，不自动杀进程、断电、清数据、改保护位；
- 不宣称一个通用实现能消除芯片、工具链与调试器的差异。

## 本机事实不进仓库

绝对路径、串口名、探针序列号、厂商私有工程、受限二进制、真实证据一律不进仓库：

```powershell
python tools\hwops.py opensource-audit                  # 框架面（默认）
python tools\hwops.py opensource-audit --scope all      # 全仓
```

它按行报出"本机事实"的形状，并给出显式豁免的口子（必须点名到**哪一份内容的哪一种形状**）。

## 许可证

本项目以 **MIT** 许可证发布，全文见 [`LICENSE`](LICENSE)。

第三方依赖、再分发条件，以及"本仓库明确不含什么"，见
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
