"""接入契约的**机器检查**（ / F2，**core**）。

需求 §4/§7 要的不是"检查清单写在文档里"，而是**能红的判据**：

| 检查 | 挡的是什么 | 为什么它值得一条判据 |
| --- | --- | --- |
| 契约完整性 | 声明缺项、步骤没覆盖、能力四态没理由 | 声明少了不会报错，只会让下一个人猜 |
| 旁路厂商命令 | 步骤里的适配器不在登记表里 | 一旦有人直接写一条命令，Core 的中立就没了 |
| 判据重复 | 按步骤名挑判据 / 两步共用一套判据 | 同一件事两个来源，迟早分叉（） |
| 跨 profile 串用 | 两份固件共用产物 / 工程入口 / 适配器 ID | 换固件时字典、ELF 与证据会互相串 |
| Core 泄漏 | core 里出现平台知识 | 平台边界靠这张表强制，不靠自觉 |
| 开源边界 | 本机路径、串口名进了开源内容 | 这些东西一旦进仓库就撤不回来 |

⚠️ 本模块**只做判断**：文件由调用方读好交进来（路径、源码、声明）。这样它能在任何
机器上得出同一个结论，也能对着做坏的输入证明自己不是摆设。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable, Mapping

try:  # imported as part of the package
    from . import hwops_contract as contract_mod
    from . import platform_boundary as boundary_mod
except ImportError:  # imported as a top-level module
    import hwops_contract as contract_mod  # type: ignore[no-redef]
    import platform_boundary as boundary_mod  # type: ignore[no-redef]


# ============================================================ 1. 契约完整性
def declaration_problems(
    declaration: contract_mod.HardwareDeclaration,
    *,
    known_layers: Iterable[str] | None = None,
    strict_registration: bool = True,
) -> list[str]:
    """这份契约**站不站得住**（空 = 站得住）。

    ⚠️ `strict_registration=False` 是给**还没登记**的契约用的（脚手架刚生成出来的
    那一份就是这种状态）：层名与模块名仍然要合法，但"模块必须已经在分层表里登记"
    这一条这时不成立 —— 那是接入**做完**之后的动作，不是开始之前的前提。
    """
    problems = list(declaration.problems())
    missing = contract_mod.missing_steps(declaration)
    if missing:
        problems.append(
            "没有覆盖这些步骤：" + "、".join(missing)
            + "（声明它，或在 excluded 里写清为什么不覆盖）"
        )
    if not declaration.capabilities:
        problems.append(
            "没有声明任何能力：平台据此决定能不能做，一条都不声明等于什么都判不了"
        )
    for step in declaration.steps:
        if step.step == "verify" and declaration.identity_expectation is None:
            problems.append(
                "声明了 verify，却没有 identity_expectation："
                "那就没有可比对的字段，核验永远只能是「未核验」"
            )
    layers = tuple(known_layers) if known_layers is not None else boundary_mod.LAYERS
    if declaration.layer == boundary_mod.LAYER_CORE:
        problems.append(
            "接一条硬件不许把声明放进 core：它是目标/项目层的事"
            "（core 里出现平台知识就是平台边界破了）"
        )
    elif not strict_registration:
        # 未登记的契约（脚手架刚生成的那一份）：层名由**接入方自己**定，
        # 平台的分层表里这时候当然没有它 —— 那不是错，是接入还没做完。
        if not str(declaration.layer).strip():
            problems.append("没有声明层名（layer）")
    elif declaration.layer not in layers:
        problems.append(
            f"层名 {declaration.layer!r} 不在平台的分层表里（{list(layers)}）"
        )
    else:
        actual = boundary_mod.MODULE_LAYERS.get(declaration.module)
        if actual is None:
            problems.append(f"声明的模块 {declaration.module!r} 没有在分层表里登记")
        elif actual != declaration.layer:
            problems.append(
                f"声明的模块 {declaration.module!r} 在分层表里是 {actual!r}，"
                f"与契约写的 {declaration.layer!r} 不一致"
            )
    return problems


# =================================================== 2. 旁路厂商命令 / 登记表
def bypass_problems(
    declaration: contract_mod.HardwareDeclaration,
    registered_adapter_ids: Iterable[str],
) -> list[str]:
    """步骤里的适配器**必须**在目标的登记表里（`ADAPTERS`）。

    ⚠️ 这条挡的是"直接写一条命令"：登记表是"哪些命令被受控地暴露出来"的唯一清单，
    绕过它就等于 Core 里多了一条没人管得着的厂商调用。
    """
    known = {str(item) for item in registered_adapter_ids}
    if not known:
        return ["目标没有交进任何适配器登记（`ADAPTERS` 为空）：没法判有没有旁路"]
    problems: list[str] = []
    for step in declaration.steps:
        if step.adapter_id not in known:
            problems.append(
                f"{step.step} 用了未登记的适配器 {step.adapter_id!r}"
                f"（登记表里有的是 {sorted(known)}）——旁路厂商命令一律不接受"
            )
    return problems


# =================================================== 3. 判据的两个来源
#: 在**分支体里真的去选判据**的写法：`ok, note = <某个判据函数>(…)`。
#: ⚠️ 光有 `if step == "flash":` 不算 —— 那也可能是设一个标志位（很正当）。
#: 一条判据要红，必须同时看到"按步骤名分支"**与**"在那个分支里挑一个判据"。
VERDICT_IN_BRANCH = re.compile(
    r"(?:\bok\s*,\s*\w+\s*=|=\s*\w*(?:succeeded|verdict|judge|upload_verdict)\w*\s*\()"
)
BRANCH_WINDOW = 2


def criterion_source_problems(sources: Mapping[str, str]) -> list[str]:
    """按**步骤名**挑判据的写法（ 的原形）。

    `sources` 是"模块名 → 源码"。判据该由声明决定；一旦某个模块里出现
    `step == "build"` 这类分支、**而且那个分支里真的在挑判据**，同一件事就有两个来源了。
    注释与纯注释行不计（说明里引到老写法是正当的）。
    """
    branch = re.compile(contract_mod.STEP_NAME_BRANCH)
    problems: list[str] = []
    #: ⚠️ 同一份源码可能按**两个名字**登记（加载器给模块的内部名 + 文件名：声明里的
    #: `module` 字段写的是后者），那会让同一条问题报两遍 —— 文本一样就只扫一次。
    seen_texts: set[str] = set()
    for name, text in sorted(sources.items()):
        if text in seen_texts:
            continue
        seen_texts.add(text)
        lines = str(text).splitlines()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not branch.search(line):
                continue
            window = lines[index: index + 1 + BRANCH_WINDOW]
            if not any(VERDICT_IN_BRANCH.search(item) for item in window):
                continue
            problems.append(
                f"{name}:{index + 1} 在按步骤名的分支里挑判据（`{stripped}`）："
                "判据只能来自该步骤的声明（ 的原形）"
            )
    return problems


def duplicate_criteria_problems(
    declaration: contract_mod.HardwareDeclaration,
) -> list[str]:
    """同一个契约里两步共用**同一套**判据（等于没判）。

    ⚠️ 只看**带具体内容**的判据：裸的 `kind="exit"`（只看退出码）是通用形状，
    两步都用它并不表示抄 —— 而带标记/统计行的判据是**为那一步写的**，撞车就是信号。
    """
    seen: dict[str, str] = {}
    problems: list[str] = []
    for step in declaration.steps:
        criterion = step.success
        if criterion is None:
            continue
        specific = bool(
            criterion.success_markers
            or criterion.failure_markers
            or criterion.error_count_pattern
            or criterion.require_count
        )
        if not specific:
            continue
        key = json.dumps(criterion.to_json(), sort_keys=True, ensure_ascii=False)
        if key in seen:
            problems.append(
                f"{step.step} 与 {seen[key]} 用了完全相同的成功判据："
                "两步的形状不同，判据相同通常意味着有一处是抄的"
            )
        else:
            seen[key] = step.step
    return problems


# =================================================== 4. 跨 profile 不串用
def isolation_problems(
    declarations: Iterable[contract_mod.HardwareDeclaration],
) -> list[str]:
    """同一个目标的几份契约**不许互相串用**（需求 §7.3）。

    串用的后果不是报错，而是"换固件时拿到另一份的字典/ELF/身份"——那种错误没有
    下游判据能发现，所以必须在这里挡住。
    """
    rows = list(declarations)
    problems: list[str] = []
    ids: dict[str, str] = {}
    entries: dict[str, str] = {}
    artifacts: dict[str, str] = {}
    #: 适配器 ID → (哪份契约, 哪个目标)。⚠️ **同一个目标之下**两份固件共用同一条
    #: 目标适配器是**对的**（裸机与 RT-Thread 共用一套构建/烧录实现）；不许串的是
    #: **跨目标**复用，以及**产物/工程入口**。
    adapters: dict[str, tuple[str, str]] = {}
    for row in rows:
        owner = ids.get(row.adapter_id)
        if owner is not None:
            problems.append(f"{row.adapter_id} 被声明了两次（{owner} 与 {row.label}）")
        ids[row.adapter_id] = row.label
        if row.project_entry:
            owner = entries.get(row.project_entry)
            if owner is not None:
                problems.append(
                    f"{row.adapter_id} 与 {owner} 用同一个工程入口"
                    f"（{row.project_entry}）：两份固件不能是同一个工程"
                )
            entries[row.project_entry] = row.adapter_id
        for step in row.steps:
            for item in step.artifacts:
                owner = artifacts.get(item)
                if owner is not None and owner[0] == row.adapter_id:
                    # ⚠️ 同一条契约里两处声明同一个产物：以前写成“A 与 A 共用产物”，
                    #    读起来像打错（2026-10-13 验收拓到）。步骤名是现成的。
                    problems.append(
                        f"{row.adapter_id} 的 {owner[1]} 与 {step.step} 共用产物 {item}："
                        "同一条链里两个步骤认同一个文件，换步骤/换固件时会拿错"
                    )
                elif owner is not None:
                    problems.append(
                        f"{row.adapter_id} 与 {owner[0]} 共用产物 {item}："
                        "换固件时会拿错字典/ELF/镜像"
                    )
                artifacts[item] = (row.adapter_id, step.step)
            owner = adapters.get(step.adapter_id)
            if owner is not None and owner[1] != row.target_id:
                problems.append(
                    f"适配器 ID {step.adapter_id!r} 被两个**不同目标**的契约登记"
                    f"（{owner[0]}@{owner[1]} 与 {row.adapter_id}@{row.target_id}）："
                    "同一个 ID 只能属于一个目标"
                )
            adapters[step.adapter_id] = (row.adapter_id, row.target_id)
    return problems


# ================================ 4b. 台架键：声明了，但这一趟读不到
#: `configured` 的形状：适配器 ID → `{台架键: (读到没有, 从哪儿读的)}`。
BenchValues = Mapping[str, Mapping[str, tuple[bool, str]]]


def bench_key_problems(
    declaration: contract_mod.HardwareDeclaration,
    configured: Mapping[str, tuple[bool, str]] | None,
) -> list[str]:
    """声明了 `bench_keys`、而这一趟**读不到**它 ⇒ 说清楚。

    ⚠️ 存在的理由（2026-10-13 验收，新 Agent 撞到）：声明里填了键、装配处的
    `bench_values` 是空的 ⇒ **机器检查全绿**，而 `preflight` 直接以「台架未配置」
    拒绝开始，后面五步全停。接一条新硬件时这是最容易踩的坑：
    “字段填了”不等于“值接上了”。

    `configured is None` = 这一趟**没交台架值**（例：`check --module` 只有单份声明）⇒
    判不了，不当成问题（要不要报“判不了”由调用方决定）。
    """
    if configured is None:
        return []
    problems: list[str] = []
    for key in declaration.bench_keys:
        row = configured.get(str(key))
        if row is None:
            problems.append(
                f"声明了台架键 {key}，但这一趟的装配（`bench_values`）里没有它 ⇒ "
                "`preflight` 会以「台架未配置」拒绝开始：只声明不够，还要在装配处接上台架值"
            )
        elif not row[0]:
            problems.append(
                f"台架键 {key} 这一趟没配上（{row[1] or '没有这一格'}）⇒ "
                "`preflight` 会以「台架未配置」拒绝开始"
            )
    return problems


# =================================================== 5. Core 泄漏（复用边界表）
def core_leakage_problems() -> list[str]:
    """core 里出现**未登记**的平台知识 / 平台能力越界 / 编排里的工具与项目知识。

    ⚠️ 用"声明 vs 实测"（而不是裸实测）：已经登记并写清理由的命中不算问题
    —— 那些是已发布的证据键名，改掉反而破坏兼容。
    """
    problems = list(boundary_mod.token_hit_problems())
    problems.extend(boundary_mod.scope_problems())
    problems.extend(boundary_mod.orchestration_problems())
    return problems


# =================================================== 6. 开源边界
@dataclass(frozen=True)
class OpenSourcePattern:
    """一条"不该进开源内容"的形状 + 为什么。"""

    name: str
    pattern: str
    why: str


#: ⚠️ 这里只放**本机/硬件私有**的形状；产品名、工具名、芯片族**不在这里**
#: （它们出现在文档里是正当的事实说明）。
OPENSOURCE_PATTERNS: tuple[OpenSourcePattern, ...] = (
    OpenSourcePattern(
        "absolute_path", r"\b[A-Za-z]:[\\/]",
        "绝对路径是原开发机的目录布局：换台机器就不成立，也泄露了本机结构",
    ),
    OpenSourcePattern(
        "serial_port", r"\bCOM\d+\b",
        "串口名属于本机台架事实：板子插在哪个口随机器变，写进仓库就是假的",
    ),
    #: ⚠️ 这一条比上面那条**严**：它只认"赋值/默认值"的形状（等号后面跟一个带引号的
    #: 口名、或者 JSON 里那种键值对），不认句子里的举例（命令行开关、文档里的反例）。
    #: 有它才能把"文档在讲不要写死端口"与"真的写死了端口"分开 —— 前者满天飞是正常的。
    #:
    #: ⚠️ 模式**分两截拼**：整段写在这里的话，本文件自己的这几行就会命中它
    #: （自匹配），框架面自扫会莫名其妙变红。
    OpenSourcePattern(
        "port_default",
        r"""["']?""" + "port" + r"""["']?\s*[:=]\s*["']""" + "COM" + r"""\d+["']""",
        "把本机端口写成了**默认值**：换一台机器就是一条看着像真的错配置",
    ),
)

_OPENSOURCE_RE = tuple(
    (re.compile(item.pattern), item) for item in OPENSOURCE_PATTERNS
)


def opensource_problems(
    entries: Iterable[tuple[str, str]],
    *,
    allow: Iterable[tuple[str, str]] = (),
) -> list[str]:
    """扫一遍"要开源的内容"，把本机事实挑出来。

    `entries` 是 `(名字, 文本)`；`allow` 是**显式豁免**的 `(名字, 形状名)` 对 ——
    豁免必须点名到"哪一份内容的哪一种形状"，不能整份放过（否则下一次泄漏就没人看得见）。
    """
    allowed = {(str(name), str(kind)) for name, kind in allow}
    problems: list[str] = []
    for name, text in entries:
        for line_no, line in enumerate(str(text).splitlines(), start=1):
            for pattern, item in _OPENSOURCE_RE:
                if (str(name), item.name) in allowed:
                    continue
                if pattern.search(line):
                    problems.append(
                        f"{name}:{line_no} 出现 {item.name}（{line.strip()[:80]}）：{item.why}"
                    )
    return problems


# ============================================================ 汇总（一处）
@dataclass(frozen=True)
class CheckReport:
    """一次检查的全部结论（人读与机器读共用）。

    ⚠️ `unjudged` 与 `problems` 是**两回事**：
    - `problems`：判了，而且不过；
    - `unjudged`：**这一项没判**（缺输入，例：目标没交出受控适配器登记表）。
    两者分开的理由是实打实的：从前缺输入时是**静默跳过** ⇒ 屏幕上写"通过"，
    而旁路那一项其实一次都没跑（2026-10-13 验收时被新 Agent 抓到）。
    """

    declarations: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    unjudged: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> dict[str, Any]:
        return {
            "declarations": list(self.declarations),
            "ok": self.ok,
            "problems": list(self.problems),
            "unjudged": list(self.unjudged),
        }


def check_declarations(
    declarations: Iterable[contract_mod.HardwareDeclaration],
    *,
    registered: Mapping[str, Iterable[str]] | None = None,
    sources: Mapping[str, str] | None = None,
    bench: BenchValues | None = None,
    compare_isolation: bool = True,
    strict_registration: bool = True,
) -> CheckReport:
    """把上面几项一次跑完（命令行 `check` 与套件共用这一份）。

    `registered` 是"目标 ID → 它登记的适配器 ID"；`sources` 是"模块名 → 源码"
    （判"按步骤名挑判据"用）；`bench` 是"适配器 ID → 它的台架值读没读到"。
    三者都由调用方读好交进来；`bench=None` = 这一趟没交（例：`check --module`
    只有单份声明）⇒ 那一项不判、也不报“判不了”。
    `strict_registration=False` = 这份声明**还没登记**（脚手架生成物的常态）。
    """
    rows = list(declarations)
    problems: list[str] = []
    unjudged: list[str] = []
    names: list[str] = []
    table = {str(key): list(value) for key, value in dict(registered or {}).items()}
    for row in rows:
        names.append(row.adapter_id)
        problems.extend(
            f"{row.adapter_id}：{item}"
            for item in declaration_problems(row, strict_registration=strict_registration)
        )
        problems.extend(
            f"{row.adapter_id}：{item}" for item in duplicate_criteria_problems(row)
        )
        if bench is not None:
            problems.extend(
                f"{row.adapter_id}：{item}"
                for item in bench_key_problems(row, bench.get(row.adapter_id))
            )
        known = table.get(row.target_id)
        if known is not None:
            problems.extend(
                f"{row.adapter_id}：{item}" for item in bypass_problems(row, known)
            )
        else:
            # ⚠️ 没登记表就**判不了**旁路 —— 但要明说，不许静默跳过（否则屏幕上写着
            #    “通过”，而这一项一次都没跑；新 Agent 验收时正是这样被误导的）。
            unjudged.append(
                f"{row.adapter_id}：旁路检查没跑 —— 目标 {row.target_id} 没有"
                "受控适配器登记表（`registered` / 契约里的 `ADAPTERS`），"
                "判不了“步骤里的适配器在不在登记表里”"
            )
    if compare_isolation:
        # ⚠️ 不按“至少两份契约”设门槛：**同一份**契约里两处声明同一个产物也是串用，
        #    而且那正是 `check --module <单份外部模块>` 那条路上唯一做得了的串用检查。
        #    从前那道的 `len(rows) > 1` 门槛，让单份模块的串用检查**静默不跑**
        #    （2026-10-13 验收：新 Agent 撞到）。
        problems.extend(isolation_problems(rows))
    if sources:
        problems.extend(criterion_source_problems(sources))
    # ⚠️ 逐份判“有没有拿到它的源码”：用**全局**门槛（`if sources:`）会让“别的模块有源码”
    #    把“这一份没源码”掩盖成静默漏判（2026-10-13 第三轮验收拓到：平台自带模块永远
    #    提供源码 ⇒ 外部模块缺源码时既不判、也不报“判不了”，屏幕写“通过”）。
    provided = {str(key) for key in (sources or {})}
    for row in rows:
        if str(row.module) in provided:
            continue
        unjudged.append(
            f"{row.adapter_id}：「按步骤名挑判据」没跑 —— 没拿到模块 {row.module} 的源码"
            "（把模块放到 `tools/` 下、或用 `--targets` 给到 `.py` 的路径，"
            "命令行会自动读它的文件）"
        )
    return CheckReport(
        declarations=tuple(names), problems=tuple(problems), unjudged=tuple(unjudged)
    )
