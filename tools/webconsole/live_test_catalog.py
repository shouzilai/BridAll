""" / L1b：**这条观察会话上**的用例目录与启动前二次校验（纯模型）。

需求 §3 要的是"服务端按事实算目录 + 启动瞬间再校验一次"：

```text
当前观察 session_id 与设备身份
+ firmware profile / build ID / capabilities
+ 对应 Project Package 的 active test_catalog
+ 台架能力与安全策略
-> 可执行用例、不可执行用例及确定性原因
```

这个模块只做那两件**判断**，一行 I/O 都没有（不开串口、不读文件、不看时钟、
不认识任何目标或项目包）：

* `live_test_catalog()` —— 逐用例给出"能不能跑 + 为什么不能"（确定性一句话）；
* `binding_for()` / `verify_start()` —— 把一个用例的选择冻结成**绑定**，
  启动时再拿当时的事实对一遍；任何一项对不上都拒绝并写明是哪一项变了。

⚠️ 这里刻意**不**发明事实：能力只有两个来源 —— 目标的能力声明
（`target_profile.Capabilities`，封闭名单）与设备自己的能力自报
（`protocols`，"只有明确的正数才算支持"，判定复用 `device_sniff` 里那一份实现）。
设备没报 ⇒ 结论是"没得核对"，**不是**"支持"。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

try:
    from .device_sniff import _protocol_supported
    from .target_profile import Capability, CAPABILITY_NAMES, Capabilities
except ImportError:  # 直跑脚本时的退化路径（与同层其它模块一致）
    from device_sniff import _protocol_supported  # type: ignore[no-redef]
    from target_profile import Capability, CAPABILITY_NAMES, Capabilities  # type: ignore[no-redef]


class LiveTestError(ValueError):
    """用例声明或调用方式的问题（**拒绝**，不是"跳过"）。"""


#: `TestCase.requires` 的两种命名空间。
#:
#: * `cap.<能力名>` —— 目标能力声明里的一项（名单是**封闭**的，拼错当场拒绝）；
#: * `device.<协议名>` —— 设备自报 `protocols` 里的一项（只有明确的正数才算支持）。
#:
#: ⚠️ 为什么要有前缀：同一个词（例如 `decode`）在两个来源里含义不同 ——
#: 目标说"我这版平台能解码"与设备说"我这版固件打开了 token 日志"是两件事。
#: 不加前缀就只能猜一个来源，而"猜"正是需求 §6 禁止 Web/Core 做的事。
CAP_PREFIX = "cap."
DEVICE_PREFIX = "device."
REQUIRE_PREFIXES = (CAP_PREFIX, DEVICE_PREFIX)

#: 能力要求不满足时**理由那一句**开头的固定措词（`reason` 里带着具体是哪个能力键）。
REQUIREMENT_REASON_PREFIX = "能力要求未满足："
#: 屏幕上给使用者的那句话（用户口径 2026-10-13）。
#:
#: 「目标没声明 `cap.storage_encryption`」「设备没报 `device.live_test`」这些在屏幕上
#: 统一说成一句人话；具体卡在哪一条只在 `reason` / `requires` 里（API、悬停、证据、开发者详情）。
UNSUPPORTED_CASE_TEXT = "当前设备不支持此用例"


def parse_requirement(name: str) -> tuple[str, str]:
    """`requires` 的一项 ⇒ `(命名空间, 名字)`；不认识的一律拒绝。

    ⚠️ 不认识的**能力名**也拒绝（不是悄悄当成 unknown）：`Capabilities` 立的就是这条规矩
    （"拼错的名字不该悄悄变成 unknown"），目录这一层照办 —— 否则
    `cap.decodee` 会永远显示"目标没声明"，而那是拼写错误，不是事实。
    """
    text = str(name or "")
    if text.startswith(CAP_PREFIX):
        capability = text[len(CAP_PREFIX):]
        if capability not in CAPABILITY_NAMES:
            raise LiveTestError(
                f"用例能力要求 {text!r} 里的能力名 {capability!r} 不认识："
                f"可用的是 {list(CAPABILITY_NAMES)}"
            )
        return "cap", capability
    if text.startswith(DEVICE_PREFIX):
        protocol = text[len(DEVICE_PREFIX):]
        if not protocol or not all(ch.isalnum() or ch == "_" for ch in protocol):
            raise LiveTestError(f"用例能力要求 {text!r} 里的协议名 {protocol!r} 非法")
        return "device", protocol
    raise LiveTestError(
        f"用例能力要求 {text!r} 缺少来源前缀：只能是 {list(REQUIRE_PREFIXES)}"
        " 之一（'目标的能力'与'设备自报的能力'是两件事，不能猜）"
    )


def _canonical(value: Any) -> Any:
    """把一份事实折成**逐字节可复现**的 JSON 形状（摘要只许建在它上面）。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {key: _canonical(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def digest_of(value: Any) -> str:
    """一份事实的摘要（排序键 + 紧凑分隔符 + UTF-8）—— 判据可以直接对拍它。"""
    blob = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def require_facts(facts: Any) -> "SessionFacts":
    """确认拿到的是"本次观察会话的事实"。

    ⚠️ 用**鸭子类型**而不是 `isinstance`：同一个文件可能被两条导入路径当成两个模块
    （`webconsole.live_test_catalog` 与裸 `live_test_catalog`，见 ）——
    那时候 `isinstance` 会把真事实判成假的。要求的是"拿得出这两样必需事实"，
    而不是"这个类的对象实例化于哪个模块"。
    """
    if facts is None or not hasattr(facts, "identity_digest") or not hasattr(facts, "protocols"):
        raise LiveTestError("需要本次观察会话的事实（身份与能力自报都在里面）")
    return facts  # type: ignore[no-any-return]


@dataclass(frozen=True)
class SessionFacts:
    """这次观察会话**此刻**的事实（由装配处从会话快照里取，本模块不自己去问）。"""

    session_id: str
    device_key: str
    firmware_id: str
    project_package_id: str
    #: 设备自报的身份字段（`@PWINFO` / 横幅回退那一份；没拿到就是空）。
    identity: Mapping[str, Any] = field(default_factory=dict)
    #: 设备自报的能力表（`identity["protocols"]` 那一份；**空 = 没得核对**）。
    protocols: Mapping[str, Any] = field(default_factory=dict)
    #: 会话是不是还开着（结束/断开后不能开始新的受控执行）。
    open: bool = True

    def __post_init__(self) -> None:
        for name in ("session_id", "device_key", "firmware_id", "project_package_id"):
            if not str(getattr(self, name) or ""):
                raise LiveTestError(f"会话事实缺少 {name}")
        if not isinstance(self.protocols, Mapping):
            raise LiveTestError("protocols 必须是映射（设备自报的那一份）")

    @property
    def identity_digest(self) -> str:
        """"身份与固件都没变"的可比对形式（身份字段 + 能力自报一起入摘要）。"""
        return digest_of({"identity": dict(self.identity), "protocols": dict(self.protocols)})

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "device_key": self.device_key,
            "firmware_id": self.firmware_id,
            "project_package_id": self.project_package_id,
            "identity_digest": self.identity_digest,
            "protocols_reported": bool(self.protocols),
            "open": bool(self.open),
        }


@dataclass(frozen=True)
class RequirementCheck:
    """一条 `requires` 的判定结果（`detail` 就是给人看的那句确定性理由）。"""

    name: str
    satisfied: bool
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "satisfied": bool(self.satisfied), "detail": self.detail}


def requirement_checks(
    requires: Iterable[str], *, capabilities: Capabilities, protocols: Mapping[str, Any]
) -> tuple[RequirementCheck, ...]:
    """逐条判定能力要求（顺序就是声明顺序，判据可逐字对拍）。"""
    table = protocols if isinstance(protocols, Mapping) else {}
    checks: list[RequirementCheck] = []
    for name in requires:
        space, bare = parse_requirement(name)
        if space == "cap":
            declared = capabilities.get(bare)
            if declared == Capability.SUPPORTED:
                checks.append(RequirementCheck(name, True, f"目标声明支持能力 {bare}"))
            elif declared == Capability.UNSUPPORTED:
                checks.append(RequirementCheck(
                    name, False, f"目标声明能力 {bare} 不可用（这一版没有它）"))
            else:
                checks.append(RequirementCheck(
                    name, False, f"目标没有声明能力 {bare}（没提 ≠ 支持）"))
            continue
        # device.<协议>：设备自己说的话才算数，而且是"只有正数才算支持"。
        if not table:
            checks.append(RequirementCheck(
                name, False, f"这次会话没有拿到设备的能力自报，{bare} 没得核对（不猜）"))
        elif bare not in table:
            checks.append(RequirementCheck(
                name, False, f"设备自报里没有 {bare}（没提 ≠ 支持）"))
        elif not _protocol_supported(table.get(bare)):
            checks.append(RequirementCheck(
                name, False, f"设备明确报了 {bare}=0（这一份构建关掉了它）"))
        else:
            checks.append(RequirementCheck(name, True, f"设备自报支持 {bare}"))
    return tuple(checks)


def _first_missing(checks: Iterable[RequirementCheck], requires: Iterable[str]) -> str:
    """"为什么不能跑"取**第一条**不满足的要求 —— 顺序固定，理由才可复现。"""
    for name in requires:
        for check in checks:
            if check.name == name and not check.satisfied:
                return f"{REQUIREMENT_REASON_PREFIX}{check.detail}"
    return ""


@dataclass(frozen=True)
class CaseVerdict:
    """一条用例在这条会话上的结论（需求 §3 的"可执行 + 不可执行及确定性原因"）。"""

    case_id: str
    version: int
    suite: str
    label: str
    lifecycle: str
    selectable: bool
    reason: str
    requires: tuple[RequirementCheck, ...] = ()
    #: 需求 §8.2：选择面板要显示名称、**预计耗时**、安全影响与不可选原因。
    #: 两个都是**用例定义**的事实，目录照拄一遍（页面不许自己估一个数）。
    expected_duration_s: float | None = None
    destructive: bool = False

    @property
    def identity(self) -> tuple[str, int]:
        return self.case_id, self.version

    @property
    def screen_reason(self) -> str:
        """屏幕上给使用者的那一句（内部术语只留在 `reason` 里）。

        用户口径（2026-10-13）：」能力不够、设备没报、目标没声明“这些事在屏幕上统一说成
        「当前设备不支持此用例」；哪个能力键不满足、卡在哪一条，全在 `reason`（与 `requires`
        逐条判定）里 —— 那是 API / 证据 / 开发者详情该看的东西。
        其他不可执行的原因（生命周期）本来就是人话，照拄服务端那句。
        """
        if self.reason.startswith(REQUIREMENT_REASON_PREFIX):
            return UNSUPPORTED_CASE_TEXT
        return self.reason

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "version": self.version,
            "suite": self.suite,
            "label": self.label,
            "lifecycle": self.lifecycle,
            "selectable": bool(self.selectable),
            # 空串 = 可以执行；不可执行时这一句是**服务端**说的，页面照拄（）。
            "reason": self.reason,
            # 屏幕上那一句（用户口径 2026-10-13）：能力类的原因统一收成「当前设备不支持此用例」，
            # 页面屏幕上拿它，`reason` 进悬停与证据。
            "screen_reason": self.screen_reason,
            "expected_duration_s": self.expected_duration_s,
            "destructive": bool(self.destructive),
            "requires": [check.to_json() for check in self.requires],
        }


def live_test_catalog(
    cases: Iterable[Any],
    *,
    facts: SessionFacts,
    capabilities: Capabilities,
    suites: Iterable[str] | None = None,
) -> tuple[CaseVerdict, ...]:
    """一条观察会话上的用例目录（顺序 = 项目包里的声明顺序，判据可逐字对拍）。

    规则只有三条，顺序固定：

    1. **范围**：只列这次固件/项目包声明的套件里的用例（`suites`）——
       别的套件的用例不出现（这是"目录由服务端按会话算"的一半）；
    2. **生命周期**：`TestCase.not_selectable_reason`（draft / 弃用 / 退役 / 禁用）
       说了就先按它拒 —— 它比能力理由更具体；
    3. **能力要求**：逐条判定，第一条不满足的那句就是理由。

    ⚠️ 会话是否还开着、设备对不对得上、项目包匹不匹配**不在这里判** ——
    那是"这次执行能不能开始"（`verify_start`），不是"目录里有什么"。
    """
    require_facts(facts)
    scope = None if suites is None else {str(name) for name in suites}
    verdicts: list[CaseVerdict] = []
    for case in cases:
        if scope is not None and str(getattr(case, "suite", "")) not in scope:
            continue
        checks = requirement_checks(
            getattr(case, "requires", ()) or (), capabilities=capabilities,
            protocols=facts.protocols,
        )
        reason = ""
        if not bool(getattr(case, "selectable", True)):
            reason = str(getattr(case, "not_selectable_reason", "") or "定义不允许新执行")
        else:
            reason = _first_missing(checks, getattr(case, "requires", ()) or ())
        verdicts.append(CaseVerdict(
            case_id=str(case.case_id), version=int(case.version), suite=str(case.suite),
            label=str(case.label), lifecycle=str(getattr(case, "lifecycle", "active")),
            selectable=not reason, reason=reason, requires=checks,
            expected_duration_s=getattr(case, "expected_duration_s", None),
            destructive=bool(getattr(case, "destructive", False)),
        ))
    return tuple(verdicts)


@dataclass(frozen=True)
class LiveTestBinding:
    """一次用例选择的**冻结绑定**（需求 §3：启动请求至少绑定这几项）。"""

    session_id: str
    device_key: str
    firmware_id: str
    project_package_id: str
    identity_digest: str
    case_id: str
    version: int
    parameters_digest: str

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "device_key": self.device_key,
            "firmware_id": self.firmware_id,
            "project_package_id": self.project_package_id,
            "identity_digest": self.identity_digest,
            "case_id": self.case_id,
            "version": self.version,
            "parameters_digest": self.parameters_digest,
        }


def binding_for(
    facts: SessionFacts, case: Any, parameters: Mapping[str, Any] | None = None
) -> LiveTestBinding:
    """把"这条会话上的这个用例、这组参数"冻结成一个绑定。"""
    require_facts(facts)
    return LiveTestBinding(
        session_id=facts.session_id,
        device_key=facts.device_key,
        firmware_id=facts.firmware_id,
        project_package_id=facts.project_package_id,
        identity_digest=facts.identity_digest,
        case_id=str(case.case_id),
        version=int(case.version),
        # ⚠️ 参数进摘要时用**归一化**形状（`FileInput` 这类对象折成字段），
        #    于是"同一个文件的同一份内容"两次得到同一个摘要。
        parameters_digest=digest_of(dict(parameters or {})),
    )


@dataclass(frozen=True)
class StartVerdict:
    """启动瞬间的结论：允许 / 拒绝 + **机器可判的码** + 给人看的一句话。"""

    allowed: bool
    code: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"allowed": bool(self.allowed), "code": self.code, "reason": self.reason}


#: 拒绝码（判据按它对拍；新增只增不改）。
START_CODES = (
    "session_changed",       # 绑定的是一个已经不在的观察会话
    "session_closed",        # 这条会话已经结束
    "device_changed",        # 会话现在拿着别的设备
    "firmware_changed",      # 固件换了
    "project_changed",       # 项目包换了
    "identity_changed",      # 设备身份/能力自报变了（重枚举、换板、重烧）
    "case_missing",          # 用例定义已经不在了
    "version_changed",       # 用例版本变了
    "case_not_selectable",   # 生命周期不许新执行
    "requires_unsatisfied",  # 能力要求不满足（含"没给能力事实"）
    "parameters_changed",    # 参数与绑定时不一致
    "batch_running",         # 同一会话已经有一个受控批次在跑
    "ok",
)


def verify_start(
    binding: LiveTestBinding,
    *,
    facts: SessionFacts | None,
    case: Any | None = None,
    capabilities: Capabilities | None = None,
    parameters: Mapping[str, Any] | None = None,
    active_batch: Any = None,
) -> StartVerdict:
    """启动瞬间的**二次校验**（需求 §3 的那张清单，顺序也照它）。

    任何一项对不上都拒绝，并写明**是哪一项**变了 —— 页面照抄这句话，
    不自己猜"大概是设备换了"。
    """
    if facts is None or not hasattr(facts, "identity_digest"):
        return StartVerdict(False, "session_closed", "这次观察会话已经不在了：不能开始受控测试")
    if facts.session_id != binding.session_id:
        return StartVerdict(
            False, "session_changed",
            f"绑定的是观察会话 {binding.session_id}，当前会话是 {facts.session_id}",
        )
    if not facts.open:
        return StartVerdict(
            False, "session_closed",
            f"观察会话 {facts.session_id} 已经结束：要重跑就重新连一次再选",
        )
    if facts.device_key != binding.device_key:
        return StartVerdict(
            False, "device_changed",
            f"这条会话现在拿着设备 {facts.device_key}，绑定时是 {binding.device_key}",
        )
    if facts.firmware_id != binding.firmware_id:
        return StartVerdict(
            False, "firmware_changed",
            f"会话的固件是 {facts.firmware_id}，绑定时是 {binding.firmware_id}",
        )
    if facts.project_package_id != binding.project_package_id:
        return StartVerdict(
            False, "project_changed",
            f"会话的项目包是 {facts.project_package_id}，绑定时是 {binding.project_package_id}",
        )
    if facts.identity_digest != binding.identity_digest:
        return StartVerdict(
            False, "identity_changed",
            "设备身份在这期间变了（重新枚举 / 换了板子 / 又被烧过）："
            "用例目录要按新身份重算",
        )
    if case is None:
        return StartVerdict(
            False, "case_missing",
            f"用例 {binding.case_id}@{binding.version} 现在不在项目包的目录里了",
        )
    if int(getattr(case, "version", 0)) != binding.version:
        return StartVerdict(
            False, "version_changed",
            f"用例 {binding.case_id} 的版本已经变成 {getattr(case, 'version', None)}，"
            f"绑定时是 {binding.version}",
        )
    if not bool(getattr(case, "selectable", True)):
        return StartVerdict(
            False, "case_not_selectable",
            str(getattr(case, "not_selectable_reason", "") or "这套用例现在不允许开始新执行"),
        )
    requires = tuple(getattr(case, "requires", ()) or ())
    if requires:
        if capabilities is None:
            # ⚠️ 没给能力事实**不能**当成"满足"（"没得核对 ≠ 通过"）。
            return StartVerdict(
                False, "requires_unsatisfied",
                f"用例 {binding.case_id} 声明了能力要求 {list(requires)}，"
                "而这次校验没有拿到能力事实：不能证明它满足",
            )
        checks = requirement_checks(requires, capabilities=capabilities,
                                    protocols=facts.protocols)
        reason = _first_missing(checks, requires)
        if reason:
            return StartVerdict(False, "requires_unsatisfied", reason)
    if digest_of(dict(parameters or {})) != binding.parameters_digest:
        return StartVerdict(
            False, "parameters_changed",
            f"提交的参数与绑定 {binding.case_id}@{binding.version} 的那一组不一样",
        )
    if active_batch:
        return StartVerdict(
            False, "batch_running",
            f"这条观察会话已经有一个受控测试批次在跑（{active_batch}）："
            "一次只允许一个",
        )
    return StartVerdict(True, "ok", "")


__all__ = [
    "CAP_PREFIX", "DEVICE_PREFIX", "REQUIRE_PREFIXES", "START_CODES",
    "CaseVerdict", "LiveTestBinding", "LiveTestError", "RequirementCheck",
    "SessionFacts", "StartVerdict", "binding_for", "digest_of",
    "live_test_catalog", "parse_requirement", "require_facts", "requirement_checks",
    "verify_start",
]
