"""Declarative embedded-test contracts ( / A0).

This module is deliberately execution-free.  It validates project-owned test
catalogs against firmware-owned memory regions and produces immutable run
snapshots.  Runners, transports, device protocols, and evidence writers are
later slices and must consume these validated snapshots instead of reopening
the declarations themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable, Mapping


CASE_KINDS = ("device", "host_scenario")
PARAMETER_KINDS = ("boolean", "integer", "enum", "file")
RESULT_STATUSES = ("PASS", "FAIL", "ERROR", "SKIPPED", "CANCELLED")
#  §2：定义的生命周期四态。`disabled` 不是声明态，是**旧定义**
# `enabled=false` 的折算值（只为让"为什么不能执行"有一句确定的话）。
CASE_STATUSES = ("draft", "active", "deprecated", "retired")
# 不能执行时给客户端的**确定性原因**：同一状态永远同一句话（目录与启动校验共用一处）。
CASE_STATUS_REASONS = {
    "draft": "还在 draft：不进入普通可选目录",
    "deprecated": "已弃用：历史仍可回放，默认不供新执行",
    "retired": "已退役：不再创建新执行",
    "disabled": "已禁用：不能开始新的执行",
}
_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _require_id(value: str, label: str) -> None:
    if not _ID.fullmatch(value):
        raise ValueError(f"{label} {value!r} 不是稳定 ID（需小写分段名）")


@dataclass(frozen=True)
class TestRegion:
    """A firmware-owned reserved writable region."""

    region_id: str
    start: int
    length: int

    def __post_init__(self) -> None:
        _require_id(self.region_id, "region_id")
        if self.start < 0 or self.length <= 0:
            raise ValueError("测试区域 start 必须非负且 length 必须大于 0")

    @property
    def end(self) -> int:
        return self.start + self.length

    def to_json(self) -> dict[str, Any]:
        return {"id": self.region_id, "start": self.start, "length": self.length}


@dataclass(frozen=True)
class SafetyPolicy:
    """Project limits which must fit inside a firmware-declared region."""

    policy_id: str
    region: TestRegion
    max_input_bytes: int
    max_chunk_bytes: int
    max_retries: int
    max_total_bytes: int
    max_readback_bytes: int

    def __post_init__(self) -> None:
        _require_id(self.policy_id, "safety_policy")
        values = (
            self.max_input_bytes,
            self.max_chunk_bytes,
            self.max_total_bytes,
            self.max_readback_bytes,
        )
        if any(value <= 0 for value in values) or self.max_retries < 0:
            raise ValueError("安全策略的字节上限必须大于 0，重试上限不能为负")
        if self.max_chunk_bytes > self.max_total_bytes:
            raise ValueError("max_chunk_bytes 不能大于 max_total_bytes")

    def validate_firmware_regions(self, regions: Iterable[TestRegion]) -> None:
        firmware = {item.region_id: item for item in regions}
        declared = firmware.get(self.region.region_id)
        if declared is None:
            raise ValueError(f"固件未声明安全策略所需区域 {self.region.region_id!r}")
        if self.region.start < declared.start or self.region.end > declared.end:
            raise ValueError(
                f"项目安全区域 {self.region.region_id!r} 超出固件声明范围："
                f"project=[0x{self.region.start:x},0x{self.region.end:x}) "
                f"firmware=[0x{declared.start:x},0x{declared.end:x})"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.policy_id,
            "region": self.region.to_json(),
            "limits": {
                "input_bytes": self.max_input_bytes,
                "chunk_bytes": self.max_chunk_bytes,
                "retries": self.max_retries,
                "total_bytes": self.max_total_bytes,
                "readback_bytes": self.max_readback_bytes,
            },
        }


@dataclass(frozen=True)
class FileInput:
    """Metadata for a file already staged inside a run input directory."""

    name: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or "\\" in self.name:
            raise ValueError("输入文件 name 必须是单个文件名")
        if self.size < 0 or not _SHA256.fullmatch(self.sha256):
            raise ValueError("输入文件必须带非负 size 与小写 SHA-256")

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class ParameterSpec:
    kind: str
    required: bool = False
    default: Any = None
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in PARAMETER_KINDS:
            raise ValueError(f"不支持的测试参数类型 {self.kind!r}")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("参数 minimum 不能大于 maximum")
        if self.kind == "enum" and not self.choices:
            raise ValueError("enum 参数必须声明 choices")
        if self.kind == "file" and self.default is not None:
            raise ValueError("file 参数不能声明默认文件，必须按 run 暂存并记录摘要")
        if self.default is not None:
            self.validate(self.default, "default")

    def validate(self, value: Any, key: str) -> Any:
        if self.kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"测试参数 {key!r} 必须是 boolean")
        elif self.kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"测试参数 {key!r} 必须是 integer")
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"测试参数 {key!r} 小于 minimum={self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"测试参数 {key!r} 大于 maximum={self.maximum}")
        elif self.kind == "enum":
            if not isinstance(value, str) or value not in self.choices:
                raise ValueError(f"测试参数 {key!r} 必须是 {list(self.choices)} 之一")
        elif not isinstance(value, FileInput):
            raise ValueError(f"测试参数 {key!r} 必须是已暂存的 FileInput")
        return value

    def to_json(self) -> dict[str, Any]:
        row = {"type": self.kind, "required": self.required}
        if self.default is not None:
            row["default"] = self.default
        if self.minimum is not None:
            row["minimum"] = self.minimum
        if self.maximum is not None:
            row["maximum"] = self.maximum
        if self.choices:
            row["choices"] = list(self.choices)
        return row


@dataclass(frozen=True)
class TestCase:
    case_id: str
    version: int
    label: str
    suite: str
    kind: str
    timeout_s: float
    parameters: Mapping[str, ParameterSpec] = field(default_factory=dict)
    default_enabled: bool = False
    destructive: bool = False
    safety_policy: str = ""
    enabled: bool = True
    deprecated: bool = False
    #  §2：定义的生命周期、能力要求与预计时长。
    # ⚠️ 三个都是**只增不改**的字段（ 需求 §5 的"新字段只增不改"）：
    #    默认值让旧定义逐字保持今天的行为；`status` 声明了就以它为准。
    status: str = ""
    requires: tuple[str, ...] = ()
    expected_duration_s: float | None = None

    def __post_init__(self) -> None:
        _require_id(self.case_id, "case_id")
        if self.version <= 0 or not self.label or not self.suite or self.timeout_s <= 0:
            raise ValueError("测试用例必须有正版本、名称、suite 与正超时")
        if self.kind not in CASE_KINDS:
            raise ValueError(f"不支持的测试用例类型 {self.kind!r}")
        if self.destructive and not self.safety_policy:
            raise ValueError("destructive=true 的用例必须声明 safety_policy")
        if not self.destructive and self.safety_policy:
            raise ValueError("非破坏性用例不能借用 safety_policy")
        for key in self.parameters:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
                raise ValueError(f"测试参数名 {key!r} 非法")
        if self.status and self.status not in CASE_STATUSES:
            raise ValueError(f"不支持的用例状态 {self.status!r}")
        # 一个事实只有一处：声明了 active 就不能同时带着"禁用/弃用"的旧布尔。
        if self.status == "active" and (not self.enabled or self.deprecated):
            raise ValueError("状态 active 与 enabled=false / deprecated=true 矛盾")
        for name in self.requires:
            if not re.fullmatch(r"[a-z][a-z0-9_.]*", str(name)):
                raise ValueError(f"用例能力要求 {name!r} 非法")
        if len(set(self.requires)) != len(self.requires):
            raise ValueError("用例能力要求不能重复")
        if self.expected_duration_s is not None and self.expected_duration_s <= 0:
            raise ValueError("预计时长必须为正")

    @property
    def identity(self) -> tuple[str, int]:
        return self.case_id, self.version

    @property
    def lifecycle(self) -> str:
        """这一份定义**现在**处在哪个状态（需求 §2 的四态 + 旧定义的禁用）。

        ⚠️ `status` 是**声明**：声明了就照它；没声明时才按旧布尔折算 —— 旧定义里
        `enabled=false` / `deprecated=true` 的语义就是"不能开始新 run"，这里不替它
        升级成四态里的某一个（那会把"暂时禁用"说成"退役"）。
        """
        if self.status:
            return self.status
        if not self.enabled:
            return "disabled"
        if self.deprecated:
            return "deprecated"
        return "active"

    @property
    def selectable(self) -> bool:
        """能不能开始一次**新**执行（唯一判定处：L1 的目录与启动校验都问它）。"""
        return self.lifecycle == "active"

    @property
    def not_selectable_reason(self) -> str:
        """为什么不能开始新执行（确定性一句话，空串 = 可以）。"""
        state = self.lifecycle
        return "" if state == "active" else CASE_STATUS_REASONS.get(state, f"状态 {state} 不可执行")

    def validate_parameters(self, values: Mapping[str, Any] | None) -> dict[str, Any]:
        supplied = dict(values or {})
        unknown = sorted(set(supplied) - set(self.parameters))
        if unknown:
            raise ValueError(f"用例 {self.case_id!r} 收到未声明参数：{unknown}")
        result: dict[str, Any] = {}
        for key, spec in self.parameters.items():
            if key in supplied:
                value = supplied[key]
            elif spec.default is not None:
                value = spec.default
            elif spec.required:
                raise ValueError(f"用例 {self.case_id!r} 缺少必填参数 {key!r}")
            else:
                continue
            result[key] = spec.validate(value, key)
        return result

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.case_id, "version": self.version, "label": self.label,
            "suite": self.suite, "kind": self.kind, "timeout_s": self.timeout_s,
            "default_enabled": self.default_enabled, "destructive": self.destructive,
            "safety_policy": self.safety_policy or None, "enabled": self.enabled,
            "deprecated": self.deprecated,
            # 新增键（旧键一个都没改）：声明状态、折算后的生命周期、能力要求、预计时长，
            # 以及**为什么不能执行**（需求 §3：目录要带“不可执行用例及确定性原因”）。
            "status": self.status or None,
            "lifecycle": self.lifecycle,
            "requires": list(self.requires),
            "expected_duration_s": self.expected_duration_s,
            "not_selectable_reason": self.not_selectable_reason or None,
            "parameters": {key: spec.to_json() for key, spec in self.parameters.items()},
        }


@dataclass(frozen=True)
class TestSelection:
    case_id: str
    version: int
    parameters: Mapping[str, Any] = field(default_factory=dict)
    confirm_destructive: bool = False


@dataclass(frozen=True)
class TestCatalog:
    cases: tuple[TestCase, ...] = ()
    safety_policies: tuple[SafetyPolicy, ...] = ()

    def __post_init__(self) -> None:
        identities = [case.identity for case in self.cases]
        if len(identities) != len(set(identities)):
            raise ValueError("test catalog 中有重复的 case_id + version")
        policy_ids = [policy.policy_id for policy in self.safety_policies]
        if len(policy_ids) != len(set(policy_ids)):
            raise ValueError("test catalog 中有重复的 safety_policy")

    def validate(self, suites: Iterable[str], firmware_regions: Iterable[TestRegion]) -> None:
        suites = set(suites)
        policies = {policy.policy_id: policy for policy in self.safety_policies}
        for policy in policies.values():
            policy.validate_firmware_regions(firmware_regions)
        for case in self.cases:
            if case.suite not in suites:
                raise ValueError(f"用例 {case.case_id!r} 属于未登记 suite {case.suite!r}")
            if case.safety_policy and case.safety_policy not in policies:
                raise ValueError(f"用例 {case.case_id!r} 引用未知安全策略 {case.safety_policy!r}")

    def prepare(
        self, selections: Iterable[TestSelection], *, suites: Iterable[str],
        firmware_regions: Iterable[TestRegion],
    ) -> list[dict[str, Any]]:
        regions = tuple(firmware_regions)
        self.validate(suites, regions)
        cases = {case.identity: case for case in self.cases}
        policies = {policy.policy_id: policy for policy in self.safety_policies}
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for selection in selections:
            identity = selection.case_id, selection.version
            if identity in seen:
                raise ValueError(f"同一用例选择了两次：{identity}")
            seen.add(identity)
            case = cases.get(identity)
            if case is None:
                raise ValueError(f"未登记的测试用例或版本：{identity}")
            problem = case.not_selectable_reason
            if problem:
                raise ValueError(f"测试用例 {identity} {problem}，不能开始新 run")
            if case.destructive and not selection.confirm_destructive:
                raise ValueError(f"破坏性用例 {identity} 缺少本轮明确确认")
            parameters = case.validate_parameters(selection.parameters)
            policy = policies.get(case.safety_policy)
            files = [value for value in parameters.values() if isinstance(value, FileInput)]
            if policy and any(item.size > policy.max_input_bytes for item in files):
                raise ValueError(f"用例 {identity} 的输入文件超过安全策略上限")
            rows.append({
                "case": case.to_json(),
                "parameters": {
                    key: value.to_json() if isinstance(value, FileInput) else value
                    for key, value in parameters.items()
                },
                "safety": policy.to_json() if policy else None,
                "confirmed_destructive": bool(case.destructive and selection.confirm_destructive),
            })
        return rows

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "cases": [case.to_json() for case in self.cases],
            "safety_policies": [policy.to_json() for policy in self.safety_policies],
        }


@dataclass(frozen=True)
class TestResult:
    case_id: str
    version: int
    status: str
    duration_ms: int
    reason: str = ""
    device_evidence: Mapping[str, Any] = field(default_factory=dict)
    host_evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id(self.case_id, "case_id")
        if self.version <= 0 or self.status not in RESULT_STATUSES or self.duration_ms < 0:
            raise ValueError("测试结果的 version/status/duration_ms 非法")
        if self.status != "PASS" and not self.reason:
            raise ValueError("非 PASS 结果必须带 reason")

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id, "version": self.version, "status": self.status,
            "duration_ms": self.duration_ms, "reason": self.reason,
            "device_evidence": dict(self.device_evidence),
            "host_evidence": dict(self.host_evidence),
        }


EMPTY_TEST_CATALOG = TestCatalog()


__all__ = [
    "CASE_KINDS", "CASE_STATUSES", "CASE_STATUS_REASONS", "EMPTY_TEST_CATALOG",
    "FileInput", "PARAMETER_KINDS", "ParameterSpec", "RESULT_STATUSES", "SafetyPolicy",
    "TestCase", "TestCatalog", "TestRegion", "TestResult", "TestSelection",
]
