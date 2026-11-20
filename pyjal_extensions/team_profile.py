"""팀 프로필·로컬 설정·신뢰 루트 상속·재색인.

공유하는 TOML 파일 하나(``pyjal3dsmcp-team.toml``)가 신뢰 루트와 자동 등록할 라이브러리를 정한다.

- 찾기: ``PYJAL_DLEL_TEAM_PROFILE``(비어 있지 않으면 그 경로만) → 로컬 설정의 ``team_profile`` → 없음.
- 프로필의 모든 경로는 프로필 파일 폴더 기준 상대 경로다. 절대 경로이거나 실제 위치(.. 해석·링크·junction을 따라간 결과)가
  ``anchor`` 밖이면 그 항목만 거절하고 경고한다. 알 수 없는 키·섹션은 무시한다.
- 로컬 설정은 개인 라이브러리 추가(``libraries``)와 프로필 항목 비활성화(``disabled_libraries``)만 할 수 있다.
  로컬 설정으로 신뢰 루트를 더할 수 없다.
- 신뢰 루트 아래(같은 경로 포함)에 ``source_root``가 있는 Catalog 항목은 두 승인을 루트에서 상속한다(프로필 배포 = 팀 승인).
  상속 승인은 **읽기 결과에만** 덧씌우고 Catalog에 저장하지 않는다. 그래서 루트가 사라지거나 프로필이 깨지면 상속도 사라진다.
- 프로필이 없거나 깨져도 예외를 내지 않는다. 문제는 ``warnings``로 돌려주고 지금 동작(상속·자동 등록 없음)으로 떨어진다.

프로필 ``[[preconditions]]``(``library_id``·``requires_loaded``·``message``)는 보호 툴의
사전 조건이다(형식이 틀린 항목은 무시). 서버 시작 때 신뢰 루트 아래 에이전트 표면을 발견해 등록한다(``surfaces``).
``library_attach``의 자동 활성화 대상은 신뢰 루트 아래 항목과 프로필 ``libraries`` 항목이다(``is_auto_enable_target``).

Python 수준 ``LibraryRegistry.refresh``의 trust 유지 계약은 바꾸지 않는다. 루트 밖 항목의 승인 초기화는
``refresh_library``(MCP ``library_refresh``)의 계약이다. 대상 라이브러리를 Import하거나 3ds Max에 요청하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pyjal_extensions import LibraryError
from pyjal_extensions.bindings import STORE_LOCK
from pyjal_extensions.locations import TEAM_PROFILE_ENV, local_settings_path
from pyjal_extensions.manifests import scan_source
from pyjal_extensions.registry import LibraryCatalogEntry, LibraryRegistry

PROFILE_SCHEMA_VERSION = 1
TRUST_SOURCE_EXPLICIT = "explicit"
TRUST_SOURCE_ROOT_PREFIX = "root:"

# warnings[].code
TEAM_PROFILE_NOT_FOUND = "TEAM_PROFILE_NOT_FOUND"
TEAM_PROFILE_INVALID = "TEAM_PROFILE_INVALID"
TEAM_PROFILE_PATH_REJECTED = "TEAM_PROFILE_PATH_REJECTED"
TEAM_PROFILE_LIBRARY_CONFLICT = "TEAM_PROFILE_LIBRARY_CONFLICT"
TEAM_PROFILE_LIBRARY_FAILED = "TEAM_PROFILE_LIBRARY_FAILED"
LOCAL_SETTINGS_INVALID = "LOCAL_SETTINGS_INVALID"
# 신뢰 루트 위치를 지금 읽을 수 없음(복사하지 않은 부분·네트워크 끊김). 호출마다 다시 확인한다
TEAM_TRUST_ROOT_UNAVAILABLE = "TEAM_TRUST_ROOT_UNAVAILABLE"

_ORIGIN_PROFILE = "profile"
_ORIGIN_SETTINGS = "settings"


def _warning(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def _real(path: str | os.PathLike[str]) -> str:
    """링크·junction·``..``를 해석한 비교용 경로(대소문자 정규화)."""
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _is_within(path: str, root: str) -> bool:
    """``_real`` 값끼리 비교: ``path``가 ``root``와 같거나 그 아래인가."""
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # 드라이브가 다르거나 절대·상대가 섞임
        return False


def _is_absolute_text(text: str) -> bool:
    """드라이브(``C:``)나 루트(``\\``·``/``)로 시작하면 절대 경로로 본다(``C:foo``·``\\foo`` 포함)."""
    pure = Path(text)
    return bool(pure.drive or pure.root or os.path.isabs(text))


# ---------------------------------------------------------------------------
# 해석 결과
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustRoot:
    root_id: str
    path: str  # 실제 위치(``_real``, 비교용·대소문자 정규화)
    location: str = ""  # 실제 위치(대소문자 보존). 표면 탐색은 실제 이름이 필요하다


@dataclass(frozen=True)
class Precondition:
    """보호 툴 사전 조건: Max ``sys.modules``에 미리 있어야 하는 모듈과 사용자 안내(이름으로 분기하지 않는다)."""

    library_id: str
    requires_loaded: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class LibrarySpec:
    """자동 등록 대상 하나. ``source_path``는 해석이 끝난 절대 경로다."""

    source_path: str
    library_id: str | None
    entrypoints: Any
    origin: str  # profile | settings


@dataclass
class TeamConfig:
    """한 서버 프로세스가 읽은 팀 프로필·로컬 설정. ``configured``가 거짓이면 지금 동작 그대로다."""

    configured: bool = False
    profile_path: str | None = None
    profile_sha256: str | None = None
    trust_roots: tuple[TrustRoot, ...] = ()
    libraries: tuple[LibrarySpec, ...] = ()
    disabled_libraries: frozenset[str] = frozenset()
    warnings: list[dict[str, Any]] = field(default_factory=list)
    anchor: str | None = None  # 프로필 anchor 실제 위치(``_real``). 프로필이 없거나 깨졌으면 None
    preconditions: dict[str, Precondition] = field(default_factory=dict)

    def is_auto_enable_target(self, entry: LibraryCatalogEntry) -> bool:
        """``library_attach``가 자동 활성화할 항목인가: 신뢰 루트 아래 항목 또는 프로필 ``libraries`` 항목.

        로컬 설정 개인 라이브러리·사용자가 등록한 루트 밖 항목은 대상이 아니다. 프로필 항목은 경로로 맞춘다
        (같은 ``library_id``가 다른 원본이면 프로필 항목이 아니다).
        """
        if self.trust_root_for(entry.source_root) is not None:
            return True
        source = _real(entry.source_root)
        return any(
            spec.origin == _ORIGIN_PROFILE
            and not _is_disabled(spec, spec.library_id or entry.library_id, self)
            and _real(spec.source_path) == source
            for spec in self.libraries
        )

    def trust_root_for(self, source_root: str | os.PathLike[str]) -> TrustRoot | None:
        """``source_root``를 포함하는 신뢰 루트(가장 구체적인 것). 없으면 None."""
        if not self.trust_roots:
            return None
        real = _real(source_root)
        matches = [root for root in self.trust_roots if _is_within(real, root.path)]
        return max(matches, key=lambda root: len(root.path)) if matches else None

    def trust_source_for(self, source_root: str | os.PathLike[str]) -> str:
        root = self.trust_root_for(source_root)
        return TRUST_SOURCE_ROOT_PREFIX + root.root_id if root is not None else TRUST_SOURCE_EXPLICIT

    def apply(self, entry: LibraryCatalogEntry) -> LibraryCatalogEntry:
        """신뢰 루트 아래 항목이면 두 승인을 true로 덧씌운 사본을 돌려준다. 원본 객체·Catalog는 바꾸지 않는다."""
        if self.trust_root_for(entry.source_root) is None:
            return entry
        return replace(entry, trust={"approved_for_import": True, "approved_for_execution": True})


# ---------------------------------------------------------------------------
# 로컬 설정
# ---------------------------------------------------------------------------


def read_local_settings(path: Path | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """로컬 설정 JSON을 읽는다. ``(None, None)``: 파일 없음, ``(None, 사유)``: 읽을 수 없음, ``(dict, None)``: 정상."""
    path = path or local_settings_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"로컬 설정을 읽지 못했습니다: {path}: {exc}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"로컬 설정이 올바른 JSON이 아닙니다: {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"로컬 설정의 최상위가 객체가 아닙니다: {path}"
    return data, None


def _settings_fields(data: dict[str, Any], path: Path, warnings: list[dict[str, Any]]) -> tuple[str | None, list[LibrarySpec], frozenset[str]]:
    """``team_profile``·``libraries``·``disabled_libraries``만 쓴다. 다른 키(``trust_roots`` 포함)는 무시한다."""
    team_profile = data.get("team_profile")
    if team_profile is not None and not (isinstance(team_profile, str) and team_profile.strip()):
        warnings.append(_warning(LOCAL_SETTINGS_INVALID, f"로컬 설정의 team_profile은 비어 있지 않은 문자열이어야 합니다: {path}"))
        team_profile = None

    personal: list[LibrarySpec] = []
    libraries = data.get("libraries", [])
    if not isinstance(libraries, list):
        warnings.append(_warning(LOCAL_SETTINGS_INVALID, f"로컬 설정의 libraries는 목록이어야 합니다: {path}"))
        libraries = []
    for item in libraries:
        source = item.get("source_path") if isinstance(item, dict) else None
        library_id = item.get("library_id") if isinstance(item, dict) else None
        if not (isinstance(source, str) and source.strip()) or not (library_id is None or isinstance(library_id, str)):
            warnings.append(_warning(LOCAL_SETTINGS_INVALID, f"로컬 설정 libraries 항목을 무시합니다(source_path 필요): {item!r}"))
            continue
        personal.append(LibrarySpec(os.path.abspath(source), library_id, item.get("entrypoints"), _ORIGIN_SETTINGS))

    disabled = data.get("disabled_libraries", [])
    if not (isinstance(disabled, list) and all(isinstance(name, str) for name in disabled)):
        warnings.append(_warning(LOCAL_SETTINGS_INVALID, f"로컬 설정의 disabled_libraries는 문자열 목록이어야 합니다: {path}"))
        disabled = []
    return (team_profile.strip() if team_profile else None), personal, frozenset(disabled)


# ---------------------------------------------------------------------------
# 팀 프로필
# ---------------------------------------------------------------------------


def _resolve_item_path(text: Any, base: Path, anchor: str, what: str, warnings: list[dict[str, Any]], **extra: Any) -> str | None:
    """프로필 항목 경로를 프로필 폴더 기준으로 해석한다. 거절하면 경고 하나를 남기고 None."""
    if not isinstance(text, str) or not text.strip():
        warnings.append(_warning(TEAM_PROFILE_PATH_REJECTED, f"{what}의 경로가 비어 있거나 문자열이 아닙니다: {text!r}", **extra))
        return None
    if _is_absolute_text(text):
        warnings.append(_warning(
            TEAM_PROFILE_PATH_REJECTED, f"{what}의 경로는 프로필 폴더 기준 상대 경로여야 합니다(절대 경로 거절): {text}",
            path=text, **extra,
        ))
        return None
    resolved = os.path.realpath(base / text)
    if not _is_within(os.path.normcase(resolved), anchor):
        warnings.append(_warning(
            TEAM_PROFILE_PATH_REJECTED, f"{what}의 실제 위치가 anchor 밖이라 거절합니다: {text} -> {resolved}",
            path=text, resolved=resolved, **extra,
        ))
        return None
    return resolved


@dataclass
class _Profile:
    sha256: str
    anchor: str
    trust_roots: tuple[TrustRoot, ...]
    libraries: list[LibrarySpec]
    preconditions: dict[str, Precondition]


def _load_preconditions(items: Any) -> dict[str, Precondition]:
    """``[[preconditions]]``를 읽는다. 형식이 틀린 항목은 경고 없이 무시한다(같은 ID가 여럿이면 처음 것)."""
    result: dict[str, Precondition] = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        library_id, required, message = item.get("library_id"), item.get("requires_loaded"), item.get("message")
        if not (isinstance(library_id, str) and library_id.strip() and isinstance(message, str)):
            continue
        if not (isinstance(required, list) and required and all(isinstance(n, str) and n.strip() for n in required)):
            continue
        result.setdefault(library_id, Precondition(library_id, tuple(dict.fromkeys(required)), message))
    return result


def _load_profile(path: Path, warnings: list[dict[str, Any]], raw: bytes | None = None) -> _Profile | None:
    """프로필을 읽어 신뢰 루트·라이브러리·사전 조건을 돌려준다. 프로필 전체가 쓸 수 없으면 경고를 남기고 None."""
    if raw is None and not path.is_file():
        warnings.append(_warning(TEAM_PROFILE_NOT_FOUND, f"팀 프로필 파일이 없습니다: {path}", profile=str(path)))
        return None
    try:
        raw = path.read_bytes() if raw is None else raw
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        warnings.append(_warning(TEAM_PROFILE_INVALID, f"팀 프로필을 TOML로 읽지 못했습니다: {path}: {exc}", profile=str(path)))
        return None

    def invalid(reason: str) -> None:
        warnings.append(_warning(TEAM_PROFILE_INVALID, f"팀 프로필을 쓸 수 없습니다({reason}): {path}", profile=str(path)))

    schema_version = data.get("schema_version")
    if type(schema_version) is not int or schema_version != PROFILE_SCHEMA_VERSION:
        invalid(f"schema_version은 {PROFILE_SCHEMA_VERSION}이어야 합니다")
        return None
    anchor_text = data.get("anchor")
    if not isinstance(anchor_text, str) or not anchor_text.strip():
        invalid("anchor가 없습니다")
        return None
    if _is_absolute_text(anchor_text):
        invalid("anchor는 프로필 폴더 기준 상대 경로여야 합니다")
        return None
    trust_items = data.get("trust_roots", [])
    library_items = data.get("libraries", [])
    if not isinstance(trust_items, list) or not isinstance(library_items, list):
        invalid("trust_roots·libraries는 테이블 배열이어야 합니다")
        return None

    base = path.parent
    anchor = _real(base / anchor_text)

    roots: list[TrustRoot] = []
    for item in trust_items:
        root_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(root_id, str) or not root_id.strip():
            warnings.append(_warning(TEAM_PROFILE_PATH_REJECTED, f"신뢰 루트 항목에 id가 없어 거절합니다: {item!r}"))
            continue
        resolved = _resolve_item_path(item.get("path"), base, anchor, f"신뢰 루트 {root_id}", warnings, trust_root=root_id)
        if resolved is not None:
            roots.append(TrustRoot(root_id, os.path.normcase(resolved), resolved))

    libraries: list[LibrarySpec] = []
    for item in library_items:
        if not isinstance(item, dict):
            warnings.append(_warning(TEAM_PROFILE_PATH_REJECTED, f"libraries 항목이 테이블이 아니어서 거절합니다: {item!r}"))
            continue
        library_id = item.get("library_id")
        if library_id is not None and not isinstance(library_id, str):
            warnings.append(_warning(TEAM_PROFILE_LIBRARY_FAILED, f"library_id는 문자열이어야 합니다: {library_id!r}", library_id=None))
            continue
        resolved = _resolve_item_path(
            item.get("source_path"), base, anchor, f"라이브러리 {library_id or item.get('source_path')}", warnings,
            library_id=library_id,
        )
        if resolved is not None:
            libraries.append(LibrarySpec(resolved, library_id, item.get("entrypoints"), _ORIGIN_PROFILE))
    return _Profile(hashlib.sha256(raw).hexdigest(), anchor, tuple(roots), libraries, _load_preconditions(data.get("preconditions")))


def load_team_config(environ: dict[str, str] | None = None, settings_path: Path | None = None) -> TeamConfig:
    """환경 변수·로컬 설정·팀 프로필을 읽는다. 예외를 내지 않고 문제는 ``warnings``에 담는다."""
    environ = os.environ if environ is None else environ
    settings_path = settings_path or local_settings_path()
    warnings: list[dict[str, Any]] = []

    settings, settings_error = read_local_settings(settings_path)
    settings_present = settings is not None or settings_error is not None
    recorded_profile: str | None = None
    personal: list[LibrarySpec] = []
    disabled: frozenset[str] = frozenset()
    if settings_error is not None:
        warnings.append(_warning(LOCAL_SETTINGS_INVALID, settings_error, settings=str(settings_path)))
    elif settings is not None:
        recorded_profile, personal, disabled = _settings_fields(settings, settings_path, warnings)

    explicit = environ.get(TEAM_PROFILE_ENV, "").strip()
    profile_text = explicit or recorded_profile
    config = TeamConfig(configured=bool(explicit) or settings_present, disabled_libraries=disabled)
    profile_libraries: list[LibrarySpec] = []
    if profile_text:
        profile_path = Path(os.path.abspath(profile_text))
        config.profile_path = str(profile_path)
        loaded = _load_profile(profile_path, warnings)
        if loaded is not None:
            config.profile_sha256 = loaded.sha256
            config.trust_roots, profile_libraries = loaded.trust_roots, loaded.libraries
            config.anchor, config.preconditions = loaded.anchor, loaded.preconditions
    config.libraries = tuple(profile_libraries + personal)
    config.warnings = warnings
    return config


# ---------------------------------------------------------------------------
# Catalog 반영
# ---------------------------------------------------------------------------


def sync_team_libraries(registry: LibraryRegistry, config: TeamConfig) -> list[dict[str, Any]]:
    """프로필·개인 라이브러리 중 Catalog에 없는 것을 등록한다(정적 분석만). 멱등이며 항목별 문제는 경고로 돌려준다.

    같은 ``library_id``가 같은 ``source_root``로 이미 있으면 그대로 쓰고, 다른 ``source_root``면 덮어쓰지 않고 경고한다.
    """
    warnings: list[dict[str, Any]] = []
    if not config.libraries:
        return warnings
    with STORE_LOCK:
        try:
            catalog = {entry.library_id: entry for entry in registry.list_libraries()}
        except LibraryError as exc:
            return [_warning(TEAM_PROFILE_LIBRARY_FAILED, f"Catalog를 읽지 못해 자동 등록을 건너뜁니다: {exc}", library_id=None)]
        for spec in config.libraries:
            if _is_disabled(spec, spec.library_id, config):
                continue
            try:
                scan = scan_source(spec.source_path)
                library_id = spec.library_id or scan.package
                if library_id is None:
                    raise LibraryError("INVALID_LIBRARY_ID", "패키지 이름을 정할 수 없어 library_id가 필요합니다.")
                if _is_disabled(spec, library_id, config):
                    continue
                existing = catalog.get(library_id)
                if existing is not None:
                    if _real(existing.source_root) != _real(scan.source_root):
                        warnings.append(_warning(
                            TEAM_PROFILE_LIBRARY_CONFLICT,
                            f"같은 library_id가 다른 원본으로 이미 등록되어 있어 덮어쓰지 않습니다: {library_id} "
                            f"(등록: {existing.source_root}, 프로필: {scan.source_root})",
                            library_id=library_id, source_root=existing.source_root, source_path=spec.source_path,
                        ))
                    continue
                catalog[library_id] = registry.register(spec.source_path, library_id=library_id, entrypoints=spec.entrypoints)
            except (LibraryError, OSError) as exc:
                warnings.append(_warning(
                    TEAM_PROFILE_LIBRARY_FAILED, f"자동 등록에 실패했습니다: {spec.source_path}: {exc}",
                    library_id=spec.library_id, source_path=spec.source_path,
                    **({"error_code": exc.code} if isinstance(exc, LibraryError) else {}),
                ))
    return warnings


def _is_disabled(spec: LibrarySpec, library_id: str | None, config: TeamConfig) -> bool:
    return spec.origin == _ORIGIN_PROFILE and library_id is not None and library_id in config.disabled_libraries


def trust_root_warnings(config: TeamConfig) -> list[dict[str, Any]]:
    """지금 읽을 수 없는 신뢰 루트마다 ``TEAM_TRUST_ROOT_UNAVAILABLE`` 경고 하나(Tool 호출마다 다시 확인).

    배포 복사본에서 복사하지 않은 부분이거나 네트워크 공유가 끊긴 경우다. Catalog 항목·Binding은 지우지 않으며 위치가
    돌아오면 경고가 사라지고 항목은 다음 확인에서 회복한다(서버 재시작 불필요).
    """
    warnings = []
    for root in config.trust_roots:
        location = root.location or root.path
        if not os.path.isdir(location):
            warnings.append(_warning(
                TEAM_TRUST_ROOT_UNAVAILABLE,
                f"신뢰 루트 {root.root_id}의 위치를 지금 읽을 수 없습니다(복사하지 않았거나 네트워크 공유가 끊김): {location}",
                trust_root=root.root_id, path=location,
            ))
    return warnings


def reindex_trusted(registry: LibraryRegistry, config: TeamConfig) -> None:
    """신뢰 루트 아래 항목 중 원본이 바뀐 것을 다시 인덱싱한다(저장된 승인·Binding 유지). 루트 밖 항목은 건드리지 않는다."""
    if not config.trust_roots:
        return
    with STORE_LOCK:
        for entry in registry.list_libraries():
            if config.trust_root_for(entry.source_root) is None:
                continue
            if registry.check(entry.library_id).status != "stale":
                continue
            try:
                registry.refresh(entry.library_id)
            except LibraryError:
                pass  # 원본이 사라지는 등: refresh가 invalid로 저장했고 사전 검사가 따로 거절한다


def refresh_library(registry: LibraryRegistry, config: TeamConfig, library_id: str) -> dict[str, Any]:
    """해제 없이 재색인한다. 루트 밖 항목은 원본이 바뀌었으면 저장된 승인을 초기화한다(``trust_reset``)."""
    with STORE_LOCK:
        previous = registry.get(library_id)
        refreshed = registry.refresh(library_id)
        changed = refreshed.source_revision != previous.source_revision
        trust_reset = changed and config.trust_root_for(refreshed.source_root) is None
        if trust_reset:
            refreshed = registry.set_trust(library_id, approved_for_import=False, approved_for_execution=False)
        shown = config.apply(refreshed)
        return {
            "library_id": library_id,
            "previous_source_revision": previous.source_revision,
            "source_revision": shown.source_revision,
            "changed": changed,
            "status": shown.status,
            "reported_version": shown.reported_version,
            "trust": dict(shown.trust),
            "trust_source": config.trust_source_for(shown.source_root),
            "trust_reset": trust_reset,
        }


class TeamRegistry(LibraryRegistry):
    """신뢰 루트 상속 승인을 공개 조회 결과에만 덧씌우는 Registry.

    기반 클래스의 쓰기 경로(``refresh``·``check``·``set_trust``)는 내부 ``_entry``로 저장된 승인을 읽어 쓰므로
    상속 승인이 Catalog에 저장되지 않는다.
    """

    def __init__(self, catalog_dir: str | os.PathLike[str], config: TeamConfig) -> None:
        super().__init__(catalog_dir)
        self._team = config

    @property
    def team(self) -> TeamConfig:
        return self._team

    def register(self, *args: Any, **kwargs: Any) -> LibraryCatalogEntry:
        return self._team.apply(super().register(*args, **kwargs))

    def register_surface(self, *args: Any, **kwargs: Any) -> LibraryCatalogEntry:
        return self._team.apply(super().register_surface(*args, **kwargs))

    def get(self, library_id: str) -> LibraryCatalogEntry:
        return self._team.apply(super().get(library_id))

    def list_libraries(self) -> list[LibraryCatalogEntry]:
        return [self._team.apply(entry) for entry in super().list_libraries()]

    def check(self, library_id: str) -> LibraryCatalogEntry:
        return self._team.apply(super().check(library_id))

    def refresh(self, library_id: str) -> LibraryCatalogEntry:
        return self._team.apply(super().refresh(library_id))

    def set_trust(self, library_id: str, **kwargs: Any) -> LibraryCatalogEntry:
        return self._team.apply(super().set_trust(library_id, **kwargs))


# ---------------------------------------------------------------------------
# 서버 프로세스 상태(서버 시작 또는 첫 library_* 호출 때 한 번)
# ---------------------------------------------------------------------------


@dataclass
class TeamState:
    config: TeamConfig
    warnings: list[dict[str, Any]]

    def copy_warnings(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.warnings]


_STATES: dict[tuple[str, str, str], TeamState] = {}
_STATES_LOCK = threading.Lock()


def team_state(catalog_dir: str | os.PathLike[str]) -> TeamState:
    """이 프로세스에서 처음 부를 때 프로필을 읽고 자동 등록한다. 이후에는 같은 결과를 쓴다(프로필 변경은 서버 재시작 때 반영).

    Catalog 위치·환경 변수·로컬 설정 위치가 다르면 따로 계산한다.
    """
    key = (
        str(Path(catalog_dir).resolve()),
        os.environ.get(TEAM_PROFILE_ENV, "").strip(),
        str(local_settings_path()),
    )
    with _STATES_LOCK:
        state = _STATES.get(key)
        if state is None:
            from pyjal_extensions.surfaces import sync_agent_surfaces  # 순환 import 방지

            config = load_team_config()
            registry = LibraryRegistry(catalog_dir)
            warnings = list(config.warnings) + sync_team_libraries(registry, config)
            # 서버 시작(첫 library_* 호출) 때 신뢰 루트 아래 에이전트 표면을 발견해 등록한다(멱등)
            warnings += sync_agent_surfaces(registry, config)[1]
            state = TeamState(config, warnings)
            _STATES[key] = state
        return state


__all__ = [
    "LOCAL_SETTINGS_INVALID",
    "TEAM_PROFILE_INVALID",
    "TEAM_PROFILE_LIBRARY_CONFLICT",
    "TEAM_PROFILE_LIBRARY_FAILED",
    "TEAM_PROFILE_NOT_FOUND",
    "TEAM_PROFILE_PATH_REJECTED",
    "TEAM_TRUST_ROOT_UNAVAILABLE",
    "TRUST_SOURCE_EXPLICIT",
    "LibrarySpec",
    "Precondition",
    "TeamConfig",
    "TeamRegistry",
    "TeamState",
    "TrustRoot",
    "load_team_config",
    "read_local_settings",
    "refresh_library",
    "reindex_trusted",
    "sync_team_libraries",
    "team_state",
    "trust_root_warnings",
]
