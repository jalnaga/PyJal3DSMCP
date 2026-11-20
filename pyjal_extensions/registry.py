"""Library Registry: 공통 Catalog와 등록·갱신·해제·검색 Facade.

``library_register/unregister/list/search/describe`` MCP Tool이 이 Facade를 얇게 감싼다.
모든 쓰기는 호출자가 지정한 ``catalog_dir`` 안에서만 일어난다.

저장 구조::

    <catalog_dir>/catalog.json                     # 항목 목록(커밋 지점)
    <catalog_dir>/libraries/lib-<id>.d/manifest.json
    <catalog_dir>/libraries/lib-<id>.d/index.json   # ApiRecord와 소스 구간
    <catalog_dir>/libraries/lib-<id>.d/sources.json # 인덱싱 시점 소스 사본(describe용)
    <catalog_dir>/libraries/lib-<id>.d/recipes.json
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from pyjal_extensions import LibraryError
from pyjal_extensions.discovery import (
    SEARCH_DEFAULT_LIMIT,
    ApiDescription,
    ApiRecord,
    IndexResult,
    SearchResult,
    build_index,
    library_of_api_id,
    normalize_entrypoints,
    normalize_search_args,
    search_records,
    source_segment,
)
from pyjal_extensions.manifests import SourceScan, build_manifest, compute_source_revision, scan_source, scan_surface
from pyjal_extensions.skills import Recipe, new_recipe, with_effective_status

CATALOG_SCHEMA_VERSION = 1
CATALOG_FILE = "catalog.json"
LIBRARIES_DIR = "libraries"

_LIBRARY_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


def _default_trust() -> dict[str, bool]:
    return {"approved_for_import": False, "approved_for_execution": False}


@dataclass
class LibraryCatalogEntry:
    library_id: str
    source_root: str
    import_root: str
    layout: str
    package: str | None
    top_level: list[str]
    reported_version: str | None
    requires_python: str | None
    source_revision: str
    api_index_revision: str
    status: str  # registered | indexed | stale | invalid
    trust: dict[str, bool] = field(default_factory=_default_trust)
    entrypoints: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    manifest_path: str = ""
    # 에이전트 표면 항목이면 표면 모듈의 점 경로, 아니면 None
    surface: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LibraryCatalogEntry:
        names = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})


@dataclass
class _LoadedIndex:
    api_index_revision: str
    records: dict[str, ApiRecord]
    spans: dict[str, list[Any]]


def _scan_entry(entry: LibraryCatalogEntry) -> SourceScan:
    """등록 때와 같은 판별로 원본을 다시 훑는다(표면 항목은 표면 전용 스캔)."""
    if entry.surface:
        return scan_surface(entry.source_root, entry.surface)
    return scan_source(entry.source_root)


class LibraryRegistry:
    """설치 환경 하나의 공통 Library Catalog.

    같은 ``catalog_dir``로 만든 인스턴스는 매 호출마다 디스크 상태를 다시 읽으므로
    서로의 변경을 본다. 동시 쓰기 잠금은 제공하지 않는다.
    """

    def __init__(self, catalog_dir: str | os.PathLike[str]) -> None:
        self._root = Path(catalog_dir).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._index_cache: dict[str, _LoadedIndex] = {}

    @property
    def catalog_dir(self) -> Path:
        return self._root

    # -- 등록·갱신·해제 -----------------------------------------------------

    def register(
        self,
        source_path: str | os.PathLike[str],
        library_id: str | None = None,
        entrypoints: list[dict[str, Any]] | None = None,
    ) -> LibraryCatalogEntry:
        """원본을 정적으로 분석해 Catalog·Manifest·인덱스를 기록한다. Import·설치는 하지 않는다."""
        catalog = self._load_catalog()
        if library_id is not None:
            self._check_new_id(library_id, catalog)
        normalized = normalize_entrypoints(entrypoints)
        scan = scan_source(source_path)
        if library_id is None:
            if scan.package is None:
                raise LibraryError(
                    "INVALID_LIBRARY_ID",
                    "패키지 이름을 정할 수 없어 library_id를 지정해야 합니다.",
                    {"top_level": scan.top_level},
                )
            library_id = scan.package
            self._check_new_id(library_id, catalog)

        return self._register_scan(library_id, scan, normalized, catalog, surface=None)

    def register_surface(
        self, surface_dir: str | os.PathLike[str], module: str, library_id: str
    ) -> LibraryCatalogEntry:
        """에이전트 표면 디렉터리 하나를 등록한다. 색인·revision 범위는 표면 아래뿐이다. Import하지 않는다."""
        catalog = self._load_catalog()
        self._check_new_id(library_id, catalog)
        scan = scan_surface(surface_dir, module)
        return self._register_scan(library_id, scan, [], catalog, surface=module)

    def migrate_surface(self, surface_dir: str, module: str, library_id: str) -> LibraryCatalogEntry:
        """기존 등록의 승인과 부가 자료를 보존하며 표면 색인을 교체한다."""
        catalog = self._load_catalog()
        previous = self._entry(catalog, library_id)
        if os.path.normcase(os.path.realpath(previous.source_root)) != os.path.normcase(os.path.realpath(surface_dir)):
            raise LibraryError("AGENT_SURFACE_CONFLICT", "등록된 원본 경로가 달라졌습니다.")
        scan = scan_surface(surface_dir, module)
        entry, index = self._index(library_id, scan, previous.entrypoints, trust=previous.trust,
                                   strict=False, surface=module)
        directory = self._library_dir(library_id)
        with tempfile.TemporaryDirectory(dir=self._root) as temporary:
            backup = Path(temporary) / "previous"
            shutil.copytree(directory, backup)
            try:
                self._write_library(entry, index)
                catalog[library_id] = entry.to_dict()
                self._save_catalog(catalog)
            except Exception:
                shutil.rmtree(directory)
                backup.rename(directory)
                raise
        return entry

    def _register_scan(
        self,
        library_id: str,
        scan: SourceScan,
        entrypoints: list[dict[str, Any]],
        catalog: dict[str, dict[str, Any]],
        *,
        surface: str | None,
    ) -> LibraryCatalogEntry:
        entry, index = self._index(library_id, scan, entrypoints, trust=_default_trust(), strict=True, surface=surface)
        library_dir = self._library_dir(library_id)
        if library_dir.exists():
            # 이전 실행이 Catalog 기록 전에 중단되어 남은 자료
            shutil.rmtree(library_dir)
        self._write_library(entry, index)
        catalog[library_id] = entry.to_dict()
        self._save_catalog(catalog)
        return entry

    def unregister(self, library_id: str) -> LibraryCatalogEntry:
        """Catalog 연결을 해제하고 Manifest·인덱스·Recipe를 지운다. 원본은 건드리지 않는다."""
        catalog = self._load_catalog()
        entry = self._entry(catalog, library_id)
        del catalog[library_id]
        self._save_catalog(catalog)
        library_dir = self._library_dir(library_id)
        if library_dir.exists():
            shutil.rmtree(library_dir)
        self._index_cache.pop(library_id, None)
        return entry

    def check(self, library_id: str) -> LibraryCatalogEntry:
        """원본을 다시 해시해 상태만 갱신한다. 저장된 revision·인덱스는 바꾸지 않는다."""
        catalog = self._load_catalog()
        entry = self._entry(catalog, library_id)
        try:
            revision = compute_source_revision(_scan_entry(entry))
        except LibraryError:
            status = "invalid"  # 원본 루트가 없거나 더는 라이브러리로 판별되지 않음
        else:
            status = "indexed" if revision == entry.source_revision else "stale"
        if status != entry.status:
            entry.status = status
            catalog[library_id] = entry.to_dict()
            self._save_catalog(catalog)
        return entry

    def refresh(self, library_id: str) -> LibraryCatalogEntry:
        """원본을 다시 인덱싱한다. 원본이 사라졌으면 ``invalid``로 저장하고 오류를 낸다."""
        catalog = self._load_catalog()
        entry = self._entry(catalog, library_id)
        try:
            scan = _scan_entry(entry)
        except LibraryError:
            entry.status = "invalid"
            catalog[library_id] = entry.to_dict()
            self._save_catalog(catalog)
            raise
        # 원본 변화로 깨진 진입점은 등록과 달리 거절하지 않고 issues로 남긴다
        refreshed, index = self._index(
            library_id, scan, entry.entrypoints, trust=entry.trust, strict=False, surface=entry.surface
        )
        self._write_library(refreshed, index)
        catalog[library_id] = refreshed.to_dict()
        self._save_catalog(catalog)
        return refreshed

    def set_trust(
        self,
        library_id: str,
        *,
        approved_for_import: bool | None = None,
        approved_for_execution: bool | None = None,
    ) -> LibraryCatalogEntry:
        """신뢰 승인을 Catalog 항목과 Manifest에 함께 저장한다. ``None``인 값은 유지한다.

        승인 여부는 사용자의 명시적 지시로만 바뀌어야 하며, 판단은 호출자(MCP Tool 안내)의 몫이다.
        """
        catalog = self._load_catalog()
        entry = self._entry(catalog, library_id)
        trust = dict(entry.trust)
        if approved_for_import is not None:
            trust["approved_for_import"] = bool(approved_for_import)
        if approved_for_execution is not None:
            trust["approved_for_execution"] = bool(approved_for_execution)
        if trust == entry.trust:
            return entry
        entry.trust = trust
        catalog[library_id] = entry.to_dict()
        self._save_catalog(catalog)
        self._write_manifest(entry)
        return entry

    # -- 조회 ---------------------------------------------------------------

    def get(self, library_id: str) -> LibraryCatalogEntry:
        return self._entry(self._load_catalog(), library_id)

    def list_libraries(self) -> list[LibraryCatalogEntry]:
        catalog = self._load_catalog()
        return [self._to_entry(catalog[key]) for key in sorted(catalog)]

    def search(self, query: str, library_id: str | None = None, limit: int = SEARCH_DEFAULT_LIMIT) -> SearchResult:
        terms, applied_limit = normalize_search_args(query, limit)
        catalog = self._load_catalog()
        if library_id is not None:
            self._entry(catalog, library_id)
            library_ids = [library_id]
        else:
            library_ids = sorted(catalog)
        records = [
            record
            for lid in library_ids
            for record in self._load_index(self._to_entry(catalog[lid])).records.values()
        ]
        surfaces = frozenset(lid for lid in library_ids if catalog[lid].get("surface"))
        return search_records(records, terms, applied_limit, surface_libraries=surfaces)

    def describe(self, api_id: str) -> ApiDescription:
        catalog = self._load_catalog()
        entry, index, record = self._find_api(catalog, api_id)
        sources = self._read_json(self._library_dir(entry.library_id) / "sources.json", default={})
        return ApiDescription(
            record=ApiRecord.from_dict(record.to_dict()),
            source_code=source_segment(sources, index.spans.get(api_id)),
            catalog_status=entry.status,
            source_revision=entry.source_revision,
            recipes=self._recipes(entry, index, api_id=api_id),
        )

    # -- Recipe -------------------------------------------------------------

    def add_recipe(self, api_id: str, title: str, code: str, notes: str = "") -> Recipe:
        catalog = self._load_catalog()
        entry, index, _ = self._find_api(catalog, api_id)
        recipe = new_recipe(
            api_id=api_id,
            library_id=entry.library_id,
            title=title,
            code=code,
            notes=notes,
            catalog_revision=entry.source_revision,
        )
        path = self._library_dir(entry.library_id) / "recipes.json"
        stored = self._read_json(path, default=[])
        stored.append(recipe.to_dict())
        self._write_json(path, stored)
        return with_effective_status(recipe, current_revision=entry.source_revision, api_exists=True)

    def list_recipes(self, library_id: str | None = None, api_id: str | None = None) -> list[Recipe]:
        catalog = self._load_catalog()
        if library_id is not None:
            self._entry(catalog, library_id)
            library_ids = [library_id]
        else:
            library_ids = sorted(catalog)
        result: list[Recipe] = []
        for lid in library_ids:
            entry = self._to_entry(catalog[lid])
            result += self._recipes(entry, self._load_index(entry), api_id=api_id)
        return result

    # -- 내부: 인덱싱 -------------------------------------------------------

    def _index(
        self,
        library_id: str,
        scan: SourceScan,
        entrypoints: list[dict[str, Any]],
        *,
        trust: dict[str, bool],
        strict: bool,
        surface: str | None = None,
    ) -> tuple[LibraryCatalogEntry, IndexResult]:
        source_revision = compute_source_revision(scan)
        index = build_index(scan, library_id, source_revision, entrypoints, strict_entrypoints=strict)
        entry = LibraryCatalogEntry(
            library_id=library_id,
            source_root=str(scan.source_root),
            import_root=str(scan.import_root),
            layout=scan.layout,
            package=scan.package,
            top_level=list(scan.top_level),
            reported_version=scan.reported_version,
            requires_python=scan.requires_python,
            source_revision=source_revision,
            api_index_revision=index.api_index_revision,
            status="indexed",
            trust=dict(trust),
            entrypoints=[dict(ep) for ep in entrypoints],
            issues=list(scan.issues) + list(index.issues),
            manifest_path=str(self._manifest_path(library_id)),
            surface=surface,
        )
        return entry, index

    def _write_library(self, entry: LibraryCatalogEntry, index: IndexResult) -> None:
        library_dir = self._library_dir(entry.library_id)
        self._write_json(
            library_dir / "index.json",
            {
                "schema_version": CATALOG_SCHEMA_VERSION,
                "library_id": entry.library_id,
                "source_revision": entry.source_revision,
                "api_index_revision": index.api_index_revision,
                "records": [record.to_dict() for record in index.records],
                "spans": index.spans,
            },
            indent=None,
        )
        self._write_json(library_dir / "sources.json", index.sources, indent=None)
        self._write_manifest(entry)
        self._index_cache.pop(entry.library_id, None)

    def _write_manifest(self, entry: LibraryCatalogEntry) -> None:
        manifest = build_manifest(
            library_id=entry.library_id,
            source_root=entry.source_root,
            import_root=entry.import_root,
            package=entry.package,
            reported_version=entry.reported_version,
            entrypoints=entry.entrypoints,
            api_index_revision=entry.api_index_revision,
            source_revision=entry.source_revision,
            trust=entry.trust,
        )
        self._write_json(self._manifest_path(entry.library_id), manifest)

    def _load_index(self, entry: LibraryCatalogEntry) -> _LoadedIndex:
        cached = self._index_cache.get(entry.library_id)
        if cached is not None and cached.api_index_revision == entry.api_index_revision:
            return cached
        data = self._read_json(self._library_dir(entry.library_id) / "index.json", default=None)
        if not isinstance(data, dict) or data.get("api_index_revision") != entry.api_index_revision:
            raise LibraryError(
                "CATALOG_CORRUPT",
                f"인덱스가 없거나 Catalog와 맞지 않습니다. refresh가 필요합니다: {entry.library_id}",
                {"library_id": entry.library_id},
            )
        records = {item["api_id"]: ApiRecord.from_dict(item) for item in data["records"]}
        loaded = _LoadedIndex(entry.api_index_revision, records, data.get("spans", {}))
        self._index_cache[entry.library_id] = loaded
        return loaded

    def _find_api(self, catalog: dict[str, dict[str, Any]], api_id: str) -> tuple[LibraryCatalogEntry, _LoadedIndex, ApiRecord]:
        if isinstance(api_id, str):
            data = catalog.get(library_of_api_id(api_id))
            if data is not None:
                entry = self._to_entry(data)
                index = self._load_index(entry)
                record = index.records.get(api_id)
                if record is not None:
                    return entry, index, record
        raise LibraryError("API_NOT_FOUND", f"API를 찾지 못했습니다: {api_id}", {"api_id": api_id})

    def _recipes(self, entry: LibraryCatalogEntry, index: _LoadedIndex, *, api_id: str | None) -> list[Recipe]:
        stored = self._read_json(self._library_dir(entry.library_id) / "recipes.json", default=[])
        result = []
        for item in stored:
            recipe = Recipe.from_dict(item)
            if api_id is not None and recipe.api_id != api_id:
                continue
            result.append(
                with_effective_status(
                    recipe, current_revision=entry.source_revision, api_exists=recipe.api_id in index.records
                )
            )
        return result

    # -- 내부: Catalog 저장 -------------------------------------------------

    def _check_new_id(self, library_id: Any, catalog: dict[str, dict[str, Any]]) -> None:
        if not isinstance(library_id, str) or not _LIBRARY_ID.fullmatch(library_id) or ".." in library_id:
            raise LibraryError(
                "INVALID_LIBRARY_ID",
                "library_id는 소문자·숫자로 시작하고 소문자·숫자·'_'·'.'·'-'만 쓰는 64자 이하여야 합니다('..' 불가).",
                {"library_id": library_id},
            )
        if library_id in catalog:
            raise LibraryError(
                "LIBRARY_ALREADY_REGISTERED",
                f"이미 등록된 library_id입니다: {library_id}",
                {"library_id": library_id, "source_root": catalog[library_id].get("source_root")},
            )

    def _entry(self, catalog: dict[str, dict[str, Any]], library_id: str) -> LibraryCatalogEntry:
        data = catalog.get(library_id) if isinstance(library_id, str) else None
        if data is None:
            raise LibraryError(
                "LIBRARY_NOT_REGISTERED", f"등록되지 않은 라이브러리입니다: {library_id}", {"library_id": library_id}
            )
        return self._to_entry(data)

    def _to_entry(self, data: dict[str, Any]) -> LibraryCatalogEntry:
        entry = LibraryCatalogEntry.from_dict(data)
        # Catalog 폴더를 옮겨도 현재 위치 기준 경로를 돌려준다
        entry.manifest_path = str(self._manifest_path(entry.library_id))
        return entry

    def _library_dir(self, library_id: str) -> Path:
        # 'con' 같은 Windows 예약 이름과 끝 '.' 제거 문제를 피하려고 접두·접미를 붙인다
        return self._root / LIBRARIES_DIR / f"lib-{library_id}.d"

    def _manifest_path(self, library_id: str) -> Path:
        return self._library_dir(library_id) / "manifest.json"

    def _load_catalog(self) -> dict[str, dict[str, Any]]:
        data = self._read_json(self._root / CATALOG_FILE, default=None)
        if data is None:
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("libraries"), list):
            raise LibraryError("CATALOG_CORRUPT", f"Catalog 형식이 잘못되었습니다: {self._root / CATALOG_FILE}")
        return {item["library_id"]: item for item in data["libraries"]}

    def _save_catalog(self, catalog: dict[str, dict[str, Any]]) -> None:
        self._write_json(
            self._root / CATALOG_FILE,
            {"schema_version": CATALOG_SCHEMA_VERSION, "libraries": [catalog[key] for key in sorted(catalog)]},
        )

    def _read_json(self, path: Path, *, default: Any) -> Any:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LibraryError("CATALOG_CORRUPT", f"Catalog 파일을 읽지 못했습니다: {path}: {exc}") from exc

    def _write_json(self, path: Path, data: Any, *, indent: int | None = 2) -> None:
        """임시 파일에 쓴 뒤 교체해 중단 시 반쯤 쓰인 파일이 남지 않게 한다."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
        os.replace(temporary, path)
