"""Read-only target/firmware/suite projection for the Web selector."""
from __future__ import annotations
import base64
import binascii
from dataclasses import dataclass
import hashlib
from typing import Any, Iterable

try:
    from . import device_sniff as sniff_mod
    from .embedded_test_model import FileInput, TestRegion, TestSelection
    from .live_test_catalog import binding_for, live_test_catalog
except ImportError:
    import device_sniff as sniff_mod  # type: ignore[no-redef]
    from embedded_test_model import FileInput, TestRegion, TestSelection  # type: ignore[no-redef]
    from live_test_catalog import binding_for, live_test_catalog  # type: ignore[no-redef]

class CatalogError(ValueError):
    pass

@dataclass(frozen=True)
class Selection:
    target_id: str
    device_key: str
    firmware_id: str
    suite: str
    project: str
    requires_flash_confirmation: bool = False

class WebCatalog:
    """Project assembly facts into JSON; never execute a target callback."""
    def __init__(self, targets: Iterable[Any], projects: Iterable[Any], firmwares: Iterable[Any] = (),
                 *, flash_confirmation_firmwares: Iterable[str] = (),
                 device_projects: Iterable[tuple[str, Iterable[str]]] = ()):
        self.targets = {str(row.target_id): row for row in targets}
        self.projects = {str(row.key): row for row in projects if getattr(row, "key", "")}
        #: `target_id -> 设备侧自称的项目名`（由装配处从插件束里拿 —— 那是目标层的事实）。
        # ⚠️ 没有它，**由目标声明的那几行固件**（`implicit`）就不知道设备会自称什么，
        #    于是“身份能对上哪一条链”这个问题永远只能答“零匹配”（实测踩到）。
        device_projects_by_target: dict[str, tuple[str, ...]] = {}
        for target_id, names in device_projects:
            device_projects_by_target[str(target_id)] = tuple(
                str(name) for name in names if str(name or "").strip()
            )
        confirmations = {str(value) for value in flash_confirmation_firmwares}
        self.firmwares: dict[str, dict[str, Any]] = {}
        for row in firmwares:
            if not getattr(row, "implemented", False):
                continue
            self._add({"firmware_id": str(row.firmware_id), "target_id": str(row.target_id),
                       "label": str(row.label), "project": str(row.project),
                       "stack": str(row.stack), "implicit": False,
                       # ：**设备侧自称的项目名**（profile 的
                       # `identity_hint`）。裸机固件回的是 `"project":"stm32bm"`，
                       # 而本机项目包键是 `baremetal` —— 兼容性判定要查这条声明。
                       "device_projects": tuple(
                           name for name in (str(getattr(row, "identity_hint", "") or ""),)
                           if name and name != str(row.project)
                       ),
                       "requires_flash_confirmation": str(row.firmware_id) in confirmations,
                       "test_regions": [region.to_json() for region in row.test_regions]})
        covered = {row["target_id"] for row in self.firmwares.values()}
        for target in self.targets.values():
            project = str(getattr(target, "project", "") or "")
            if target.target_id not in covered and project:
                self._add({"firmware_id": f"{target.target_id}/default",
                           "target_id": str(target.target_id),
                           # ：固件条目的名字只描述**固件特性**。
                           # 优先用目标声明的 `firmware_label`；没声明就退到"平台 · 项目包"。
                           # ⚠️ 不拿 `target.label` 顶 —— 那可能是台架名义（“板 A”）。
                           "label": str(
                               getattr(target, "firmware_label", "")
                               or f"{target.platform} · {project}"
                           ),
                           "project": project,
                           "stack": "declared-by-target", "implicit": True,
                           "requires_flash_confirmation": False,
                           # 设备侧自称的项目名（身份匹配要查它；装配处从插件束里给）。
                           "device_projects": device_projects_by_target.get(
                               str(target.target_id), ()
                           ),
                           # ⚠️ 这一支以前**漏了这个键**：`/api/run` 那条路无条件去取
                           #    `firmwares[<id>]["test_regions"]` ⇒ 点“启动自动验证”时服务端
                           #    直接 KeyError、连接被关，页面只看到 “Failed to fetch”
                           #    （真机验收量到的：板 A 就属于这一支）。
                           #    “没声明测试区域”就是**零个**区域（`FirmwareProfile.test_regions`
                           #    的默认值也是空元组），不是“这个键不存在”。
                           "test_regions": []})

    def _add(self, row: dict[str, Any]) -> None:
        key = row["firmware_id"]
        if key in self.firmwares:
            raise CatalogError(f"firmware_id 重复：{key!r}")
        if row["target_id"] not in self.targets:
            raise CatalogError(f"固件 {key!r} 指向未登记目标 {row['target_id']!r}")
        self.firmwares[key] = row

    def selection(self, target_id: str, firmware_id: str, suite: str) -> Selection:
        target = self.targets.get(str(target_id or ""))
        if target is None:
            raise CatalogError(f"未登记的 Web 目标 {target_id!r}")
        firmware = self.firmwares.get(str(firmware_id or ""))
        if firmware is None or firmware["target_id"] != target.target_id:
            raise CatalogError(f"固件 {firmware_id!r} 不属于目标 {target.target_id!r}")
        project = self.projects.get(firmware["project"])
        allowed = tuple(name for name in (getattr(target, "suites", ()) or ())
                        if name in (getattr(project, "suites", ()) or ()))
        if suite not in allowed:
            raise CatalogError(f"套件 {suite!r} 不属于目标/固件组合；可用的是 {allowed}")
        if not bool(getattr(target, "pipeline_allowed", False)):
            raise CatalogError(f"目标 {target.label} 的策略不允许自动验证")
        return Selection(target.target_id, str(target.device_key or ""), firmware_id,
                         suite, firmware["project"],
                         bool(firmware.get("requires_flash_confirmation")))

    # ---------------------------------------------- ：兼容性（服务端说）
    def requirement_for(self, firmware_id: str) -> "sniff_mod.FirmwareRequirement":
        """一条固件 + 它所属目标的**声明** ⇒ 兼容性需求（组装只有这一处）。"""
        key = str(firmware_id or "")
        row = self.firmwares.get(key)
        if row is None:
            raise CatalogError(f"未登记的固件 {key!r}")
        target = self.targets.get(str(row.get("target_id")))
        # ：**同一个 target 上其它固件**的自称名（连同它们的项目包键）。
        # 设备自报落在这一组里 = 板上跑的是同板另一份固件 ⇒ 允许覆盖（逐次确认），
        # 而不是把一次合法的“换固件”判成“这块板不对”。
        # 注意：同项目多构建的情形（ESP32 那两份）会把本项目的名字也算进来，
        # 但判定里用 `reported != requirement.project` 挡住了自己。
        siblings = tuple(sorted({
            name
            for other_id, other in self.firmwares.items()
            if other_id != key and str(other.get("target_id")) == str(row.get("target_id"))
            for name in (*(other.get("device_projects") or ()), str(other.get("project") or ""))
            if name
        }))
        return sniff_mod.FirmwareRequirement(
            firmware_id=key,
            label=str(row.get("label") or key),
            project=str(row.get("project") or ""),
            # ：设备侧自称的项目名（声明在 profile 的 `identity_hint`）。
            device_projects=tuple(row.get("device_projects") or ()),
            # ：同板其它固件 —— 设备自报命中这一组就允许覆盖。
            sibling_projects=siblings,
            required_protocols=tuple(getattr(target, "required_protocols", ()) or ()),
            absent_protocols=tuple(getattr(target, "absent_protocols", ()) or ()),
            first_flash_allowed=bool(getattr(target, "first_flash_allowed", False)),
            #  / D3：通信口与烧录通路分家的目标要求一条**显式**台架绑定。
            requires_flasher_binding=bool(
                getattr(target, "requires_flasher_binding", False)
            ),
            requires_flash=bool(row.get("requires_flash_confirmation")),
        )

    def compatible_firmwares(
        self, card: Any, *, flasher_bound: bool = True, only_runnable: bool = True
    ) -> tuple[dict[str, Any], ...]:
        """给一张设备卡片算出**逐份固件**能不能用 + 理由（页面只展示结论）。

        ⚠️ 这是"哪份固件配这个设备"的**唯一**判断处：页面不许自己按名字或按端口推。
        `only_runnable=True` 跳过策略上不允许跑的目标（那些在页面上本来就选不动）。
        顺序固定（允许的在前，再按 firmware_id），判据可以逐字对拍。
        """
        rows: list[dict[str, Any]] = []
        for key, row in self.firmwares.items():
            target = self.targets.get(str(row.get("target_id")))
            if only_runnable and not bool(getattr(target, "pipeline_allowed", False)):
                continue
            verdict = sniff_mod.compatibility(
                card, self.requirement_for(key), flasher_bound=flasher_bound
            )
            entry = verdict.to_json()
            entry["target_id"] = str(row.get("target_id") or "")
            entry["project"] = str(row.get("project") or "")
            rows.append(entry)
        rows.sort(key=lambda item: (not item["allowed"], item["firmware_id"]))
        return tuple(rows)

    # ------------------------------------------  / D9：身份 ⇒ 运行组合
    def match_firmware(self, card: Any, *, flasher_bound: bool = True) -> Any:
        """这次实测身份 ⇒ **唯一的运行组合**（需求 §9.1，服务端唯一判定）。

        与 `compatible_firmwares()` 的差别（两个问题，不要混）：

        - `compatible_firmwares()` 回答"**这一份能不能用**"：烧录门那一族，允许
          "覆盖同板另一份固件"（`switches_sibling`）这种**开发**路径；
        - `match_firmware()` 回答"**设备此刻跑的是哪一份**"：观察会话、`observe.firmware_id`、
          解码能力与 `live_test_catalog` 都按它装配 —— 所以只认**真命中**
          （不是首次烧录方案、也不是覆盖兄弟固件），数不出唯一的那一份就如实说零/多匹配。

        ⚠️ 判据全在 core 的 `sniff_mod.match_firmware()`；这里只负责把声明凑齐，
        而且与 `compatible_firmwares()` **同一套筛选**（只算策略上允许跑的目标、
        `flasher_bound` 同一处来源）—— 两处不一致就会出现"卡片说有 1 份可用、
        组合却说零匹配"这种自相矛盾的屏幕。
        """
        requirements = [
            self.requirement_for(key)
            for key, row in sorted(self.firmwares.items())
            # ⚠️ **不按 `pipeline_allowed` 过滤**：那一条是"允不允许构建/烧录"的**策略**，
            #    而这里回答的是"设备此刻跑的是哪一份"（观察组合）。只读的适配链
            #    （随仓库发布的那两条就是这样）照样要能被身份命中 —— 否则
            #    "先当串口终端用、再逐步适配"这条阶梯永远走不到"能力升级"那一步，
            #    用户看到的是"明明接入的板子自报了一个在册的项目名，却说零匹配"。
            #    `compatible_firmwares()` 那边保持原样：它管的是"能不能跑 run"。
        ]
        return sniff_mod.match_firmware(card, tuple(requirements), flasher_bound=flasher_bound)

    # ---------------------------------------  / L1b：会话上的用例目录
    def _live_test_project(self, firmware_id: str, facts: Any) -> Any:
        """会话事实 × 固件条目 ⇒ 项目包；**不匹配就拒绝**（这就是 A/B 隔离那一半）。"""
        key = str(firmware_id or "")
        row = self.firmwares.get(key)
        if row is None:
            raise CatalogError(f"未登记的固件 {key!r}")
        project_key = str(row.get("project") or "")
        if str(getattr(facts, "project_package_id", "")) != project_key:
            raise CatalogError(
                f"这次观察会话的项目包是 {getattr(facts, 'project_package_id', '')!r}，"
                f"而固件 {key!r} 属于 {project_key!r}：用例目录按会话那条链算，不能借用别人的定义"
            )
        project = self.projects.get(project_key)
        if project is None:
            raise CatalogError(f"固件 {key!r} 的项目包 {project_key!r} 没有登记")
        return project

    def live_test_catalog(self, firmware_id: str, facts: Any) -> tuple[Any, ...]:
        """这条观察会话上的用例目录（逐用例给出"能不能跑 + 为什么不能"）。

        事实只有三样，都是由**声明**交进来的：会话事实（设备身份与能力自报）、
        目标的能力声明、项目包里的用例定义。判断全在核心模型 `live_test_catalog`
        里 —— 这里只负责把三样凑齐（装配只有这一处）。
        """
        project = self._live_test_project(firmware_id, facts)
        target = self.targets.get(str(self.firmwares[str(firmware_id)]["target_id"]))
        capabilities = getattr(target, "capabilities", None)
        if capabilities is None:
            raise CatalogError(f"目标 {getattr(target, 'target_id', None)!r} 没有能力声明")
        return live_test_catalog(
            project.test_catalog.cases,
            facts=facts,
            capabilities=capabilities,
            suites=tuple(getattr(project, "suites", ()) or ()),
        )

    def live_test_binding(
        self, firmware_id: str, facts: Any, case_id: str, version: int,
        parameters: Any = None,
    ) -> Any:
        """把一个用例的选择冻结成绑定（启动时拿它对表）。"""
        project = self._live_test_project(firmware_id, facts)
        for case in project.test_catalog.cases:
            if case.identity == (str(case_id), int(version)):
                return binding_for(facts, case, parameters)
        raise CatalogError(f"用例 {case_id}@{version} 不在项目包 {project.key!r} 的目录里")

    def prepare_tests(
        self, selection: Selection, submitted: Any
    ) -> tuple[dict[str, Any], ...]:
        """Turn an untrusted browser request into project-owned run snapshots."""
        if submitted is None:
            return ()
        if not isinstance(submitted, list):
            raise CatalogError("tests 必须是数组")
        project = self.projects[selection.project]
        declared_cases = {case.identity: case for case in project.test_catalog.cases}
        rows: list[TestSelection] = []
        for item in submitted:
            if not isinstance(item, dict):
                raise CatalogError("tests 中每一项必须是对象")
            allowed = {"case_id", "version", "parameters", "confirm_destructive"}
            unknown = sorted(set(item) - allowed)
            if unknown:
                raise CatalogError(f"测试选择含服务端不接受的字段：{unknown}")
            parameters = item.get("parameters", {})
            if not isinstance(parameters, dict):
                raise CatalogError("测试 parameters 必须是对象")
            try:
                identity = (str(item.get("case_id", "")), int(item.get("version", 0)))
                declared = declared_cases.get(identity)
                converted = dict(parameters)
                if declared is not None:
                    for key, spec in declared.parameters.items():
                        value = converted.get(key)
                        if spec.kind == "file" and isinstance(value, dict):
                            converted[key] = FileInput(
                                str(value.get("name", "")), int(value.get("size", -1)),
                                str(value.get("sha256", "")),
                            )
                rows.append(TestSelection(
                    case_id=identity[0], version=identity[1], parameters=converted,
                    confirm_destructive=item.get("confirm_destructive") is True,
                ))
            except (TypeError, ValueError) as error:
                raise CatalogError(f"测试选择格式错误：{error}") from None
        # ⚠️ 一个测试用例都没提交 ⇒ **没有要准备的东西**：直接返回空。
        #    否则下面会拿"项目目录里声明的用例"去校验固件区域，一次**普通 run**（不跑用例）
        #    会因为"固件没声明某个区域"被拒 —— 真机验收量到的
        #    `CatalogError: 固件未声明安全策略所需区域 'external.w25q16.autotest'`：
        #    点"启动自动验证"直接失败，页面只看到 "Failed to fetch"（）。
        if not rows:
            return ()
        try:
            prepared = project.test_catalog.prepare(
                rows,
                suites=project.suites,
                firmware_regions=tuple(
                    TestRegion(str(row["id"]), int(row["start"]), int(row["length"]))
                    for row in self.firmwares[selection.firmware_id]["test_regions"]
                ),
            )
        except ValueError as error:
            raise CatalogError(str(error)) from None
        outside_suite = [row["case"]["id"] for row in prepared
                         if row["case"]["suite"] != selection.suite]
        if outside_suite:
            raise CatalogError(
                f"测试用例不属于所选套件 {selection.suite!r}：{outside_suite}"
            )
        return tuple(prepared)

    def prepare_test_inputs(
        self, selection: Selection, submitted: Any
    ) -> tuple[tuple[dict[str, Any], ...], dict[str, bytes]]:
        """Validate uploaded bytes while keeping them out of the manifest snapshot."""
        prepared = self.prepare_tests(selection, submitted)
        payloads: dict[str, bytes] = {}
        for request, snapshot in zip(submitted or (), prepared):
            parameters = request.get("parameters") or {}
            for key, spec in (snapshot["case"].get("parameters") or {}).items():
                if spec.get("type") != "file":
                    continue
                value = parameters.get(key)
                encoded = value.get("content_base64") if isinstance(value, dict) else None
                if not isinstance(encoded, str):
                    raise CatalogError(f"文件参数 {key!r} 缺少本轮上传内容")
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    raise CatalogError(f"文件参数 {key!r} 的 base64 非法") from None
                frozen = snapshot["parameters"][key]
                if (len(data) != int(frozen["size"])
                        or hashlib.sha256(data).hexdigest() != str(frozen["sha256"])):
                    raise CatalogError(f"文件参数 {key!r} 的内容与大小/SHA-256 声明不一致")
                payloads[f"{snapshot['case']['id']}@{snapshot['case']['version']}:{key}"] = data
        return prepared, payloads

    def to_json(self) -> dict[str, Any]:
        targets = []
        for row in self.targets.values():
            item = row.to_json()
            item["firmware_ids"] = [fw["firmware_id"] for fw in self.firmwares.values()
                                    if fw["target_id"] == row.target_id]
            targets.append(item)
        runnable = [row for row in targets if row.get("pipeline_allowed") and row["firmware_ids"]]
        target_id = runnable[0]["target_id"] if runnable else ""
        firmware_id = runnable[0]["firmware_ids"][0] if runnable else ""
        firmware = self.firmwares.get(firmware_id, {})
        project = self.projects.get(str(firmware.get("project", "")))
        suites = list(getattr(project, "suites", ()) or ()) if project else []
        return {"schema": 1, "targets": targets, "firmwares": list(self.firmwares.values()),
                "projects": {key: value.to_json() for key, value in self.projects.items()},
                "defaults": {"target_id": target_id, "firmware_id": firmware_id,
                             "suite": suites[0] if suites else ""}}

__all__ = ["CatalogError", "Selection", "WebCatalog"]
