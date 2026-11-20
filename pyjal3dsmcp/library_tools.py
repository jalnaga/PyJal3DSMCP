"""DLEL Library의 고정 MCP Tool 12개와 에이전트 가이드 resource·안내 prompt.

``pyjal_extensions``의 ``LibraryRegistry``(Catalog)와 ``LibraryRuntime``(인스턴스별 Binding·Runtime)을 얇게 감싸
Upstream 서버(``upstream.load_server().mcp``)에 등록한다. 결과 envelope는 Upstream의 공통 structured wrapper가 만들고,
``LibraryError``와 Upstream 예외는 그대로 올려 보내 wrapper가 ``error.code``·``details``·``hint``를 옮기게 한다.
라이브러리 API를 Tool로 동적 등록하지 않는다.

3ds Max로 가는 요청은 ``library_scan``·``library_attach``·``library_probe``·``library_execute``이며 ``upstream.run_python_in_max``(Upstream Python 실행
경로를 대상 PID 보호 안에서 재사용)로만 나간다. 자체 transport를 만들거나 다른 MCP Tool을 MCP로 다시
호출하지 않으며 안전 모드 설정을 바꾸지 않는다.

팀 프로필: 모든 ``library_*`` Tool은 처음 불릴 때 ``team_profile.team_state``로 프로필을
읽고 프로필 라이브러리를 자동 등록한다. 신뢰 루트 상속 승인은 ``TeamRegistry``가 조회 결과에만 덧씌우며 Catalog에 저장하지 않는다.
``library_list``·``library_search``·``library_describe``는 결과를 만들기 전에 신뢰 루트 항목만 자동 재색인한다.

인스턴스 연결·에이전트 표면: 서버 시작 때와 매 ``library_attach`` 때 신뢰 루트 아래 에이전트 표면을
발견해 등록한다(``surfaces``). ``library_attach``는 대상 Max를 읽기 전용으로 한 번 조사해 항목별 상태를 분류하고 신뢰 루트·프로필
항목을 자동 활성화한다. 프로필 사전 조건은 ``LibraryRuntime``에 넘겨 probe·execute·attach가 Max 안에서 확인한다.

Tool의 이름·인자·docstring·annotation은 외부 계약이다.
annotation은 Upstream의 이름 집합에 없으므로 등록할 때 직접 지정한다.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pyjal_extensions import surfaces, team_profile
from pyjal_extensions.bindings import STORE_LOCK
from pyjal_extensions.locations import default_catalog_dir
from pyjal_extensions.registry import LibraryRegistry
from pyjal_extensions.runtime import LibraryRuntime

from . import upstream

# Upstream 서버 객체. 프로필 고정은 진입점(server.py)이 이 모듈을 불러오기 전에 끝낸다.
mcp = upstream.load_server().mcp

GUIDE_RESOURCE_URI = "resource://pyjal3dsmcp/agent-guide"
_GUIDE_NAME = "DLEL-AGENT-GUIDE.md"


def _team() -> team_profile.TeamState:
    # 서버 프로세스에서 처음 부를 때 프로필을 읽고 자동 등록한다(어느 library_* Tool이 처음이어도 같다).
    return team_profile.team_state(default_catalog_dir())


def _registry() -> LibraryRegistry:
    # Catalog는 디스크가 정본이므로 호출마다 위치를 해석해 새로 연다. 상속 승인은 읽기 결과에만 덧씌운다.
    return team_profile.TeamRegistry(default_catalog_dir(), _team().config)


def _reindex_trusted() -> team_profile.TeamState:
    """신뢰 루트 아래 항목 중 원본이 바뀐 것을 결과를 만들기 전에 재색인한다(루트 밖 항목은 그대로)."""
    state = _team()
    team_profile.reindex_trusted(LibraryRegistry(default_catalog_dir()), state.config)
    return state


def _runtime() -> LibraryRuntime:
    return LibraryRuntime(
        _registry(),
        run_python=upstream.run_python_in_max,
        process_start_time=upstream.process_start_time,
        preconditions=_team().config.preconditions,
    )


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def library_scan(expected_pid: int) -> dict[str, Any]:
    """대상 Max의 sys.path 직계 항목과 sys.modules에서 라이브러리 후보를 발견한다.

    서버 가상환경 대신 expected_pid의 Max를 조사하며 import·실행·자동 등록·경로 변경은 하지 않는다.
    경로별 발견 근거, sys.path 순서, 로드 여부, 기존 등록 ID, 등록 가능한 source_path와
    미지원 사유 및 부분 실패 warnings를 반환한다. 후보를 선택한 뒤 library_register로 등록한다.
    단일 파일·native·namespace·ZIP은 발견하되 단독 등록 경로를 제공하지 않는다.
    Upstream Python bridge를 사용하므로 safe_mode=false가 필요하며 설정을 자동 변경하지 않는다.
    PID·시작 시각 검증과 기존 bridge 오류를 보존하며 실패한 요청을 자동 재전송하지 않는다.
    """
    return _runtime().scan(expected_pid)


@mcp.tool()
def library_register(
    source_path: str,
    library_id: str | None = None,
    entrypoints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Register a local Python package or module directory in the DLEL library catalog.

    Static AST analysis only: the library is never imported, installed, built or executed,
    no file is written into the source tree, and 3ds Max is not contacted.
    source_path is a source tree root (src or flat layout), or one installed package directory
    (the folder with __init__.py, e.g. site-packages/<pkg>): that registers only this package
    (layout "package", import_root = its parent, version from the matching *.dist-info METADATA).
    Do not pass a whole site-packages folder. library_id defaults to the detected package name.
    entrypoints are optional adapter
    factories: [{"id", "module", "factory", "returns"?}] (factory is "func" or "Class.method").
    Returns the catalog entry. Errors: SOURCE_NOT_FOUND, SOURCE_LAYOUT_UNRECOGNIZED,
    INVALID_LIBRARY_ID, LIBRARY_ALREADY_REGISTERED, SOURCE_PATH_ESCAPE, INVALID_ENTRYPOINT.
    """
    entry = _registry().register(source_path, library_id=library_id, entrypoints=entrypoints)
    return entry.to_dict()


@mcp.tool(annotations={"destructiveHint": True})
def library_unregister(library_id: str) -> dict[str, Any]:
    """Remove a library from the DLEL catalog with its manifest, API index, recipes and instance bindings.

    The registered source files are never deleted and 3ds Max is not contacted. Disable the library
    first on every running 3ds Max where it is enabled. Errors: LIBRARY_NOT_REGISTERED, LIBRARY_IN_USE.
    """
    return _runtime().unregister(library_id)


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def library_list(expected_pid: int | None = None) -> dict[str, Any]:
    """List registered DLEL libraries (catalog summary with trust, sorted by library_id).

    Without expected_pid only the catalog is read. With expected_pid, each item also shows
    that 3ds Max instance's binding (enabled/disabled), runtime_state, loaded_source_revision and
    last_operation, plus target_instance (pid, process start time, bridge pipe). A restarted Max or a
    reused PID is a new instance without bindings. 3ds Max is never contacted.
    trust_source is "root:<id>" when trust is inherited from a team profile trust root (the team's
    deployment approval) or "explicit" for approvals stored with library_set_enabled. Team profile
    libraries are registered automatically, libraries under a trust root are re-indexed when their
    source changes, and other libraries keep their index until library_refresh. Team profile problems
    are reported in warnings.
    Errors: INVALID_ARGUMENT, TARGET_UNAVAILABLE.
    """
    state = _reindex_trusted()
    result = _runtime().list_libraries(expected_pid)
    surface_of = {entry.library_id: entry.surface for entry in LibraryRegistry(default_catalog_dir()).list_libraries()}
    for item in result["libraries"]:
        item["trust_source"] = state.config.trust_source_for(item["source_root"])
        item["surface"] = surface_of.get(item["library_id"])
    if state.config.configured:
        # 프로필·로컬 설정이 설정되지 않았으면 지금 결과 그대로(warnings 키 없음)
        result["warnings"] = state.copy_warnings() + team_profile.trust_root_warnings(state.config)
    return result


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def library_search(query: str, library_id: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Search registered library APIs by terms (case-insensitive name/module/docstring match).

    APIs of agent surfaces (agentapi packages) come first and are marked surface=true.
    Returns compact summaries only (signature and first docstring line); call
    library_describe with an api_id for the full record and source. limit is capped at 50.
    library_id restricts the search to one library; omit it to search the whole catalog.
    Errors: INVALID_ARGUMENT (empty query, limit < 1), LIBRARY_NOT_REGISTERED.
    """
    _reindex_trusted()
    return _registry().search(query, library_id=library_id, limit=limit).to_dict()


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def library_describe(api_id: str) -> dict[str, Any]:
    """Describe one library API: exact signature, docstring, source, access paths, recipes and revision.

    Use an api_id from library_search. Error: API_NOT_FOUND.
    """
    _reindex_trusted()
    return _registry().describe(api_id).to_dict()


@mcp.tool(annotations={"idempotentHint": True})
def library_set_enabled(
    library_id: str,
    enabled: bool,
    expected_pid: int,
    approve_import: bool | None = None,
    approve_execution: bool | None = None,
) -> dict[str, Any]:
    """Enable or disable a registered library for one 3ds Max instance (expected_pid is required).

    Bindings are per Max instance; other instances are not changed. Nothing is sent to 3ds Max:
    enabled=true does not import the library, and enabled=false never unloads modules already
    imported in Max. Two libraries with the same top-level module cannot be enabled together in one
    instance. Disabling while a managed call runs lets that call finish and refuses new calls.
    approve_import and approve_execution store the library's trust approval in the catalog (null keeps
    it, false revokes it). A library under a trust root of the active team profile inherits both
    approvals (trust_source "root:<id>", see library_list) and needs no approve_* call. For any other
    library, pass them only when the user has given an explicit instruction to approve importing or
    executing this library; never approve on your own.
    Errors: LIBRARY_NOT_REGISTERED, TARGET_UNAVAILABLE, LIBRARY_VERSION_CONFLICT, INVALID_ARGUMENT.
    """
    return _runtime().set_enabled(
        library_id,
        enabled,
        expected_pid,
        approve_import=approve_import,
        approve_execution=approve_execution,
    )


@mcp.tool()
def library_probe(
    library_id: str,
    expected_pid: int,
    mode: Literal["find_spec", "import"] = "find_spec",
) -> dict[str, Any]:
    """Check that an enabled library resolves from its registered path inside one 3ds Max instance.

    Adds at most the library's registered import root to the end of Max's sys.path (existing entries are
    never reordered), then checks each top-level module. An installed package (layout "package") never
    adds its shared parent folder: if Max lacks that path, path_status is "not_added" and a module not
    already loaded is reported missing. An agent surface (agentapi) without a team profile precondition
    appends its import_root (the tool folder). A surface whose team profile declares [[preconditions]]
    never adds a path: unless the required modules are already loaded (the user opened the tool), it
    stops with LIBRARY_PRECONDITION_UNMET before changing sys.path or importing anything.
    mode="find_spec" locates modules without running their code. mode="import" imports them,
    which runs module top-level code and can have side effects; it needs approved_for_import.
    A module already loaded from another path is reported as a conflict and never replaced or
    reloaded. Runs through execute_python, so the bridge needs safe_mode=false.
    Errors: LIBRARY_DISABLED, LIBRARY_NOT_APPROVED, LIBRARY_PRECONDITION_UNMET, LIBRARY_STALE,
    LIBRARY_IMPORT_FAILED, LIBRARY_VERSION_CONFLICT, RUNTIME_RESTART_REQUIRED, TARGET_MISMATCH,
    OPERATION_IN_PROGRESS, and bridge errors such as SAFE_MODE or BLOCKED_BY_DIALOG.
    """
    return _runtime().probe(library_id, expected_pid, mode=mode)


@mcp.tool()
def library_execute(
    library_id: str,
    code: str,
    expected_pid: int,
    expected_source_revision: str,
    operation_id: str,
) -> dict[str, Any]:
    """Run Python that uses an enabled, approved library in one 3ds Max instance (managed execute_python).

    Before sending, checks registration, the binding, approved_for_import and approved_for_execution,
    the target instance and expected_source_revision (see library_list). The library is prepared like
    library_probe(mode="import"), then the code runs once in a fresh namespace on Max's main thread with
    undo, like execute_python (assign `result` to return a JSON-compatible value). Requires bridge
    safe_mode=false. A failed or lost call is never resent: after REQUEST_OUTCOME_UNKNOWN, MAX_BUSY or
    BLOCKED_BY_DIALOG inspect the scene before retrying. operation_id is a tracing label recorded in
    library_list; it does not prevent duplicate runs. One managed call runs per Max instance at a time.
    On PYTHON_ERROR, when the raised exception has a str code attribute, error.details.agent_error also
    holds {code, message, details}; classify the failure by it before reading the traceback.
    Errors: LIBRARY_DISABLED, LIBRARY_NOT_APPROVED, LIBRARY_PRECONDITION_UNMET, LIBRARY_STALE,
    LIBRARY_IMPORT_FAILED, LIBRARY_VERSION_CONFLICT, RUNTIME_RESTART_REQUIRED, TARGET_MISMATCH,
    OPERATION_IN_PROGRESS, PYTHON_ERROR, UNSAVED_CHANGES and bridge errors.
    """
    return _runtime().execute(library_id, code, expected_pid, expected_source_revision, operation_id)


@mcp.tool(annotations={"idempotentHint": True})
def library_refresh(library_id: str) -> dict[str, Any]:
    """Re-index one registered library from its source without unregistering it; 3ds Max is not contacted.

    Rebuilds the static API index like library_register and returns previous_source_revision,
    source_revision, changed, trust, trust_source and trust_reset. A library under a trust root of the
    team profile keeps the approval inherited from that root (trust_source "root:<id>"). For any other
    library a source change resets the approvals stored with library_set_enabled (trust_reset=true):
    ask the user again before approving. Code already imported in a running 3ds Max is not replaced;
    managed calls may then need a Max restart (RUNTIME_RESTART_REQUIRED).
    Errors: LIBRARY_NOT_REGISTERED, SOURCE_NOT_FOUND.
    """
    state = _team()
    return team_profile.refresh_library(LibraryRegistry(default_catalog_dir()), state.config, library_id)


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def library_diagnose_surfaces() -> dict[str, Any]:
    """Max 연결과 등록 변경 없이 신뢰 루트에서 발견한 surface의 경로와 등록 상태를 비교한다."""
    return surfaces.diagnose_surfaces(default_catalog_dir(), team_profile.load_team_config())


@mcp.tool(annotations={"idempotentHint": True})
def library_attach(expected_pid: int) -> dict[str, Any]:
    """Attach to one 3ds Max instance: classify every catalog library for it and auto-enable team libraries.

    expected_pid is required. Sends one read-only inspection to that Max: it reads sys.path, sys.modules and
    loaded file times, never imports anything and never changes sys.path. First re-indexes libraries under
    a team profile trust root and registers agent surfaces found there (agentapi packages and top-level
    <name>_agentapi packages; entry scripts and launchers are not indexed); new IDs are in auto_registered.
    Promoted old registrations appear once in auto_migrated.
    Each library gets attach_state: current (loaded and confirmed up to date), not_loaded, restart_required
    (confirmed: the revision DLEL remembers loading into this Max differs from the catalog; restart Max, never
    reload), unverified (loaded by a startup script without DLEL memory and its files changed after Max
    started or cannot be read now; not blocked, a LOADED_CODE_UNVERIFIED warning is added and a Max restart
    may be recommended), conflict (same module loaded from another path), precondition_unmet (a protected
    tool must be opened by the user first; see precondition.message) or unavailable (catalog stale or
    invalid). A team trust root location that cannot be read now (not copied, network share offline) is
    reported as TEAM_TRUST_ROOT_UNAVAILABLE in warnings and recovers when it returns. Libraries under a trust root and team
    profile libraries are enabled for this instance (nothing is sent to Max; a top-level module clash is
    reported in enable_error); other libraries are never enabled or approved. unregistered_loaded lists
    loaded modules without a catalog entry (not registered). Uses the Python bridge, so safe_mode=false is
    required; bridge errors are returned unchanged and nothing is resent or changed.
    Errors: INVALID_ARGUMENT, TARGET_UNAVAILABLE, TARGET_MISMATCH and bridge errors.
    """
    runtime = _runtime()
    runtime.identify(expected_pid)  # 대상이 없으면 재색인·발견 전에 거절(전송 없음)
    state = _reindex_trusted()
    registered, state.surface_warnings = surfaces.sync_agent_surfaces(
        LibraryRegistry(default_catalog_dir()), state.config, state.pending_migrations
    )
    result = runtime.attach(expected_pid, auto_enable=state.config.is_auto_enable_target)
    source_root_of = {entry.library_id: entry.source_root for entry in LibraryRegistry(default_catalog_dir()).list_libraries()}
    for item in result["libraries"]:
        item["trust_source"] = state.config.trust_source_for(source_root_of[item["library_id"]])
    warnings = state.copy_warnings() + team_profile.trust_root_warnings(state.config)
    warnings += result.get("warnings", [])  # LOADED_CODE_UNVERIFIED(항목마다 하나)
    result["team_profile"] = {"path": state.config.profile_path, "sha256": state.config.profile_sha256}
    with STORE_LOCK:
        result["auto_migrated"] = sorted(state.pending_migrations)
        state.pending_migrations.clear()
    result["auto_registered"] = registered
    result["warnings"] = warnings
    return result


def _find_guide_file() -> Path:
    """에이전트 가이드 위치: 패키지 안 사본(wheel 설치)을 먼저, 저장소 ``docs/pyjal``(checkout)을 다음으로 찾는다."""
    package_dir = Path(__file__).resolve().parent
    packaged = package_dir / "docs" / _GUIDE_NAME
    if packaged.is_file():
        return packaged
    return package_dir.parent / "docs" / "pyjal" / _GUIDE_NAME


@lru_cache(maxsize=1)
def _read_guide_file() -> str:
    """가이드 문서를 한 번 읽어 캐시한다. 읽지 못하면 서버를 멈추지 않고 그 사실을 본문으로 돌려준다."""
    path = _find_guide_file()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"DLEL agent guide could not be loaded from {path}: {exc}"


@mcp.resource(GUIDE_RESOURCE_URI)
def get_dlel_agent_guide() -> str:
    """PyJal3DSMCP library tools (DLEL) guide for AI agents, exposed as an MCP resource."""
    return _read_guide_file()


@mcp.prompt()
def pyjal_library_guide() -> str:
    """Where to start when a task needs a registered Python library (library_* tools)."""
    return (
        f"For specialist work with registered Python libraries (library_* tools), read {GUIDE_RESOURCE_URI} first; "
        "general 3ds Max work still uses the dedicated tools.\n"
    )
