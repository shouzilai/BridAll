"""平台核心的**两个基础原语**（PCR-001 / R2a）：时间戳与行切分。

它们原本借住在目标层的模块里，于是四个 core 模块为了一个时间戳、两个 core 模块为了
一个纯函数，被迫 import 目标层（`devices` / `pipeline`）—— 那是**反向依赖**：
核心不该为了拿一个纯函数而拉进参考目标的构建与设备知识。

```text
utc_now()              原来在 devices.py   ← 目标层（串口登记）
split_complete_lines() 原来在 pipeline.py  ← 目标层（PlatformIO 编排）
```

现在它们住在这里（core），**全平台只有这一份实现**：

- `devices.utc_now` / `pipeline.split_complete_lines` 仍然可用，但已改成**转口**
  （`from .core_primitives import …`），所以老调用方一个都不用改；
  契约测试用 `is` 比对两个名字指向同一个函数对象，防止有人又抄一份。
- 这个模块**不 import 任何东西**（连 `from __future__` 之外都没有），
  所以它永远不会把目标层拖进 core。

两条不变量（都有测试）：

1. **`utc_now()` 的格式是证据的一部分**：`YYYY-MM-DDTHH:MM:SS.mmmZ`（UTC、毫秒、末尾 `Z`）。
   会话/manifest 里所有人类可读的时间戳都用它 —— 改了格式就是改了证据的可读约定。
2. **`split_complete_lines()` 保留行终止符**、只吐出以 `\\n` 结尾的完整行，
   剩下的字节留在缓冲区里等下一次。解码器要靠行终止符判断行边界，
   而 run 的 `_LineCollector` 与实时观察的读取线程**必须**把同一段字节切成一模一样的行
   （需求 §5：TX/RX 的字节才是证据）。
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> str:
    """当前 UTC 时间，`YYYY-MM-DDTHH:MM:SS.mmmZ`（毫秒精度、以 `Z` 结尾）。

    ⚠️ 这是**墙钟**，用来让人读（"这条是什么时候发生的"）；判定先后关系必须用单调时钟
    （`time.perf_counter()`）—— 墙钟可能被自动校时改动（见 `observe_store` 的模块说明）。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def split_complete_lines(buffer: bytearray) -> list[bytes]:
    """从缓冲区里切出**完整的行**（以 `\\n` 结尾），剩下的继续留在缓冲区里。

    单独抽出来是因为它有两个使用者：run 的 `_LineCollector` 与实时观察的读取线程。
    两条路必须把同一段字节切成一模一样的行 —— 否则"同一份字节流、两种解释"会变成
    两个都不值得信的结论（需求 §5：TX/RX 的字节才是证据）。
    保留行终止符：解码器要靠它判断行边界。
    """
    lines: list[bytes] = []
    while True:
        index = buffer.find(b"\n")
        if index < 0:
            break
        lines.append(bytes(buffer[: index + 1]))
        del buffer[: index + 1]
    return lines


# 一段字节安静超过它就"算一行"（终端语义：字节到了就显示，不必等换行）。
#
# ⚠️ 为什么要它（真机实测）：设备的回显**不带换行**（STM32 那块板回的是 `1\x1b[0m`），
#    而窗口是按行显示的 ⇒ 敲一个字符要等设备**下一次输出换行**才看得见；实测那次
#    心跳（`[D/main] Hello RT-Thread!`）是 1 s 一次，于是敲下去到看见它花了 45〜1940 ms。
#    30 ms 是拿感知阈值（~50 ms）反推的：再大就能被看出"慢"。
PARTIAL_LINE_IDLE_S = 0.03

# 读的超时：**它同时是"安静多久才发现"的上限**（读线程在 read 空手而归时才检查安静）。
#
# ⚠️ 这一条以前写在**驱动层**（`devices.open_read_only` 里硬写 `port.timeout = 0.1`），
#    而它会直接决定"安静的尾巴"晚多久才出现：0.1 s ⇒ 最多迟 100 ms（真机实测 104 ms，
#    就是它）。读超时属于**读取策略**，不该住在"怎么打开端口"那一层。
READ_TIMEOUT_S = 0.01


def partial_idle_s(baud: int | None, minimum: float = PARTIAL_LINE_IDLE_S) -> float:
    """安静多久算一行：**至少 `minimum`**；波特率很低时按"一行大概多少字节"放宽。

    ⚠️ 阈值本身会把"一行"切开：115200 波特率下 30 ms ≈ 345 字节，正常日志行到不了
    （最长的也就一百多字节）；但如果有人用 9600 甚至 1200 跑，同样的 30 ms 只能盖住
    36 / 4.5 字节 —— 那就会把一行切成好几段。所以这里按"20 字节的传输时间"兜住。
    不知遗波特率时就用 `minimum`（调用方知道自己开在什么速度上）。
    """
    speed = float(baud or 0)
    if speed <= 0:
        return minimum
    return max(minimum, 20 * 10 / speed)


def idle_partial(
    buffer: bytearray,
    *,
    shown: int = 0,
    idle_s: float,
    threshold_s: float = PARTIAL_LINE_IDLE_S,
) -> bytes:
    """安静够久 ⇒ 把"半行"里**还没显示过**的那一段交出来（只给视图）。

    两个字段的意思：
    - `shown`：这一行的前多少字节已经推给窗口了（免得下一行完整到达时又画一遍）；
    - `idle_s`：距上一次收到字节过了多久。

    ⚠️ **不消费缓冲区**：半行仍然是半行 —— 身份识别与实时解码只认完整行，
    否则一句话被切开就可能认不出启动横幅（那是"看着合理"的错误）。
    """
    if idle_s < threshold_s or shown >= len(buffer):
        return b""
    return bytes(buffer[shown:])


def visible_line_remainder(line: bytes, shown: int = 0) -> bytes:
    """Return the completed-line bytes that have not already been painted.

    ⚠️ 这个函数也用在**半行（idle）补写**那条路上，所以“把纯行尾也返回”会立刻变成
    每个字符一行（实测）。行尾丢失的问题不能在这里补 —— 终端要的是**字节流**，
    不是从行事件里拼回来的片段（见 `docs`/后续改造：RX 分块直接推给终端）。
    """
    remainder = bytes(line[max(0, int(shown)):])
    if shown > 0 and not remainder.rstrip(b"\r\n"):
        return b""
    return remainder
