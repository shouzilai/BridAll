# 命令参考

> 只列**公开入口**的开关与它们的含义。想看实现看 `tools/hwops.py` 与 `tools/doctor.py`。

## 1. `tools/hwops.py` —— 统一操作入口

```text
python tools\hwops.py [全局开关] [子命令] [子命令开关]
```

不给子命令 = 跑一遍 `list`。

### 全局开关（⚠️ `--bench` 只能写在**子命令之前**；`--targets` 两边都收）

| 开关 | 含义 |
| --- | --- |
| `--bench <路径>` | 台架文件（默认按 `PW_BENCH_FILE` > `<仓库>/bench.local.json` 找）—— **只能写在子命令前** |
| `--targets <清单>` | 额外的接入契约在哪儿（逗号分隔的**模块名**或 `.py` **路径**）；也可用 `PW_HWOPS_TARGETS`；这个子命令后也能写 |

### `list`

| 开关 | 含义 |
| --- | --- |
| `--json` | 机器可读 |

打印这台机器上装配的接入契约（一句话自述 + 通信口）。本机配置里**交不出来**的那几条
会让退出码变成 `2`。

### `check`

| 开关 | 含义 |
| --- | --- |
| `--json` | 机器可读（含 `problems` 与 `unjudged`） |
| `--skip-core` | 跳过「Core 泄漏」那一项 |
| `--module <文件>` | 额外检查一个外部声明模块（脚手架生成物） |
| `--factory <名>` | 上面那个模块里的工厂函数名（默认 `declaration`） |

判五件事：契约完整性 / 旁路厂商命令 / 判据来源 / 跨 profile 串用 / Core 泄漏。
（"本机事实"不进仓库是第六件事，但它是**独立命令** `opensource-audit`，`check` 不做。）
`unjudged` = **这一项没判**（缺材料），不是通过。

### `run`

| 开关 | 含义 |
| --- | --- |
| `--adapter <id>` | 跑哪一条接入契约（`list` 里有） |
| `--firmware <值>` | 没点名 adapter 时按固件/配置挑 |
| `--steps <清单>` | 逗号分隔，默认全部（`preflight,build,flash,verify,debug-check`） |
| `--port <口>` / `--baud <数>` | 通信口与波特率（默认取台架里配的那一个） |
| `--seconds <秒>` | `debug-check` / 身份读多久（默认 3） |
| `--timeout <秒>` | 覆盖命令超时 |
| `--confirm-flash` + `--allow-overwrite` | **烧录的两条显式同意**：缺一条 ⇒ `refused`，一个字节都不写 |
| `--fake [<id>=fail\|timeout\|silent]…` | 拿平台自带的假工具跑（不碰硬件） |
| `--no-evidence` | 不落证据（⚠️ 熔断账本仍会更新） |
| `--json` | 机器可读 |

### `create-adapter`

| 开关 | 含义 |
| --- | --- |
| `--out <目录>` | 生成到哪儿（必需） |
| `--id <适配器 id>` | 稳定、小写、用连字符分隔（必需）。输入里的 `.` 会被换算成 `-`：模块名 = `--module`，或 "id 里的 `-` 换成 `_`" |
| `--target <目标 id>` | 默认与适配器 id 相同 |
| `--label <人话名字>` | 报告与 `list` 里显示的名字 |
| `--layer <层名>` | 它属于哪一层（默认 `target-demo`；随仓库发布要登记进分层表） |
| `--module <模块名>` | 默认由 id 推导 |
| `--project-entry <相对路径>` | 工程入口（默认 `project/entry.ini`） |
| `--config <名>` | 构建配置名（默认 `Debug`） |
| `--baud <数>` / `--line-ending cr\|lf\|crlf` | 通信参数（默认 115200 / lf） |
| `--identity-query <字符串>` | 问身份的那一句（默认 `identity`） |
| `--project <名字>` | **设备应该自报的 project**（默认 `demo`，写进 `identity_expectation`） |
| `--no-verify` | 生成后不立刻跑生成物的契约测试 |

### `fuse`

| 开关 | 含义 |
| --- | --- |
| （无） | 打印熔断账本 |
| `--clear` | 人工处置之后的**显式恢复**（全部） |
| `--clear --adapter <id>` | 只清这一条 |

### `opensource-audit`

| 开关 | 含义 |
| --- | --- |
| `--scope framework\|platform\|all` | 扫哪一档（默认 `framework`：核心框架 + 入口文档） |
| `--path <glob>` | 自定义范围（可多次） |
| `--include-tests` | 把测试也算进来 |
| `--allow <文件>:<形状>` | 显式豁免（必须点名到**哪一份内容的哪一种形状**，可多次） |
| `--json` | 机器可读 |

## 2. `tools/doctor.py` —— 只读体检

| 开关 | 含义 |
| --- | --- |
| `--json` | 机器可读（同一份报告） |
| `--target <id>` / `--firmware <id>` | 只看某个目标/固件 |
| `--hardware` | 额外做一次**只读**串口枚举（不打开、不复位、不写入） |
| `--host` / `--port` | 端口检查的地址与端口（默认 `127.0.0.1:8770`） |
| `--root <目录>` | 平台工作目录（默认本文件所在的仓库根） |
| `--bench <路径>` | 台架文件 |

退出码：`0` 无平台阻断（允许有 `WARN`）；`1` 存在平台 `FAIL`；`2` 体检自身跑不完。

## 3. `tests/`

| 命令 | 说明 |
| --- | --- |
| `python tests\run_all.py` | 跑一遍全部**不需要硬件**的检查（推荐） |
| `python tests\test_hwops_contract.py` | 核心契约套件（假工具 + 假串口） |
| `python tests\test_stm32_target.py` / `test_esp32_target.py` | 参考适配的契约测试 |
| `python tests\target_contract_harness.py` | 共用夹具的自检 |
| `python tests\test_web_acceptance.py` | Web 验收：页面 / 静态资源 / 三种模式 / 历史回放 / 嗅探 / 连接生命周期 / 空候选口 / 未知身份 / 串口参数 / 公共传输实现与失败面（无需硬件与 pyserial） |
| `python tests\test_repo_hygiene.py` | 仓库卫生：随发布的文本文件不许带 UTF-8 BOM |
| `python tests\browser_e2e.py` | **真实浏览器** E2E：桌面 1280×800 + 窄屏 390×844（嗅探 / 选择 / 连接 / 终端收发 / 断开重连 / 三种模式）。需本机有 `node` + `playwright`；没有就如实打 `SKIP`（`run_all` 会把它单独记成跳过，**不算通过**） |

## 4. `tools/webconsole/server.py` —— Web 工作台

| 命令 | 说明 |
| --- | --- |
| `python tools\webconsole\server.py --demo` | 演示模式：用仓库自带的合成数据（无硬件），默认 `http://127.0.0.1:8770/` |
| `python tools\webconsole\server.py` | 普通模式：**不填任何配置也能用**（系统枚举到的串口就是候选口）；写 `bench.local.json` 那么那些口就带上别名、默认参数与已适配目标绑定 |
| `python tools\webconsole\server.py --bench <文件>` | 指定本机台架文件（默认按 `PW_BENCH_FILE` > `bench.local.json` 找） |
| `python tools\webconsole\server.py --stop` | 让正在跑的那个实例优雅退出（握着串口时会拒，除非 `--force`） |

- 候选通信口 = **系统串口枚举**（基础源） + `bench.local.json` 的 `devices.<键>.port`（增强），
  在**同一份列表**里合并去重、台架点名的排前面；没有 pyserial 时枚举不出来，那句话会写在候选口说明里；
- 口可以是 pyserial URL（`loop://`、`socket://主机:端口`）；**没适配、身份问不到的口照样能选、能连、
  能收发、能断开重连**（“通用串口”，没有身份与能力声明）—— 适配是**增强**，不是使用前置条件；
- 基础串口参数（波特率 / 数据位 / 校验位 / 停止位）在**打开之前**设上去，非法值当场 400；
- 普通模式是**只读**的：不构建、不烧录、不动调制线；嗅探不抢占已连接的口。

## 5. `tools/webconsole/platform_boundary.py`

直接跑一次 = 打印分层表与边界检查结论（`core` 里有没有混进目标层知识）。
