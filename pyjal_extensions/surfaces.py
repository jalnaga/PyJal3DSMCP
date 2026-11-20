"""에이전트 표면 발견·자동 등록.

활성 팀 프로필의 **신뢰 루트 아래** 파일 트리를 파일 시스템만으로 훑는다(Import·원본 쓰기 없음).

- 규칙 (a): 조상이 모두 ``__init__.py``를 가진 패키지 안의 ``agentapi`` 하위 패키지(깊이 무관, src layout 포함).
- 규칙 (b): 최상위 패키지 ``<name>_agentapi``(부모 폴더에 ``__init__.py`` 없음).
- 표면이 아닌 것: 패키지 밖 최상위 ``agentapi``, namespace 조상 아래 ``agentapi``, ``agentapi.py`` 모듈, 패키지 안의 ``*_agentapi``.

표면 디렉터리가 신뢰 루트 기준 ``SURFACE_DEPTH_LIMIT``단계 이하일 때만 발견한다. 링크·junction은 실제 위치가
프로필 anchor 안일 때만 따라가며(같은 실제 디렉터리는 한 번만) 시간·디렉터리 수 상한을 넘으면 그 루트의 탐색을 멈추고 경고한다.

표면 항목의 ``library_id``는 표면 모듈 점 경로의 소문자다(ID 규칙에 맞지 않으면 등록하지 않음). 같은 ID가 같은 표면
디렉터리로 이미 있으면 표면 등록을 갱신하고, 다른 원본이면 덮어쓰지 않고 ``AGENT_SURFACE_CONFLICT`` 경고를 낸다.
"""

from __future__ import annotations

import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyjal_extensions import LibraryError
from pyjal_extensions.bindings import STORE_LOCK
from pyjal_extensions.registry import LibraryCatalogEntry, LibraryRegistry
from pyjal_extensions.team_profile import TeamConfig, TrustRoot

SURFACE_PACKAGE_NAME = "agentapi"
SURFACE_PACKAGE_SUFFIX = "_agentapi"
SURFACE_DEPTH_LIMIT = 8
SCAN_TIME_LIMIT_SECONDS = 20.0
SCAN_DIRECTORY_LIMIT = 50_000

# warnings[].code
AGENT_SURFACE_CONFLICT = "AGENT_SURFACE_CONFLICT"
AGENT_SURFACE_FAILED = "AGENT_SURFACE_FAILED"
AGENT_SURFACE_SCAN_LIMIT = "AGENT_SURFACE_SCAN_LIMIT"

_LIBRARY_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


@dataclass(frozen=True)
class AgentSurface:
    library_id: str
    module: str  # 표면 모듈 점 경로(예: pyjallib.max.agentapi)
    surface_dir: str


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _is_package(directory: Path) -> bool:
    return (directory / "__init__.py").is_file()


def surface_module(directory: Path) -> str | None:
    """``directory``가 표면 규칙 (a)·(b)에 맞으면 표면 모듈 점 경로, 아니면 None."""
    name = directory.name
    if not name.isidentifier() or not _is_package(directory):
        return None
    if name == SURFACE_PACKAGE_NAME:
        parts = [name]
        parent = directory.parent
        while _is_package(parent):
            if not parent.name.isidentifier():
                return None
            parts.append(parent.name)
            if parent.parent == parent:
                break
            parent = parent.parent
        return ".".join(reversed(parts)) if len(parts) > 1 else None
    if name.endswith(SURFACE_PACKAGE_SUFFIX) and name != SURFACE_PACKAGE_SUFFIX:
        return None if _is_package(directory.parent) else name
    return None


def find_agent_surfaces(config: TeamConfig) -> tuple[list[AgentSurface], list[dict[str, Any]]]:
    """신뢰 루트 아래 표면을 찾는다(루트는 프로필 순서, 루트 안은 경로 순서). 같은 실제 디렉터리는 한 번만."""
    surfaces: list[AgentSurface] = []
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in config.trust_roots:
        found, warning = _walk_root(root, config.anchor)
        if warning is not None:
            warnings.append(warning)
        for surface in found:
            key = _real(surface.surface_dir)
            if key not in seen and config.trust_root_for(surface.surface_dir) is not None:
                seen.add(key)
                surfaces.append(surface)
    return surfaces, warnings


def _walk_root(root: TrustRoot, anchor: str | None) -> tuple[list[AgentSurface], dict[str, Any] | None]:
    start = Path(root.location or root.path)
    found: list[AgentSurface] = []
    deadline = time.monotonic() + SCAN_TIME_LIMIT_SECONDS
    visited: set[str] = set()
    pending: deque[tuple[Path, int]] = deque([(start, 0)])
    count = 0

    def record(directory: Path) -> bool:
        module = surface_module(directory)
        if module is None:
            return False
        library_id = module.lower()
        if _LIBRARY_ID.fullmatch(library_id) and ".." not in library_id:
            found.append(AgentSurface(library_id, module, str(directory)))
        return True

    if record(start):
        return found, None
    while pending:
        directory, depth = pending.popleft()
        real = _real(directory)
        if real in visited:
            continue
        visited.add(real)
        count += 1
        if count > SCAN_DIRECTORY_LIMIT or time.monotonic() > deadline:
            return found, {
                "code": AGENT_SURFACE_SCAN_LIMIT,
                "message": f"신뢰 루트 {root.root_id}의 에이전트 표면 탐색이 상한에 걸려 일부만 확인했습니다: {start}",
                "trust_root": root.root_id,
            }
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name == "__pycache__":
                continue
            try:
                if not entry.is_dir():
                    continue
                child = Path(entry.path)
                if (entry.is_symlink() or entry.is_junction()) and (
                    anchor is None or not _is_within(_real(child), anchor)
                ):
                    continue  # anchor 밖(또는 anchor를 모름)을 가리키는 링크는 따라가지 않는다
            except OSError:
                continue
            child_depth = depth + 1
            if child_depth > SURFACE_DEPTH_LIMIT:
                continue
            if record(child):
                continue  # 표면 안은 표면 항목이 색인한다(중첩 표면 없음)
            if child_depth < SURFACE_DEPTH_LIMIT:
                pending.append((child, child_depth))
    return found, None


def _has_manual_approval(entry: LibraryCatalogEntry) -> bool:
    """팀 루트의 상속 승인을 적용하기 전 카탈로그 항목에서 수동 승인을 확인한다."""
    return bool(entry.trust.get("approved_for_import") or entry.trust.get("approved_for_execution"))


def diagnose_surfaces(catalog_dir: Path, config: TeamConfig) -> dict[str, Any]:
    """카탈로그 생성·등록 없이 발견 경로와 등록 경로를 비교한다."""
    found, warnings = find_agent_surfaces(config)
    with STORE_LOCK:
        entries = LibraryRegistry(catalog_dir).list_libraries() if catalog_dir.is_dir() else []
    catalog = {entry.library_id: entry for entry in entries}
    rows = []
    for surface in found:
        entry = catalog.get(surface.library_id)
        row = {
            "library_id": surface.library_id,
            "surface_dir": surface.surface_dir,
            "source_root": entry.source_root if entry else None,
            "status": "missing",
        }
        if entry:
            if _real(entry.source_root) != _real(surface.surface_dir):
                row.update(status="conflict", code=AGENT_SURFACE_CONFLICT)
            else:
                row["status"] = "ok" if entry.surface == surface.module else "old_registration"
                if row["status"] == "old_registration" and _has_manual_approval(entry):
                    row["reason"] = "explicit"
        rows.append(row)
    excluded = [{"library_id": e.library_id, "source_root": e.source_root, "reason": "explicit"}
                for e in entries if config.trust_root_for(e.source_root) is None]
    return {"surfaces": rows, "excluded": excluded, "warnings": list(config.warnings) + warnings}


def sync_agent_surfaces(
    registry: LibraryRegistry, config: TeamConfig, migrated: set[str] | None = None
) -> tuple[list[str], list[dict[str, Any]]]:
    """발견한 표면 중 Catalog에 없는 것을 등록한다(정적 분석만, 멱등). (이번에 새로 등록한 ID, 경고)."""
    if not config.trust_roots:
        return [], []
    if migrated is None:
        migrated = set()
    surfaces, warnings = find_agent_surfaces(config)
    registered: list[str] = []
    with STORE_LOCK:
        try:
            catalog = {entry.library_id: entry for entry in registry.list_libraries()}
        except LibraryError as exc:
            return [], warnings + [{
                "code": AGENT_SURFACE_FAILED, "message": f"Catalog를 읽지 못해 표면 등록을 건너뜁니다: {exc}", "library_id": None,
            }]
        for surface in surfaces:
            existing = catalog.get(surface.library_id)
            if existing is not None:
                if _real(existing.source_root) != _real(surface.surface_dir):
                    warnings.append({
                        "code": AGENT_SURFACE_CONFLICT,
                        "message": (
                            f"같은 library_id가 다른 원본으로 이미 등록되어 있어 에이전트 표면을 등록하지 않습니다: "
                            f"{surface.library_id} (등록: {existing.source_root}, 표면: {surface.surface_dir})"
                        ),
                        "library_id": surface.library_id,
                        "source_root": existing.source_root,
                        "surface_dir": surface.surface_dir,
                    })
                    continue
                if existing.surface == surface.module or _has_manual_approval(existing):
                    continue
            try:
                write = registry.migrate_surface if existing else registry.register_surface
                catalog[surface.library_id] = write(
                    surface.surface_dir, surface.module, library_id=surface.library_id
                )
            except (LibraryError, OSError) as exc:
                warnings.append({
                    "code": AGENT_SURFACE_FAILED,
                    "message": f"에이전트 표면 등록에 실패했습니다: {surface.surface_dir}: {exc}",
                    "library_id": surface.library_id,
                    "surface_dir": surface.surface_dir,
                    "error_code": exc.code if isinstance(exc, LibraryError) else AGENT_SURFACE_FAILED,
                })
                continue
            if existing:
                migrated.add(surface.library_id)
            else:
                registered.append(surface.library_id)
    return registered, warnings


__all__ = [
    "AGENT_SURFACE_CONFLICT",
    "AGENT_SURFACE_FAILED",
    "AGENT_SURFACE_SCAN_LIMIT",
    "SURFACE_DEPTH_LIMIT",
    "AgentSurface",
    "diagnose_surfaces",
    "find_agent_surfaces",
    "surface_module",
    "sync_agent_surfaces",
]
