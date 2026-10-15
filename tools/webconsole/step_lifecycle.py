"""测试用例/阶段的**实时生命周期**（ / T0，**core**）。

形状在这里，内容在项目包里 —— 与 `device_dialogue`、`target_adapters` 同一个做法。

六步流水线（`precheck…assert`）说的是**平台生命周期**；这一层说的是**里面的测试**：
本轮选中的用例或受控序列的每个阶段，现在是等着、在跑、还是已经有了结果。
两者共用同一条事件总线与同一份证据目录，**不是第二套流水线**。

```text
test_step 事件（追加进既有 events.jsonl，经既有 SSE 发布）
  ├─ kind        "test_step"
  ├─ case_id     项目包声明的稳定阶段 id（不是 core 取的名字）
  ├─ version     阶段声明的版本
  ├─ label       人话标题（页面卡片上那行）
  ├─ expected    预期发生什么（页面点开详情要看到的那句，需求 §3.2）
  ├─ status      pending | running | PASS | FAIL | ERROR | SKIPPED | CANCELLED
  ├─ detail      当前动作或等待对象（人话）
  ├─ started_utc / ended_utc / duration_ms      （UTC 只用于展示，时长来自单调时钟）
  └─ evidence    ["raw.bin", "events.jsonl", …]
```

四条规矩（都有宿主回归钉着）：

1. **状态只能单向推进**：`pending -> running -> 终态`；终态**不可**被迟到的
   `running` 改回去（需求 §3.3）。允许 `pending -> 终态`（这一轮没轮到它，但要有个明确收尾）。
2. **终态就是既有结果枚举**（`PASS/FAIL/ERROR/SKIPPED/CANCELLED`）：本模块**不发明**新终态，
   也不把运行态混进 `RESULT_STATUSES`（那是 `TestResult` 的校验枚举，只增不改）。
3. **时长由单调时钟算**：调用方把 `time.monotonic()` 递进来；UTC 只用来显示与关联证据。
4. **不猜**：`case_id` 必须由项目包声明；`detail` 由执行处如实填写；本模块不认识任何
   具体产品、设备或命令（core 里不出现平台字眼）。

⚠️ 这里**不做**超时管理：超时是**执行处**按阶段自己声明的上限判定的（需求 §6.6），
本模块只负责在收尾时把状态收敛到终态（`converge`），避免留下永久 `running`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable, Iterable, Mapping

try:  # imported as ``webconsole.step_lifecycle``
    from .embedded_test_model import RESULT_STATUSES
except ImportError:  # imported as the top-level package
    from embedded_test_model import RESULT_STATUSES  # type: ignore[no-redef]

#: 写进 `events.jsonl` 的事件 kind（页面按它挑出用例卡片）。
TEST_STEP_KIND = "test_step"

#: 终态 = **既有**结果枚举（不新增、不改名）。
TERMINAL_STATUSES: tuple[str, ...] = tuple(RESULT_STATUSES)

#: 生命周期里看得到的全部状态：两个运行态 + 五个终态。
TEST_STEP_STATUSES: tuple[str, ...] = ("pending", "running") + TERMINAL_STATUSES

#: 合法的单向转换。终态不再有任何后继 —— "终态不可回退"就靠这张表。
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("running",) + TERMINAL_STATUSES,
    "running": TERMINAL_STATUSES,
    **{status: () for status in TERMINAL_STATUSES},
}

#: 阶段 id 的形状（项目包声明，跨版本稳定）：小写字母/数字开头，允许 `._-`。
_STAGE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class TestStepError(ValueError):
    """生命周期写错了（非法转换、坏 id、缺字段）—— 当场报出来，不静默改正。"""


def is_terminal(status: str) -> bool:
    """这个状态是不是终态（终态不可再变）。"""
    return status in TERMINAL_STATUSES


def advance(current: str, incoming: str) -> str:
    """校验一次状态转换，返回新状态；非法转换抛 `TestStepError`。

    纯函数：宿主回归直接用它跑正反 fixture（合法/非法转换各一组）。
    ⚠️ 有一条**不能**放宽：终态收到迟到的 `running` 必须报错，而不是"看着更新就采用" ——
    那正是需求 §3.3 要防的（设备回包晚到把已经失败的结果改回"正在跑"）。
    """
    if current not in ALLOWED_TRANSITIONS:
        raise TestStepError(f"未知的当前状态 {current!r}（合法：{list(TEST_STEP_STATUSES)}）")
    if incoming not in ALLOWED_TRANSITIONS:
        raise TestStepError(f"未知的目标状态 {incoming!r}（合法：{list(TEST_STEP_STATUSES)}）")
    if incoming not in ALLOWED_TRANSITIONS[current]:
        if is_terminal(current):
            raise TestStepError(
                f"终态不可回退：{current} -> {incoming}（迟到的更新不能改写已经确定的结果）"
            )
        raise TestStepError(f"非法转换：{current} -> {incoming}")
    return incoming


def check_stage_id(stage_id: str) -> str:
    """校验项目包声明的阶段 id（跨版本要稳定，页面与证据按它对齐）。"""
    if not isinstance(stage_id, str) or not _STAGE_ID.match(stage_id):
        raise TestStepError(
            f"阶段 id 不合法：{stage_id!r}"
            "（小写字母/数字开头，允许 . _ -，总长 ≤ 64；它进证据，不能随手改）"
        )
    return stage_id


#: 证据流的**四类来源**（需求 §8.2：只有这四种，不建立第五份证据）。
#: 第四类由用户 2026-10-09 定名 `app_events`（对应 `app_events.jsonl`）—— **故意不叫 `events`**，
#: 免得与平台事件流 `events.jsonl` 混淆（两者不是同一个文件）。
EVIDENCE_SOURCES: tuple[str, ...] = ("tool", "raw", "decoded", "app_events")

#: 阶段**当时**就能给得出的引用（observe 期）：工具输出与原始串口。
#: `decoded` / `app_events` 由 decode 之后的补写事件提供（用户 2026-10-09 口径）。
LIVE_REF_SOURCES: tuple[str, ...] = ("tool", "raw")

#: **观察会话**里能给得出**行号**坐标的三类来源（ /  L7）。
#:
#: ⚠️ 会话里 `tool` 不在这一组，两个理由都还成立：会话的「工具输出」窗口是 `io_events.jsonl`
#: 的**派生视图**（`io_timeline.tool_rows()`：跳过 RX 分块事件、离线回看还会在最前面加一行
#: 会话标题），它的行号**不是**任何一份证据文件的行号，而且实时与离线两条路的行数本来就不一样
#: （离线多一行标题）。给它一个"大概对"的行号，只会把使用者带到别的地方 ——
#: 那条规矩在需求 §8.2 里写得很清楚：**不伪造引用**，缺失就诚实缺着（那一窗保持原位）。
#: raw / decoded / app_events 三类的窗口就是文件本身，行号与文件逐行对齐。
#:
#: ⚠️ **2026-10-13（）**：工具窗不是"没有坐标"，而是它的坐标**不是行号** ——
#: 它的行身份是那条 io 事件的事件号：`{"source": "tool", "event_seq": N}`（见 `declared_refs`）。
#: 那种坐标**不由这里算**（它不来自行数游标），由宿主用例在落终态时声明（`extra_refs`）。
SESSION_REF_SOURCES: tuple[str, ...] = ("raw", "decoded", "app_events")

#: 补写引用的**事件种类**：decode 完成后追加，只补坐标（需求 §8.2 的时序补充）。
TEST_STEP_REFS_KIND = "test_step_refs"


class EvidenceCursor:
    """四类证据流的**行号游标**（core：只数行，不认识业务）。

    谁往证据里写一行，谁就在**写成功之后**调 `advance(source)`。阶段开始时快照一次、
    结束时再快照一次，两次之差就是这个阶段在那类证据里占的行号区间。

    行号口径（需求 §8.2）：**1-based、含端点**；`start_line` = 阶段开始之后写的第一行。
    某一类“这一阶段一行都没写” ⇒ 根本不出现在 `refs()` 里（**不伪造引用**），
    定位时那一窗保持原位。

    ⚠️ 为什么不让 Web 自己数：同一次 run 里证据文件会被反复追加，"行号"必须是
    写入顺序的**见证**，而不是事后按文件重数（后者会被重写、截断、导入影响）。
    """

    def __init__(self) -> None:
        self._lines: dict[str, int] = {source: 0 for source in EVIDENCE_SOURCES}

    def advance(self, source: str, count: int = 1) -> None:
        """记下"这一类证据又多写了一行"。`source` 只能是那四类之一。"""
        if source not in self._lines:
            raise TestStepError(
                f"证据来源只允许 {list(EVIDENCE_SOURCES)}，收到 {source!r}"
                "（不建立第五份证据）"
            )
        if count < 0:
            raise TestStepError(f"游标只能往前走，收到 {count!r}")
        self._lines[source] += count

    def snapshot(self) -> dict[str, int]:
        """四类各自的当前行数（给阶段开始时留一个基线）。"""
        return dict(self._lines)

    def refs(
        self,
        before: dict[str, int] | None,
        *,
        sources: tuple[str, ...] | None = None,
        anchors: dict[str, int] | None = None,
    ) -> list[dict]:
        """把"阶段前基线"变成区间行号表；没写过一行的来源不进表。

        `sources` 限定只看哪几类（observe 期的终态事件只给 `LIVE_REF_SOURCES`，
        `decoded` / `app_events` 留给 decode 之后的补写）。

        `anchors` = 每一类来源的**实际命中行**（1 基，调用方量出来的，比如"等的那句
        `@TASKS` 出现在第 13 行"）。它是**可选**键 `anchor_line` 的来源（用户口径 2026-10-13）：

        - 给了、而且**真的落在这一段里** ⇒ 用它（这才是"足以判定本阶段结果的关键行"）；
        - 没给 / 落在区间外（坐标过期、来源变了）⇒ 退回 `end_line`：那一段最后写下的东西，
          通常正是决定性证据（`@TASKS` 就是那一阶段 raw 的最后一行）；
        - **绝不**生成区间外的锚点，也**绝不**把一个来源的锚点套到另一个来源上。
        """
        wanted = tuple(sources) if sources else EVIDENCE_SOURCES
        unknown = [name for name in wanted if name not in EVIDENCE_SOURCES]
        if unknown:
            raise TestStepError(f"引用来源只允许 {list(EVIDENCE_SOURCES)}，收到 {unknown}")
        base = before or {}
        after = self._lines
        given = dict(anchors or {})
        rows: list[dict] = []
        for source in wanted:
            start = int(base.get(source, 0))
            end = int(after.get(source, 0))
            # 只在这一阶段真的写过这一类的行时才给引用 —— 缺失就诚实缺着（需求 §8.2）。
            if end > start:
                first_line = start + 1
                anchor = int(given.get(source, 0) or 0)
                if anchor < first_line or anchor > end:
                    anchor = end
                rows.append(
                    {
                        "source": source,
                        "start_line": first_line,
                        "end_line": end,
                        "anchor_line": anchor,
                    }
                )
        return rows


class StageEvidence:
    """**观察会话**（受控测试）那条路上的阶段证据坐标：行数由写证据的人报进来。

    与 `EvidenceCursor` 用**同一套** `refs()` 契约（四类来源 + 可选 `anchor_line`，
    1 基、含端点、缺失不伪造），区别只在"当前行数从哪来"：

    * run 那条路自己逐行 `advance()`（执行处就是写证据的人，它数得准）；
    * 会话这条路，四个文件由会话的写口各自在写 ⇒ 这里**只问它们要行数**
      （`totals()` 是注入的：真身是 `ObserveStore.evidence_totals()`，
      行号与磁盘上的行逐行对齐，页面自己数不出来）。

    ⚠️ 为什么不让会话也逐行 `advance()`：那会让"行号"有两个来源（写口与这个游标），
    两边一旦漂开就没人发现 —— 而这里要的正是"与证据文件的行号对齐"。
    会话只负责告诉它**现在各有几行**，差多少由这里自己算（只前进，不回退）。

    用法（`LiveTestBatch` 就是这么用的）：

    ```python
    evidence.open(key)                  # 阶段进入 running：记下这一阶段开始前的基线
    rows = evidence.refs(key, anchors=...)   # 阶段落终态：区间 + 中心锚点
    ```
    """

    def __init__(self, totals: Callable[[], Mapping[str, int]]) -> None:
        self._totals = totals
        self._cursor = EvidenceCursor()
        self._seen: dict[str, int] = {source: 0 for source in EVIDENCE_SOURCES}
        self._open: dict[str, dict[str, int]] = {}

    def totals(self) -> dict[str, int]:
        """各来源**当前**的行数（顺带把游标推到这个位置）。"""
        current = self._totals() or {}
        for source in EVIDENCE_SOURCES:
            value = int(current.get(source, 0) or 0)
            done = self._seen.get(source, 0)
            if value > done:
                self._cursor.advance(source, value - done)
                self._seen[source] = value
        return self._cursor.snapshot()

    def open(self, key: str, *, first: Mapping[str, int] | None = None) -> None:
        """记下这一阶段开始前的基线（同一个 key 只记一次，重复的 `running` 不重置）。

        `first` = "这一行就是这一步的第一行证据"（各来源各是第几行）：设备在某一行上报了
        这一步开始 ⇒ 那一行本身也算这一步的证据。不传就按"现在这一刻"当基线，
        于是区间从**下一行**开始（当时的旧行为）—— 差一行的定位就是这么来的。
        """
        if key in self._open:
            return
        baseline = self.totals()
        for source in SESSION_REF_SOURCES:
            value = int((first or {}).get(source, 0) or 0)
            # ⚠️ 只采信**已经写过的那一行**（1 ≤ value ≤ 当前行数）：调用方量出来的行号不会
            #    超过已经落盘的字节，超了就是坐标错了 —— 那时宁可退回"现在这一刻"当基线，
            #    也不要拿一个凭空的数把区间**撑大**（区间撑大 = 把无关的行圈进来）。
            if 0 < value <= int(baseline.get(source, 0) or 0):
                baseline[source] = value - 1
        self._open[key] = baseline

    def opened(self, key: str) -> bool:
        """这个阶段有没有记过基线（没有 ⇒ 它还没进过 `running`）。"""
        return key in self._open

    def refs(
        self, key: str, *, anchors: Mapping[str, int] | None = None
    ) -> list[dict]:
        """这一阶段在四类证据里占的行号区间（含 `anchor_line`）。

        没记过基线（阶段没进过 `running`）⇒ **空表**：那一段没有"阶段开始时"这个基准，
        硬算出来的区间是错的，不如诚实缺着（页面不给那一类坐标，保持原位）。
        """
        before = self._open.pop(key, None)
        if before is None:
            return []
        # ⚠️ 先把游标推到"现在各有多少行"，再算区间 —— 少这一步的话游标还停在
        #    `open()` 那一刻，算出来的区间恒为空（阶段看着就是"没有证据"）。
        self.totals()
        return self._cursor.refs(before, sources=SESSION_REF_SOURCES, anchors=dict(anchors or {}))


def refs_event_json(stage_id: str, version: int, refs: Iterable[dict], *, utc: str = "") -> dict:
    """补写事件的字段（`test_step_refs`）：**只带证据坐标**。

    ⚠️ 用户 2026-10-09 口径要求"合并时不得修改阶段状态、耗时和判定"。这里把它做成
    **结构性**保证：这个形状里**根本没有** status / duration_ms / result 可写 ——
    不是"合并的时候别忘了跳过"，而是想改也没地方改。

    ⚠️ `run_id` 由事件总线落盘时带上，关联键是 `run_id + stage_id + version`；
    `version` 与阶段声明同源，声明改语义就该 +1（旧补写因此不会被误合到新语义上）。
    """
    check_stage_id(stage_id)
    if int(version) <= 0:
        raise TestStepError(f"补写引用的 version 必须为正，收到 {version!r}")
    payload: dict = {
        "kind": TEST_STEP_REFS_KIND,
        "case_id": stage_id,
        "version": int(version),
        "evidence_refs": [dict(row) for row in refs],
    }
    if utc:
        payload["observed_utc"] = utc
    return payload


def merge_refs(base: Iterable[dict], patch: Iterable[dict]) -> list[dict]:
    """合并两份引用：**只动坐标**。

    - 同一个来源以 `patch`（补写）为准 —— 它才是 decode 之后的权威位置；
    - `patch` 没有的来源保留 `base`（实时先得的那一份）；
    - 输出顺序固定按 `EVIDENCE_SOURCES`，与事件里的顺序一致。

    ⚠️ 它只接受坐标行 —— 阶段状态/耗时/判定根本不在参数里，所以"合并不会改判定"
    是这条函数的类型就决定了的事。
    """
    merged: dict[str, dict] = {}
    for row in list(base) + list(patch):
        source = str(row.get("source") or "")
        if source in EVIDENCE_SOURCES:
            merged[source] = dict(row)
    return [merged[source] for source in EVIDENCE_SOURCES if source in merged]


def declared_refs(rows: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """一份**调用方声明**的坐标 → 规整过的坐标行（）。

    为什么需要它：不是每一类来源都有"文件行号"这个空间。「工具输出」是 `io_events.jsonl`
    的**派生视图**（RX 索引不画、离线还多一行标题）⇒ 拿行号指它一定会指歪。它的行身份是
    那条 io 事件的**事件号**：`{"source": "tool", "event_seq": N}` —— 唯一一种非行号坐标。

    - 两种味道都收：行号味（`start_line` / `end_line` / `anchor_line`，1 基含端点，
      与 `refs()` 同一套口径）与事件号味（`event_seq`，只允许 `tool`）；
    - 来源必须在那四类里，坐标必须是正整数：写错的**当场报出来**（这是代码写错了，
      不是设备说错了 —— 悄悄丢掉比报错更糟：坐标会凭空消失）；
    - 空/缺省 ⇒ 空表（调用方没声明就什么都不加）。
    """
    cleaned: list[dict[str, Any]] = []
    for raw in rows or ():
        row = dict(raw or {})
        source = str(row.get("source") or "")
        if source not in EVIDENCE_SOURCES:
            raise TestStepError(
                f"声明引用的来源只允许 {list(EVIDENCE_SOURCES)}，收到 {source!r}"
            )
        event_seq = row.get("event_seq")
        if event_seq is not None:
            if source != "tool":
                raise TestStepError(
                    f"事件号坐标只适用于 tool（派生视图）；{source!r} 有自己的文件行号，"
                    "请给 start_line / end_line"
                )
            value = int(event_seq)
            if value < 1:
                raise TestStepError(f"事件号必须为正，收到 {event_seq!r}")
            cleaned.append({"source": source, "event_seq": value})
            continue
        first = int(row.get("start_line") or 0)
        if first < 1:
            raise TestStepError(
                f"声明引用要么给 event_seq，要么给 start_line（1 基），收到 {row!r}"
            )
        end = int(row.get("end_line") or first)
        if end < first:
            raise TestStepError(f"声明引用的 end_line（{end}）在 start_line（{first}）之前")
        anchor = int(row.get("anchor_line") or end)
        if not first <= anchor <= end:
            anchor = end   # 与 `refs()` 同一条老规矩：越界的锚点退回区间末行
        cleaned.append(
            {
                "source": source,
                "start_line": first,
                "end_line": end,
                "anchor_line": anchor,
            }
        )
    return cleaned


@dataclass(frozen=True)
class TestStep:
    """**一个阶段**的声明（由项目包提供；core 只按它记录状态）。"""

    stage_id: str
    #: 人话名字（页面卡片标题）。
    label: str
    #: 期望发生什么（需求要求点击详情能看到"预期结果"）。
    expected: str = ""
    #: 声明版本：同 id 改语义就要 +1，好让旧证据仍然可解释。
    version: int = 1
    #: 这一阶段最多等多久（秒；0 = 不限）。**超时由执行处按它判定**。
    timeout_s: float = 0.0
    #: 证据文件名（如 `raw.bin`）；页面只显示引用，不复制内容。
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_stage_id(self.stage_id)
        if not self.label:
            raise TestStepError(f"阶段 {self.stage_id!r} 必须有 label（页面要显示它）")
        if self.version <= 0:
            raise TestStepError(f"阶段 {self.stage_id!r} 的 version 必须为正")
        if self.timeout_s < 0:
            raise TestStepError(f"阶段 {self.stage_id!r} 的 timeout_s 不能为负")


@dataclass
class TestStepState:
    """一个阶段的**运行期状态**（执行处持有；落盘的是 `TestStepEvent`）。"""

    step: TestStep
    status: str = "pending"
    detail: str = ""
    started_mono: float | None = None
    ended_mono: float | None = None
    started_utc: str = ""
    ended_utc: str = ""
    #: 这一阶段在四类证据里各占的行号区间（由执行处按 `EvidenceCursor` 填，见需求 §8.2）。
    evidence_refs: tuple[dict, ...] = ()

    @property
    def stage_id(self) -> str:
        return self.step.stage_id

    def duration_ms(self, now_mono: float) -> int:
        """已耗时（毫秒）。

        - 还没开始 ⇒ 0（`pending` 不假装自己跑过）；
        - 正在跑 ⇒ 到现在为止（页面要显示递增耗时）；
        - 已结束 ⇒ 定格在结束那一刻，**不再随查询变化**。
        """
        if self.started_mono is None:
            return 0
        end = self.ended_mono if self.ended_mono is not None else now_mono
        return max(0, int(round((end - self.started_mono) * 1000)))


def step_event_json(state: TestStepState, now_mono: float, *, now_utc: str = "") -> dict:
    """把一个阶段的当前状态变成 `test_step` 事件的字段（形状见模块 docstring）。"""
    payload: dict = {
        "kind": TEST_STEP_KIND,
        "case_id": state.step.stage_id,
        "version": state.step.version,
        "label": state.step.label,
        # 需求 §3.2：点开卡片要能看到"预期发生什么"。它来自项目包声明，不在这里编。
        "expected": state.step.expected,
        "status": state.status,
        "detail": state.detail,
        "started_utc": state.started_utc or None,
        "ended_utc": state.ended_utc or None,
        "duration_ms": state.duration_ms(now_mono),
        "evidence": list(state.step.evidence),
        # 需求 §8.2：引用随事件落盘，刷新/回看/导入后得到相同位置。**只增字段**：
        # 旧 run 没有它就退回到“没有定位信息”，而不是猜一个。
        "evidence_refs": [dict(row) for row in state.evidence_refs],
    }
    if now_utc and state.started_utc:
        payload["observed_utc"] = now_utc
    return payload


def _exchange_label(exchange: Any, stage_id: str) -> str:
    """给阶段卡的人话标题：`purpose` 优先，其次 `notice`，都没有就用 id（**不替它编**）。"""
    for name in ("purpose", "notice"):
        text = str(getattr(exchange, name, "") or "").strip()
        if text:
            return text if len(text) <= 80 else text[:79] + "…"
    return stage_id


def steps_from_exchanges(
    exchanges: Iterable[Any], *, gate_is_open: Callable[[Any], bool] | None = None
) -> list[TestStep]:
    """从一批"声明的交互"里挑出**本轮要做的**阶段（T1 的样板入口）。

    ⚠️ **鸭子类型**：只读 `stage_id / purpose / notice / wait_seconds`，不 import 对话模块，
    也不认识"门控"是什么 —— 要不要做由调用处递进来的 `gate_is_open` 回答。
    于是"哪些交互算阶段""这一轮做哪些"仍然由项目声明 + 执行处决定。

    - 没有 `stage_id` 的交互**不进阶段表**：旧声明照旧执行，只是不上阶段卡（向后兼容）。
    - 顺序就是声明顺序（需求 §3.1：页面按声明顺序铺开）。
    - 重复 id 交给 `TestStepRecorder` 报错，不在这里静默去重。
    """
    steps: list[TestStep] = []
    for exchange in exchanges:
        stage_id = str(getattr(exchange, "stage_id", "") or "")
        if not stage_id:
            continue
        if gate_is_open is not None and not gate_is_open(exchange):
            continue
        steps.append(
            TestStep(
                stage_id=stage_id,
                label=_exchange_label(exchange, stage_id),
                expected=str(getattr(exchange, "purpose", "") or "").strip(),
                timeout_s=float(getattr(exchange, "wait_seconds", 0.0) or 0.0),
            )
        )
    return steps


class TestStepRecorder:
    """按声明顺序记录每个阶段的状态，并把变化交给调用方发出去。

    **core，不认识任何业务**：它按 `TestStep` 的声明工作，`plan()` 的时候一次性确定顺序
    （需求 §3.1：run 创建后先写 `pending`，顺序来自项目包声明）。

    ⚠️ 它**不自己**写 `events.jsonl`：那件事由执行处走既有事件总线做（需求 §3.5），
    这样"事件只从一处发布"这条不变。
    """

    def __init__(self, steps: list[TestStep] | tuple[TestStep, ...]):
        seen: set[str] = set()
        for step in steps:
            if step.stage_id in seen:
                raise TestStepError(f"阶段 id 重复：{step.stage_id!r}（顺序要唯一）")
            seen.add(step.stage_id)
        self.states: list[TestStepState] = [TestStepState(step=step) for step in steps]
        self._by_id = {state.stage_id: state for state in self.states}

    def __len__(self) -> int:
        return len(self.states)

    def get(self, stage_id: str) -> TestStepState:
        try:
            return self._by_id[stage_id]
        except KeyError as error:
            raise TestStepError(f"没声明过这个阶段：{stage_id!r}") from error

    # ---------------------------------------------------------------- 推进
    def start(
        self,
        stage_id: str,
        *,
        detail: str,
        mono: float,
        utc: str,
        refs: list[dict] | None = None,
    ) -> dict:
        """进入 `running`：记下单调起点与展示用时刻。"""
        state = self.get(stage_id)
        state.status = advance(state.status, "running")
        state.detail = detail
        state.started_mono = mono
        state.started_utc = utc
        if refs:
            state.evidence_refs = tuple(refs)
        return self.snapshot(stage_id, mono)

    def finish(
        self,
        stage_id: str,
        status: str,
        *,
        detail: str,
        mono: float,
        utc: str,
        refs: list[dict] | None = None,
    ) -> dict:
        """进入终态。`status` 必须是五个终态之一（运行态不接受 —— 那是 `start` 的事）。"""
        if status not in TERMINAL_STATUSES:
            raise TestStepError(
                f"finish 只接受终态 {list(TERMINAL_STATUSES)}，收到 {status!r}"
                f"（要进 running 用 start()）"
            )
        state = self.get(stage_id)
        state.status = advance(state.status, status)
        state.detail = detail
        # `pending -> 终态` 也要有时长（0）：没跑过就不假装跑过，但状态得收尾。
        state.ended_mono = mono
        state.ended_utc = utc
        if state.started_mono is None:
            state.started_mono = mono
            state.started_utc = utc
        # 终态才落引用：这一阶段的证据到这一刻才算写完（需求 §8.2）。
        if refs is not None:
            state.evidence_refs = tuple(refs)
        return self.snapshot(stage_id, mono)

    def converge(
        self,
        *,
        detail: str,
        mono: float,
        utc: str,
        terminal: str = "ERROR",
        refs_for: Callable[[str], list[dict] | None] | None = None,
    ) -> list[dict]:
        """把**还没结束**的阶段收口（连接断了、超时、取消、异常都走这里）。

        `running` ⇒ `terminal`（默认 `ERROR`：跑到一半没了下文，那是执行错误，不是"没跑"）；
        `pending` ⇒ `SKIPPED`（这一轮没轮到它，理由要写进 `detail`）。
        取消这一路显式传 `terminal="CANCELLED"` —— 用户取消与执行出错是两件事。

        `refs_for` = 按阶段 id 要证据坐标的回调（可空）。**只有正在跑的阶段**问它 ——
        被这一步/这一批拖停的那些阶段一行证据都没写过，给坐标等于伪造引用（需求 §8.2）。

        ⚠️ 需求 §6.6：不允许留下永久 `running` 的卡片。宁可如实报错，
        也不要让页面一直转圈。
        """
        if terminal not in TERMINAL_STATUSES:
            raise TestStepError(
                f"converge 的 terminal 必须是终态 {list(TERMINAL_STATUSES)}，收到 {terminal!r}"
            )
        changed: list[dict] = []
        for state in self.states:
            if is_terminal(state.status):
                continue
            target = terminal if state.status == "running" else "SKIPPED"
            refs = None
            if refs_for is not None and state.status == "running":
                refs = refs_for(state.stage_id)
            changed.append(self.finish(state.stage_id, target, detail=detail, mono=mono,
                                       utc=utc, refs=refs))
        return changed

    # ---------------------------------------------------------------- 读
    def snapshot(self, stage_id: str, mono: float) -> dict:
        return step_event_json(self.get(stage_id), mono)

    def snapshots(self, mono: float) -> list[dict]:
        """全部阶段的当前状态（按声明顺序）—— 刷新页面时用它重建。"""
        return [step_event_json(state, mono) for state in self.states]

    def summary(self) -> dict:
        """按终态口径汇总（与既有 `test_summary` 同一套词，便于页面复用）。"""
        counts: dict[str, int] = {}
        for state in self.states:
            counts[state.status] = counts.get(state.status, 0) + 1
        terminals = [state.status for state in self.states if is_terminal(state.status)]
        return {
            "total": len(self.states),
            "counts": counts,
            "finished": len(terminals),
            "result": (
                "PASS" if terminals and all(row == "PASS" for row in terminals) and len(terminals) == len(self.states)
                else next((row for row in terminals if row != "PASS"), "")
            ),
        }


__all__ = [
    "EVIDENCE_SOURCES",
    "LIVE_REF_SOURCES",
    "SESSION_REF_SOURCES",
    "TEST_STEP_REFS_KIND",
    "EvidenceCursor",
    "StageEvidence",
    "declared_refs",
    "merge_refs",
    "refs_event_json",
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATUSES",
    "TEST_STEP_KIND",
    "TEST_STEP_STATUSES",
    "TestStep",
    "TestStepError",
    "TestStepRecorder",
    "TestStepState",
    "advance",
    "check_stage_id",
    "is_terminal",
    "step_event_json",
    "steps_from_exchanges",
]
