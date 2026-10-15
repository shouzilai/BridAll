"""**一份装配**：把一个 firmware 的整条链组装起来，并核验各条链一一对应（**core**，W4c）。

W4b 之后服务能同时登记三份固件（ESP32 示例项目、STM32 裸机、STM32 RT-Thread）。
"能选"与"选对了"是两件事：页面上一次选择会同时经过 **项目包 / 套件 / Pipeline /
证据目录 / 平台自述 / 观察会话** 六处，其中任何一处对不上，用户看到的就是
"选了 A、跑的却是 B"这种最难查的错误。所以这里把那条链**写成一个记录**，
并且能自己把"一一对应"逐条核出来：

```text
Assembly（一个 firmware 一条）
  ├─ firmware_id / target_id / device_key   ← 页面 POST 的那三个 id
  ├─ project_key / suites                   ← 项目包与它带来的套件
  ├─ pipeline                               ← 构建/烧录/采集/断言的那一条
  ├─ session                                ← 它的实时观察会话（自己的登记与字典）
  └─ profile / plugins                      ← 自述与适配器的出处
```

`WebAssembly.problems()` 是**冻结用的判据**（W4c 的产出）：它把 catalog、runner registry、
观察会话与平台自述四边的说法互相核对，任何一处对不上就返回一句中文理由。
宿主测试直接拿它当断言，于是"多目标"这件事不是靠一次人工验收成立的。

⚠️ 这里**不 import 任何目标/项目模块**：谁被登记进来由装配处（composition root）决定，
core 只负责把它们装到一起、并检查它们说的是不是同一件事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

try:  # 包内 import
    from . import platform_info, runner_registry, web_catalog
    from . import bench_config
    from .observe_center import ObserveCenter, ObserveTarget
except ImportError:  # 直接跑脚本/测试时的回退
    import platform_info  # type: ignore[no-redef]
    import bench_config  # type: ignore[no-redef]
    import web_catalog  # type: ignore[no-redef]
    from observe_center import ObserveCenter, ObserveTarget  # type: ignore[no-redef]


@dataclass(frozen=True)
class Assembly:
    """**一个 firmware 的完整链条**（装配处交进来，core 只读）。

    `pipeline` / `session` / `plugins` / `profile` 都是**别人造好的对象**，
    这里只持有引用 —— 所以这个模块既不知道 ESP32，也不知道 IAR。
    """

    firmware_id: str
    label: str
    target_id: str
    device_key: str
    project_key: str
    platform: str
    suites: tuple[str, ...]
    pipeline: Any
    session: Any
    plugins: Any
    profile: Any = None
    project: Any = None
    pipeline_allowed: bool = True
    requires_flash_confirmation: bool = False
    token_domain: str = ""
    notes: tuple[str, ...] = ()
    #: 这一路的**目标能力表**（自述/观察行的出处；`None` = 没说）。
    capability_table: Any = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ 查询
    @property
    def reset_default(self) -> bool:
        """自动验证**默认**要不要复位（来自这个目标的 `ResetPolicy`，不是平台偏好）。"""
        policy = getattr(self.profile, "reset", None)
        return bool(getattr(policy, "default_request", False))

    @property
    def reset_default_reason(self) -> str:
        """为什么是这个默认值（页面在确认对话框里要说清，不能只说"关着"）。"""
        policy = getattr(self.profile, "reset", None)
        if policy is None:
            return "这个目标没有复位声明 ⇒ 默认不复位"
        note = str(getattr(policy, "note", "") or "")
        adapter = str(getattr(policy, "adapter", "") or "（没说走哪条路）")
        return (
            f"复位走 {adapter}；默认{'复位' if self.reset_default else '不复位'}"
            + (f"。{note}" if note else "")
        )

    def session_capabilities(self) -> dict[str, bool]:
        """这一路的会话能力（四项）—— 取自**这一份**会话，不跨固件合并。"""
        return dict(self.session.capabilities())

    def facts(self) -> platform_info.AssemblyFacts:
        """这一份装配在平台自述里的那一行。"""
        return platform_info.AssemblyFacts(
            firmware_id=self.firmware_id,
            label=self.label,
            # 「这套组合自己叫什么」由插件束声明（`esp32+示例项目` / `stm32+baremetal`）：
            # 平台自述的 label 读它，选择列表读上面的 `label`（两者的读者不同）。
            platform_label=str(getattr(self.plugins, "label", "") or ""),
            platform=self.platform,
            project=self.project_key,
            target_id=self.target_id,
            device_key=self.device_key,
            pipeline_allowed=bool(self.pipeline_allowed),
            requires_flash_confirmation=bool(self.requires_flash_confirmation),
            token_domain=self.token_domain,
            suites=tuple(self.suites),
            adapters=tuple(getattr(self.plugins, "adapter_ids", ()) or ()),
            decoder_available=(
                bool(self.plugins.decoder_is_available())
                if hasattr(self.plugins, "decoder_is_available")
                else False
            ),
            capability_table=self.capability_table,
            session_capabilities=self.session_capabilities(),
            bench=bench_config.block(owners=(self.target_id,)),
        )

    def document(self) -> dict[str, Any]:
        """这一份装配自己的平台自述（run manifest 用它：一次 run 只有一条链）。"""
        return platform_info.describe(
            plugins=self.plugins,
            targets=platform_info.targets_from_profiles(
                (self.profile,) if self.profile is not None else ()
            ),
            session_capabilities=self.session_capabilities(),
            bench=bench_config.block(owners=(self.target_id,)),
        )

    def to_json(self) -> dict[str, Any]:
        """给人看的那一份（**不含** Pipeline/Session 对象）。"""
        row = self.facts().to_json()
        row["reset_default"] = self.reset_default
        row["notes"] = list(self.notes)
        return row


@dataclass
class WebAssembly:
    """服务登记的全部装配 + 把它们接起来的四个入口（runner / observe / catalog / 自述）。"""

    assemblies: tuple[Assembly, ...]
    targets: tuple[Any, ...]
    primary: str
    runners: runner_registry.RunnerRegistry
    observe: ObserveCenter
    catalog: web_catalog.WebCatalog
    platform_facts: dict[str, Any]
    #: 因为**没配置**（ / B6）而没登记上来的链，一句一条（装配处写，启动横幅与体检都能读）。
    #: ⚠️ 它不影响 `problems()`：少一条链不是“不一致”，而是“这台机器还没告诉平台它接的什么”。
    bench_skipped: tuple[str, ...] = ()
    #:  / D1：设备嗅探的执行器（**构造不碰端口**；只有页面上显式点"设备嗅探"才会跑）。
    #: 类型是 `sniff_service.SniffExecutor`；这里只持一个引用，所以 core 不需要认识它。
    sniff: Any = None

    # ------------------------------------------------------------------ 查询
    def by_firmware(self, firmware_id: str) -> Assembly | None:
        for row in self.assemblies:
            if row.firmware_id == str(firmware_id or ""):
                return row
        return None

    def require(self, firmware_id: str) -> Assembly:
        found = self.by_firmware(firmware_id)
        if found is None:
            raise KeyError(
                f"未登记的装配 {firmware_id!r}：有 "
                f"{[row.firmware_id for row in self.assemblies]}"
            )
        return found

    @property
    def runs_dir(self):
        """证据目录：**所有装配共用一份**（页面把三份固件的 run 放在一个列表里）。"""
        if not self.assemblies:
            return None
        store = getattr(self.assemblies[0].pipeline, "store", None)
        return getattr(store, "runs_dir", None)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "primary": self.primary,
            "assemblies": [row.to_json() for row in self.assemblies],
        }

    # -------------------------------------------------------------- 冻结判据
    def problems(self) -> list[str]:
        """一一对应核对（空列表 = 这四边的说法完全一致）。

        核对的是**六处**必须同时成立：
        1. catalog 里每一个 firmware 都有一套装配（否则页面能选、跑不了）；
        2. 每一套装配在 catalog 里都查得到，且 `target_id` / `project` / 设备键一致；
        3. 套件集合 = `target.suites ∩ project.suites`（页面列出来的就是真能跑的）；
        4. 每一套装配在 runner registry 里都能被 `require()` 到，且烧录确认门槛一致；
        5. 每一套装配在观察中心里都有一份会话，而且那一份会话真能连 catalog 说的设备；
        6. 平台自述里每一行装配的 firmware_id 与这里一致，顶层不再冒充"唯一能力表"。
        """
        issues: list[str] = []
        rows = tuple(self.assemblies)
        if not rows:
            return ["一套装配都没登记（页面没有可选项）"]
        ids = [row.firmware_id for row in rows]
        duplicates = {name for name in ids if ids.count(name) > 1}
        if duplicates:
            issues.append(f"同一份 firmware 登记了不止一套装配：{sorted(duplicates)}")
        if self.primary not in ids:
            issues.append(f"主装配 {self.primary!r} 不在登记里：{sorted(ids)}")

        catalog_rows = dict(getattr(self.catalog, "firmwares", {}) or {})
        for row in rows:
            issues.extend(self._assembly_problems(row, catalog_rows))

        covered = {row.firmware_id for row in rows}
        for firmware_id, catalog_row in catalog_rows.items():
            if firmware_id in covered:
                continue
            target = getattr(self.catalog, "targets", {}).get(
                str(catalog_row.get("target_id"))
            )
            if target is not None and not bool(
                getattr(target, "pipeline_allowed", False)
            ):
                # 只读目标（例如板 B）本来就不允许自动验证：catalog 里登记它是为了
                # **如实展示**"这台设备在册、但策略不允许跑"，不算"选了跑不了"。
                continue
            issues.append(
                f"catalog 里登记了固件 {firmware_id!r}"
                f"（目标 {catalog_row.get('target_id')!r}），但没有对应的装配："
                "页面能选中它，却没有任何一条 Pipeline 能跑"
            )

        assembly_rows = list(self.platform_facts.get("assemblies") or ())
        if len(assembly_rows) != len(rows):
            issues.append(
                f"平台自述里有 {len(assembly_rows)} 行装配，实际登记了 {len(rows)} 套"
            )
        described = {str(item.get("firmware_id") or "") for item in assembly_rows}
        for missing in sorted(covered - described):
            issues.append(f"平台自述里没有 {missing!r} 这一行（页面看不到这条链）")
        if len(rows) > 1 and self.platform_facts.get("capabilities") is not None:
            issues.append(
                "多套装配下顶层不该有唯一的能力表（那会借用某一份固件的能力）"
            )
        issues.extend(platform_info.problems(self.platform_facts))
        return issues

    def _assembly_problems(
        self, row: Assembly, catalog_rows: dict[str, dict[str, Any]]
    ) -> list[str]:
        issues: list[str] = []
        where = row.firmware_id
        catalog_row = catalog_rows.get(row.firmware_id)
        target = getattr(self.catalog, "targets", {}).get(row.target_id)
        if catalog_row is None:
            issues.append(
                f"装配 {where!r} 不在 catalog 里（页面选不到它，只能靠 API 硬点）"
            )
        else:
            if str(catalog_row.get("target_id")) != row.target_id:
                issues.append(
                    f"装配 {where!r} 说的是目标 {row.target_id!r}，"
                    f"catalog 里却挂在 {catalog_row.get('target_id')!r}"
                )
            if str(catalog_row.get("project")) != row.project_key:
                issues.append(
                    f"装配 {where!r} 的项目包是 {row.project_key!r}，"
                    f"catalog 里却是 {catalog_row.get('project')!r}"
                )
            if bool(catalog_row.get("requires_flash_confirmation")) != bool(
                row.requires_flash_confirmation
            ):
                issues.append(
                    f"装配 {where!r} 的烧录确认门槛与 catalog 不一致"
                    f"（catalog={catalog_row.get('requires_flash_confirmation')!r}"
                    f"，装配={row.requires_flash_confirmation!r}）"
                )
        if target is None:
            issues.append(f"装配 {where!r} 的目标 {row.target_id!r} 不在自述的目标表里")
        else:
            if str(getattr(target, "device_key", "") or "") != row.device_key:
                issues.append(
                    f"装配 {where!r} 的设备键是 {row.device_key!r}，"
                    f"目标登记里却是 {getattr(target, 'device_key', '')!r}"
                )
            project = getattr(self.catalog, "projects", {}).get(row.project_key)
            if project is None:
                issues.append(
                    f"装配 {where!r} 的项目包 {row.project_key!r} 没有登记在 catalog 里"
                )
            else:
                allowed = [
                    name
                    for name in (getattr(target, "suites", ()) or ())
                    if name in (getattr(project, "suites", ()) or ())
                ]
                if set(allowed) != set(row.suites):
                    issues.append(
                        f"装配 {where!r} 声明的套件与"
                        f"「目标 ∩ 项目包」算出来的不一致："
                        f"装配={list(row.suites)}，算出来={allowed}"
                    )
        try:
            self.runners.require(row.firmware_id)
        except Exception as error:  # runner_registry.RunnerNotFound / KeyError 都算
            issues.append(f"装配 {where!r} 没有对应的 runner：{error}")
        if not self.observe.firmware_ids or row.firmware_id not in self.observe.firmware_ids:
            issues.append(f"装配 {where!r} 没有对应的实时观察会话")
        else:
            session = self.observe.session(row.firmware_id)
            keys = [str(spec.key) for spec in getattr(session, "registry").specs()]
            if row.device_key and row.device_key not in keys:
                issues.append(
                    f"装配 {where!r} 的观察会话连不上它自己的设备键 "
                    f"{row.device_key!r}（那份会话登记的是 {keys}）"
                )
            if session is not row.session:
                issues.append(f"装配 {where!r} 的观察会话不是它自己那一份（串了）")
        runs_dir = getattr(getattr(row.pipeline, "store", None), "runs_dir", None)
        if self.runs_dir is not None and runs_dir is not None:
            if str(runs_dir) != str(self.runs_dir):
                issues.append(
                    f"装配 {where!r} 的证据目录是 {runs_dir}"
                    f"，与其它装配的 {self.runs_dir} 不是同一份"
                )
        return issues


def assemble(
    *,
    assemblies: Iterable[Assembly],
    targets: Iterable[Any],
    projects: Iterable[Any] = (),
    firmwares: Iterable[Any] = (),
    primary: str = "",
    flash_confirmation_firmwares: Iterable[str] = (),
    runners: runner_registry.RunnerRegistry | None = None,
    bench_skipped: Iterable[str] = (),
) -> WebAssembly:
    """把登记好的装配接起来（**装配处只调这一个函数**，测试也调它）。

    - `targets` / `projects` / `firmwares` 是 **catalog 的选择空间**（板级 target 行：
      一块板可能承载两份固件，所以它跟某一份装配不是一对一）；
    - `assemblies` 是**每一份固件一条链**；
    - 三份 Pipeline 共用一条事件总线与一份证据目录由 `RunnerRegistry` 强制（它自己会拒绝
      不一致的登记），实时观察会话则交给一个 `ObserveCenter` 分派；
    - `runners` 可以由装配处**先**造好再交进来（观察会话要拿它的 `active_run_port` 当闸门，
      所以顺序必须是先 registry 后会话）。不给就在这里按装配造一个。
    """
    rows = tuple(assemblies)
    target_rows = tuple(targets)
    project_rows = tuple(projects)
    firmware_rows = tuple(firmwares)
    primary_id = str(primary or (rows[0].firmware_id if rows else ""))
    if runners is None:
        runners = runner_registry.RunnerRegistry(
            tuple(
                runner_registry.Runner(
                    row.firmware_id,
                    row.pipeline,
                    requires_flash_confirmation=row.requires_flash_confirmation,
                )
                for row in rows
            ),
            primary=primary_id,
        )
    observe = ObserveCenter(
        {row.firmware_id: row.session for row in rows},
        targets=tuple(
            ObserveTarget(
                firmware_id=row.firmware_id,
                label=row.label,
                target_id=row.target_id,
                device_key=row.device_key,
                platform=row.platform,
                project=row.project_key,
                note=row.notes[0] if row.notes else "",
                capability_table=row.capability_table,
            )
            for row in rows
        ),
        primary=primary_id,
    )
    for row in rows:
        # 闸门盖住**每一份**装配：任何一份 Pipeline 启动都要问过观察中心
        # （串口被实时观察拿着时给 409）。
        row.pipeline.observe = observe
        # run manifest 用**这一份装配自己**的自述（一次 run 只有一条链）。
        row.pipeline.platform_facts = row.document
    catalog = web_catalog.WebCatalog(
        target_rows,
        project_rows,
        firmware_rows,
        flash_confirmation_firmwares=tuple(flash_confirmation_firmwares),
        # ⚠️ "设备自报的项目名 -> 哪一条链"只有目标层知道（插件束里那一条声明），
        # 而 catalog 只认形状 ⇒ 由这里把它交进 catalog（不交的话身份永远只能"零匹配"）。
        device_projects=tuple(
            (row.target_id, tuple(getattr(row.plugins, "device_projects", ()) or ()))
            for row in rows
            if getattr(row.plugins, "device_projects", None)
        ),
    )
    facts = platform_info.describe_assemblies(
        assemblies=[row.facts() for row in rows],
        targets=platform_info.targets_from_profiles(target_rows),
    )
    return WebAssembly(
        assemblies=rows,
        targets=target_rows,
        primary=primary_id,
        runners=runners,
        observe=observe,
        catalog=catalog,
        platform_facts=facts,
        bench_skipped=tuple(str(item) for item in bench_skipped),
    )


__all__ = ["Assembly", "WebAssembly", "assemble"]
