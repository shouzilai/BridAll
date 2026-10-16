"""受控设备嗅探的**执行器**（ / D1，core）。

与 `device_sniff`（D0 冻结的**形状与判断**）的分工：

```text
device_sniff   结论是什么、身份是哪一种、这份固件能不能用 —— 纯判断，不碰端口
sniff_service  怎么按顺序问那几个口 —— 执行，但只通过**注入**的四件事碰端口
```

它自己**不认识任何设备**。四个注入点：

| 注入 | 是什么 | 谁来给 |
| --- | --- | --- |
| `candidates` | 候选通信口（**台架策略允许的那些**） | 装配处（台架绑定） |
| `access(port)` | 这个口现在什么状态（`free`/`busy`/`absent`/`unknown`） | 目标层探测（零共享 `CreateFileW`） |
| `conflict(port)` | 资源闸门：实时观察 / 正在跑的 run 是不是握着它（返回一句"谁握着"） | `ObserveCenter` / `RunnerRegistry` |
| `exchange(port, baud, payload, max_bytes, wait_s)` | 打开 → 写 → 读到期限 → 关；**不碰复位线** | 目标层的传输实现 |

四条硬规矩（每条都有宿主夹具）：

1. **没有用户动作就没有端口访问。** 构造 `SniffExecutor` 不打开任何口；
   `sniff()` 是唯一入口，而且**只读端口状态 + 只对被允许的口发字节**。
2. **不可用的口一个字节都不发。** 占用 / 不存在 / 被闸门拦住 ⇒ 结论直接给出，
   `exchange` 一次都不会被调用（夹具直接数调用次数）。
3. **探针是受控、有限、可审计的声明**：一个口上最多**问 3 次**（`MAX_ATTEMPTS_PER_PORT`），
   声明的探针**轮流**用完这份额度 —— 于是"同一条探针失败后隔 3～5 s 重发"与
   "换下一条探针"是同一份预算，重试不会把一个口问成风暴。
   设备明说"不认识"（`unsupported`）或已经答上（`ok`）就立刻停
   （**不许对一个未知口依次试任意项目命令**）。
4. **每次尝试都留 TX/RX 证据**：写出去的字节与读回来的字节都以十六进制落盘
   （`probes.jsonl` **一次提问一行**，含第几次），结论与失败原因一起写；
   结论说"发出了什么"的时候，那份字节一定在证据里。
   证据里同步记下这是**第几次**问（`attempt` / `attempts`）——
   "只问了一次"与"问满三次都没答"不是一回事。

⚠️ 不做的事：不枚举系统里的全部串口去猜设备（只问台架点名的那些），
不重试风暴（一个口一轮最多 3 次提问，重试不另开额度），不复位，不清设备数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # 包内 import
    from . import device_sniff as sniff
    from .console_input import build_text_payload
    from .core_primitives import utc_now
    from .device_registry import ACCESS_ABSENT, ACCESS_BUSY, ACCESS_UNKNOWN
    from .identity import normalize as normalize_identity
except ImportError:  # 直接跑脚本/测试时的回退
    import device_sniff as sniff  # type: ignore[no-redef]
    from console_input import build_text_payload  # type: ignore[no-redef]
    from core_primitives import utc_now  # type: ignore[no-redef]
    from device_registry import (  # type: ignore[no-redef]
        ACCESS_ABSENT,
        ACCESS_BUSY,
        ACCESS_UNKNOWN,
    )
    from identity import normalize as normalize_identity  # type: ignore[no-redef]

#: 一轮嗅探最多问几个口（候选来源本身已经是台架点名的，这里是第二道闸）。
DEFAULT_MAX_PORTS = 16
#: 一个口上最多**问几次**（不管重发同一条还是换下一条）。
#: ⚠️ 这条上限是"不许对未知设备乱试"的具体形式 —— **重试不另开额度**，
#: 它和"换下一条声明探针"共用这三次：所以一个口上有几条探针就轮流问几条，
#: 只有**一条**声明探针的口就自然变成"失败后隔一阵重发，最多 3 次"。
MAX_ATTEMPTS_PER_PORT = 3
#: 兼容旧名（数值一样）：以前这里只能"换下一条探针"，现在是"下一次提问"。
MAX_PROBES_PER_PORT = MAX_ATTEMPTS_PER_PORT
#: 默认重发间隔（秒）。落在 3～5 s 里：太密会把设备的回答挤坏，太稀则一次嗅探要等很久。
DEFAULT_RETRY_GAP_SECONDS = 4.0

# ==================================== 一轮嗅探的进度（需求 §12.2）
#: 进度的四个状态 —— **只有这四个**（页面、契约夹具与验收都按它判）。
SNIFF_IDLE = "idle"
SNIFF_RUNNING = "running"
SNIFF_COMPLETED = "completed"
SNIFF_FAILED = "failed"
SNIFF_STATES: tuple[str, ...] = (SNIFF_IDLE, SNIFF_RUNNING, SNIFF_COMPLETED, SNIFF_FAILED)

#: 进度快照的字段（名字与顺序都进契约：两个仓库用**同一份夹具**核对它）。
PROGRESS_FIELDS: tuple[str, ...] = (
    "state",
    "total_ports",
    "completed_ports",
    "current_port",
    "percent",
    "started_utc",
    "updated_utc",
    "reason",
)


def derived_percent(completed_ports: int, total_ports: int) -> int:
    """`percent` **只**由计数派生（零候选口 ⇒ 0，**不除零**）。

    ⚠️ 它是派生值而不是第二份事实：任何地方都不许单独写一个 `percent` 进去 ——
    两份值一漂，进度条与那句文字就会互相矛盾，而屏幕上没人知道该信哪一个。
    """
    total = max(0, int(total_ports))
    if total <= 0:
        return 0
    done = max(0, min(int(completed_ports), total))
    return int(done * 100 // total)


@dataclass(frozen=True)
class SniffProgress:
    """一轮嗅探的**进度快照**（不可变；读它的人拿到的是副本，不是一个活对象）。

    ⚠️ 它**不是** `sniff.last`：`last` 是上一轮的**完整**结果，`progress` 是**本轮**走到哪。
    这是两类事实 —— 本轮还没完成的卡片不许覆盖上一轮的完整结果（需求 §12.1）。
    """

    state: str = SNIFF_IDLE
    #: 这一轮有几个候选通信口（计数单位 = 候选口，不是探针次数）。
    total_ports: int = 0
    #: 已经形成确定观测的口数（同一个口问几次都只算一个）。
    completed_ports: int = 0
    #: 正在检测的那个口；没有正在检测的口时是**空串**（类型在各种状态下都稳定）。
    current_port: str = ""
    #: 派生值 `completed_ports / total_ports`（`derived_percent()` 是唯一算法）。
    percent: int = 0
    started_utc: str = ""
    updated_utc: str = ""
    #: 只有**出了事**才非空：整体失败的原因，或"这轮跑完但有话要说"（例：证据没落盘）。
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in PROGRESS_FIELDS}


class SniffProgressBoard:
    """逐通信口的进度（**线程安全**，而且是这一轮进度的**唯一**事实）。

    三条硬规矩（每条都有宿主判据）：

    1. **读只复制内存快照**：`snapshot()` 不枚举、不打开、不读写通信口，也不碰 DTR/RTS
       —— 页面的 2 秒轮询走的正是这条路（需求 §12.2）。
    2. **计数只由这一层派生**：`percent` 没有第二份来源；同一个口内部问几条探针都只算一个口，
       计数只增不减、且封顶在总数上。
    3. **收口只有一条路**：`complete()` / `fail()` 之外还有 `settle()` 兜底 ——
       `sniff()` 在 `finally` 里调它，于是**任何**路径都不会把 `running` 留在快照里。
    """

    def __init__(self, *, clock: Callable[[], str] = utc_now) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._row = SniffProgress()

    def snapshot(self) -> SniffProgress:
        """这一轮的进度**副本**（只碰内存，不碰任何硬件）。"""
        with self._lock:
            return self._row

    def begin(self, total_ports: int) -> SniffProgress | None:
        """`(idle|completed|failed) → running`。**已经在跑 ⇒ 返回 None**。

        ⚠️ 返回 `None` 就是并发闸门：调用方据此拒掉第二轮，而这一判发生在任何端口访问之前。
        """
        with self._lock:
            if self._row.state == SNIFF_RUNNING:
                return None
            now = self._clock()
            self._row = SniffProgress(
                state=SNIFF_RUNNING,
                total_ports=max(0, int(total_ports)),
                completed_ports=0,
                current_port="",
                percent=0,
                started_utc=now,
                updated_utc=now,
                reason="",
            )
            return self._row

    def entering(self, port: str) -> None:
        """开始检测这个口：先把**它**显示出来（这时完成数**不动**）。"""
        with self._lock:
            self._write(state=SNIFF_RUNNING, current_port=str(port or ""))

    def finished_port(self) -> None:
        """这个口已经形成确定观测 ⇒ 完成数 +1（**只加一次**，且封顶在总数上）。"""
        with self._lock:
            self._write(
                completed_ports=min(self._row.total_ports, self._row.completed_ports + 1)
            )

    def complete(self, reason: str = "") -> None:
        """正常跑完（零候选口也走这条：确定性完成、不除零）。

        `reason` 给"跑完了但有话要说"留位（例：结果没能落盘）—— 那种情况不算失败
        （卡片与结论仍然有效），但**绝不能静默**：原因必须能从接口与页面上看到。
        """
        with self._lock:
            self._write(state=SNIFF_COMPLETED, current_port="", reason=str(reason or ""))

    def fail(self, reason: str) -> None:
        """这一轮没跑完：进 `failed`，并把原因留下（页面/证据都要能说清为什么）。"""
        with self._lock:
            self._write(state=SNIFF_FAILED, current_port="", reason=str(reason or ""))

    def settle(self) -> None:
        """**兜底收口**：还停在 `running` 就按失败收口，并如实说"异常路径没给原因"。

        ⚠️ 它存在的唯一理由："不残留 running"不能靠"每条路径都记得收口"这种自觉。
        """
        with self._lock:
            if self._row.state == SNIFF_RUNNING:
                self._write(
                    state=SNIFF_FAILED,
                    current_port="",
                    reason="这一轮没有正常收口（异常路径没有给出原因）",
                )

    # ------------------------------------------------------------------ 内部
    def _write(self, **changes: Any) -> None:
        """改这份快照（**调用方持锁**）：`percent` 与 `updated_utc` 都在这里派生。"""
        row = replace(self._row, **changes)
        self._row = replace(
            row,
            percent=derived_percent(row.completed_ports, row.total_ports),
            updated_utc=self._clock(),
        )


class ExchangeError(Exception):
    """目标层的传输实现打不开/写不出去时抛这个。

    ⚠️ core 不猜原因（权限？驱动？半死？）：它只把这句话原样记成 `open_failed` 的理由。
    """


class SniffBusy(RuntimeError):
    """已经有一轮嗅探在跑（**并发闸门**）：第二轮必须被拒，且**一个字节都不发**。

    ⚠️ 前端那个 `sniffBusy` 只是交互反馈，**不是**资源锁（需求 §12.2）：
    两个标签页、或者一个直接打接口的客户端都不受它约束 —— 唯一有效的闸门在这里。
    """


@dataclass(frozen=True)
class Candidates:
    """这一轮的候选通信口 + 它们**从哪来**（可审计）。"""

    ports: tuple[str, ...]
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"ports": list(self.ports), "note": self.note}


def candidate_ports(ports: Iterable[str], *, limit: int = DEFAULT_MAX_PORTS) -> Candidates:
    """去空、去重、保序、限量 —— 候选口就是**调用方给的那一份清单**。

    ⚠️ 这一层不决定“候选口从哪来”（那是装配处的事：系统枚举 + 台架声明合并）：
    以前它写死“只来自台架点名”，而需求 §6.11 改成“系统枚举是基础候选源” ——
    策略变化只应改在装配处，不该藏在 core 的这一句话里。

    ⚠️ 保序很重要：候选顺序就是卡片显示顺序（Device Num 的来历），
    而"按发现顺序猜设备"是需求 §5 明令禁止的 —— 顺序只影响编号，不影响任何判断。
    """
    rows: list[str] = []
    seen: set[str] = set()
    for raw in ports:
        name = str(raw or "").strip()
        if not name or name.upper() in seen:
            continue
        seen.add(name.upper())
        rows.append(name)
    kept = tuple(rows[: max(0, int(limit))])
    dropped = len(rows) - len(kept)
    note = "候选通信口来自调用方给的清单（去空、去重、保序）"
    if dropped:
        note += f"；本轮超过上限 {int(limit)} 个，跳过了 {dropped} 个"
    return Candidates(kept, note)


def hex_bytes(data: bytes | bytearray | None) -> str:
    """字节 ⇒ 证据里那种十六进制写法（与既有证据风格一致）。"""
    return " ".join(f"{value:02X}" for value in bytes(data or b""))


def decode_rx(data: bytes | bytearray | None) -> str:
    """RX 字节 ⇒ 给人读的文本（坏字节如实替换，不静默丢）。"""
    return bytes(data or b"").decode("utf-8", "replace")


def _has_any_value(fields: Mapping[str, Any] | None) -> bool:
    """规范化之后**真的有一个字段有值**才算拿到了身份。"""
    for value in (fields or {}).values():
        if value not in (None, "", {}):
            return True
    return False


def identity_from_project(
    parser: Callable[[str], Any], text: str
) -> dict[str, Any]:
    """项目自带的身份解析器：逐行丢给它，**第一份真的非空的结果**算数。

    ⚠️ 解析器是**设备/项目的事实**（例：某一行是 `key=value` 而不是 JSON），
    所以 core 只负责“把行交给它、把结果当字段”，不碰它的语法。
    抛异常 / 返回非字典 / 空字典都算“没拿到”，继续试下一行（不编）。
    """
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        try:
            fields = parser(line.strip())
        except Exception:  # noqa: BLE001 - 项目解析器的异常不该掀掉整轮嗅探
            continue
        if isinstance(fields, Mapping) and _has_any_value(fields):
            return dict(fields)
    return {}


def parse_identity(text: str, *, prefix: str = "") -> dict[str, Any]:
    """从回答文本里取设备自报字段（**只留协议白名单**，缺的补 `None`）。

    没有符合前缀的 JSON 对象、或者对象里**一个白名单字段都没有** ⇒ 空字典
    （"没拿到身份"，**不编**；一个全是 `null` 的字段表看起来像证据，其实什么都没证明）。
    """
    for line in str(text or "").splitlines():
        body = line.strip()
        if prefix:
            if not body.startswith(prefix):
                continue
            body = body[len(prefix):].strip()
        if not body:
            continue
        try:
            payload = json.loads(body)
        except ValueError:
            continue
        if isinstance(payload, dict):
            fields = normalize_identity(payload)
            return fields if _has_any_value(fields) else {}
    return {}


def requested_card(
    result: sniff.SniffResult | None, request: Any
) -> tuple[sniff.DeviceCard | None, str]:
    """把页面点名的设备 `{"scan_seq": n, "port": p}` 复核成**这一次嗅探结果里**那张卡片。

    返回值三态（**不要把三态压成两态**）：

    | 返回 | 含义 |
    | --- | --- |
    | `(None, "")` | 页面**没有点名** ⇒ 调用方按老路径走（静态登记里那块板的口） |
    | `(None, 理由)` | 点名了但**站不住** ⇒ 调用方原样把它拒掉（409） |
    | `(卡片, "")` | 通过 |

    为什么这条判断住在 core：它是"页面说的算不算数"的**唯一**一处 ——
    换个地方再写一遍（比如页面自己比端口）就会两头各有一套，而错的那一套不会报错。
    """
    if not isinstance(request, dict) or not request:
        return None, ""
    if result is None:
        return None, "还没有做过设备嗅探：请先点「设备嗅探」，再从结果里选设备"
    try:
        seq = int(request.get("scan_seq"))
    except (TypeError, ValueError):
        return None, "设备的显示序号必须是数字"
    card = result.card(seq)
    if card is None:
        return None, f"这次嗅探结果里没有第 {seq} 个设备（重新嗅探之后旧的显示序号就失效了）"
    wanted = str(request.get("port") or "").strip()
    if wanted and wanted != card.port:
        return None, f"第 {seq} 个设备现在是 {card.port}，不是 {wanted}（请重新嗅探后再选）"
    return card, ""


def new_sniff_id(stamp: str, root: Path | None = None) -> str:
    """`<stamp>-sniff`，同一秒第二次加 `-2`（与观察会话的编号约定一致）。"""
    base = f"{stamp}-sniff"
    if root is None:
        return base
    directory = Path(root)
    if not (directory / base).exists():
        return base
    index = 2
    while (directory / f"{base}-{index}").exists():
        index += 1
    return f"{base}-{index}"


class SniffEvidence:
    """一轮嗅探的证据（**一次一动盘**：新目录 + 两个文件，不覆盖历史）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def write(
        self,
        result: sniff.SniffResult,
        *,
        sniff_id: str,
        observations: Sequence[sniff.PortObservation] = (),
    ) -> Path:
        """落盘一份嗅探证据（**一次一动盘**：新目录 + 两个文件，不覆盖历史）。

        ⚠️ `observations` 是**每一次提问**的观测（一个口可能问了好几次）。
        不给时退到"每张卡片最后那一次"—— 那是旧调用方的行为。
        给了就不能省：重试之后"第一次问的 TX/RX 是什么"是这一轮的一部分，
        只留最后一次会让"问了三次都没答"与"只问了一次"在证据里长得一模一样。
        """
        directory = self.root / str(sniff_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(
            json.dumps(result.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        rows = [
            json.dumps(item.to_json(), ensure_ascii=False)
            for item in (observations or [card.observation for card in result.cards])
        ]
        (directory / "probes.jsonl").write_text(
            ("\n".join(rows) + "\n") if rows else "",
            encoding="utf-8",
        )
        return directory


class SniffExecutor:
    """一轮受控嗅探（构造**不碰端口**；只有 `sniff()` 会）。

    `probes` 是声明式的只读身份查询；`clock` / `timer` / `sleep` 可注入，于是同一份输入
    在夹具里永远得到同一份结论（时间戳与耗时除外），而且夹具**不用真的等 4 秒**。
    """

    def __init__(
        self,
        *,
        candidates: Iterable[str] | Candidates,
        probes: Sequence[sniff.IdentityProbe],
        access: Callable[[str], str],
        conflict: Callable[[str], str],
        exchange: Callable[[str, int, bytes, int, float], bytes],
        held_observation: Callable[[str], sniff.PortObservation | None] | None = None,
        parsers: Mapping[str, Callable[[str], Any]] | None = None,
        activity: Callable[[str], tuple[str | None, str]] | None = None,
        clock: Callable[[], str] = utc_now,
        timer: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] | None = None,
        evidence: SniffEvidence | None = None,
        limit: int = DEFAULT_MAX_PORTS,
    ) -> None:
        self._candidates = (
            candidates
            if isinstance(candidates, Candidates)
            else candidate_ports(candidates, limit=limit)
        )
        self._probes = tuple(probes)
        #: 项目自带的身份解析器（按声明里的 `parser_id` 选）—— core 不认识它们的语法。
        self._parsers = {
            str(name): parser for name, parser in dict(parsers or {}).items()
            if callable(parser)
        }
        self._access = access
        self._conflict = conflict
        self._exchange = exchange
        self._held_observation = held_observation
        self._clock = clock
        self._timer = timer
        #: ：卡片上的"端口最近活动"从哪里来 ——
        #: 由**调用方注入**（服务端去读 run/会话记录），core 不碰文件、也不猜。
        #: 没注入就如实空（`recent_activity` 的一条都不算）。
        self._activity = activity if callable(activity) else None
        #: 等间隔也**注入**：夹具里不该真的等 4 秒，而"等过多久"要能数出来。
        self._sleep = sleep if callable(sleep) else time.sleep
        self._evidence = evidence
        #: 这一轮的**逐口进度**（线程安全；读它只复制内存快照）。
        #: ⚠️ 它与 `last_result` 是两类事实：这里说的是**本轮走到哪**，那里是上一轮的完整结果。
        self._progress = SniffProgressBoard(clock=self._clock)
        #: 证据计数（夹具用它证明"没有用户动作就没有端口访问"）。
        self.port_opens = 0
        self.bytes_written = 0
        #: 真正等过的重发间隔（按顺序；"什么时候等了多久"是这一轮的一部分）。
        self.gap_seconds: list[float] = []
        #: 这一轮**每一次提问**的观测（一个口可能问了好几次）—— 证据要能重建整个过程。
        self.observations: list[sniff.PortObservation] = []
        self.last_result: sniff.SniffResult | None = None
        self.last_evidence_dir: Path | None = None

    # ------------------------------------------------------------- 只读问答
    @property
    def candidates(self) -> Candidates:
        """候选口（**只读**：连状态都不探，更不开口）。"""
        return self._candidates

    @property
    def probes(self) -> tuple[sniff.IdentityProbe, ...]:
        return self._probes

    def state(self) -> dict[str, Any]:
        """给 `/api/state` 看的那一块（**不触发任何端口访问**）。

        ⚠️ `progress` 只复制内存快照：页面每 2 秒读它一次，那一步绝不能探口、不能开口、
        更不能碰 DTR/RTS（需求 §12.2）。
        """
        return {
            "schema": sniff.SCHEMA,
            "candidates": self._candidates.to_json(),
            "probes": [probe.to_json() for probe in self._probes],
            "parsers": sorted(self._parsers),
            "max_probes_per_port": MAX_PROBES_PER_PORT,
            "max_attempts_per_port": MAX_ATTEMPTS_PER_PORT,
            "scan_seq_persistent": sniff.SCAN_SEQ_PERSISTENT,
            #: 本轮进度（idle/running/completed/failed）—— 与 `last` 分开，互不污染。
            "progress": self._progress.snapshot().to_json(),
            "last": None if self.last_result is None else self.last_result.to_json(),
            "evidence_dir": (
                None if self.last_evidence_dir is None else str(self.last_evidence_dir)
            ),
        }

    # ----------------------------------------------------------------- 执行
    def sniff(self) -> sniff.SniffResult:
        """跑一轮（**显式动作**）：逐个候选口受控查询，落证据，返回结果。

        ⚠️ **并发闸门在第一行**（需求 §12.2）：已经有一轮在跑时抛 `SniffBusy`，
        而这一抛发生在**任何端口访问之前** —— 所以第二轮一个字节都不发、也不会多探一次口。
        `finally` 里的 `settle()` 保证正常、零候选口、异常都退出 `running`。
        """
        ports = self._candidates.ports
        if self._progress.begin(len(ports)) is None:
            raise SniffBusy(
                "已经有一轮设备嗅探在进行中：等它结束再点（同一时刻只允许一轮）"
            )
        try:
            return self._run_round(ports)
        except BaseException as error:
            # ⚠️ 任何异常都要收口到确定终态，并带上**可诊断的原因**（不残留 running）。
            self._progress.fail(f"{type(error).__name__}: {error}")
            raise
        finally:
            # 正常结束/已收口 ⇒ 什么都不做；万一还有路径漏了，也绝不留 running。
            self._progress.settle()

    def _run_round(self, ports: tuple[str, ...]) -> sniff.SniffResult:
        """一轮的正文（闸门与收口在 `sniff()` 里，这里只管把口一个个问过去）。"""
        started = self._clock()
        self.observations = []
        self.gap_seconds = []
        cards: list[sniff.DeviceCard] = []
        missing: list[str] = []
        for port in ports:
            # ① 开始检测这个口：先把**它**显示出来（完成数这时不动）。
            self._progress.entering(port)
            try:
                # ⚠️ 一个口把整轮打崩是不可接受的：占用探测、资源闸门、传输实现
                #    都是**注入进来的别人写的代码**，它抛什么异常都不该让“这一轮没有结果”。
                #    所以逐个口兜住：崩掉的那个口照样出一张卡片，理由写着崩在哪儿。
                try:
                    observation = self._probe_port(port)
                except Exception as error:  # noqa: BLE001 - 见上：不拿一个口换一整轮
                    observation = self._crashed(port, error)
            finally:
                # ② 这个口已经形成确定观测 ⇒ 完成数 +1。可归类的失败（口不在 / 被占 /
                #    没回答 / 打不开）与崩溃都算，这就是“逐口隔离”在进度上的样子。
                #    ⚠️ 放在 finally 里：一个口内部问了几条探针、重试几次，都只算**一个口**，
                #    而且这条提问路径无论怎么出去都只加一次（不会漏加、更不会重复加）。
                self._progress.finished_port()
            # ⚠️ 用户口径（2026-10-10）：「系统里不存在的设备就不要展示出来了」。
            #    台架声明里可能留着已经拔掉的口（换机器之后最常见：声明还在、口不在），
            #    它不是“一台设备”—— 摆一张卡片只会让人去点一个点不动的口。
            #    声明的清单（`candidates`）与这个口的观测**照旧留在这一轮证据里**，
            #    只是不进卡片表；少掉哪几个用 `notes` 如实说出来（不静默丢）。
            if observation.access == ACCESS_ABSENT:
                missing.append(str(port))
                continue
            # 显示序号按**展示出来的卡片**连续编号（跳过不存在的口之后不留空洞）。
            cards.append(sniff.card_for(observation, num=len(cards) + 1))
        extra_note = ""
        if missing:
            extra_note = (
                f"声明的通信口里有 {len(missing)} 个当前不存在（{'、'.join(missing)}）："
                "不展示卡片（它们不是设备；清单与观测仍在这轮证据里）"
            )
        result = sniff.SniffResult(
            candidates=self._candidates.ports,
            cards=tuple(cards),
            started_utc=started,
            ended_utc=self._clock(),
            notes=tuple(note for note in (self._candidates.note, extra_note) if note),
            skipped=tuple(missing),
        )
        self.last_result = result
        evidence_note = ""
        if self._evidence is not None:
            sniff_id = new_sniff_id(started.replace(":", "").replace("-", "")[:15],
                                    self._evidence.root)
            try:
                self.last_evidence_dir = self._evidence.write(
                    result, sniff_id=sniff_id, observations=tuple(self.observations)
                )
            except Exception as error:  # noqa: BLE001 - 见下：写不进去也要留下结论
                # ⚠️ 证据目录可能写不进去（只读检出、被同步盘占住、
                #    路径上恰好有个同名文件……）。那时**结论仍然有效** —— 把它换成
                #    500 等于“探测全做完了，只因为没落盘就当没做”，而少掉的那份证据
                #    会变成一句没人知道的缺失。所以：结论照返，缺失写在脸上。
                self.last_evidence_dir = None
                evidence_note = (
                    f"这一轮结果没能落盘（{type(error).__name__}: {error}）："
                    "卡片与结论仍然有效，缺的是这份证据文件"
                )
                result = replace(result, notes=(*result.notes, evidence_note))
        self.last_result = result
        # 这一轮**跑完了**：终态是 completed（零候选口也走这条），并按需带上那句"有话要说"。
        self._progress.complete(reason=evidence_note)
        return result

    # ------------------------------------------------------------- 单个口
    def _crashed(self, port: str, error: BaseException) -> sniff.PortObservation:
        """某个口上抛了预料之外的异常 ⇒ 照样出一张卡片（不掀掉整轮）。

        理由必须点名“这是平台/适配的问题”，否则页面上它长得跟“设备没回答”一样，
        而这两件事的下一步动作完全不同。
        """
        return self._observation(
            port,
            access=ACCESS_UNKNOWN,
            outcome=sniff.PROBE_OPEN_FAILED,
            reason=(
                f"处理这个口时抛了预料之外的异常（{type(error).__name__}: {error}）："
                "这一轮的其它口照常判断 —— 这是实现的问题，不是「没有设备」"
            ),
        )

    def _port_activity(self, port: str) -> tuple[str | None, str]:
        """这个口**最近一次活动**（注入的来源里查；没注入/没记录 ⇒ 如实空）。

        ⚠️ 它只影响卡片上的一句展示，**不参与任何判定**（不改变"这个口能不能用"）。
        来源抛错也不让它弄断嗅探：展示字段不值得拿整轮结果去换。
        """
        if self._activity is None:
            return (None, "")
        try:
            utc, note = self._activity(port)
        except Exception:  # noqa: BLE001 - 展示字段降级，不是结论
            return (None, "")
        return (str(utc) if utc else None, str(note or ""))

    def _probe_port(self, port: str) -> sniff.PortObservation:
        # A port already owned by this platform must not be opened a second time.  Reuse the live
        # session's identity as this scan's observation, so the device remains selectable.
        if self._held_observation is not None:
            try:
                held = self._held_observation(port)
            except Exception:  # noqa: BLE001 - 复用已连会话只是省一次打开：查不动就照常探
                held = None
            if held is not None:
                self.observations.append(held)
                return held
        access = ACCESS_UNKNOWN
        access_note = ""
        try:
            access = str(self._access(port) or ACCESS_UNKNOWN)
        except Exception as error:  # noqa: BLE001 - 探占用崩了 ≠ 这个口不能用
            # ⚠️ 这一条**不改变判定**（`unknown` 本来就走“照常问”那条路，与旧行为一致），
            #    只是把那句异常留在卡片上 —— 静默降级会让“探测坏了”与“口是空的”
            #    在证据里长得一模一样。
            access_note = (
                f"（探测这个口的占用状态时抛了异常：{type(error).__name__}: {error}）"
            )

        def noted(observation: sniff.PortObservation) -> sniff.PortObservation:
            """把那句异常附到这**一条**观测上（含已经记进 `observations` 的那份）。"""
            if not access_note:
                return observation
            fixed = replace(observation, reason=f"{observation.reason}{access_note}")
            for index, item in enumerate(self.observations):
                if item is observation:
                    self.observations[index] = fixed
            return fixed

        if access == ACCESS_BUSY:
            return noted(self._verdict(
                port, access=access, outcome=sniff.PROBE_PORT_BUSY,
                reason="端口被其他 owner 占用（探测到占用，本轮**没有打开它**）",
            ))
        if access == ACCESS_ABSENT:
            return noted(self._verdict(
                port, access=access, outcome=sniff.PROBE_PORT_ABSENT,
                reason="这个通信口当前不存在",
            ))
        try:
            holder = str(self._conflict(port) or "").strip()
        except Exception as error:  # noqa: BLE001 - 闸门坏了就**不发字节**（见下）
            # ⚠️ 保守选择：资源闸门存在的意义就是“别碰别人握着的口”。读不出结论时
            #    宁可少问一个口，也不要撞上正在跑的 run / 实时观察会话。
            return noted(self._verdict(
                port, access=access, outcome=sniff.PROBE_PORT_BUSY,
                reason=(
                    f"资源闸门出错了（{type(error).__name__}: {error}）："
                    "读不出谁握着这个口 ⇒ 这一轮**没有打开它**"
                ),
            ))
        if holder:
            return noted(self._verdict(
                port, access=access, outcome=sniff.PROBE_PORT_BUSY,
                reason=f"资源闸门：{holder}",
            ))
        if not self._probes:
            return noted(self._verdict(
                port, access=access, outcome=sniff.PROBE_SKIPPED,
                reason="没有装配任何身份探针声明 ⇒ 不向这个口发字节（不猜）",
            ))
        last: sniff.PortObservation | None = None
        asked: dict[str, int] = {}
        asks: list[sniff.PortObservation] = []
        # ⚠️ 每个口的**总提问次数**上限不变（还是 3 次）：重试不另开额度。
        # 排在前面的是"问得最少的那一条"——一个口上只声明了一条探针时，
        # 这自然就是"失败后隔一阵重发，最多 3 次"；声明了多条就轮流问，
        # 不会因为第一条不回答就把后面几条永远晾着。
        for _slot in range(MAX_ATTEMPTS_PER_PORT):
            probe = self._next_probe(asked)
            if probe is None:
                break
            if last is not None:
                self._wait_between(probe)
            attempt = asked.get(probe.probe_id, 0) + 1
            asked[probe.probe_id] = attempt
            last = self._ask(port, probe, access=access, attempt=attempt)
            asks.append(last)
            if last.outcome in (
                sniff.PROBE_OK,
                sniff.PROBE_UNSUPPORTED,
                sniff.PROBE_OPEN_FAILED,
            ):
                break
            if last.outcome not in (sniff.PROBE_NO_RESPONSE, sniff.PROBE_MALFORMED):
                break
        assert last is not None  # `self._probes` 非空 ⇒ 至少问过一次
        if (
            last.outcome in (sniff.PROBE_NO_RESPONSE, sniff.PROBE_MALFORMED)
            and sum(asked.values()) >= MAX_ATTEMPTS_PER_PORT
        ):
            # ⚠️ 这里要说清"**这个口**这一轮问满了"，而不是"这条探针问满了"：
            # 多条探针轮流用时，最后落笔的那条往往还剩额度，是**口**的预算先没了。
            # 不说清的话，"还能再试"与"已经试到头"在卡片上长得一模一样。
            last = replace(
                last,
                reason=(
                    f"{last.reason}；这个口这一轮已问满 {MAX_ATTEMPTS_PER_PORT} 次"
                ),
            )
        # 每一次提问都进证据：`asks[:-1]` 是过程，最后那一条（带上面那句说明）是结论。
        if asks:
            self.observations.extend(asks[:-1])
            self.observations.append(last)
        return noted(last)

    def _verdict(
        self, port: str, *, access: str, outcome: str, reason: str
    ) -> sniff.PortObservation:
        """**没有提问**的结论（口不在 / 被占 / 没声明探针）。

        ⚠️ 它也要进证据：`probes.jsonl` 里"这个口一个字节都没收到"与"这个口没出现过"
        是两回事 —— 前者是结论（有理由的），后者是漏记。
        """
        observation = self._observation(
            port, access=access, outcome=outcome, reason=reason
        )
        self.observations.append(observation)
        return observation

    def _next_probe(self, asked: Mapping[str, int]) -> sniff.IdentityProbe | None:
        """下一个该问谁：**问得最少的先来**，问满自己声明次数的跳过。

        ⚠️ 不能是"一条失败就死磕这一条"：一个口上可能声明了多条探针（同一块板的
        不同固件各认一条协议），死磕第一条会让第二条永远问不到 —— 那会把
        "设备不认这条协议"错报成"没有身份"。
        """
        best: sniff.IdentityProbe | None = None
        best_used = 0
        for probe in self._probes:
            used = int(asked.get(probe.probe_id, 0))
            if used >= max(1, int(probe.attempts)):
                continue
            if best is None or used < best_used:
                best, best_used = probe, used
        return best

    def _wait_between(self, probe: sniff.IdentityProbe) -> None:
        """两次提问之间的间隔（**声明**给的值，夹具注入的 `sleep` 让它不花真实时间）。"""
        gap = max(0.0, float(probe.retry_gap_seconds))
        if gap <= 0:
            return
        self.gap_seconds.append(gap)
        self._sleep(gap)

    def _ask(
        self,
        port: str,
        probe: sniff.IdentityProbe,
        *,
        access: str,
        attempt: int = 1,
    ) -> sniff.PortObservation:
        payload = build_text_payload(probe.command, probe.line_ending)
        started_utc = self._clock()
        began = self._timer()
        try:
            received = self._exchange(
                port, int(probe.baud), payload, int(probe.max_bytes), float(probe.wait_s)
            )
        except ExchangeError as error:
            return self._observation(
                port, access=access, outcome=sniff.PROBE_OPEN_FAILED,
                reason=f"打不开或写不出去：{error}", probe=probe, attempt=attempt,
                tx=payload, started_utc=started_utc, began=began,
            )
        except OSError as error:
            return self._observation(
                port, access=access, outcome=sniff.PROBE_OPEN_FAILED,
                reason=f"打开这个通信口失败：{error}", probe=probe, attempt=attempt,
                tx=payload, started_utc=started_utc, began=began,
            )
        except BaseException as error:  # noqa: BLE001 - 见下：注入实现抛什么都不能掀掉整轮
            # ⚠️ `ExchangeError` / `OSError` 是**约定的**失败信号；
            #    除此之外的任何异常（`ValueError` / `ImportError` / 自定义异常……）
            #    都说明传输实现自己坏了。它必须变成卡片上的一句理由 ——
            #    否则一个口就能把整轮嗅探变成 HTTP 500，而页面会把它显示成
            #    “连不上服务”，与真正的原因完全无关。
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return self._observation(
                port, access=access, outcome=sniff.PROBE_OPEN_FAILED,
                reason=(
                    f"传输实现抛了预料之外的异常（{type(error).__name__}: {error}）："
                    "这个口没有结论，但这一轮照常走完"
                ),
                probe=probe, attempt=attempt,
                tx=payload, started_utc=started_utc, began=began,
            )
        self.port_opens += 1
        self.bytes_written += len(payload)
        text = decode_rx(received)
        outcome = sniff.outcome_for_reply(
            text, prefix=probe.prefix, unsupported_markers=tuple(probe.unsupported_markers)
        )
        fields: Mapping[str, Any] = {}
        reason = {
            sniff.PROBE_OK: "设备自报了一行身份",
            sniff.PROBE_NO_RESPONSE: f"等待 {probe.wait_s:g} s 没有拿到符合声明的回答",
            sniff.PROBE_MALFORMED: "有回答，但不是这条协议（前缀/JSON 不符合）",
            sniff.PROBE_UNSUPPORTED: f"设备不认这条查询（探针 {probe.probe_id}）",
        }.get(outcome, "")
        if int(probe.attempts) > 1 and outcome in (
            sniff.PROBE_NO_RESPONSE, sniff.PROBE_MALFORMED
        ):
            # 重试过没过、还能不能再试，要能从卡片上一眼看到（否则"试了三次"像"只试了一次"）。
            if attempt < int(probe.attempts):
                reason += f"（第 {attempt}/{int(probe.attempts)} 次）"
            else:
                reason += (
                    f"（已按声明的 {int(probe.attempts)} 次问完，"
                    f"每次间隔 {float(probe.retry_gap_seconds):g} s）"
                )
        if outcome == sniff.PROBE_OK:
            # 声明的 `parser_id` 决定用谁解析：空串 = 平台通用的“前缀 + JSON”。
            parser = self._parsers.get(str(probe.parser_id)) if probe.parser_id else None
            fields = (
                identity_from_project(parser, text)
                if parser is not None
                else parse_identity(text, prefix=probe.prefix)
            )
            if not fields:
                # ⚠️ 声明了 `parser_id` 却没有注入对应实现，也要能看出来：
                # “没拿到身份”与“拿错了工具”是两件事。
                why = (
                    f"没有注入探针声明的解析器 {probe.parser_id!r}"
                    if probe.parser_id and parser is None
                    else "回答里没有可解析的身份对象（字段为空）"
                )
                outcome = sniff.PROBE_MALFORMED
                reason = why
        return self._observation(
            port, access=access, outcome=outcome, reason=reason, probe=probe,
            attempt=attempt,
            tx=payload, rx=received, identity_fields=fields,
            started_utc=started_utc, began=began,
        )

    def _observation(
        self,
        port: str,
        *,
        access: str,
        outcome: str,
        reason: str = "",
        probe: sniff.IdentityProbe | None = None,
        tx: bytes = b"",
        rx: bytes = b"",
        identity_fields: Mapping[str, Any] | None = None,
        started_utc: str | None = None,
        began: float | None = None,
        attempt: int = 1,
    ) -> sniff.PortObservation:
        ended = self._clock()
        duration = None if began is None else max(0, int(round((self._timer() - began) * 1000)))
        activity_utc, activity_note = self._port_activity(port)
        return sniff.PortObservation(
            port=port,
            outcome=outcome,
            access=access,
            identity_fields=dict(identity_fields or {}),
            probe_id="" if probe is None else probe.probe_id,
            attempt=max(1, int(attempt)),
            attempts=1 if probe is None else max(1, int(probe.attempts)),
            tx_hex=hex_bytes(tx),
            rx_hex=hex_bytes(rx),
            rx_text=decode_rx(rx),
            baud=None if probe is None else int(probe.baud),
            reason=reason,
            started_utc=started_utc or ended,
            ended_utc=ended,
            duration_ms=duration,
            # 卡片上的"端口最近活动"：来自**记录**，不是现场猜的。
            last_activity_utc=activity_utc,
            last_activity_note=activity_note,
        )


__all__ = [
    "DEFAULT_MAX_PORTS",
    "MAX_PROBES_PER_PORT",
    "PROGRESS_FIELDS",
    "SNIFF_COMPLETED",
    "SNIFF_FAILED",
    "SNIFF_IDLE",
    "SNIFF_RUNNING",
    "SNIFF_STATES",
    "Candidates",
    "ExchangeError",
    "SniffBusy",
    "SniffEvidence",
    "SniffExecutor",
    "SniffProgress",
    "SniffProgressBoard",
    "candidate_ports",
    "decode_rx",
    "derived_percent",
    "hex_bytes",
    "identity_from_project",
    "new_sniff_id",
    "parse_identity",
    "requested_card",
]
