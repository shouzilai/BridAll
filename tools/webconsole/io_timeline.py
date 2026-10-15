"""`io_events.jsonl` → 「工具输出」窗口的一行行文字（Phase 7.1 切片 6）。

需求 §5.2 说得很清楚：工具窗口里那些 `TX · command_text · STATUS<CR><LF>` **只是
`io_events.jsonl` 的视图**。既然只是视图，它就只有一个正确的画法 —— 所以这里把画法
做成纯函数，并且有**两个**使用者：

- 实时观察时由 `observe.py` 当场说（`_say()` / `_emit_tx_line()`）；
- 会话结束回看时由 `session_review.py` 把同一份事件重新说一遍。

两份说法必须一致：否则"现场看到的"和"事后回看的"会变成两个故事，而它们都被当作证据。
所以 TX 行的格式只有一个实现 —— `tx_line()`，两条路都调它。

**不编造**：`io_events.jsonl` 里没有的事件这里一行都不画。运行期里那些没有进时间线的
说明文字（`observe._say()` 不带 `kind` 的调用，例如"实时解码已就绪…"）回看时**不会**出现 ——
这一点写在回看页的说明里，而不是靠补一句"大概发生过什么"。回看时唯一由主机现写的一行是
开头的 `[observe] 回看：…`，它明明白白写着"回看"，不冒充设备或运行期的记录。
"""

from __future__ import annotations

from typing import Any, Iterable

# 运行期主机说明的前缀（与 `observe._say()` 一致）。
TOOL_PREFIX = "[observe] "
# 回看时那一行开头的标记：让人一眼看出这一行是**回看时现写的**，不是当时的记录。
REVIEW_PREFIX = "[observe] 回看："

# 这些 kind 在工具窗口里**不出现**（它们在别的窗口里，重复画一遍只会让人以为有两件事）：
#   rx      → 原始字节在 raw.bin / 「原始串口」窗口，它的事件只是索引
#   identity（没有 message 的那种）→ 它是同一个身份结论的结构化孪生，
#                                    带 message 的那条已经把它说成人话了
_KINDS_WITHOUT_LINE = frozenset({"rx"})

# 这些字段是"时间线本身"（每条事件都有），说成人话时不该混进去。
_ENVELOPE_FIELDS = frozenset({"seq", "t_mono_us", "host_time", "kind"})


def tx_line(source: object, display: object) -> str:
    """TX 在工具窗口里的那一行（需求 §5.2 的示例格式，**唯一**实现）。

    ⚠️ 它以 `TX` 开头，且**永远不进 `raw.bin`** —— 那里只有设备说的话。
    """
    return f"TX · {source or '?'} · {display or ''}"


def _compact(event: dict[str, Any]) -> str:
    """认不出来的事件也要**如实说出来**，而不是悄悄丢掉。

    字段按名字排序，所以同一条事件每次渲染出来都一样（可复核）。
    """
    parts = [
        f"{key}={value}"
        for key, value in sorted(event.items())
        if key not in _ENVELOPE_FIELDS and value not in (None, "")
    ]
    return " ".join(parts) or "（没有字段）"


def _live_test_line(event: dict[str, Any]) -> str:
    """受控测试在工具窗口里的那一行（粗粒度：开始 / 一条执行的结论 / 整批收口）。

    ⚠️ 阶段变化**不**逐步画在这里：它们已经在阶段卡与 manifest 的冻结块里，
    逐步写会把工具窗口灌满同义词（那个窗口是给人看时间线的）。
    """
    what = str(event.get("event") or "")
    if what == "started":
        rows = event.get("executions") or []
        names = "、".join(str(row.get("case_id") or "?") for row in rows)
        return f"{TOOL_PREFIX}受控测试开始：{len(rows)} 条（{names}）"
    if what == "execution":
        detail = f"：{event.get('reason')}" if event.get("reason") else ""
        return (
            f"{TOOL_PREFIX}受控测试 {event.get('case_id')}@{event.get('version')}"
            f" → {event.get('status')}（{event.get('duration_ms')} ms）{detail}"
        )
    if what == "finished":
        return (
            f"{TOOL_PREFIX}受控测试结束：{event.get('status')}"
            f"（{event.get('reason_code') or 'ok'}）{event.get('reason') or ''}".rstrip()
        )
    return f"{TOOL_PREFIX}受控测试: {_compact(event)}"


def event_line(event: dict[str, Any]) -> str | None:
    """一条 io 事件 → 工具窗口的一行。返回 None = 这一条不该在这里出现。"""
    kind = str(event.get("kind") or "")
    if kind in _KINDS_WITHOUT_LINE:
        return None
    if kind == "identity":
        message = str(event.get("message") or "").strip()
        return f"{TOOL_PREFIX}{message}" if message else None
    if kind == "tx":
        return tx_line(event.get("source"), event.get("display"))
    if kind == "connect":
        port, baud = event.get("port"), event.get("baud")
        where = f"{port} @ {baud}" if port and baud else (port or baud or "未知端口")
        return f"{TOOL_PREFIX}连接 {where}"
    if kind == "reconnect":
        port = event.get("port")
        return f"{TOOL_PREFIX}重连{(' ' + str(port)) if port else ''}：只关闭并重新打开串口（不复位、不清设备数据）"
    if kind == "disconnect":
        reason = event.get("reason") or "未记录原因"
        counts = ""
        if event.get("rx_bytes") is not None:
            counts = f"；这次会话 RX {event.get('rx_bytes')} B"
            if event.get("rx_chunks") is not None:
                counts += f" / {event.get('rx_chunks')} 次"
        return f"{TOOL_PREFIX}断开（{reason}）{counts}"
    if kind == "live_test":
        return _live_test_line(event)
    message = str(event.get("message") or "").strip()
    return f"{TOOL_PREFIX}{kind}: {message or _compact(event)}"


def tool_timeline(
    events: Iterable[dict[str, Any]], *, header: str | None = None
) -> tuple[list[str], dict[str, int]]:
    """整条 io 时间线 → 工具窗口的行 + 一份"画了多少、跳过了什么"的账。

    返回的账是给回看页的统计行与宿主断言用的：**跳过 RX 事件这件事必须说得出来**，
    否则"工具窗口里没有 RX"看起来像丢了证据。
    """
    rows, stats = tool_rows_with_stats(events, header=header)
    return [text for text, _source_event in rows], stats


def tool_rows(
    events: Iterable[dict[str, Any]], *, header: str | None = None
) -> list[tuple[str, int | None]]:
    """工具窗口的行 + 每一行**来自哪一条 io 事件**（`io_events.jsonl` 的 `seq`）。

    ⚠️ 这一窗是**派生视图**（RX 索引不画、回看还多一行标题）⇒ 它的行号与文件行号不同源。
    所以阶段卡要指到这一窗的某一行时，用的是**事件号**而不是行区间：
    `{"source": "tool", "event_seq": N}`（）。`header` 那一行不是任何事件画的
    ⇒ 事件号是 `None`（不编一个）。
    """
    rows, _stats = tool_rows_with_stats(events, header=header)
    return rows


def tool_rows_with_stats(
    events: Iterable[dict[str, Any]], *, header: str | None = None
) -> tuple[list[tuple[str, int | None]], dict[str, int]]:
    """上面两个出口的**同一份**实现（画法与记账都不会各走各的）。"""
    rows: list[tuple[str, int | None]] = []
    if header:
        rows.append((header, None))
    stats = {
        "events": 0,
        "rx_events": 0,
        "tx_events": 0,
        "rendered": 0,
        "skipped": 0,
        "unrecognised": 0,
    }
    known = {"connect", "reconnect", "disconnect", "tx", "identity", "rx", "live_test"}
    for event in events:
        stats["events"] += 1
        kind = str(event.get("kind") or "")
        if kind == "rx":
            stats["rx_events"] += 1
        elif kind == "tx":
            stats["tx_events"] += 1
        if kind and kind not in known:
            stats["unrecognised"] += 1
        line = event_line(event)
        if line is None:
            stats["skipped"] += 1
            continue
        seq = event.get("seq")
        rows.append((line, int(seq) if isinstance(seq, int) else None))
        stats["rendered"] += 1
    return rows, stats
