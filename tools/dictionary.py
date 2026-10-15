#!/usr/bin/env python3
"""Dictionary selection: pick the dictionary of the build that produced a capture.

The bootstrap problem this module exists to solve: a captured log is only decodable
with the dictionary of the *exact* build that produced it, and the identity needed to
choose that dictionary therefore cannot itself be tokenized. So the firmware prints a
plain-text banner line (``BOOT build=0x… fw=…``) and this module turns that id into a
dictionary.

Policy, in order of preference:

1. **exact** -- a dictionary whose recorded ``build_id32`` equals the one the device
   reported. This is the only automatic path.
2. **refuse** -- if no exact dictionary is known, decoding stops and says so. Decoding
   with a merely *plausible* dictionary is the failure mode this whole module exists
   to prevent: the output looks like a decoded log while being fiction.
3. **merged (explicit only)** -- a human may ask for the union of all known
   dictionaries (``--merged``). It is never used automatically, and tokens that two
   dictionaries define differently are reported as **collisions** instead of being
   silently resolved to whichever came first.

A "dictionary" here is a ``tokens.csv`` in the format the official Workbench extractor
writes: ``token,?,domain,"format string"``.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Iterable

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
import unicode_console  # noqa: E402

unicode_console.install()

LOGGER_DIR = TOOLS_DIR.parent
CURRENT_DICTIONARY = LOGGER_DIR / ".pio" / "build" / "demo" / "logging" / "tokens.csv"
ARCHIVE_ROOT = LOGGER_DIR / "artifacts" / "logging" / "builds"


@dataclass(frozen=True)
class Entry:
    token: int
    domain: str
    format_string: str
    source: str

    def to_csv_row(self) -> list[str]:
        return [f"{self.token:08x}", "", self.domain, self.format_string]


@dataclass(frozen=True)
class DictionarySource:
    """One dictionary on disk, with the identity it belongs to."""

    path: Path
    build_id32: int | None
    pio_env: str | None
    elf_sha256: str | None
    origin: str

    def describe(self) -> str:
        identity = f"0x{self.build_id32:08x}" if self.build_id32 else "未登记身份"
        return f"{self.origin}（{identity}，{self.path.name}）"


def load(path: Path, *, origin: str | None = None) -> list[Entry]:
    """Read one ``tokens.csv``. Tolerates the extractor's quoting quirks."""
    entries: list[Entry] = []
    label = origin or str(path)
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for row in csv.reader(text.splitlines()):
        if len(row) < 4:
            continue
        raw_token = row[0].strip()
        if not raw_token:
            continue
        try:
            token = int(raw_token, 16)
        except ValueError:
            continue
        entries.append(
            Entry(
                token=token,
                domain=row[2].strip(),
                format_string=row[3],
                source=label,
            )
        )
    return entries


def collisions(entries: Iterable[Entry]) -> dict[int, list[Entry]]:
    """Tokens that more than one source defines *differently*.

    Same token + same format string in two dictionaries is not a collision: it is the
    same message compiled twice. Different format strings for one token is a genuine
    ambiguity and must be reported, never resolved silently.
    """
    by_token: dict[int, list[Entry]] = {}
    for entry in entries:
        by_token.setdefault(entry.token, []).append(entry)
    return {
        token: group
        for token, group in by_token.items()
        if len({entry.format_string for entry in group}) > 1
    }


class Library:
    """Every dictionary this machine knows about, plus how to choose one."""

    def __init__(self, sources: Iterable[DictionarySource]) -> None:
        self.sources: list[DictionarySource] = list(sources)
        self._cache: dict[Path, list[Entry]] = {}

    # ------------------------------------------------------------------ loading
    def entries_for(self, source: DictionarySource) -> list[Entry]:
        if source.path not in self._cache:
            self._cache[source.path] = load(source.path, origin=source.origin)
        return self._cache[source.path]

    def all_entries(self) -> list[Entry]:
        rows: list[Entry] = []
        for source in self.sources:
            rows.extend(self.entries_for(source))
        return rows

    # ---------------------------------------------------------------- selection
    def exact(self, build_id32: int | None) -> DictionarySource | None:
        if build_id32 is None:
            return None
        for source in self.sources:
            if source.build_id32 == build_id32:
                return source
        return None

    def describe_available(self) -> list[str]:
        return [source.describe() for source in self.sources]

    def read_banner(self, text: str) -> tuple[int | None, dict[str, str]]:
        """从明文捕获里读出**构建身份**（解码前唯一能读的东西）。

        ⚠️  / R4：读横幅的规则是**这份库所属的工具链/固件**的知识：
        编排以前直接调本模块的 `read_build_banner()`，换一个固件就会拿
        别人的横幅格式去解自己的捕获（）。现在编排只认
        “库要能 `read_banner`”这一个约定。
        """
        return read_build_banner(text)

    def merged(self) -> tuple[list[Entry], dict[int, list[Entry]]]:
        """Union of every dictionary, plus the tokens that disagree.

        The conflicting tokens keep their first definition (so a decode can still run
        and a human can see *why* it is not to be trusted), and they are returned
        separately so callers can refuse, warn or fail an assertion.
        """
        entries = self.all_entries()
        by_token: dict[int, Entry] = {}
        for entry in entries:
            by_token.setdefault(entry.token, entry)
        return list(by_token.values()), collisions(entries)


def discover(*, include_current: bool = True, archive_root: Path = ARCHIVE_ROOT) -> Library:
    """Build the library from what is on disk: archived builds + the current build."""
    import build_archive  # noqa: PLC0415  (same tools dir; avoids an import cycle)

    sources: list[DictionarySource] = []
    for index in build_archive.iter_indexes(archive_root):
        directory = Path(index["_dir"])
        path = directory / "tokens.csv"
        if not path.is_file():
            continue
        sources.append(
            DictionarySource(
                path=path,
                build_id32=index.get("build_id32"),
                pio_env=index.get("pio_env"),
                elf_sha256=index.get("elf_sha256"),
                origin=f"归档 {directory.name}",
            )
        )
    if include_current:
        for manifest_path in sorted(
            (LOGGER_DIR / ".pio" / "build").glob("*/logging/manifest.json")
        ):
            path = manifest_path.parent / "tokens.csv"
            if not path.is_file():
                continue
            try:
                import json  # noqa: PLC0415

                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            sources.append(
                DictionarySource(
                    path=path,
                    build_id32=payload.get("build_id32"),
                    pio_env=payload.get("pio_env"),
                    elf_sha256=payload.get("elf_sha256"),
                    origin=f"当前构建 {manifest_path.parent.parent.name}",
                )
            )
    # De-duplicate by resolved path, keeping the first (archived) description.
    seen: set[Path] = set()
    unique: list[DictionarySource] = []
    for source in sources:
        if source.path in seen:
            continue
        seen.add(source.path)
        unique.append(source)
    return Library(unique)


# --------------------------------------------------------------------- banner
def read_build_banner(text: str) -> tuple[int | None, dict[str, str]]:
    """Extract ``build`` / ``fw`` / ``protocol`` / ``reset_reason`` from the banner.

    Plain text scan on purpose: this is the one thing that must work *before* any
    dictionary is known.
    """
    fields: dict[str, str] = {}
    build: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("BOOT "):
            continue
        for token in stripped[5:].split():
            key, _, value = token.partition("=")
            if not key or not value:
                continue
            fields[key] = value
            if key == "build" and value.startswith("0x"):
                try:
                    build = int(value, 16)
                except ValueError:
                    build = None
        break
    return build, fields


def write_merged_csv(entries: Iterable[Entry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        for entry in entries:
            writer.writerow(entry.to_csv_row())


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--list", action="store_true", help="list known dictionaries")
    parser.add_argument("--select", metavar="BUILD_ID32", help="resolve a build id (hex ok)")
    parser.add_argument("--collisions", action="store_true", help="report ambiguous tokens")
    parser.add_argument("--merged-out", type=Path, help="write the merged library here")
    parser.add_argument(
        "--banner",
        help="read a build banner from this file (or '-' for stdin) and resolve it",
    )
    args = parser.parse_args()

    library = discover()

    if args.list or (not args.select and not args.collisions and not args.merged_out and not args.banner):
        print(f"已知字典 {len(library.sources)} 本：")
        for line in library.describe_available():
            print(f"  {line}")
        return 0

    if args.banner:
        text = (
            sys.stdin.read()
            if args.banner == "-"
            else Path(args.banner).read_bytes().decode("utf-8", "replace")
        )
        build, fields = read_build_banner(text)
        print(f"捕获里的构建身份：{fields or '（没有 BOOT 横幅）'}")
        if build is None:
            print("-> 拒绝选库：捕获里没有构建身份，猜字典会产出看似正确的结果", file=sys.stderr)
            return 2
        source = library.exact(build)
        if source is None:
            print(
                f"-> 拒绝选库：没有 0x{build:08x} 对应的字典。已知：\n  "
                + "\n  ".join(library.describe_available()),
                file=sys.stderr,
            )
            return 2
        print(f"-> 精确命中：{source.describe()}")
        return 0

    if args.select:
        try:
            build = int(args.select, 16)
        except ValueError:
            print(f"error: {args.select!r} 不是十六进制构建号", file=sys.stderr)
            return 2
        source = library.exact(build)
        if source is None:
            print(f"没有 0x{build:08x} 对应的字典", file=sys.stderr)
            return 2
        print(source.path)
        return 0

    merged_entries, ambiguous = library.merged()
    if args.collisions:
        print(f"合并后 {len(merged_entries)} 条；歧义 token {len(ambiguous)} 个")
        for token, group in sorted(ambiguous.items()):
            print(f"  0x{token:08x}:")
            for entry in group:
                print(f"    {entry.format_string!r}  <- {entry.source}")
        return 1 if ambiguous else 0

    if args.merged_out:
        write_merged_csv(merged_entries, args.merged_out)
        print(
            f"已写入 {args.merged_out}（{len(merged_entries)} 条）"
            + (f"，其中 {len(ambiguous)} 个 token 有歧义" if ambiguous else "")
        )
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
