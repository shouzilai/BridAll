""" / L8：**宿主用例**的通用执行器（纯模型，注入式）。

需求 §6.1 把跨平台基础用例定成 `system.identity_verify@v1`（展示名“硬件身份验证”）：它
**复用当前观察连接与既有身份查询**，至少展示三个阶段 ——
「发起身份查询 → 解析设备自报 → 与当前运行组合核对」。这条用例与 L5 那两条设备用例的
根本区别是：**三个阶段都是主机知道的**，设备侧一条新命令都没有（于是 ESP32 那份固件
也不必重烧：它本来就会回 `@PWINFO`，裸机那份本来就会回 `BOOT build=` 横幅）。

分工（需求 §6 那张表，谁的知识放谁那儿）：

| 谁 | 提供什么 |
| --- | --- |
| 这个模块（Core） | **通用流程**：发查询 → 等自报 → 判超时/协议错误 → 收口；三个阶段与结果形状 |
| 项目包 | **声明**（用例 id/版本/suite/requires/阶段）与**判据**（自报字段 ↔ 当前运行组合） |
| 观察会话 | 唯一写口 + **既有**身份解析（`ctx.identity()` 那份视图就是会话自己的状态） |
| 装配处 | `ctx.expected()`：当前组合的**声明**（设备可能自称的项目名、domain、本机构建产物） |

⚠️ 这里**一个平台名都没有**：谁发哪条查询、自报里哪个字段是什么、怎么算对，全由项目包
按下面这几个注入接口给（`IdentityVerifySpec`）。于是 "Web/Core 不出现平台分支" 是结构性的，
不是靠自觉。

⚠️ 三个阶段的状态推进仍然走 `step_lifecycle` + `live_test_batch`（**没有第二套状态机**）：
本模块只是"在合适的时候告诉它们该长什么样"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

try:
    from .embedded_test_model import TestCase
    from .step_lifecycle import TestStep
except ImportError:  # 直跑脚本时的退化路径（与同层其它模块一致）
    from embedded_test_model import TestCase  # type: ignore[no-redef]
    from step_lifecycle import TestStep  # type: ignore[no-redef]


#: 宿主用例的 `TestCase.kind`（模型里本来就有这一态：`CASE_KINDS`）。
HOST_CASE_KIND = "host_scenario"

#: 基础用例的 id / 版本 / 展示名。需求 §6.1 把这三样写死了 —— 三个项目包登记的是
#: **同一个 id 的同一条语义**（命令、字段与判据各自包内），页面上看起来才是同一条用例。
BASE_CASE_ID = "system.identity_verify"
BASE_CASE_VERSION = 1
BASE_CASE_LABEL = "硬件身份验证"

#: 三个阶段的 id。⚠️ 阶段 id 在整个页面里是唯一键（`stageBag().rows` 按它索引）——
#: 这三个名字只属于这一条用例，不会和存储那两条（`write` / `read` / …）撞上。
STAGE_QUERY = "identity.query"
STAGE_PARSE = "identity.parse"
STAGE_MATCH = "identity.match"

#: 一条执行允许的结论（与 `RESULT_STATUSES` 里这条用例用得上的那三个一致）。
HOST_RESULTS = ("PASS", "FAIL", "ERROR")


def describe_fields(fields: Mapping[str, Any], *, keys: Iterable[str] = ()) -> str:
    """自报字段 → 一句**给人看**的摘要（阶段卡上那一行说明）。

    只挑声明的那几个键（顺序 = 声明顺序），没有的就跳过 —— 不打印整份 JSON：
    证据里本来就有完整那一行，卡片上要的是"设备到底自称是谁"。
    """
    parts: list[str] = []
    for key in keys:
        value = fields.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            parts.append(f"{key}={'true' if value else 'false'}")
        elif isinstance(value, (str, int, float)):
            parts.append(f"{key}={value}")
    return " ".join(parts)


@dataclass(frozen=True)
class IdentityVerifySpec:
    """一个项目包对这条基础用例的声明（命令、字段与判据都在这里面）。

    * `suite` —— 登记进哪个套件（必须是本项目包 `suites` 里已有的一条：不另开套件）；
    * `requires` —— 能力要求（`cap.*` / `device.*`）：设备身份没认出来时这一条自然不可选
      （需求 §6.1 最后一条：不猜协议，透明原始串口连接照旧可用）；
    * `judge` —— **判据**：`(设备自报字段, 当前组合声明) -> (PASS|FAIL|ERROR, 说明)`；
    * `summary_keys` —— 阶段卡上那句摘要显示哪几个字段；
    * `timeout_s` —— 自报等待上限（到点 ⇒ 第二个阶段 ERROR，需求 §6.1：超时不得算成功）。
    """

    suite: str
    requires: tuple[str, ...]
    judge: Callable[[Mapping[str, Any], Mapping[str, Any]], tuple[str, str]]
    label: str = BASE_CASE_LABEL
    version: int = BASE_CASE_VERSION
    timeout_s: float = 3.0
    expected_duration_s: float = 1.0
    summary_keys: tuple[str, ...] = ("mcu", "project", "build_id")
    #: 这条用例要不要**等一串**自报（默认只要一条）。留给以后的多行自报（如任务表）。
    extra_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.suite or ""):
            raise ValueError("宿主用例必须声明 suite（登记进哪一条既有套件）")
        if not callable(self.judge):
            raise ValueError("宿主用例必须给判据（项目包提供）")
        if float(self.timeout_s) <= 0:
            raise ValueError("宿主用例的等待上限必须为正")


def identity_steps(spec: IdentityVerifySpec) -> tuple[TestStep, ...]:
    """三个阶段（顺序 = 页面铺开顺序；需求 §6.1 那三句）。"""
    wait = float(spec.timeout_s)
    return (
        TestStep(
            stage_id=STAGE_QUERY,
            label="发起身份查询",
            expected="用这条会话既有的身份查询命令问一次（同一个写口，不构建/不烧录/不复位）",
            timeout_s=wait,
        ),
        TestStep(
            stage_id=STAGE_PARSE,
            label="解析设备自报",
            expected=f"{wait:g} s 内收到设备自报并解析成字段（缺字段/超时/协议错误都不得算成功）",
            timeout_s=wait,
        ),
        TestStep(
            stage_id=STAGE_MATCH,
            label="与当前运行组合核对",
            expected="设备自报的芯片/项目/固件与构建标识必须与当前这一路组合一致",
            timeout_s=wait,
        ),
    )


def identity_case(spec: IdentityVerifySpec) -> TestCase:
    """这条基础用例的定义（三个项目包用同一个 id、同一套语义，判据各自包内）。"""
    return TestCase(
        case_id=BASE_CASE_ID,
        version=int(spec.version),
        label=str(spec.label),
        suite=str(spec.suite),
        kind=HOST_CASE_KIND,
        # 整条用例的超时：三个阶段各 `timeout_s`，再加收口余量。
        timeout_s=float(spec.timeout_s) * 3 + 2.0,
        status="active",
        requires=tuple(spec.requires),
        expected_duration_s=float(spec.expected_duration_s),
    )


@dataclass(frozen=True)
class HostCase:
    """项目包对一条宿主用例的执行声明（`protocol.host_case(case_id)` 返回它）。

    `runner()` 每次执行造一个**新的**运行器实例 —— 状态留在实例里，批与批之间不共享。
    """

    steps: tuple[TestStep, ...]
    runner: Callable[[], "IdentityVerifyRun"]


def _missing(fields: Mapping[str, Any], names: Iterable[str]) -> list[str]:
    """哪些声明过的字段**设备没说**（空串 / None 都算没说：需求 §6.2 不许为界面编值）。"""
    out: list[str] = []
    for name in names:
        value = fields.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            out.append(str(name))
    return out


def _domain_of(fields: Mapping[str, Any]) -> str:
    """自报里的 token domain（`protocols.domain` 一份；没有就是空串）。"""
    protocols = fields.get("protocols")
    if not isinstance(protocols, Mapping):
        return ""
    return str(protocols.get("domain") or "")


def judge_identity(
    fields: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    require: Iterable[str] = ("build_id", "software_version"),
    check_project: bool = True,
    check_domain: bool = True,
    chip_prefix: str = "",
    fw_prefix: str = "",
    build_key: str = "build_id",
) -> tuple[str, str]:
    """判据的**共同那一半**：缺字段 / 项目名 / domain / 芯片前缀 / fw 前缀 / 构建标识。

    项目包各自调它，再补自己那条专有的核对（共享的是"怎么问事实"，不是"这台设备该报什么"）。
    结论只有三种，且都是**确定性**的：

    * `ERROR` —— 缺字段（设备没说）、超时/协议错误、或**没得核对**（本机没有这一份固件的
      构建产物）⇒ 这是"证据不足"，不是"设备坏了"；
    * `FAIL` —— 核对上了、但对不上（自称别的项目 / 别的芯片 / 别的构建）；
    * `PASS` —— 上面每一条都逐项对上了。

    ⚠️ 顺序固定（缺字段 → 项目 → 域 → 芯片 → fw → 构建），因为理由要**可复现**：
    同一个现场每次点都得出同一句话。
    """
    missing = _missing(fields, require)
    if missing:
        return "ERROR", f"设备自报缺字段：{'、'.join(missing)}（宁缺不猜）"

    projects = tuple(
        str(name) for name in (expected.get("project_names") or expected.get("device_projects") or ())
    )
    reported_project = str(fields.get("project") or "")
    if check_project and projects and reported_project and reported_project not in projects:
        return "FAIL", (
            f"设备自称的项目是 {reported_project!r}，而当前这一路声明的是 {list(projects)}"
            "（包键或设备侧名字都算对上；都不是就是别的固件）"
        )

    declared_domain = str(expected.get("token_domain") or "")
    reported_domain = _domain_of(fields)
    if check_domain and declared_domain and reported_domain and reported_domain != declared_domain:
        return "FAIL", (
            f"设备自报的 domain 是 {reported_domain!r}，而当前这一路声明的是 {declared_domain!r}"
        )

    if chip_prefix:
        chip = str(fields.get("mcu") or "")
        if not chip:
            return "ERROR", "设备没有自报芯片型号（`mcu` 缺字段）"
        if not chip.upper().startswith(chip_prefix.upper()):
            return "FAIL", f"设备自报的芯片是 {chip!r}，与这一路声明的 {chip_prefix!r} 不符"

    if fw_prefix:
        fw = str(fields.get("software_version") or "")
        if not fw.upper().startswith(fw_prefix.upper()):
            return "FAIL", f"设备自报的固件是 {fw!r}，与这一路声明的 {fw_prefix!r} 不符"

    build = str(fields.get(build_key) or "")
    known = expected.get("reported_build_known")
    if known is None:
        return "ERROR", "本机没有这一份固件的构建产物：构建标识没得核对（不猜）"
    if known is False:
        return "FAIL", (
            f"设备自报的构建标识 {build} 不在本机这一份固件的构建产物里"
            "（板上跑的不是这里构建出来的那一份）"
        )
    return "PASS", f"芯片/项目/固件与构建标识都与当前这一路组合一致（{build}）"


class IdentityVerifyRun:
    """`system.identity_verify@v1` 的执行（语义三个包一致，判据由 `spec` 给）。

    由链路（`live_test_link`）在**执行开始时**造一个实例，然后按事件驱动它：
    `begin()`（开跑）→ 每一行设备输出 `line()` → 读取循环每圈 `tick()`（判超时）。

    ⚠️ 它**不认识串口**：发命令走 `ctx.probe()`（会话的唯一写口），拿自报走 `ctx.identity()`
    （会话既有的身份状态机），推进阶段与收口走 `ctx.stage()` / `ctx.done()`
    （`step_lifecycle` + `live_test_batch`，不另建一套）。
    """

    def __init__(self, spec: IdentityVerifySpec) -> None:
        self.spec = spec
        self._deadline = 0.0
        self._done = False
        self._query = ""
        self._bad_line = ""       # 看起来是自报、却没解出来的那一行（协议错误的现场）

    # ------------------------------------------------------------- 事件
    def begin(self, ctx: Any) -> None:
        """开跑：**重新**问一次身份（会话会先把上一次的结论作废），并把命令写出去。

        ⚠️ 两个阶段的**基线都在发命令之前**开（`ctx.stage` 的 `first_line`）：
        “等自报”那一段的区间必须从发命令之前开始 —— 否则设备秒回时那一行落在基线之前，
        这一段就变成“没有定位信息”（真机验收当场撞到：同一份代码两次跑，一次有坐标一次没有）。
        “发起查询”那一段同样先开，而它的证据是**我们自己写出去的那一行**（在“工具输出”
        里，`source=live_test`）—— 那一行不在任何文件行号空间里，它的身份是那条 io 事件的
        **事件号**：写口把它随答案一起返回（`sent["seq"]`），这里就把那一个号声明成坐标
        （`{"source": "tool", "event_seq": N}`，）。写口没给（老服务 / 写失败）就
        不声明 —— 那一步如实显示“没有坐标”，而不是指到别处去。
        """
        ctx.stage(STAGE_QUERY, "running", "正在发出身份查询")
        ctx.stage(STAGE_PARSE, "running", f"等设备自报（{self.spec.timeout_s:g} s 超时）")
        sent = dict(ctx.probe() or {})
        # ⚠️ 命令由 `probe()` 自己走会话那条**唯一**写口发出去（并记进这一批的命令清单）——
        #    这里不能再 `send()` 一遍：真机验收量到设备收到了两条一模一样的查询。
        self._query = str(sent.get("display") or sent.get("query_text") or "")
        self._deadline = float(ctx.now()) + float(self.spec.timeout_s)
        ctx.stage(STAGE_QUERY, "PASS",
                  f"已发出 {self._query}" if self._query else "已发出身份查询",
                  event_seq=sent.get("seq"))

    def line(self, text: str, ctx: Any) -> None:
        """收到一行设备输出：可能是自报，也可能是普通日志（只有认出来的才算数）。"""
        if self._done:
            return
        state = dict(ctx.identity() or {})
        self._note_bad_line(text, state)
        if state.get("ok"):
            self._finish(dict(state.get("fields") or {}), ctx)
            return
        if str(state.get("state") or "") == "unsupported":
            self._abort(ctx, "设备回答不认识这条查询命令："
                              f"{state.get('detail') or state.get('query_text') or ''}")

    def tick(self, ctx: Any) -> None:
        """读取循环每跑一圈问一次：自报到了就收，到点还没到就判 ERROR（安静时也要判）。"""
        if self._done:
            return
        state = dict(ctx.identity() or {})
        if state.get("ok"):
            self._finish(dict(state.get("fields") or {}), ctx)
            return
        if str(state.get("state") or "") == "unsupported":
            self._abort(ctx, "设备回答不认识这条查询命令："
                              f"{state.get('detail') or state.get('query_text') or ''}")
            return
        if float(ctx.now()) >= self._deadline:
            detail = f"{self.spec.timeout_s:g} s 内没有等到设备自报"
            if self._bad_line:
                detail += f"（看到过一条像自报的行却解析不出来：{self._bad_line[:80]}）"
            elif state.get("detail"):
                detail += f"（{state.get('detail')}）"
            self._abort(ctx, detail, stages=(STAGE_PARSE,))

    # ------------------------------------------------------------- 内部
    def _note_bad_line(self, text: str, state: Mapping[str, Any]) -> None:
        """一条"看着像自报"却没能解析的行 ⇒ 记下来（超时时它就是协议错误的现场）。"""
        prefix = str(state.get("prefix") or "")
        line = str(text or "").strip()
        if not prefix or not line.startswith(prefix) or state.get("ok"):
            return
        self._bad_line = line

    def _abort(self, ctx: Any, detail: str, *, stages: tuple[str, ...] = ()) -> None:
        """没拿到可用的自报 ⇒ 第二个阶段 ERROR，整条 ERROR（需求 §6.1：超时/协议错误不算成功）。"""
        stage_id = stages[0] if stages else STAGE_PARSE
        note = "" if stage_id == STAGE_PARSE else detail
        ctx.stage(stage_id, "ERROR", note or detail)
        self._done = True
        ctx.done("ERROR", detail)

    def _finish(self, fields: Mapping[str, Any], ctx: Any) -> None:
        """拿到了自报：先落第二个阶段，再让判据把第三个阶段与整条结论定下来。"""
        summary = describe_fields(fields, keys=self.spec.summary_keys)
        ctx.stage(STAGE_PARSE, "PASS", f"设备自报：{summary}" if summary else "设备自报已解析")
        # 核对这一步的证据就是**刚刚读到的那一行自报**：开这一阶段时把当行当它的第一行
        # 证据（`first_line=True`），卡片上的“定位”因此指向判据真正读到的那一行。
        ctx.stage(STAGE_MATCH, "running", "核对当前这一路组合", first_line=True)
        expected = dict(ctx.expected() or {})
        build = fields.get("build_id")
        if build is not None:
            expected["reported_build_known"] = ctx.build_known(build)
        try:
            verdict, detail = self.spec.judge(dict(fields), expected)
        except Exception as error:  # 判据自己坏了 = ERROR，绝不能因此判 PASS
            verdict, detail = "ERROR", f"判据执行失败：{type(error).__name__}: {error}"
        status = str(verdict or "ERROR").upper()
        if status not in HOST_RESULTS:
            status, detail = "ERROR", f"判据给出的结论不合法：{verdict!r}"
        ctx.stage(STAGE_MATCH, status, str(detail or ""))
        self._done = True
        ctx.done(status, "" if status == "PASS" else str(detail or ""))


__all__ = [
    "BASE_CASE_ID", "BASE_CASE_LABEL", "BASE_CASE_VERSION", "HOST_CASE_KIND", "HOST_RESULTS",
    "HostCase", "IdentityVerifyRun", "IdentityVerifySpec", "STAGE_MATCH", "STAGE_PARSE",
    "STAGE_QUERY", "describe_fields", "identity_case", "identity_steps", "judge_identity",
]
