# STM32 参考适配

两条 profile，同一块板：

| 适配器 ID | 固件 | 构建 | 烧录 | 说明 |
| --- | --- | --- | --- | --- |
| `stm32f103-baremetal` | 裸机 | IAR `IarBuild` | C-SPY 下载（argv 由本层算） | 判据 = 数日志里的错误行 + 核产物 |
| `stm32f103-rtthread` | RT-Thread | GNU `make` | C-SPY 下载（同上） | 判据 = 退出码 + 产物 |

## 本机事实（不进仓库）

按同一个梯子取值（`--bench` / `bench.local.json` → 环境变量 → 无兜底值）：

| 槽 | 环境变量 |
| --- | --- |
| `tools.iar_build` | `PW_STM32_IAR_BUILD` |
| `tools.cspybat` | `PW_STM32_CSPYBAT` |
| `tools.make` | `PW_STM32_MAKE` |
| `projects.stm32f103-baremetal` | `PW_STM32_BARE_PROJECT` |
| `projects.stm32f103-rtthread` | `PW_STM32_RT_PROJECT` |
| `devices.stm32a.port` | `PW_STM32_PORT` |

## 跑一遍（先假工具，再真机）

```powershell
python tools\hwops.py list  --targets targets\stm32_target.py
python tools\hwops.py check --targets targets\stm32_target.py
python tests\test_stm32_target.py                      # 不接硬件：假工具 + 假串口
python tools\hwops.py run --adapter stm32f103-baremetal --steps preflight,build
python tools\hwops.py run --adapter stm32f103-baremetal --steps preflight,flash,verify `
       --confirm-flash --allow-overwrite
```

## 已知故障处置（写进判据里的那一条）

`Flash loading failed` / `No MCU device found` / `Session aborted` 都是 `flash_failed`，
处置只有一条：**把探针拔插一次再试，不要连着重试**。
同一故障码连续两次之后会被**熔断**挡住（第三次在调用工具之前被拒），
人工处置之后用 `python tools\hwops.py fuse --clear` 显式恢复。

## 身份核验

设备自报一行 `@PWINFO project=… build_id=…`；核验比对 `project` 与 `build_id`。
⚠️ 这份参考构建**没有**交出一份可读的身份产物，所以 `build_id` 只证明"设备报得出来"，
证不了"这次烧的就是这次编的"（要接上就先让构建落一份身份清单，形状见 ESP32 那条）。
