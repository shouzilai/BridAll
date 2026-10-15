# AI Agent 短入口

> 只读这一份就能开始干活。**不要**去猜设计意图，也不要去读不存在的历史文档。
> 需要接一条新硬件时再读 [docs/adapter-guide.md](docs/adapter-guide.md)。

## 1. 你现在能做什么（四条命令）

```powershell
python tools\doctor.py                     # 这台电脑能不能跑（只读，不碰设备）
python tools\hwops.py list                 # 这台机器上装配了哪些接入契约
python tools\hwops.py check                # 机器检查：完整性 / 旁路 / 判据 / 串用 / Core 泄漏
python tests\test_hwops_contract.py        # 核心契约套件（假工具，不需要硬件）
```

生成一条骨架并当场跑通（不需要硬件）：

```powershell
python tools\hwops.py create-adapter --out <目录> --id <适配器 id> --label "<人话名字>"
```

## 2. 五步入口（四条命令）

```text
preflight -> build -> flash -> verify -> debug-check
```

```powershell
python tools\hwops.py run --adapter <id> --steps preflight
python tools\hwops.py run --adapter <id> --steps preflight,build
python tools\hwops.py run --adapter <id> --steps preflight,flash,verify --confirm-flash --allow-overwrite
python tools\hwops.py run --adapter <id> --steps preflight,debug-check --port <PORT>
```

- 只想验通路：上面每条都加 `--fake`（用平台自带的假工具，不碰任何硬件）。
- 不想落**证据**：加 `--no-evidence`（⚠️ 熔断账本 `artifacts/hwops/fuse.json` **仍会**更新
  —— 它是"同一故障连续几次"的记账，不落它熔断就不成立）。
- 想看机器可读结论：加 `--json`。

## 3. 退出码就是结论

| 码 | 含义 | 你该做什么 |
| ---: | --- | --- |
| `0` | 通过 | 继续下一步 |
| `1` | 有失败 | 读每一行的**故障码**与详情里的处置建议 |
| `2` | 被拒（安全闸门 / 熔断 / 台架文件坏） | **没有写任何字节**；先处置再显式恢复（`fuse --clear`） |
| `3` | 没失败但也没跑完（未核验 / 不支持 / 跳过） | 不允许当成"通过"；按详情补齐再跑 |

## 4. 停止条件（出现这些就停下来问人）

- 需要**真机授权**：烧录要两条显式同意，且要有 `--allow-overwrite` 的明确理由；
- 熔断（`fuse_open`）：不要重试，先人工处置（例：拔插一次探针），再 `fuse --clear`；
- 台架键未配置（`bench_unconfigured`）：不要去猜工具/端口的路径，让人填 `bench.local.json`；
- 归属或授权不明的文件：不要放进仓库，先登记。

## 5. 不许做

- 不许把绝对路径、串口名、序列号、私有工程、受限二进制写进仓库
  （`python tools\hwops.py opensource-audit` 会逐行报出来）；
- 不许绕开登记表直接拼厂商命令行（`check` 的旁路检查会红）；
- 不许按步骤名挑判据（判据只能来自声明）；
- 不许在 core 里写 `if <某块板>`（`platform_boundary.scope_problems()` 会红）；
- 不许为让测试变绿而放松判据。

## 6. 一次改动的完成定义

1. `python tests\test_hwops_contract.py` 全绿；
2. `python tools\hwops.py check` 通过（没有 `problems`）；
3. `python tools\hwops.py opensource-audit` 无本机事实命中；
4. 提交信息里写清：改了什么、用什么命令验的、结果是什么。
