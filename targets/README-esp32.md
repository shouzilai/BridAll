# ESP32 参考适配

一条 profile：

| 适配器 ID | 构建 | 烧录 | 读回校验 | 通信 |
| --- | --- | --- | --- | --- |
| `esp32-generic-fw` | PlatformIO（`pio run`） | PlatformIO（`pio run -t upload`） | esptool（`verify_flash`） | 串口只读 |

判据是**数据**：

- 构建：看 PlatformIO 的 `[SUCCESS]` + **核产物**（`firmware.bin` / `firmware.elf`）；
- 烧录：看 `[SUCCESS]`，并把 `Failed to connect` / `Timed out waiting for packet header` /
  `A fatal error occurred` 归到 `flash_failed`；
- 读回校验：esptool 的 `Hash of data verified.` —— 判的是"读回来的和我这份一样"，不是"退 0"。

## 本机事实（不进仓库）

| 槽 | 环境变量 |
| --- | --- |
| `tools.pio` | `PW_ESP32_PIO` |
| `tools.esptool` | `PW_ESP32_ESPTOOL` |
| `projects.esp32-generic-fw` | `PW_ESP32_PROJECT` |
| `devices.esp32a.port` | `PW_ESP32_PORT` |

## 期望值来自**本次构建的产物**

`verify` 那一步的 `build_id` 期望值从 `build/identity.json` 读（`IdentitySource`）：

```python
contract_mod.IdentitySource(
    artifact="build/identity.json", key="build_id", field="build_id",
    template="0x{value:08x}",
)
```

- 读得到 ⇒ 拿它比（不符就红）；
- 读不到 ⇒ 该字段记成**未比**（结论里点名，**不是默默放过**）。

写死的常量只能证明"设备报得出某个值"，证不了"这次烧的就是这次编的"。

## 跑一遍

```powershell
python tools\hwops.py list  --targets targets\esp32_target.py
python tests\test_esp32_target.py                      # 不接硬件
python tools\hwops.py run --adapter esp32-generic-fw --steps preflight,build
python tools\hwops.py run --adapter esp32-generic-fw --steps preflight,flash,verify `
       --confirm-flash --allow-overwrite
python tools\hwops.py run --adapter esp32-generic-fw --steps preflight,debug-check --port <PORT>
```

## 常见失败与处置

| 标记 | 故障码 | 下一步 |
| --- | --- | --- |
| `Failed to connect` | `flash_failed` | 先确认没有别的程序占着串口；再按一下板子复位键 |
| `Timed out waiting for packet header` | `flash_failed` | 板子没进下载模式（多半是串口被占） |
| `A fatal error occurred` | `flash_failed` | esptool 自己放弃了：先看它上面那几行 |
| `*** [.pio` | `build_failed` | PlatformIO 报错：看它上面那几行 |

`debug-check` **不写 DTR/RTS**，所以打开串口不会复位板子。
