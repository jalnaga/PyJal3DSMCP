"""원본 라이브러리 트리 판별과 Manifest 문서.

원본 폴더는 읽기만 한다. layout 판별, 링크 탈출 검사, source revision 계산,
``pyproject.toml``·dist-info 메타데이터 읽기와 Manifest 구성을 맡는다.

layout 판별 순서(앞의 규칙이 우선):

1. ``package``: ``source_path`` 바로 아래에 ``__init__.py``가 있으면 그 패키지 디렉터리 하나만 등록한다
   (설치된 패키지 등). ``import_root``는 부모 디렉터리이며 부모에서는 목록 조회와 대상 dist-info ``METADATA``
   읽기만 한다.
2. ``src``: ``<root>/src/<pkg>/__init__.py``가 있으면 ``import_root=<root>/src``.
3. ``flat``: 그 밖에는 ``import_root=<root>``.

에이전트 표면(``surface``)은 ``scan_surface``가 따로 판별한다. 표면 디렉터리 아래만
인덱싱·revision 범위이고 모듈 이름은 전체 점 경로(예: ``pyjallib.max.agentapi.bone``)이며, ``import_root``는 최상위 패키지의
부모다. 표면 밖(진입점·런처·내부 구현)은 읽지 않는다.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from email.parser import HeaderParser
from pathlib import Path
from typing import Any

from pyjal_extensions import LibraryError

MANIFEST_SCHEMA_VERSION = 1

PACKAGE_LAYOUT = "package"
SURFACE_LAYOUT = "surface"
DIST_INFO_SUFFIX = ".dist-info"

NATIVE_SUFFIXES = (".pyd", ".so")
BYTECODE_SUFFIXES = (".pyc", ".pyo")
# flat layout 루트에서 라이브러리 모듈로 보지 않는 이름
FLAT_EXCLUDED_NAMES = frozenset({"setup", "conftest", "tests", "test"})


@dataclass(frozen=True)
class ModuleFile:
    """인덱싱 대상 모듈 파일 하나."""

    name: str  # Import 경로
    relpath: str  # source_root 기준 POSIX 상대 경로
    path: Path
    kind: str  # "source" | "native"
    is_package: bool


@dataclass
class SourceScan:
    """원본 트리를 한 번 훑은 결과. 여기서 얻은 파일 집합이 revision의 기준이다."""

    source_root: Path
    import_root: Path
    layout: str
    top_level: list[str]
    package: str | None
    reported_version: str | None
    requires_python: str | None
    modules: list[ModuleFile]
    revision_files: list[tuple[str, Path]]
    issues: list[dict[str, Any]] = field(default_factory=list)


def scan_source(source_path: str | os.PathLike[str]) -> SourceScan:
    """원본 라이브러리 트리를 판별한다. 파일을 읽기만 하고 Import하지 않는다."""
    raw = Path(source_path)
    if not raw.exists():
        raise LibraryError("SOURCE_NOT_FOUND", f"원본 경로가 없습니다: {raw}", {"source_path": str(raw)})
    root = raw.resolve(strict=True)
    if not root.is_dir():
        raise LibraryError(
            "SOURCE_LAYOUT_UNRECOGNIZED",
            f"원본 경로가 디렉터리가 아닙니다: {root}",
            {"source_path": str(root)},
        )
    if (root / "__init__.py").is_file():
        return _scan_package_dir(root)

    walker = _TreeWalker(root)
    layout, import_root = _detect_layout(root)
    walker.ensure_inside(import_root, "src")

    candidates = _top_level_candidates(import_root, layout)
    if not candidates:
        raise LibraryError(
            "SOURCE_LAYOUT_UNRECOGNIZED",
            f"라이브러리 패키지나 모듈을 찾지 못했습니다: {root}",
            {"source_root": str(root), "layout": layout},
        )

    for name, path, kind in candidates:
        if _is_link(path):
            walker.ensure_inside(path, name)
            walker.issues.append(_link_skipped_issue(root, path))
            continue
        if kind == "package":
            walker.walk_package(path, name)
        else:
            walker.add_module_file(path, name)

    pyproject = root / "pyproject.toml"
    metadata: dict[str, Any] = {}
    if pyproject.is_file():
        walker.ensure_inside(pyproject, "pyproject.toml")
        walker.revision_files.append(("pyproject.toml", pyproject))
        metadata = _read_project_metadata(pyproject, walker.issues)

    top_level = sorted(name for name, _, _ in candidates)
    packages = sorted(name for name, _, kind in candidates if kind == "package")
    return SourceScan(
        source_root=root,
        import_root=import_root,
        layout=layout,
        top_level=top_level,
        package=_detect_package(packages, metadata.get("name")),
        reported_version=_optional_str(metadata.get("version")),
        requires_python=_optional_str(metadata.get("requires-python")),
        modules=sorted(walker.modules, key=lambda m: (m.name, m.relpath)),
        revision_files=sorted(walker.revision_files),
        issues=walker.issues,
    )


def scan_surface(surface_dir: str | os.PathLike[str], module: str) -> SourceScan:
    """에이전트 표면 디렉터리 하나를 판별한다. 표면 아래만 읽고 Import하지 않는다.

    ``module``은 표면 모듈의 점 경로다. 표면 디렉터리와 그 위 패키지 디렉터리의 이름이 점 경로와 같고 모두
    ``__init__.py``를 가져야 하며, 최상위 패키지의 부모에는 ``__init__.py``가 없어야 한다. 아니면(원본 구조가 바뀌면)
    ``SOURCE_LAYOUT_UNRECOGNIZED``다.
    """
    raw = Path(surface_dir)
    if not raw.exists():
        raise LibraryError("SOURCE_NOT_FOUND", f"원본 경로가 없습니다: {raw}", {"source_path": str(raw)})
    root = raw.resolve(strict=True)
    parts = module.split(".") if isinstance(module, str) else []
    if not parts or not all(part.isidentifier() for part in parts):
        raise LibraryError(
            "SOURCE_LAYOUT_UNRECOGNIZED", f"표면 모듈 이름이 올바르지 않습니다: {module!r}", {"source_root": str(root)}
        )
    package_dirs = [root, *root.parents][: len(parts)]
    if len(package_dirs) < len(parts) or any(
        directory.name != part or not (directory / "__init__.py").is_file()
        for directory, part in zip(package_dirs, reversed(parts))
    ):
        raise LibraryError(
            "SOURCE_LAYOUT_UNRECOGNIZED",
            f"표면 디렉터리가 모듈 이름과 맞는 패키지 구조가 아닙니다: {root} ({module})",
            {"source_root": str(root), "layout": SURFACE_LAYOUT, "surface": module},
        )
    top_dir = package_dirs[-1]
    import_root = top_dir.parent
    if (import_root / "__init__.py").is_file():
        raise LibraryError(
            "SOURCE_LAYOUT_UNRECOGNIZED",
            f"표면의 최상위 패키지가 모듈 이름과 다릅니다: {top_dir} ({module})",
            {"source_root": str(root), "layout": SURFACE_LAYOUT, "surface": module},
        )
    walker = _TreeWalker(root)
    walker.walk_package(root, module)
    top = parts[0]
    reported_version, requires_python = _read_dist_info_metadata(import_root, top, walker.issues)
    return SourceScan(
        source_root=root,
        import_root=import_root,
        layout=SURFACE_LAYOUT,
        top_level=[top],
        package=top,
        reported_version=reported_version,
        requires_python=requires_python,
        modules=sorted(walker.modules, key=lambda m: (m.name, m.relpath)),
        revision_files=sorted(walker.revision_files),
        issues=walker.issues,
    )


def compute_source_revision(scan: SourceScan) -> str:
    """상대 경로와 내용만으로 계산하므로 같은 내용의 사본은 같은 값을 가진다."""
    digest = hashlib.sha256()
    for relpath, path in scan.revision_files:
        with path.open("rb") as handle:
            file_digest = hashlib.file_digest(handle, "sha256").digest()
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest)
    return "sha256:" + digest.hexdigest()


def build_manifest(
    *,
    library_id: str,
    source_root: str,
    import_root: str,
    package: str | None,
    reported_version: str | None,
    entrypoints: list[dict[str, Any]],
    api_index_revision: str,
    source_revision: str,
    trust: dict[str, bool],
) -> dict[str, Any]:
    """라이브러리 메타데이터로 Manifest 문서를 만든다."""
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "library_id": library_id,
        "source": {"kind": "local_path", "root": source_root, "import_root": import_root},
        "package": package,
        "reported_version": reported_version,
        "entrypoints": [dict(ep) for ep in entrypoints],
        "discovery": {"strategy": "ast", "api_index_revision": api_index_revision},
        "source_revision": source_revision,
        "trust": dict(trust),
    }


# ---------------------------------------------------------------------------
# 내부 구현
# ---------------------------------------------------------------------------


class _TreeWalker:
    """패키지 트리를 링크를 따라가지 않고 훑으며 모듈·revision 파일을 모은다."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.modules: list[ModuleFile] = []
        self.revision_files: list[tuple[str, Path]] = []
        self.issues: list[dict[str, Any]] = []

    def ensure_inside(self, path: Path, label: str) -> None:
        target = path.resolve()
        if not target.is_relative_to(self.root):
            raise LibraryError(
                "SOURCE_PATH_ESCAPE",
                f"원본 루트 밖을 가리키는 경로가 있습니다: {label} -> {target}",
                {"path": str(path), "target": str(target), "source_root": str(self.root)},
            )

    def relpath(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def walk_package(self, directory: Path, module_name: str | None) -> None:
        """module_name이 None이면 Import할 수 없는 데이터 디렉터리로 보고 revision만 모은다."""
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda e: e.name)
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name == "__pycache__":
                continue
            path = Path(entry.path)
            if entry.is_symlink() or entry.is_junction():
                # 루트 안을 가리키는 링크도 중복·순환을 피하려고 따라가지 않는다
                self.ensure_inside(path, self.relpath(path))
                self.issues.append(_link_skipped_issue(self.root, path))
                continue
            if entry.is_dir(follow_symlinks=False):
                child = f"{module_name}.{name}" if module_name and name.isidentifier() else None
                self.walk_package(path, child)
                continue
            if not entry.is_file(follow_symlinks=False) or name.endswith(BYTECODE_SUFFIXES):
                continue
            self.revision_files.append((self.relpath(path), path))
            if module_name is None:
                continue
            if name.endswith(".py"):
                stem = name[:-3]
                if stem == "__init__":
                    self._add(module_name, path, "source", is_package=True)
                elif stem.isidentifier():
                    self._add(f"{module_name}.{stem}", path, "source", is_package=False)
            elif name.endswith(NATIVE_SUFFIXES):
                stem = _native_module_name(name)
                if stem:
                    self._add(f"{module_name}.{stem}", path, "native", is_package=False)

    def add_module_file(self, path: Path, name: str) -> None:
        self.revision_files.append((self.relpath(path), path))
        kind = "native" if path.name.endswith(NATIVE_SUFFIXES) else "source"
        self._add(name, path, kind, is_package=False)

    def _add(self, name: str, path: Path, kind: str, *, is_package: bool) -> None:
        self.modules.append(ModuleFile(name, self.relpath(path), path, kind, is_package))


def _scan_package_dir(package_dir: Path) -> SourceScan:
    """패키지 디렉터리 하나만 훑는다. 인덱싱·revision·링크 검사 범위는 그 디렉터리 아래뿐이다."""
    name = package_dir.name
    if not name.isidentifier():
        raise LibraryError(
            "SOURCE_LAYOUT_UNRECOGNIZED",
            f"패키지 디렉터리 이름이 Python Import 이름이 아닙니다: {name}",
            {"source_root": str(package_dir), "layout": PACKAGE_LAYOUT},
        )
    walker = _TreeWalker(package_dir)
    walker.walk_package(package_dir, name)
    import_root = package_dir.parent
    reported_version, requires_python = _read_dist_info_metadata(import_root, name, walker.issues)
    return SourceScan(
        source_root=package_dir,
        import_root=import_root,
        layout=PACKAGE_LAYOUT,
        top_level=[name],
        package=name,
        reported_version=reported_version,
        requires_python=requires_python,
        modules=sorted(walker.modules, key=lambda m: (m.name, m.relpath)),
        revision_files=sorted(walker.revision_files),
        issues=walker.issues,
    )


def _read_dist_info_metadata(
    parent: Path, package: str, issues: list[dict[str, Any]]
) -> tuple[str | None, str | None]:
    """부모 디렉터리에서 패키지 이름과 정규화 이름이 같은 ``*.dist-info`` 하나의 ``METADATA``만 읽는다.

    다른 dist-info·패키지는 열거나 나열하지 않는다. 대상이 없거나 여럿이면 버전을 추정하지 않는다(None).
    """
    wanted = _normalize_name(package)
    try:
        with os.scandir(parent) as iterator:
            matches = sorted(
                Path(entry.path)
                for entry in iterator
                if entry.name.endswith(DIST_INFO_SUFFIX)
                and _normalize_name(entry.name.split("-", 1)[0]) == wanted
                and entry.is_dir()
            )
    except OSError:
        return None, None
    if not matches:
        return None, None
    if len(matches) > 1:
        issues.append(
            {
                "code": "DIST_INFO_AMBIGUOUS",
                "message": "같은 배포 이름의 dist-info가 여럿이라 버전을 정하지 않았습니다: "
                + ", ".join(path.name for path in matches),
            }
        )
        return None, None
    metadata_path = matches[0] / "METADATA"
    try:
        headers = HeaderParser().parsestr(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        issues.append(
            {"code": "DIST_INFO_INVALID", "path": f"{matches[0].name}/METADATA", "message": str(exc)}
        )
        return None, None
    return _header_value(headers.get("Version")), _header_value(headers.get("Requires-Python"))


def _header_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _detect_layout(root: Path) -> tuple[str, Path]:
    src = root / "src"
    if src.is_dir():
        with os.scandir(src) as iterator:
            for entry in iterator:
                if entry.is_dir() and (Path(entry.path) / "__init__.py").is_file():
                    return "src", src
    return "flat", root


def _top_level_candidates(import_root: Path, layout: str) -> list[tuple[str, Path, str]]:
    """(Import 이름, 경로, "package"|"module") 목록. 같은 이름이면 패키지가 우선한다."""
    packages: dict[str, Path] = {}
    modules: dict[str, Path] = {}
    with os.scandir(import_root) as iterator:
        entries = sorted(iterator, key=lambda e: e.name)
    for entry in entries:
        name = entry.name
        if name.startswith(".") or name == "__pycache__":
            continue
        path = Path(entry.path)
        if entry.is_dir():
            if name.isidentifier() and (path / "__init__.py").is_file():
                packages[name] = path
        elif entry.is_file():
            if name.endswith(".py") and name[:-3].isidentifier():
                modules.setdefault(name[:-3], path)
            elif name.endswith(NATIVE_SUFFIXES):
                stem = _native_module_name(name)
                if stem:
                    modules.setdefault(stem, path)
    if layout == "flat":
        packages = {k: v for k, v in packages.items() if k not in FLAT_EXCLUDED_NAMES}
        modules = {k: v for k, v in modules.items() if k not in FLAT_EXCLUDED_NAMES}
    result = [(name, path, "package") for name, path in packages.items()]
    result += [(name, path, "module") for name, path in modules.items() if name not in packages]
    return sorted(result)


def _native_module_name(filename: str) -> str | None:
    """``fastcore.cp312-win_amd64.pyd`` → ``fastcore`` (ABI 태그 제거)."""
    stem = filename.split(".", 1)[0]
    return stem if stem.isidentifier() else None


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _link_skipped_issue(root: Path, path: Path) -> dict[str, Any]:
    return {
        "code": "LINK_SKIPPED",
        "path": path.relative_to(root).as_posix(),
        "message": "원본 루트 안을 가리키는 링크는 중복·순환 방지를 위해 인덱싱하지 않았습니다.",
    }


def _read_project_metadata(pyproject: Path, issues: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        issues.append({"code": "PYPROJECT_INVALID", "path": "pyproject.toml", "message": str(exc)})
        return {}
    project = data.get("project")
    return project if isinstance(project, dict) else {}


def _detect_package(packages: list[str], project_name: Any) -> str | None:
    if isinstance(project_name, str):
        wanted = _normalize_name(project_name)
        for name in packages:
            if _normalize_name(name) == wanted:
                return name
    return packages[0] if len(packages) == 1 else None


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
