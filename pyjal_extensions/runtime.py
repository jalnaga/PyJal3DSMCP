"""Library Runtime: 인스턴스별 활성화·Runtime 준비 검사·관리형 실행.

외부 효과는 생성자에 주입한 두 함수로만 일으킨다.

- ``run_python(code, expected_pid) -> dict``: DLEL이 만든 Python 원문을 대상 Max에서 Upstream ``execute_python``과
  같은 runner로 실행해 runner payload를 돌려준다. 전송·대상 문제는 Upstream 예외를 그대로 던진다.
- ``process_start_time(pid) -> int | None``: 프로세스 생성 시각(Windows FILETIME). None이면 종료·접근 불가.

Max로 보내는 코드는 Python 3.10 문법과 표준 라이브러리만 쓴다(Max 2024 내장 Python 3.10.14). 등록된 ``import_root``
하나만 ``sys.path``에 넣고, 이미 로드된 모듈을 교체·reload하거나 ``sys.modules``에서 지우지 않는다. 라이브러리
종류(이름)에 따른 분기는 없다.

예외: ``layout="package"``(설치된 패키지 하나)의 ``import_root``는 site-packages 같은 공용 디렉터리일 수 있다. 그 경로를
추가하면 이웃 패키지까지 그 Max 수명 동안 노출되므로 추가하지 않는다. 경로가 없으면 ``path_status="not_added"``로 알린다.
에이전트 표면(``layout="surface"``)은 사전 조건을 선언하지 않았을 때만 ``import_root``(최상위 패키지의 부모 = 툴 폴더)를
다른 항목처럼 없으면 끝에 하나 추가한다(``added``→``owned``). 진입점·런처 모듈은 import하지 않는다. 사전 조건을 선언한 표면은
충족 여부와 관계없이 추가하지 않는다(``not_added``). ``attach``는 경로를 바꾸지 않는다.

보호 툴 사전 조건(프로필 ``[[preconditions]]``): probe·execute는 Max 안에서 ``sys.path``를 바꾸기 전에 필요한 모듈이
``sys.modules``에 있는지 보고, 없으면 경로 추가·import·사용자 코드 실행 없이 ``LIBRARY_PRECONDITION_UNMET``으로 끝낸다.

``attach``: 대상 Max를 읽기 전용으로 한 번 조사(``library_scan`` 스크립트 확장)해 Catalog 항목마다
이 인스턴스 상태를 분류하고, 호출자가 정한 대상을 자동 활성화한다(Max에는 아무것도 보내지 않음).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from typing import Any, Callable, Mapping, Sequence

from pyjal_extensions import LibraryError
from pyjal_extensions.bindings import (
    STORE_LOCK,
    BindingStore,
    MaxInstanceIdentity,
    bridge_target_for,
    default_library_state,
)
from pyjal_extensions.manifests import PACKAGE_LAYOUT, SURFACE_LAYOUT
from pyjal_extensions.registry import LibraryCatalogEntry, LibraryRegistry
from pyjal_extensions.scanning import ATTACH_SCRIPT, SCAN_SCRIPT

PROBE_MODES = ("find_spec", "import")
_OPERATION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SUMMARY_FIELDS = ("library_id", "package", "reported_version", "status", "source_root", "source_revision")
_READY_STATES = {"loaded", "imported"}
# import_root를 sys.path에 넣지 않는 layout(공용 디렉터리일 수 있음). 표면은 사전 조건 선언 여부로 따로 판단한다(_adds_path)
_NO_ADD_PATH_LAYOUTS = frozenset({PACKAGE_LAYOUT})
PRECONDITION_UNMET = "LIBRARY_PRECONDITION_UNMET"

# library_attach의 attach_state(판정 순서대로)
ATTACH_STATES = ("unavailable", "conflict", "restart_required", "precondition_unmet", "unverified", "current", "not_loaded")
# library_attach warnings[].code: 로드됐지만 최신인지 확인되지 않은 항목(추정 근거, 항목마다 하나)
LOADED_CODE_UNVERIFIED = "LOADED_CODE_UNVERIFIED"
# Windows FILETIME(1601-01-01 기준 100ns)과 Unix epoch의 차이(100ns 단위)
_FILETIME_EPOCH_OFFSET = 116_444_736_000_000_000

# 실행 중인 관리형 호출(probe·execute). 같은 프로세스의 모든 LibraryRuntime이 공유한다.
_RUNNING: dict[str, dict[str, Any]] = {}
_RUNNING_LOCK = threading.Lock()

# Max가 응답했거나 Upstream이 "보내지 않음"을 확인한 오류 코드(결과가 확정된 실패)
_NOT_RUN_CODES = {"TARGET_MISMATCH", "TARGET_UNAVAILABLE", "UNSAVED_CHANGES", "BRIDGE_DOWN"}

RunPython = Callable[[str, int], Any]
ProcessStartTime = Callable[[int], "int | None"]


class LibraryRuntime:
    """Catalog(공통)와 인스턴스별 Binding·Runtime을 묶는 Facade. MCP Tool은 이 객체를 얇게 감싼다."""

    def __init__(
        self,
        registry: LibraryRegistry,
        *,
        run_python: RunPython,
        process_start_time: ProcessStartTime,
        preconditions: Mapping[str, Any] | None = None,
    ) -> None:
        """``preconditions``: ``library_id`` → ``requires_loaded``(모듈 이름 목록)·``message`` 속성을 가진 사전 조건."""
        self._registry = registry
        self._run_python = run_python
        self._process_start_time = process_start_time
        self._preconditions = dict(preconditions or {})
        self._store = BindingStore(registry.catalog_dir)

    # -- 대상 식별 ----------------------------------------------------------

    def identify(self, expected_pid: Any) -> MaxInstanceIdentity:
        _check_pid(expected_pid)
        return self._identify(expected_pid)

    def _identify(self, pid: int) -> MaxInstanceIdentity:
        started = self._process_start_time(pid)
        if not _is_start_time(started):
            raise LibraryError(
                "TARGET_UNAVAILABLE",
                f"대상 프로세스(PID {pid})를 찾지 못했습니다. 아무것도 보내지 않았습니다.",
                {"expected_pid": pid},
            )
        return MaxInstanceIdentity(pid=pid, process_start_time_utc=started, bridge_target=bridge_target_for(pid))

    # -- 목록 ---------------------------------------------------------------

    def list_libraries(self, expected_pid: Any = None) -> dict[str, Any]:
        if expected_pid is not None:
            _check_pid(expected_pid)
        with STORE_LOCK:
            entries = self._registry.list_libraries()
            if expected_pid is None:
                items = [_summary(entry) for entry in entries]
                return {"libraries": items, "count": len(items)}
            identity = self._identify(expected_pid)
            states = self._store.load(identity)["libraries"]
            items = []
            for entry in entries:
                item = _summary(entry)
                state = default_library_state()
                state.update(states.get(entry.library_id, {}))
                state["loaded_source_revision"] = self._known_loaded_revision(identity, entry, state)
                item.update({key: state[key] for key in default_library_state()})
                items.append(item)
            return {"libraries": items, "count": len(items), "target_instance": identity.to_dict()}

    def scan(self, expected_pid: Any) -> dict[str, Any]:
        """대상 Max의 직계 라이브러리 후보를 발견한다. Catalog·Binding은 변경하지 않는다."""
        identity = self.identify(expected_pid)
        with STORE_LOCK:
            registrations = []
            for entry in self._registry.list_libraries():
                paths = []
                for name in entry.top_level:
                    paths.extend([
                        os.path.join(entry.import_root, name),
                        os.path.join(entry.import_root, name + ".py"),
                    ])
                registrations.append({"library_id": entry.library_id, "paths": paths})
        script = f"_SCAN_REGISTERED = {registrations!r}\n" + SCAN_SCRIPT
        payload = self._run_python(script, expected_pid)
        current = self._identify(expected_pid)
        if current != identity:
            raise LibraryError(
                "TARGET_MISMATCH",
                "스캔 중 대상 프로세스의 시작 시각이 바뀌었습니다. 대상을 다시 확인하십시오.",
                {"previous_identity": identity.to_dict(), "current_identity": current.to_dict()},
            )
        report = _unwrap_report(payload, script)
        if not isinstance(report.get("candidates"), list) or not isinstance(report.get("warnings"), list):
            raise LibraryError("REQUEST_OUTCOME_UNKNOWN", "Max 응답에서 스캔 결과를 찾지 못했습니다.")
        return {"target_instance": identity.to_dict(), "candidates": report["candidates"], "warnings": report["warnings"]}

    # -- attach ----------------------------------

    def attach(
        self,
        expected_pid: Any,
        *,
        auto_enable: Callable[[LibraryCatalogEntry], bool] | None = None,
    ) -> dict[str, Any]:
        """대상 Max를 읽기 전용으로 한 번 조사하고 Catalog 항목마다 이 인스턴스 상태를 분류한다.

        - 조사는 ``library_scan`` 스크립트 확장 1회다(``sys.path``·``sys.modules`` 무변경, import 없음). 실패하면 Upstream
          예외·오류를 그대로 올리고 Binding·Runtime 상태를 바꾸지 않는다(재전송 없음).
        - 판정 순서: ``unavailable``(Catalog stale·invalid) → ``conflict``(다른 경로에서 로드) → ``restart_required``(확정 근거:
          이 인스턴스의 로드 기억 revision이 현재와 다름) → ``precondition_unmet`` → ``unverified``(추정 근거: 로드 기억 없이
          로드된 파일이 Max 시작 뒤 수정됐거나 지금 읽을 수 없음) → ``current`` → ``not_loaded``.
        - ``current``는 로드 기억에 현재 revision을, ``restart_required``는 Runtime 상태를 기록한다. ``unverified``·``not_loaded``는
          기록하지 않는다. ``unverified`` 항목마다 ``warnings``에 ``LOADED_CODE_UNVERIFIED`` 하나를 낸다(차단하지 않음).
        - ``auto_enable``이 참인 항목(``unavailable`` 제외)을 활성화한다. 같은 최상위 모듈 충돌은 그 항목의 ``enable_error``다.
        """
        identity = self.identify(expected_pid)
        with STORE_LOCK:
            entries = self._registry.list_libraries()
            names = sorted({name for entry in entries for name in entry.top_level})
            required = sorted(
                {name for entry in entries for name in self._required_modules(entry.library_id) or ()}
            )
            registrations = [
                {
                    "library_id": entry.library_id,
                    "paths": [
                        path
                        for name in entry.top_level
                        for path in (os.path.join(entry.import_root, name), os.path.join(entry.import_root, name + ".py"))
                    ],
                }
                for entry in entries
            ]
        script = (
            f"_SCAN_REGISTERED = {registrations!r}\n"
            f"_ATTACH_NAMES = {names!r}\n"
            f"_ATTACH_REQUIRED = {required!r}\n"
            + SCAN_SCRIPT
            + ATTACH_SCRIPT
        )
        payload = self._run_python(script, expected_pid)
        current = self._identify(expected_pid)
        if current != identity:
            raise LibraryError(
                "TARGET_MISMATCH",
                "조사 중 대상 프로세스의 시작 시각이 바뀌었습니다. 대상을 다시 확인하십시오.",
                {"previous_identity": identity.to_dict(), "current_identity": current.to_dict()},
            )
        report = _unwrap_report(payload, script)
        observed = report.get("attach")
        if not isinstance(observed, dict) or not isinstance(observed.get("modules"), list):
            raise LibraryError("REQUEST_OUTCOME_UNKNOWN", "Max 응답에서 연결 조사 결과를 찾지 못했습니다.")
        loaded = {
            row["name"]: row for row in observed["modules"] if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
        present = set(observed.get("required_loaded") or [])

        with STORE_LOCK:
            items = []
            enabled_any = False
            for entry in self._registry.list_libraries():
                item = self._attach_item(identity, entry, loaded, present)
                if auto_enable is not None and item["attach_state"] != "unavailable" and auto_enable(entry):
                    enabled_any |= self._attach_enable(identity, entry, item)
                items.append(item)
            if enabled_any:
                # set_enabled와 같이 같은 PID의 이전 인스턴스 기록과 종료된 인스턴스 기록을 정리한다
                self._store.forget_previous(identity)
                self._cleanup_ended_instances()
        items.sort(key=lambda item: item["library_id"])
        summary = {state: sorted(i["library_id"] for i in items if i["attach_state"] == state) for state in ATTACH_STATES}
        warnings = [
            {
                "code": LOADED_CODE_UNVERIFIED,
                "message": (
                    f"{library_id}: 이 Max에 이미 로드된 코드가 현재 원본과 같은지 확인되지 않았습니다(DLEL이 로드하지 않았고 "
                    "로드된 파일이 Max 시작 뒤 수정됐거나 지금 읽을 수 없음). 실행은 막지 않습니다. 결과가 이상하면 Max 재시작을 권장하십시오."
                ),
                "library_id": library_id,
            }
            for library_id in summary["unverified"]
        ]
        return {
            "target_instance": identity.to_dict(),
            "libraries": items,
            "count": len(items),
            "summary": summary,
            "unregistered_loaded": _unregistered_loaded(report.get("candidates"), observed.get("stdlib")),
            "warnings": warnings,
        }

    def _attach_item(
        self,
        identity: MaxInstanceIdentity,
        entry: LibraryCatalogEntry,
        loaded: dict[str, dict[str, Any]],
        present: set[str],
    ) -> dict[str, Any]:
        """항목 하나를 분류하고 판정과 일관되게 로드 기억·Runtime 상태를 기록한다."""
        checked = self._registry.check(entry.library_id)
        state = self._store.library_state(identity, entry.library_id)
        item: dict[str, Any] = {
            "library_id": checked.library_id,
            "attach_state": "not_loaded",
            "binding": state["binding"],
            "auto_enabled": False,
            "surface": checked.surface,
            "trust": dict(checked.trust),
            "catalog_status": checked.status,
            "source_revision": checked.source_revision,
            "loaded_source_revision": None,
            "enable_error": None,
            "precondition": None,
        }
        if checked.status in ("stale", "invalid"):
            item["attach_state"] = "unavailable"
            return item

        tops = [loaded[name] for name in checked.top_level if name in loaded]
        if any(not _location_under(row.get("location"), checked.import_root) for row in tops):
            item["attach_state"] = "conflict"
            return item
        loaded_names = [row["name"] for row in tops]
        unverified = False
        # 확정 근거: 이 인스턴스의 로드 기억(로드 기억 또는 라이브러리 기록). DLEL은 reload하지 않으므로 기억이 곧 메모리 코드다
        memory = set(
            self._store.loaded_revisions(identity, checked.import_root, loaded_names, checked.surface).values()
        )
        if state["loaded_source_revision"] is not None:
            memory.add(state["loaded_source_revision"])
        if any(revision != checked.source_revision for revision in memory) or (
            state["runtime_state"] == "restart_required"
        ):
            item["attach_state"] = "restart_required"
            item["loaded_source_revision"] = self._known_loaded_revision(identity, checked, state)
            if item["loaded_source_revision"] == checked.source_revision:
                item["loaded_source_revision"] = None  # 디스크 내용과 메모리 코드가 같다고 볼 근거가 없다
            self._store.update_library(identity, checked.library_id, runtime_state="restart_required")
            return item
        if loaded_names:
            # 기억이 없으면 수정 시각은 추정 근거일 뿐이다("로드 뒤 수정"과 "수정 뒤 첫 로드"를 구분할 수 없고, 네트워크
            # 공유는 파일 서버 시계로 기록된다). 차단하지 않고 기준값도 기록하지 않는다
            unverified = not memory and _changed_after_start(loaded, checked.top_level, identity.process_start_time_utc)

        precondition = self._preconditions.get(checked.library_id)
        if precondition is not None:
            missing = [name for name in precondition.requires_loaded if name not in present]
            if missing:
                item["attach_state"] = "precondition_unmet"
                item["precondition"] = {"missing_modules": missing, "message": precondition.message}
                return item

        if unverified:
            item["attach_state"] = "unverified"
            return item
        if loaded_names:
            item["attach_state"] = "current"
            item["loaded_source_revision"] = checked.source_revision
            self._store.remember_loaded(
                identity, checked.import_root, loaded_names, checked.source_revision, checked.surface
            )
        return item

    def _attach_enable(self, identity: MaxInstanceIdentity, entry: LibraryCatalogEntry, item: dict[str, Any]) -> bool:
        """자동 활성화(Max 요청 없음). 이번에 새로 켰으면 True. 충돌은 항목의 ``enable_error``로 남긴다."""
        if item["binding"] == "enabled":
            item["auto_enabled"] = True
            return False
        try:
            self._check_top_level_conflict(identity, entry)
        except LibraryError as exc:
            item["enable_error"] = exc.to_dict()
            return False
        self._store.update_library(identity, entry.library_id, binding="enabled")
        item["binding"] = "enabled"
        item["auto_enabled"] = True
        return True

    # -- Binding ------------------------------------------------------------

    def set_enabled(
        self,
        library_id: str,
        enabled: Any,
        expected_pid: Any,
        *,
        approve_import: Any = None,
        approve_execution: Any = None,
    ) -> dict[str, Any]:
        """대상 인스턴스의 Binding을 바꾼다. Max에는 아무것도 보내지 않는다."""
        if not isinstance(enabled, bool):
            raise LibraryError("INVALID_ARGUMENT", "enabled는 true/false여야 합니다.", {"enabled": repr(enabled)})
        for name, value in (("approve_import", approve_import), ("approve_execution", approve_execution)):
            if value is not None and not isinstance(value, bool):
                raise LibraryError("INVALID_ARGUMENT", f"{name}는 true/false/null이어야 합니다.", {name: repr(value)})
        _check_pid(expected_pid)
        with STORE_LOCK:
            entry = self._registry.get(library_id)
            identity = self._identify(expected_pid)
            if enabled:
                self._check_top_level_conflict(identity, entry)
            if approve_import is not None or approve_execution is not None:
                entry = self._registry.set_trust(
                    library_id, approved_for_import=approve_import, approved_for_execution=approve_execution
                )
            # 같은 PID의 이전 인스턴스(재시작·PID 재사용) 기록은 다시 쓰이지 않으므로 정리한다
            self._store.forget_previous(identity)
            state = self._store.update_library(identity, library_id, binding="enabled" if enabled else "disabled")
            result = {
                "library_id": library_id,
                "target_instance": identity.to_dict(),
                "binding": state["binding"],
                "trust": dict(entry.trust),
                "runtime_state": state["runtime_state"],
                "disable_deferred": False,
            }
            running = _running(identity)
            if not enabled and running is not None and running["library_id"] == library_id:
                # 실행 중인 코드는 중단하지 않는다. 새 관리형 호출은 거절되고 완료 후 비활성 상태가 최종이다
                result["disable_deferred"] = True
                result["running_operation_id"] = running.get("operation_id")
            self._cleanup_ended_instances()
            return result

    def _check_top_level_conflict(self, identity: MaxInstanceIdentity, entry: LibraryCatalogEntry) -> None:
        states = self._store.load(identity)["libraries"]
        names = set(entry.top_level)
        for other_id in sorted(states):
            if other_id == entry.library_id or states[other_id].get("binding") != "enabled":
                continue
            try:
                other = self._registry.get(other_id)
            except LibraryError:
                continue
            overlap = sorted(names & set(other.top_level))
            if overlap and (entry.surface or other.surface) and _same_path(entry.import_root, other.import_root):
                continue  # 표면과 같은 소스(같은 import_root·최상위 패키지)의 항목은 충돌이 아니다
            if overlap:
                raise LibraryError(
                    "LIBRARY_VERSION_CONFLICT",
                    f"이 Max 인스턴스에서 같은 최상위 모듈을 쓰는 '{other_id}'가 이미 활성입니다. 먼저 비활성화하십시오.",
                    {
                        "library_id": entry.library_id,
                        "conflicting_library_id": other_id,
                        "top_level": overlap,
                        "target_instance": identity.to_dict(),
                    },
                )

    # -- 해제 ---------------------------------------------------------------

    def unregister(self, library_id: str) -> dict[str, Any]:
        """살아 있는 인스턴스에서 활성·실행 중이 아니면 Catalog와 모든 Binding 기록을 지운다."""
        with STORE_LOCK:
            self._registry.get(library_id)
            in_use = []
            for identity, record in self._store.records():
                state = record["libraries"].get(library_id, {})
                running = _running(identity)
                busy = running is not None and running["library_id"] == library_id
                if state.get("binding") != "enabled" and not busy:
                    continue
                if self._process_start_time(identity.pid) == identity.process_start_time_utc:
                    in_use.append(identity.to_dict())
            if in_use:
                raise LibraryError(
                    "LIBRARY_IN_USE",
                    "살아 있는 Max 인스턴스에서 활성 상태입니다. 먼저 library_set_enabled(enabled=false)로 해제하십시오.",
                    {"library_id": library_id, "instances": in_use},
                )
            self._registry.unregister(library_id)
            self._store.remove_library(library_id)
            # 라이브러리 항목을 지운 뒤 정리해야 그 라이브러리만 활성이던 instance_changed 근거도 함께 정리된다
            self._cleanup_ended_instances()
            return {"library_id": library_id, "unregistered": True}

    def _cleanup_ended_instances(self) -> None:
        """종료된 인스턴스의 기록을 지운다(성공한 ``set_enabled``·``unregister`` 뒤에만 부른다).

        - 기록된 시작 시각과 현재 시작 시각이 같으면 살아 있으므로 보존한다(로드 기억·``owned_paths`` 포함).
        - 프로세스가 없으면(None 등) 정리한다.
        - 같은 PID가 다른 프로세스로 바뀌었으면 정리하되, 활성 라이브러리가 남은 기록은
          ``TARGET_MISMATCH(instance_changed)`` 안내의 근거이므로 보존한다.
        - 판정할 수 없거나(조회 예외) 관리형 호출이 실행 중인 인스턴스는 보존한다.
        """
        for identity, record in list(self._store.records()):
            if _running(identity) is not None:
                continue
            try:
                started = self._process_start_time(identity.pid)
            except Exception:
                continue
            if started == identity.process_start_time_utc:
                continue
            if _is_start_time(started) and any(
                isinstance(state, dict) and state.get("binding") == "enabled" for state in record["libraries"].values()
            ):
                continue
            self._store.discard(identity)

    # -- probe --------------------------------------------------------------

    def probe(self, library_id: str, expected_pid: Any, mode: Any = "find_spec") -> dict[str, Any]:
        """대상 Max에서 Runtime 준비를 검사한다. ``import`` 모드는 모듈 최상위 코드를 실행한다."""
        _check_pid(expected_pid)
        if mode not in PROBE_MODES:
            raise LibraryError("INVALID_ARGUMENT", "mode는 'find_spec' 또는 'import'여야 합니다.", {"mode": repr(mode)})
        do_import = mode == "import"
        with STORE_LOCK:
            entry, identity, state = self._precheck(
                library_id,
                expected_pid,
                required_trust=("approved_for_import",) if do_import else (),
            )
            if state["loaded_source_revision"] not in (None, entry.source_revision) or (
                state["runtime_state"] == "restart_required"
            ):
                raise _restart_required(entry, identity, state)
            self._check_loaded_memory(entry, identity, state)
            self._claim(identity, {"kind": "probe", "library_id": library_id, "operation_id": None})
        try:
            code = _probe_script(entry, do_import, self._required_modules(library_id))
            try:
                payload = self._run_python(code, expected_pid)
            except BaseException as exc:
                if _outcome_of(exc) == "outcome_unknown":
                    self._record(identity, library_id, runtime_state="unknown")
                raise
            try:
                report = _unwrap_report(payload, code)
            except LibraryError as exc:
                if exc.code == "REQUEST_OUTCOME_UNKNOWN":
                    self._record(identity, library_id, runtime_state="unknown")
                raise
            self._raise_if_precondition_unmet(entry, identity, report)
            self._remember_loaded(identity, entry, report["modules"])
            details = self._probe_details(entry, identity, mode, do_import, report)
            return self._finish_probe(entry, identity, details)
        finally:
            _release(identity)

    def _probe_details(
        self,
        entry: LibraryCatalogEntry,
        identity: MaxInstanceIdentity,
        mode: str,
        do_import: bool,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        path_status = self._path_status(identity, entry, report)
        return {
            "library_id": entry.library_id,
            "source_revision": entry.source_revision,
            "target_instance": identity.to_dict(),
            "mode": mode,
            "runtime_state": _runtime_state_of(report["modules"]),
            "import_performed": do_import,
            "side_effects_possible": do_import,
            "python": report.get("python"),
            "max_pid": report.get("max_pid"),
            "import_root": entry.import_root,
            "path_status": path_status,
            "modules": report["modules"],
        }

    def _finish_probe(
        self, entry: LibraryCatalogEntry, identity: MaxInstanceIdentity, details: dict[str, Any]
    ) -> dict[str, Any]:
        runtime_state = details["runtime_state"]
        self._record_runtime(identity, entry, runtime_state)
        failure = _preparation_failure(runtime_state)
        if failure is not None:
            code, message = failure
            raise LibraryError(code, message, details)
        return details

    # -- execute ------------------------------------------------------------

    def execute(
        self,
        library_id: str,
        code: Any,
        expected_pid: Any,
        expected_source_revision: Any,
        operation_id: Any,
    ) -> dict[str, Any]:
        """검사를 통과하면 라이브러리를 준비하고 사용자 코드를 대상 Max에서 한 번 실행한다(자동 재실행 없음)."""
        _check_pid(expected_pid)
        if not isinstance(code, str) or not code.strip():
            raise LibraryError("INVALID_ARGUMENT", "code는 비어 있지 않은 Python 문자열이어야 합니다.")
        if not isinstance(expected_source_revision, str):
            raise LibraryError("INVALID_ARGUMENT", "expected_source_revision은 문자열이어야 합니다.")
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise LibraryError(
                "INVALID_ARGUMENT",
                "operation_id는 영문자·숫자·'_.:-'만 쓰는 1~128자여야 합니다.",
                {"operation_id": repr(operation_id)},
            )
        context: dict[str, Any] = {"library_id": library_id, "operation_id": operation_id}
        with STORE_LOCK:
            try:
                entry, identity, state = self._precheck(
                    library_id,
                    expected_pid,
                    required_trust=("approved_for_import", "approved_for_execution"),
                    context=context,
                )
                if expected_source_revision != entry.source_revision:
                    raise LibraryError(
                        "LIBRARY_STALE",
                        "호출자가 본 source revision이 Catalog와 다릅니다. library_list로 현재 revision을 확인하십시오.",
                        {
                            "reason": "revision_mismatch",
                            "source_revision": entry.source_revision,
                            "expected_source_revision": expected_source_revision,
                        },
                    )
                if state["runtime_state"] == "conflict":
                    raise LibraryError(
                        "LIBRARY_VERSION_CONFLICT",
                        "이 Max 인스턴스에 같은 이름의 모듈이 다른 경로로 로드되어 있습니다. Max 재시작이 필요할 수 있습니다.",
                        {"runtime_state": "conflict", "source_revision": entry.source_revision},
                    )
                if state["loaded_source_revision"] not in (None, entry.source_revision) or (
                    state["runtime_state"] == "restart_required"
                ):
                    raise _restart_required(entry, identity, state)
                self._check_loaded_memory(entry, identity, state)
            except LibraryError as exc:
                _merge_details(exc, context)
                raise
            agent_mark = _agent_error_mark()
            script = _execute_script(entry, code, agent_mark, self._required_modules(library_id))
            digest = "sha256:" + hashlib.sha256(script.encode("utf-8")).hexdigest()
            self._claim(identity, {"kind": "execute", "library_id": library_id, "operation_id": operation_id})
            operation = {
                "operation_id": operation_id,
                "state": "running",
                "script_digest": digest,
                "source_revision": entry.source_revision,
            }
            try:
                self._store.update_library(identity, library_id, last_operation=dict(operation))
            except BaseException:
                _release(identity)
                raise
        context.update(
            {
                "source_revision": entry.source_revision,
                "target_instance": identity.to_dict(),
                "script_digest": digest,
            }
        )
        try:
            return self._send_execute(entry, identity, script, expected_pid, operation, context, agent_mark)
        finally:
            _release(identity)

    def _send_execute(
        self,
        entry: LibraryCatalogEntry,
        identity: MaxInstanceIdentity,
        script: str,
        expected_pid: int,
        operation: dict[str, Any],
        context: dict[str, Any],
        agent_mark: str,
    ) -> dict[str, Any]:
        library_id = entry.library_id
        try:
            payload = self._run_python(script, expected_pid)
        except BaseException as exc:
            outcome = _outcome_of(exc)
            changes: dict[str, Any] = {
                "last_operation": {**operation, "state": outcome, "error_code": getattr(exc, "code", None)}
            }
            if outcome == "outcome_unknown":
                changes["runtime_state"] = "unknown"
            self._record(identity, library_id, **changes)
            raise

        if isinstance(payload, dict) and str(payload.get("status", "")).lower() == "error":
            code = str(payload.get("code") or "PYTHON_ERROR")
            if code == "PYTHON_ERROR":
                # 준비 보고는 runner 오류에 실리지 않는다. 사용자 코드는 준비가 끝난(모든 최상위 모듈이 import_root에서
                # 로드된) 뒤에만 실행되므로 Python 예외는 로드를 뜻한다고 보수적으로 기억한다(옛 코드 실행 방지)
                self._remember_loaded(
                    identity, entry, [{"name": name, "state": "imported"} for name in entry.top_level]
                )
            details = dict(payload.get("details") or {})
            agent_error = _take_agent_error(details, agent_mark) if code == "PYTHON_ERROR" else None
            if payload.get("error_type"):
                details["error_type"] = payload["error_type"]
            details.update(context)
            details["operation_state"] = "failed"
            if agent_error is not None:
                details["agent_error"] = agent_error
            self._record(identity, library_id, last_operation={**operation, "state": "failed", "error_code": code})
            raise LibraryError(code, str(payload.get("error") or code), details)

        try:
            report = _unwrap_report(payload, script)
        except LibraryError as exc:
            self._record(
                identity,
                library_id,
                runtime_state="unknown",
                last_operation={**operation, "state": "outcome_unknown", "error_code": exc.code},
            )
            _merge_details(exc, {**context, "operation_state": "outcome_unknown"})
            raise

        try:
            self._raise_if_precondition_unmet(entry, identity, report)
        except LibraryError as exc:
            self._record(identity, library_id, last_operation={**operation, "state": "failed", "error_code": exc.code})
            _merge_details(exc, {**context, "operation_state": "failed"})
            raise
        self._remember_loaded(identity, entry, report["modules"])
        runtime_state = _runtime_state_of(report["modules"])
        path_status = self._path_status(identity, entry, report)
        failure = _preparation_failure(runtime_state)
        if failure is not None:
            code, message = failure
            self._record_runtime(identity, entry, runtime_state)
            self._record(identity, library_id, last_operation={**operation, "state": "failed", "error_code": code})
            details = {
                **context,
                "operation_state": "failed",
                "runtime_state": runtime_state,
                "modules": report["modules"],
                "python": report.get("python"),
                "max_pid": report.get("max_pid"),
                "import_root": entry.import_root,
                "path_status": path_status,
            }
            raise LibraryError(code, message + " 사용자 코드는 실행하지 않았습니다.", details)

        self._record_runtime(identity, entry, "ready")
        self._record(identity, library_id, last_operation={**operation, "state": "succeeded"})
        return {
            "library_id": library_id,
            "source_revision": entry.source_revision,
            "target_instance": identity.to_dict(),
            "operation_id": operation["operation_id"],
            "script_digest": operation["script_digest"],
            "runtime_state": "ready",
            "operation_state": "succeeded",
            "value": payload["value"].get("value"),
            "stdout": payload.get("stdout", ""),
            "stderr": payload.get("stderr", ""),
        }

    # -- 공통 사전 검사 -----------------------------------------------------

    def _precheck(
        self,
        library_id: str,
        pid: int,
        *,
        required_trust: tuple[str, ...],
        context: dict[str, Any] | None = None,
    ) -> tuple[LibraryCatalogEntry, MaxInstanceIdentity, dict[str, Any]]:
        """검사 순서(계약): 등록 → 대상 → 실행 중 → 대상 변경/비활성 → 승인 → 원본 stale. 모두 전송 전이다."""
        entry = self._registry.get(library_id)
        identity = self._identify(pid)
        if context is not None:
            context["target_instance"] = identity.to_dict()
        running = _running(identity)
        if running is not None:
            raise LibraryError(
                "OPERATION_IN_PROGRESS",
                "이 Max 인스턴스에서 다른 관리형 호출이 실행 중입니다. 끝난 뒤 다시 요청하십시오.",
                {"running": dict(running)},
            )
        state = self._store.library_state(identity, library_id)
        if state["binding"] != "enabled":
            previous = self._previous_enabled(identity, library_id)
            if previous is not None:
                raise LibraryError(
                    "TARGET_MISMATCH",
                    f"PID {pid}의 Max가 활성화 이후 재시작되었거나 PID가 재사용되었습니다. 이전 Binding은 쓰지 않습니다. "
                    "대상을 확인한 뒤 이 인스턴스에서 다시 활성화하십시오.",
                    {
                        "reason": "instance_changed",
                        "previous_identity": previous.to_dict(),
                        "current_identity": identity.to_dict(),
                    },
                )
            raise LibraryError(
                "LIBRARY_DISABLED",
                f"이 Max 인스턴스(PID {pid})에서 비활성 상태입니다. library_set_enabled로 먼저 활성화하십시오.",
                {"library_id": library_id},
            )
        missing = [key for key in required_trust if not entry.trust.get(key)]
        if missing:
            raise LibraryError(
                "LIBRARY_NOT_APPROVED",
                "필요한 신뢰 승인이 없습니다. 사용자가 명시적으로 지시한 경우에만 library_set_enabled로 승인하십시오.",
                {"missing": missing},
            )
        checked = self._registry.check(library_id)
        if checked.status == "stale":
            raise LibraryError(
                "LIBRARY_STALE",
                "등록 이후 원본이 바뀌었습니다. library 갱신 후 다시 확인하십시오.",
                {"reason": "source_changed", "source_revision": checked.source_revision},
            )
        if checked.status == "invalid":
            raise LibraryError(
                "LIBRARY_STALE",
                "등록된 원본을 찾지 못했습니다.",
                {"reason": "source_invalid", "source_root": checked.source_root},
            )
        return checked, identity, state

    def _previous_enabled(self, identity: MaxInstanceIdentity, library_id: str) -> MaxInstanceIdentity | None:
        for previous in self._store.previous_identities(identity):
            if self._store.library_state(previous, library_id)["binding"] == "enabled":
                return previous
        return None

    # -- 상태 기록 ----------------------------------------------------------

    def _claim(self, identity: MaxInstanceIdentity, running: dict[str, Any]) -> None:
        with _RUNNING_LOCK:
            current = _RUNNING.get(identity.key)
            if current is not None:
                raise LibraryError(
                    "OPERATION_IN_PROGRESS",
                    "이 Max 인스턴스에서 다른 관리형 호출이 실행 중입니다. 끝난 뒤 다시 요청하십시오.",
                    {"running": dict(current)},
                )
            _RUNNING[identity.key] = dict(running)

    def _record(self, identity: MaxInstanceIdentity, library_id: str, **changes: Any) -> None:
        with STORE_LOCK:
            self._store.update_library(identity, library_id, **changes)

    def _record_runtime(self, identity: MaxInstanceIdentity, entry: LibraryCatalogEntry, runtime_state: str) -> None:
        with STORE_LOCK:
            changes: dict[str, Any] = {"runtime_state": runtime_state}
            state = self._store.library_state(identity, entry.library_id)
            if runtime_state == "ready" and state["loaded_source_revision"] is None:
                changes["loaded_source_revision"] = entry.source_revision
            self._store.update_library(identity, entry.library_id, **changes)

    # -- 보호 툴 사전 조건 --------------------------------------------------

    def _required_modules(self, library_id: str) -> list[str] | None:
        """선언된 사전 조건의 필요 모듈. 선언이 없으면 None(선언한 표면은 경로를 추가하지 않으므로 빈 목록과 구분)."""
        precondition = self._preconditions.get(library_id)
        return list(precondition.requires_loaded) if precondition is not None else None

    def _raise_if_precondition_unmet(
        self, entry: LibraryCatalogEntry, identity: MaxInstanceIdentity, report: dict[str, Any]
    ) -> None:
        """Max가 사전 조건 미충족을 보고했으면(경로 추가·import 전에 멈춤) ``LIBRARY_PRECONDITION_UNMET``."""
        missing = report.get("precondition_missing")
        if not missing:
            return
        precondition = self._preconditions.get(entry.library_id)
        message = precondition.message if precondition is not None else ""
        raise LibraryError(
            PRECONDITION_UNMET,
            "보호 툴의 사전 조건이 충족되지 않아 경로를 추가하거나 import하지 않았습니다"
            + (f": {message}" if message else "."),
            {
                "missing_modules": list(missing),
                "message": message,
                "library_id": entry.library_id,
                "target_instance": identity.to_dict(),
            },
        )

    # -- 인스턴스 로드 기억 ---------------------------------------------

    def _remember_loaded(
        self, identity: MaxInstanceIdentity, entry: LibraryCatalogEntry, modules: list[dict[str, Any]]
    ) -> None:
        """Max가 ``import_root`` 아래에서 로드를 확인한 모듈만 기억한다(준비 실패 응답 포함)."""
        names = [
            module["name"]
            for module in modules
            if isinstance(module, dict) and module.get("state") in _READY_STATES and module.get("name") in entry.top_level
        ]
        if names:
            with STORE_LOCK:
                self._store.remember_loaded(identity, entry.import_root, names, entry.source_revision, entry.surface)

    def _check_loaded_memory(
        self, entry: LibraryCatalogEntry, identity: MaxInstanceIdentity, state: dict[str, Any]
    ) -> None:
        """같은 ``import_root``의 최상위 모듈이 다른 revision으로 이미 로드돼 있으면 전송 전에 거절한다.

        라이브러리 해제·재등록(다른 ``library_id`` 포함)으로 라이브러리별 기록이 사라져도 Max의 ``sys.modules``에는
        옛 코드가 남아 있고 DLEL은 reload하지 않으므로 그 Max를 재시작해야 한다.
        """
        revisions = self._store.loaded_revisions(identity, entry.import_root, list(entry.top_level), entry.surface)
        for name in sorted(revisions):
            if revisions[name] != entry.source_revision:
                raise _restart_required(entry, identity, {**state, "loaded_source_revision": revisions[name]}, module=name)

    def _known_loaded_revision(
        self, identity: MaxInstanceIdentity, entry: LibraryCatalogEntry, state: dict[str, Any]
    ) -> str | None:
        """그 인스턴스에 실제로 로드된 것으로 DLEL이 아는 revision. 여럿이면 현재 revision과 다른 것을 우선한다."""
        known = set(
            self._store.loaded_revisions(identity, entry.import_root, list(entry.top_level), entry.surface).values()
        )
        if state["loaded_source_revision"] is not None:
            known.add(state["loaded_source_revision"])
        others = sorted(revision for revision in known if revision != entry.source_revision)
        if others:
            return others[0]
        return entry.source_revision if known else None

    def _path_status(self, identity: MaxInstanceIdentity, entry: LibraryCatalogEntry, report: dict[str, Any]) -> str:
        with STORE_LOCK:
            if report.get("path_added"):
                self._store.add_owned_path(identity, entry.import_root)
                return "added"
            if report.get("path_present") is False:
                # package layout과 사전 조건을 선언한 표면은 import_root를 추가하지 않는다
                return "not_added"
            return "owned" if self._store.owns_path(identity, entry.import_root) else "preexisting"


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _location_under(location: Any, root: str) -> bool:
    """Max가 보고한 모듈 위치가 ``root`` 아래인가(링크 해석·대소문자 무시). 위치를 모르면 거짓.

    서버와 Max가 같은 폴더를 다른 표기로 볼 수 있다(로컬 드라이브 경로와 UNC 경로, 한쪽에서만 UNC로 해석되는 매핑 드라이브).
    문자열로 같지 않으면 위치의 조상 폴더가 ``root``와 같은 실제 폴더인지 파일 ID로 확인한다.
    """
    if not isinstance(location, str) or not location:
        return False
    try:
        candidate = os.path.normcase(os.path.realpath(location))
        base = os.path.normcase(os.path.realpath(root))
    except (OSError, ValueError):
        return False
    try:
        if os.path.commonpath([candidate, base]) == base:
            return True
    except ValueError:  # 드라이브·UNC 공유가 다름: 아래 실제 폴더 비교로 넘어간다
        pass
    return _has_ancestor(candidate, base)


def _has_ancestor(path: str, root: str) -> bool:
    """``path`` 자신이나 조상 폴더 중 ``root``와 같은 실제 폴더가 있는가(파일 ID 비교, 없는 경로는 거짓)."""
    try:
        root_stat = os.stat(root)
    except (OSError, ValueError):
        return False
    if not root_stat.st_ino:  # 파일 ID를 주지 않는 파일 시스템은 비교하지 않는다(거짓 일치 방지)
        return False
    current = path
    while True:
        try:
            if os.path.samestat(os.stat(current), root_stat):
                return True
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _changed_after_start(loaded: dict[str, dict[str, Any]], top_level: list[str], start_filetime: int) -> bool:
    """최상위 패키지 아래 로드된 모듈 파일 중 하나라도 Max 시작 뒤 수정됐거나 지금 읽을 수 없는가(E-2).

    Python은 import한 소스를 보관하지 않으므로 디스크 해시가 메모리 코드와 같다고 보장할 수 없다. 로드되지 않은 파일은 보지 않는다.
    참이어도 추정 근거다: 삭제와 네트워크 끊김, 로드 뒤 수정과 수정 뒤 첫 로드를 구분하지
    못한다. 호출자는 로드 기억이 없을 때만 이 값을 쓰고 ``unverified``로 알릴 뿐 차단하지 않는다.
    """
    for name, row in loaded.items():
        if name.split(".", 1)[0] not in top_level or not row.get("file"):
            continue
        if row.get("missing"):
            return True
        mtime_ns = row.get("mtime_ns")
        if not isinstance(mtime_ns, int) or mtime_ns // 100 + _FILETIME_EPOCH_OFFSET > start_filetime:
            return True
    return False


def _unregistered_loaded(candidates: Any, stdlib: Any) -> list[dict[str, Any]]:
    """Max에 로드됐지만 어느 Catalog 항목에도 해당하지 않는 후보(표준 라이브러리 위치 제외). 알리기만 한다."""
    excluded = [os.path.normcase(path) for path in (stdlib or []) if isinstance(path, str) and path]

    def in_stdlib(path: str) -> bool:
        normalized = os.path.normcase(path)
        for base in excluded:
            try:
                inside = os.path.commonpath([normalized, base]) == base
            except ValueError:
                continue
            if inside and "site-packages" not in os.path.relpath(normalized, base).split(os.sep):
                return True
        return False

    rows = []
    for row in candidates if isinstance(candidates, list) else []:
        if not isinstance(row, dict) or not row.get("loaded") or row.get("registered_library_ids"):
            continue
        path = row.get("path")
        if not isinstance(path, str) or in_stdlib(path):
            continue
        rows.append({
            "name": row.get("name"),
            "path": path,
            "kind": row.get("kind"),
            "source_path": row.get("source_path"),
        })
    return sorted(rows, key=lambda item: (str(item["name"]), item["path"]))


def _check_pid(pid: Any) -> None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise LibraryError("INVALID_ARGUMENT", "expected_pid는 양의 정수 PID여야 합니다.", {"expected_pid": repr(pid)})


def _summary(entry: LibraryCatalogEntry) -> dict[str, Any]:
    data = entry.to_dict()
    item = {key: data[key] for key in _SUMMARY_FIELDS}
    item["trust"] = dict(entry.trust)
    return item


def _running(identity: MaxInstanceIdentity) -> dict[str, Any] | None:
    with _RUNNING_LOCK:
        current = _RUNNING.get(identity.key)
        return dict(current) if current is not None else None


def _release(identity: MaxInstanceIdentity) -> None:
    with _RUNNING_LOCK:
        _RUNNING.pop(identity.key, None)


def _merge_details(exc: LibraryError, extra: dict[str, Any]) -> None:
    for key, value in extra.items():
        exc.details.setdefault(key, value)


def _restart_required(
    entry: LibraryCatalogEntry, identity: MaxInstanceIdentity, state: dict[str, Any], *, module: str | None = None
) -> LibraryError:
    details = {
        "loaded_source_revision": state["loaded_source_revision"],
        "source_revision": entry.source_revision,
        "runtime_state": state["runtime_state"],
        "target_instance": identity.to_dict(),
    }
    if module is not None:
        details["module"] = module
    return LibraryError(
        "RUNTIME_RESTART_REQUIRED",
        "이 Max에 로드된 라이브러리 코드가 현재 Catalog revision과 다릅니다. reload하지 않으므로 Max 재시작이 필요합니다"
        "(해제·재등록으로는 풀리지 않습니다).",
        details,
    )


def _is_start_time(value: Any) -> bool:
    """``process_start_time`` 결과가 살아 있는 프로세스의 시작 시각(양의 정수)인지."""
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _runtime_state_of(modules: list[dict[str, Any]]) -> str:
    states = {module.get("state") for module in modules}
    if "conflict" in states:
        return "conflict"
    if states & {"missing", "import_failed"}:
        return "import_failed"
    if modules and states <= _READY_STATES:
        return "ready"
    return "not_loaded"


def _preparation_failure(runtime_state: str) -> tuple[str, str] | None:
    if runtime_state == "conflict":
        return (
            "LIBRARY_VERSION_CONFLICT",
            "같은 이름의 모듈이 등록된 import_root 밖의 경로에서 로드·발견되었습니다. 교체·reload하지 않았습니다.",
        )
    if runtime_state == "import_failed":
        return "LIBRARY_IMPORT_FAILED", "대상 Max에서 라이브러리 모듈을 찾지 못했거나 Import가 실패했습니다."
    return None


def _outcome_of(exc: BaseException) -> str:
    """Upstream 예외를 Operation 결과로 분류한다(maxmcp를 Import하지 않고 공개 속성으로 판단).

    Max가 오류로 응답했거나 Upstream이 "보내지 않음"을 확인한 경우만 ``failed``이고, 나머지는
    결과 불명(``outcome_unknown``)으로 둔다. 어느 쪽이든 재전송하지 않는다.
    """
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        details = _bridge_details(exc)
    code = getattr(exc, "code", None)
    if details.get("changed") is False:
        return "failed"
    if (
        any(cls.__name__ == "RequestOutcomeUnknown" for cls in type(exc).__mro__)
        or details.get("request_sent") is True
        or code == "BLOCKED_BY_DIALOG"
    ):
        return "outcome_unknown"
    if details.get("request_sent") is False or code in _NOT_RUN_CODES:
        return "failed"
    if hasattr(exc, "bridge_response"):
        return "failed"
    return "outcome_unknown"


def _bridge_details(exc: BaseException) -> dict[str, Any]:
    try:
        payload = json.loads(getattr(exc, "bridge_message", "") or "")
    except (TypeError, ValueError):
        return {}
    details = payload.get("details") if isinstance(payload, dict) else None
    return details if isinstance(details, dict) else {}


def _agent_error_mark() -> str:
    """호출마다 새로 만드는 구조화 오류 표식. 사용자 출력의 비슷한 글자와 구분된다."""
    return "\n<<dlel-agent-error:" + secrets.token_hex(16) + ">>"


def _take_agent_error(details: dict[str, Any], mark: str) -> dict[str, Any] | None:
    """runner 오류 details의 ``stderr`` 끝에 Max 쪽 코드가 남긴 표식을 꺼낸다.

    표식이 정확히 끝에 있고 형식이 맞을 때만 ``agent_error``를 돌려주고 표식을 ``stderr``에서 지운다. 그 밖에는
    details를 건드리지 않고 None(기존 오류 그대로)이다. traceback·출력의 글자로 추측하지 않는다.
    """
    stderr = details.get("stderr")
    if not isinstance(stderr, str):
        return None
    index = stderr.rfind(mark)
    if index < 0:
        return None
    body = stderr[index + len(mark):]
    if not body.endswith("\n") or "\n" in body[:-1]:
        return None
    try:
        info = json.loads(body)
    except ValueError:
        return None
    if not (
        isinstance(info, dict)
        and set(info) == {"code", "message", "details"}
        and isinstance(info["code"], str)
        and isinstance(info["message"], str)
        and isinstance(info["details"], dict)
    ):
        return None
    details["stderr"] = stderr[:index]
    return {"code": info["code"], "message": info["message"], "details": info["details"]}


def _unwrap_report(payload: Any, script: str) -> dict[str, Any]:
    """runner 성공 payload에서 DLEL 준비 보고를 꺼낸다. 형식이 다르면 결과를 알 수 없는 응답으로 본다."""
    if isinstance(payload, dict) and str(payload.get("status", "")).lower() == "error":
        raise LibraryError(
            str(payload.get("code") or "PYTHON_ERROR"),
            str(payload.get("error") or "Max의 Python 실행이 오류를 돌려주었습니다."),
            {**dict(payload.get("details") or {}), "error_type": payload.get("error_type")},
        )
    value = payload.get("value") if isinstance(payload, dict) else None
    report = value.get("dlel") if isinstance(value, dict) else None
    if not isinstance(report, dict) or not isinstance(report.get("modules"), list):
        raise LibraryError(
            "REQUEST_OUTCOME_UNKNOWN",
            "Max 응답에서 DLEL 준비 결과를 찾지 못했습니다. 실행되었을 수 있으니 재실행하기 전에 상태를 확인하십시오.",
            {"request_sent": True, "response": repr(payload)[:2000]},
        )
    return report


# ---------------------------------------------------------------------------
# Max 쪽 코드(Python 3.10, 표준 라이브러리만)
# ---------------------------------------------------------------------------

# 이미 로드된 모듈은 위치만 확인하고 교체·재실행하지 않는다. sys.path에는 import_root가 없고 add_path가 참일 때만
# 끝에 덧붙인다(package layout은 공용 경로라, 사전 조건을 선언한 표면은 조건 우회를 막으려고 add_path가 거짓).
_PREPARE = '''
import importlib
import importlib.util
import os
import sys


def _dlel_norm(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


# 서버(Catalog)와 Max가 같은 폴더를 다른 표기로 볼 수 있다(로컬 드라이브와 UNC, 한쪽에서만 UNC로 해석되는 매핑 드라이브).
# 문자열이 다르면 파일 ID(os.stat)로 같은 실제 폴더인지 확인한다. 없는 경로·읽을 수 없는 경로는 다른 위치로 본다
_dlel_root_stats = {}


def _dlel_root_stat(root_key):
    if root_key not in _dlel_root_stats:
        try:
            _dlel_root_stats[root_key] = os.stat(root_key)
            if not _dlel_root_stats[root_key].st_ino:
                _dlel_root_stats[root_key] = None
        except (OSError, ValueError):
            _dlel_root_stats[root_key] = None
    return _dlel_root_stats[root_key]


def _dlel_is_root(path, root_key):
    root_stat = _dlel_root_stat(root_key)
    if root_stat is None:
        return False
    try:
        return os.path.samestat(os.stat(path), root_stat)
    except (OSError, ValueError):
        return False


def _dlel_same(path, root_key):
    if not isinstance(path, str) or not path:
        return False
    try:
        candidate = _dlel_norm(path)
    except (OSError, ValueError):
        return False
    return candidate == root_key or _dlel_is_root(candidate, root_key)


def _dlel_under(path, root_key):
    if not isinstance(path, str) or not path:
        return False
    try:
        candidate = _dlel_norm(path)
    except (OSError, ValueError):
        return False
    if candidate == root_key or candidate.startswith(root_key.rstrip(os.sep) + os.sep):
        return True
    current = candidate
    while True:
        if _dlel_is_root(current, root_key):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _dlel_module_location(module):
    location = getattr(module, "__file__", None)
    if location:
        return location
    for entry in list(getattr(module, "__path__", None) or ()):
        return entry
    return None


def _dlel_spec_location(spec):
    if spec.origin and spec.origin not in ("namespace", "built-in", "frozen"):
        return spec.origin
    for entry in list(spec.submodule_search_locations or ()):
        return entry
    return None


def _dlel_error(exc):
    return {"type": type(exc).__name__, "message": str(exc) or type(exc).__name__}


def _dlel_check(name, root_key, do_import):
    item = {"name": name, "state": "missing", "origin": None}
    loaded = sys.modules.get(name)
    if loaded is not None:
        item["origin"] = _dlel_module_location(loaded)
        item["state"] = "loaded" if _dlel_under(item["origin"], root_key) else "conflict"
        return item
    try:
        spec = importlib.util.find_spec(name)
    except (Exception, SystemExit) as exc:
        item["state"] = "import_failed"
        item["error"] = _dlel_error(exc)
        return item
    if spec is None:
        item["error"] = {"type": "ModuleNotFoundError", "message": "No module named %r" % (name,)}
        return item
    item["origin"] = _dlel_spec_location(spec)
    if not _dlel_under(item["origin"], root_key):
        item["state"] = "conflict"
        return item
    if not do_import:
        item["state"] = "found"
        return item
    try:
        module = importlib.import_module(name)
    except (Exception, SystemExit) as exc:
        item["state"] = "import_failed"
        item["error"] = _dlel_error(exc)
        return item
    item["origin"] = _dlel_module_location(module)
    item["state"] = "imported" if _dlel_under(item["origin"], root_key) else "conflict"
    return item


def _dlel_prepare(import_root, names, do_import, add_path, requires=()):
    root_key = _dlel_norm(import_root)
    path_present = any(_dlel_same(entry, root_key) for entry in list(sys.path))
    # 보호 툴 사전 조건: sys.path를 바꾸거나 import하기 전에 확인하고, 미충족이면 아무것도 바꾸지 않고 멈춘다
    missing = [name for name in requires if name not in sys.modules]
    if missing:
        return {
            "max_pid": os.getpid(),
            "python": {"version": ".".join(str(part) for part in sys.version_info[:3]), "executable": sys.executable},
            "path_added": False,
            "path_present": path_present,
            "modules": [],
            "ready": False,
            "precondition_missing": missing,
        }
    path_added = False
    if not path_present and add_path:
        sys.path.append(import_root)
        path_added = True
        path_present = True
    modules = [_dlel_check(name, root_key, do_import) for name in names]
    return {
        "max_pid": os.getpid(),
        "python": {"version": ".".join(str(part) for part in sys.version_info[:3]), "executable": sys.executable},
        "path_added": path_added,
        "path_present": path_present,
        "modules": modules,
        "ready": bool(modules) and all(item["state"] in ("loaded", "imported") for item in modules),
    }
'''

_PROBE_TAIL = '''
result = {"dlel": _dlel_prepare(_DLEL_IMPORT_ROOT, _DLEL_NAMES, _DLEL_DO_IMPORT, _DLEL_ADD_PATH, _DLEL_REQUIRES)}
'''

# 사용자 코드는 Upstream 장면 파일 명령 검사가 볼 수 있게 repr 평문으로 싣고, 호출마다 새 namespace에서 실행한다
# 사용자 코드 예외에 code(str) 속성이 있으면 code·message·details를 표식과 함께 stderr(runner가 details에 담음) 끝에 남기고
# 같은 예외를 다시 던진다. runner의 undo 문맥이 그 예외를 받아 되돌려야 하므로 삼키거나 바꾸지 않는다.
_AGENT_ERROR = '''
import json as _dlel_json
import math as _dlel_math

_DLEL_AGENT_ERROR_LIMIT = 65536


def _dlel_text(value):
    try:
        return str(value)
    except Exception:
        return "<%s>" % type(value).__name__


def _dlel_jsonable(value, depth=0):
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return value if _dlel_math.isfinite(value) else repr(value)
    if depth >= 32:
        return _dlel_text(value)
    if isinstance(value, dict):
        converted = {}
        for key, item in list(value.items()):
            converted[key if isinstance(key, str) else _dlel_text(key)] = _dlel_jsonable(item, depth + 1)
        return converted
    if isinstance(value, (list, tuple)):
        return [_dlel_jsonable(item, depth + 1) for item in value]
    return _dlel_text(value)


def _dlel_agent_error(exc):
    try:
        code = getattr(exc, "code", None)
    except Exception:
        return None
    if not isinstance(code, str):
        return None
    try:
        message = getattr(exc, "message", None)
        if not isinstance(message, str):
            message = str(exc) or type(exc).__name__
    except Exception:
        message = type(exc).__name__
    try:
        details = getattr(exc, "details", None)
        details = _dlel_jsonable(details) if isinstance(details, dict) else {}
    except Exception:
        details = {}
    return {"code": str(code), "message": str(message), "details": details}


def _dlel_emit_agent_error(exc):
    try:
        info = _dlel_agent_error(exc)
        if info is None:
            return
        text = _dlel_json.dumps(info, ensure_ascii=True, allow_nan=False)
        if len(text) > _DLEL_AGENT_ERROR_LIMIT:
            info["details"] = {}
            text = _dlel_json.dumps(info, ensure_ascii=True, allow_nan=False)
        sys.stderr.write(_DLEL_AGENT_MARK + text + "\\n")
    except BaseException:
        pass
'''

_EXECUTE_TAIL = '''
_dlel_report = _dlel_prepare(_DLEL_IMPORT_ROOT, _DLEL_NAMES, True, _DLEL_ADD_PATH, _DLEL_REQUIRES)
if _dlel_report["ready"]:
    _dlel_namespace = {"__name__": "__main__"}
    try:
        exec(compile(_DLEL_USER_CODE, "<library_execute>", "exec"), _dlel_namespace)
    except BaseException as _dlel_exc:
        _dlel_emit_agent_error(_dlel_exc)
        raise
    result = {"dlel": _dlel_report, "value": _dlel_namespace.get("result")}
else:
    result = {"dlel": _dlel_report}
'''


def _adds_path(entry: LibraryCatalogEntry, requires: Sequence[str] | None) -> bool:
    """``import_root``가 없을 때 ``sys.path`` 끝에 넣을지. ``requires``가 None이 아니면 사전 조건을 선언한 항목이다."""
    if entry.layout in _NO_ADD_PATH_LAYOUTS:
        return False
    # 사전 조건을 선언한 표면(보호 툴)은 DLEL이 툴 폴더를 넣어 조건을 우회하지 않도록 충족돼도 넣지 않는다
    return not (entry.layout == SURFACE_LAYOUT and requires is not None)


def _script_header(entry: LibraryCatalogEntry, do_import: bool, requires: Sequence[str] | None = None) -> str:
    return (
        f"_DLEL_IMPORT_ROOT = {entry.import_root!r}\n"
        f"_DLEL_NAMES = {list(entry.top_level)!r}\n"
        f"_DLEL_DO_IMPORT = {do_import!r}\n"
        f"_DLEL_ADD_PATH = {_adds_path(entry, requires)!r}\n"
        f"_DLEL_REQUIRES = {list(requires or ())!r}\n"
    )


def _probe_script(entry: LibraryCatalogEntry, do_import: bool, requires: Sequence[str] | None = None) -> str:
    return _script_header(entry, do_import, requires) + _PREPARE + _PROBE_TAIL


def _execute_script(
    entry: LibraryCatalogEntry, code: str, agent_mark: str, requires: Sequence[str] | None = None
) -> str:
    return (
        _script_header(entry, True, requires)
        + f"_DLEL_USER_CODE = {code!r}\n"
        + f"_DLEL_AGENT_MARK = {agent_mark!r}\n"
        + _PREPARE
        + _AGENT_ERROR
        + _EXECUTE_TAIL
    )


__all__ = ["ATTACH_STATES", "LibraryRuntime", "PRECONDITION_UNMET", "PROBE_MODES"]
