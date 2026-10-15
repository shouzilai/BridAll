# 安装与第一次运行

## 依赖

| 依赖 | 必需？ | 用途 |
| --- | --- | --- |
| Python ≥ 3.9 | **必需** | 框架自身（只用标准库） |
| pyserial | 可选 | 串口探测、`debug-check`、烧录后身份核验 |
| 你自己的工具链 | 可选 | 真机：构建、烧录（IAR / PlatformIO / esptool / make …） |

框架**不需要**任何第三方 Python 包就能跑完体检、机器检查、脚手架与全部契约测试。

## 三步

```powershell
# 1. 确认解释器够新
python --version            # 需要 >= 3.9

# 2. 只读体检（不连设备、不构建、不联网）
python tools\doctor.py

# 3. 契约测试（假工具 + 假串口，不需要硬件）
python tests\test_hwops_contract.py
```

第 3 步末尾应当打印 `全部通过`，进程退出码为 `0`。

想用串口相关能力（`debug-check`、烧录后身份核验）时再装：

```powershell
python -m pip install pyserial
```

## 本机台架文件（可选）

工具装在哪、板子插在哪个口，是**这台电脑的事实**，不进仓库。形状见 `bench.example.json`：

```powershell
Copy-Item bench.example.json bench.local.json
# 然后编辑 bench.local.json：tools / projects / devices
```

- 没有这份文件是**正常**的：框架一切照旧，只在真正要用到某一格时如实报"未配置"；
- 有这份文件但写坏了：入口**拒绝启动**（退出码 2），并把问题一次列全；
- 也可以用 `--bench <路径>` 或环境变量 `PW_BENCH_FILE` 指到别处。

## 接一条新硬件（最短路径）

```powershell
python tools\hwops.py create-adapter --out my_target --id my-board --label "我的板子"
python my_target\test_<模块>_contract.py            # 生成物自带的契约测试（假工具）
python tools\hwops.py check --module my_target\my_board.py
python tools\hwops.py list  --targets my_target\my_board.py
python tools\hwops.py run   --adapter my-board --targets my_target\my_board.py --steps preflight --fake
```

⚠️ 生成物**在仓库里**跑时不需要任何额外设置（它自己往上找 `tools/`，或用
`PW_PLATFORM_TOOLS` 指到 `<仓库>/tools`）；把生成目录放到**仓库之外**时，
请先设 `PW_PLATFORM_TOOLS`，否则生成物会拒绝启动并告诉你要设什么：

```powershell
$env:PW_PLATFORM_TOOLS = "<仓库路径>/tools"
```

完整清单见 [adapter-guide.md](docs/adapter-guide.md)。

## 退出码

`hwops.py` 与 `doctor.py` 用的码**含义相近但不完全一样**（都以“2 = 拒绝开始”为主）：

| 码 | `tools\hwops.py` | `tools\doctor.py` |
| ---: | --- | --- |
| `0` | 通过 | 平台无阻断（允许有 `WARN`） |
| `1` | 有失败（构建/烧录/核验没过） | 存在平台阻断（`FAIL`） |
| `2` | 被拒：台架文件无效，或安全闸门/熔断挡住了（**一个字节都没写**） | 体检自身跑不完（异常） |
| `3` | 没失败但也没跑完（跳过 / 不支持 / 未核验） | —— |

## 常见问题

**`doctor.py` 报"没有 pyserial"（WARN）**
只在不需要串口时忽略它；要接真机就 `pip install pyserial`。

**`doctor.py` 报"随仓库发布的参考适配 0 条"（WARN）**
这个仓库当前没有随发布的参考适配器；自己接一条，或忽略它（框架本身是完整的）。

**`hwops.py run` 报"工具不可用 / 命令起不来"**
工具键没有交出可执行文件，或指向的不是一个可执行文件。接真机时把 `tools` 换成真实路径；
只想验通路就跑 `--fake`。

**中文在管道里变成乱码**
框架的入口会自行把标准输出设成"编码不了就替换"；自己写的脚本请先调
`unicode_console.install()`，或设 `PYTHONIOENCODING=utf-8`。

## 在浏览器里用（可选）

```powershell
python tools\webconsole\server.py --demo     # 演示数据，不接硬件
```

浏览器打开 `http://127.0.0.1:8770/`。三种模式（自动验证 / 实时观察 / 离线诊断）、
统一日志工作台与演示回放都不需要硬件；接真机需要的工具与端口仍从本机台架取。
