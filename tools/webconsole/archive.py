"""把设备身份映射到**主机的构建归档**（Phase 7.1 切片 5，需求 §6.2 / §8）。

需求 §6.2 说得很直白：**设备自报的信息与主机匹配出的 ELF/字典信息必须分开标注来源**。
所以这个模块只回答一个问题：

    给定一个 build id，主机能不能找到**同一份构建**的字典？

答案只有两种，没有第三种：

- 找得到 ⇒ 返回 `{path, origin, entries, elf_sha256, exact_match: True}`（选库的唯一依据是
  "字典里记的 build_id32 == 设备报的那个"，不是"名字看起来像"）；
- 找不到 ⇒ 返回 `None`（**拒绝猜字典**）。调用方必须把"没有字典"当成一个**状态**说出来，
  而不是拿一份别的字典把日志解成看起来像日志的东西。

`dictionary.Library` 已经把这个规矩实现好了（Phase 2 的 A23 断言就是它），这里只是
"库 + 记忆化"的薄封装，让实时观察路径不必每次 RX 都去扫归档目录。

 / R2b：**库从哪来是注入的**（`library_factory`）。这个模块过去在没注入时
`import dictionary` 自己去找库 —— 一条 core → 目标层的反向依赖（边界清单 §4）。现在
没有库工厂就不选库，并把"没有字典库"如实说出来（`LibraryUnavailable`）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class LibraryUnavailable(RuntimeError):
    """没有装配字典库：不猜字典，也不假装查过了。"""

    def __init__(self, message: str = "没有装配字典库（组合入口没提供 library_factory）") -> None:
        super().__init__(message)


class ArchiveIndex:
    """构建归档的索引（记忆化：一次会话里同一份库不会重扫）。"""

    def __init__(self, library_factory: Callable[[], Any] | None = None) -> None:
        self._library_factory = library_factory
        self._library: Any = None
        self._cache: dict[int, dict[str, Any] | None] = {}

    # ------------------------------------------------------------------ 库本身
    def library(self) -> Any:
        if self._library is None:
            if self._library_factory is None:
                raise LibraryUnavailable()
            self._library = self._library_factory()
        return self._library

    def available(self) -> list[str]:
        """手上有哪些字典（"拒绝猜字典"时要把这个说给用户听）。"""
        try:
            return list(self.library().describe_available())
        except Exception as error:  # 目录读不动不该让整个会话崩掉
            return [f"（归档不可读：{type(error).__name__}: {error}）"]

    # ------------------------------------------------------------------ 选库
    def match(self, build_id32: int | None) -> dict[str, Any] | None:
        """按 build id **精确**选库；没有就是没有。

        ⚠️ **未命中时先重扫一次**（）：库是记忆化的，而 run 会在服务运行期间
        往归档目录里放新构建 —— 实时观察如果一直用启动时那份快照，就会对着刚烧进去的
        固件说"主机没有对应的字典、拒绝解码"，看起来像系统坏了。
        每个 build id 最多多扫这一次：重扫后仍没有就把 `None` 记进缓存。
        """
        if build_id32 is None:
            return None
        if build_id32 in self._cache:
            return self._cache[build_id32]
        selection = self._lookup(build_id32)
        if selection is None:
            self.invalidate()
            selection = self._lookup(build_id32)
        self._cache[build_id32] = selection
        return selection

    def _lookup(self, build_id32: int) -> dict[str, Any] | None:
        """在当前这份库里找 build id（命中就是命中；读不动就当没命中）。"""
        try:
            source = self.library().exact(build_id32)
        except Exception:
            return None
        if source is None:
            return None
        return {
            "path": str(source.path),
            "origin": source.origin,
            "build_id32": source.build_id32,
            "device_build_id32": build_id32,
            "elf_sha256": source.elf_sha256,
            "entries": len(self.library().entries_for(source)),
            "exact_match": source.build_id32 == build_id32,
        }

    def invalidate(self) -> None:
        """归档目录变了（刚归档了一个构建）时叫它重扫。

        `match()` 未命中时会自己调它；其它知道"库变了"的调用方也可以显式调。
        """
        self._library = None
        self._cache.clear()
