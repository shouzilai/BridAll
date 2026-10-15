"""实时解码（Phase 7.1 切片 5，需求 §5.1 / §6）。

需求 §5.1 把会话证据定成五个文件，其中两个是**可派生产物**：

```text
observe-sessions/<session-id>/
  raw.bin           设备发来的字节（权威）
  io_events.jsonl    TX / RX 索引 / 连接状态（权威）
  manifest.json      身份与字典匹配（权威）
  decoded.log        由 raw.bin + 字典推导
  app_events.jsonl   由 decoded.log 推导
```

这个模块负责后两样的**增量**推导：每来一整行 RX，就

1. 用**注入的解码器**把 token 行换成明文（自动验证路径用的是同一个 Workbench 解码器）；
2. 把明文交给**注入的项目事件解析器**（与项目的批量解析共用同一条匹配规则），
   `seq` / `offset` 随行递增 —— 于是实时路径与"事后整体重放"给出同一种事件。

⚠️ 与自动验证的区别：这里**没有**"后面再来一次权威解码"的机会，实时写下去的就是最终证据。
所以：
- 字典必须**精确命中**（由 `archive.ArchiveIndex` 决定），没命中就不解码、只留 raw；
- 解码器不可用（Workbench 子模块缺失 / 没装配）时如实报"没解码"，不写半份 `decoded.log`。

 / R2b：**解码器、token domain、事件解析器全是注入的**。这个模块过去直接
`import token 解码模块` 与 `import app_events`（两条 core → 目标/项目的反向依赖，
并且把项目的 domain 示例项目 硬写在这里）。现在 core 只认两个函数契约：

```text
decoder_factory(dictionary_path, domain) -> (bytes -> bytes)     一行进、一行出，保留行终止符
event_parser(text, seq=…, offset=…) -> 事件 | None                 事件要有 to_json()
```

没装配就是没装配：`domain` 没声明就**不解码**（按错 domain 过滤出来的"日志"
比不解码更坏），事件解析器缺了就只出文本、不产生结构化事件（不编事件）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class DecoderNotWired(RuntimeError):
    """没有装配解码 / 没声明 domain：不猜、不降级成一个假解码器。"""


class LiveDecoder:
    """一行一行地解码，并维护 `app_events` 需要的 `seq` / `offset`。"""

    def __init__(
        self,
        dictionary_path: Path,
        *,
        domain: str = "",
        decoder_factory: Callable[[Path, str], Callable[[bytes], bytes]] | None = None,
        event_parser: Callable[..., Any] | None = None,
    ) -> None:
        self.dictionary_path = Path(dictionary_path)
        self.domain = str(domain or "")
        self._decoder_factory = decoder_factory
        self._event_parser = event_parser
        self._decode_line: Callable[[bytes], bytes] | None = None
        self.lines = 0          # 解过的行数
        self.decoded_lines = 0  # 其中真的被换掉的行数（token 行）
        self.events = 0         # 落进 app_events 的记录数
        self.offset = 0         # decoded.log 里的字节偏移（与 app_events 的 offset 同基准）

    # ------------------------------------------------------------------ 解码器
    def decoder(self) -> Callable[[bytes], bytes]:
        """拿到"一行 → 一行"的解码器；失败就抛，**不返回一个假解码器**。"""
        if self._decode_line is None:
            if self._decoder_factory is None:
                raise DecoderNotWired(
                    "没有装配解码器（组合入口没提供 decoder_factory）：只采 raw.bin"
                )
            if not self.domain:
                raise DecoderNotWired(
                    "没有声明 token domain（应由目标 profile 提供）："
                    "不知道按哪个 domain 过滤 token，所以不解码"
                )
            self._decode_line = self._decoder_factory(self.dictionary_path, self.domain)
        return self._decode_line

    # ------------------------------------------------------------------- 逐行
    def feed_line(self, raw_line: bytes) -> tuple[bytes, Any | None]:
        """一行 RX（含行终止符）→ `(decoded_line, app_event|None)`。

        `decoded_line` 始终以原来的行终止符结尾（解码器契约），
        所以调用方可以直接把它追加到 `decoded.log`，行与行不会粘在一起。

        `app_event` 来自注入的项目解析器；没装配解析器时永远是 `None`
        —— 宁可没有结构化事件，也不去猜一条记录的意思。
        """
        decoded = self.decoder()(raw_line)
        if decoded != raw_line:
            self.decoded_lines += 1
        self.lines += 1
        event: Any | None = None
        if self._event_parser is not None:
            text = decoded.decode("utf-8", "replace")
            event = self._event_parser(
                text.rstrip("\r\n"), seq=self.events + 1, offset=self.offset
            )
        self.offset += len(decoded)
        if event is not None:
            self.events += 1
        return decoded, event

    # ------------------------------------------------------------------- 摘要
    def summary(self) -> dict[str, Any]:
        return {
            "dictionary": str(self.dictionary_path),
            "lines": self.lines,
            "token_lines": self.decoded_lines,
            "events": self.events,
            "decoded_bytes": self.offset,
        }
