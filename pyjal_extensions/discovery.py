"""AST 기반 API 인덱싱과 용어 검색.

대상 모듈은 ``ast.parse``로만 읽으며 Import·실행하지 않는다. 정적으로 확정할 수 없는
요소는 추정으로 채우지 않고 ``resolution="unresolved"``와 ``limitations``로 드러낸다.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import io
import json
import re
import tokenize
import warnings
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from pyjal_extensions import LibraryError
from pyjal_extensions.manifests import ModuleFile, SourceScan

if TYPE_CHECKING:
    from pyjal_extensions.skills import Recipe

SEARCH_DEFAULT_LIMIT = 10
SEARCH_MAX_LIMIT = 50

ENTRYPOINT_KEYS = frozenset({"id", "module", "factory", "returns"})
ENTRYPOINT_REQUIRED = ("id", "module", "factory")

_DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_PROPERTY_ACCESSORS = frozenset({"setter", "getter", "deleter"})
# 호출 형태를 바꾸지 않거나 capabilities로 표시하는 데코레이터
_KNOWN_DECORATORS = frozenset(
    {
        "classmethod",
        "staticmethod",
        "property",
        "cached_property",
        "abstractmethod",
        "overload",
        "override",
        "final",
        "lru_cache",
        "cache",
        "wraps",
    }
    | _PROPERTY_ACCESSORS
)
_BUILTIN_NAMES = frozenset(dir(builtins))


# ---------------------------------------------------------------------------
# 공개 자료형
# ---------------------------------------------------------------------------


@dataclass
class ApiRecord:
    """인덱싱된 API 하나. 상속·Factory 경유 정보를 포함한다."""

    api_id: str
    library_id: str
    module: str
    qualname: str
    kind: str
    signature: str
    description: str
    source_location: dict[str, Any]
    capabilities: list[str]
    limitations: list[str]
    catalog_revision: str
    verification_status: str
    resolution: str
    inherited_from: str | None
    access_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApiRecord:
        return cls(**data)

    def summary(self) -> ApiSummary:
        return ApiSummary(
            api_id=self.api_id,
            library_id=self.library_id,
            module=self.module,
            qualname=self.qualname,
            kind=self.kind,
            signature=self.signature,
            summary=self.description.split("\n", 1)[0].strip(),
            resolution=self.resolution,
            verification_status=self.verification_status,
        )


@dataclass
class ApiSummary:
    """검색 결과 항목. 토큰 절약을 위해 description·source_code를 싣지 않는다."""

    api_id: str
    library_id: str
    module: str
    qualname: str
    kind: str
    signature: str
    summary: str
    resolution: str
    verification_status: str
    # 이 API가 에이전트 표면 항목에 속하는지
    surface: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchResult:
    results: list[ApiSummary]
    total: int
    limit: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [item.to_dict() for item in self.results],
            "total": self.total,
            "limit": self.limit,
            "truncated": self.truncated,
        }


@dataclass
class ApiDescription:
    record: ApiRecord
    source_code: str
    catalog_status: str
    source_revision: str
    recipes: list[Recipe]

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "source_code": self.source_code,
            "catalog_status": self.catalog_status,
            "source_revision": self.source_revision,
            "recipes": [recipe.to_dict() for recipe in self.recipes],
        }


@dataclass
class IndexResult:
    """인덱싱 결과. spans·sources는 describe의 소스 구간 조회에만 쓰는 내부 자료다."""

    records: list[ApiRecord]
    spans: dict[str, list[Any]]  # api_id -> [relpath, start_line, end_line]
    sources: dict[str, str]  # relpath -> 인덱싱 시점 소스 텍스트
    issues: list[dict[str, Any]]
    api_index_revision: str


def make_api_id(library_id: str, module: str, qualname: str) -> str:
    # library_id 형식에 ':'가 없으므로 첫 구분자로 라이브러리를 찾을 수 있다
    return f"{library_id}:{module}:{qualname}"


def library_of_api_id(api_id: str) -> str:
    return api_id.split(":", 1)[0]


def compute_index_revision(records: Iterable[ApiRecord]) -> str:
    payload = json.dumps(
        [record.to_dict() for record in records], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_entrypoints(entrypoints: Any) -> list[dict[str, Any]]:
    """Adapter 진입점의 형태를 검사하고 입력 그대로의 사본을 돌려준다."""
    if entrypoints is None:
        return []
    if not isinstance(entrypoints, (list, tuple)):
        raise _entrypoint_error("entrypoints는 dict 목록이어야 합니다.")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in entrypoints:
        if not isinstance(item, dict):
            raise _entrypoint_error("진입점 항목은 dict여야 합니다.")
        unknown = set(item) - ENTRYPOINT_KEYS
        if unknown:
            raise _entrypoint_error(f"알 수 없는 진입점 키: {sorted(unknown)}", item)
        for key in ENTRYPOINT_REQUIRED:
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise _entrypoint_error(f"진입점 필수 키 '{key}'가 없거나 비었습니다.", item)
        for key in ("module", "factory", "returns"):
            if key in item and not (isinstance(item[key], str) and _DOTTED.fullmatch(item[key])):
                raise _entrypoint_error(f"진입점 '{key}' 형식이 잘못되었습니다.", item)
        if item["factory"].count(".") > 1:
            raise _entrypoint_error("factory는 'func' 또는 'Class.method' 형식이어야 합니다.", item)
        if item["id"] in seen:
            raise _entrypoint_error(f"진입점 id가 중복되었습니다: {item['id']}", item)
        seen.add(item["id"])
        result.append(dict(item))
    return result


def build_index(
    scan: SourceScan,
    library_id: str,
    source_revision: str,
    entrypoints: list[dict[str, Any]],
    *,
    strict_entrypoints: bool,
) -> IndexResult:
    """원본 트리를 AST로 인덱싱한다.

    strict_entrypoints가 참이면(등록) 잘못된 진입점을 ``INVALID_ENTRYPOINT``로 거절하고,
    거짓이면(재인덱싱) 원본 변화로 깨진 진입점을 issues로만 남긴다.
    """
    analyzer = _Analyzer(scan, library_id, source_revision)
    analyzer.build_records()
    analyzer.apply_entrypoints(entrypoints, strict=strict_entrypoints)
    records = sorted(analyzer.records.values(), key=lambda r: (r.module, r.qualname))
    used_paths = {span[0] for span in analyzer.spans.values()}
    return IndexResult(
        records=records,
        spans=analyzer.spans,
        sources={path: text for path, text in analyzer.texts.items() if path in used_paths},
        issues=analyzer.issues,
        api_index_revision=compute_index_revision(records),
    )


def normalize_search_args(query: Any, limit: Any) -> tuple[list[str], int]:
    if not isinstance(query, str) or not query.strip():
        raise LibraryError("INVALID_ARGUMENT", "검색어가 비었습니다.", {"query": query})
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise LibraryError("INVALID_ARGUMENT", "limit은 1 이상의 정수여야 합니다.", {"limit": limit})
    terms = list(dict.fromkeys(term.casefold() for term in query.split()))
    return terms, min(limit, SEARCH_MAX_LIMIT)


def search_records(
    records: Iterable[ApiRecord],
    terms: list[str],
    limit: int,
    *,
    surface_libraries: frozenset[str] = frozenset(),
) -> SearchResult:
    """용어 기반 검색. 점수는 정렬에만 쓰며 결과에 싣지 않는다.

    ``surface_libraries``(에이전트 표면 항목)의 API를 모두 앞에 두고, 각 그룹 안에서는 기존 점수 규칙을 따른다.
    """
    ranked: list[tuple[tuple[Any, ...], ApiRecord]] = []
    for record in records:
        qualname = record.qualname.casefold()
        module = record.module.casefold()
        description = record.description.casefold()
        last_name = qualname.rsplit(".", 1)[-1]
        name_hits = sum(term in qualname for term in terms)
        module_hits = sum(term in module for term in terms)
        text_hits = sum(term in description for term in terms)
        matched = sum(term in qualname or term in module or term in description for term in terms)
        if not matched:
            continue
        exact = any(term == last_name for term in terms)
        key = (int(record.library_id not in surface_libraries), -name_hits, -int(exact), -matched, -module_hits, -text_hits, record.library_id, record.module, qualname)
        ranked.append((key, record))
    ranked.sort(key=lambda pair: pair[0])
    results = []
    for _, record in ranked[:limit]:
        summary = record.summary()
        summary.surface = record.library_id in surface_libraries
        results.append(summary)
    return SearchResult(results=results, total=len(ranked), limit=limit, truncated=len(ranked) > len(results))


def source_segment(sources: dict[str, str], span: list[Any] | None) -> str:
    if not span:
        return ""
    relpath, start, end = span
    text = sources.get(relpath)
    if text is None or start is None:
        return ""
    return "\n".join(text.split("\n")[start - 1 : end])


# ---------------------------------------------------------------------------
# 내부 구현
# ---------------------------------------------------------------------------


def _entrypoint_error(message: str, item: Any = None) -> LibraryError:
    details = {"entrypoint": item} if isinstance(item, dict) else {}
    return LibraryError("INVALID_ENTRYPOINT", message, details)


@dataclass
class _Module:
    info: ModuleFile
    tree: ast.Module | None = None
    # 최상위 이름 → ("def", 노드) | ("import", 참조) | ("value", 대입식)
    bindings: dict[str, tuple[str, Any]] = field(default_factory=dict)
    stars: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def name(self) -> str:
        return self.info.name


@dataclass
class _Class:
    module: str
    name: str
    node: ast.ClassDef
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]


# 이름 해석 결과: ("class", module, name) | ("function", module, name) | ("module", name) | ("external", dotted)
_Ref = tuple[str, ...]
_ClassKey = tuple[str, str]


class _Analyzer:
    def __init__(self, scan: SourceScan, library_id: str, source_revision: str) -> None:
        self.library_id = library_id
        self.source_revision = source_revision
        self.top_level = set(scan.top_level)
        self.modules: dict[str, _Module] = {}
        self.classes: dict[_ClassKey, _Class] = {}
        self.texts: dict[str, str] = {}
        self.issues: list[dict[str, Any]] = []
        self.records: dict[str, ApiRecord] = {}
        self.spans: dict[str, list[Any]] = {}
        self._mro_cache: dict[_ClassKey, list[_ClassKey]] = {}
        self._base_cache: dict[_ClassKey, tuple[list[_ClassKey], list[str], list[str]]] = {}
        for info in scan.modules:
            if info.name in self.modules:
                continue  # 같은 이름의 .py와 네이티브 확장이 함께 있으면 .py 우선(정렬상 먼저 옴)
            self.modules[info.name] = self._load_module(info)

    # -- 파싱 -------------------------------------------------------------

    def _load_module(self, info: ModuleFile) -> _Module:
        module = _Module(info)
        if info.kind != "source":
            return module
        try:
            data = info.path.read_bytes()
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            text = data.decode(encoding)
        except (OSError, SyntaxError, UnicodeDecodeError, LookupError) as exc:
            module.error = f"소스를 읽지 못했습니다: {exc}"
            self.issues.append({"code": "READ_ERROR", "path": info.relpath, "message": module.error})
            return module
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(text, filename=info.relpath)
        except (SyntaxError, ValueError) as exc:
            line = getattr(exc, "lineno", None)
            module.error = f"{getattr(exc, 'msg', exc)}" + (f" (line {line})" if line else "")
            self.issues.append({"code": "SYNTAX_ERROR", "path": info.relpath, "message": module.error})
            return module
        # ast 줄 번호와 맞추기 위해 줄바꿈을 \n으로 통일해 보관한다
        self.texts[info.relpath] = text.replace("\r\n", "\n").replace("\r", "\n")
        module.tree = tree
        self._collect_namespace(module, tree.body, primary=True)
        return module

    def _collect_namespace(self, module: _Module, body: list[ast.stmt], *, primary: bool) -> None:
        """최상위 이름 바인딩을 모은다.

        ``try``/``if`` 본문은 주 경로로 보고, ``except``·``elif/else`` 분기는 아직 바인딩되지
        않은 이름에만 쓰는 대체 경로로 본다(예: 상대 Import 실패 시 절대 Import).
        """
        for stmt in body:
            if isinstance(stmt, (ast.Try, ast.TryStar)):
                self._collect_namespace(module, stmt.body, primary=primary)
                for handler in stmt.handlers:
                    self._collect_namespace(module, handler.body, primary=False)
                self._collect_namespace(module, stmt.orelse, primary=primary)
                self._collect_namespace(module, stmt.finalbody, primary=primary)
            elif isinstance(stmt, ast.If):
                self._collect_namespace(module, stmt.body, primary=primary)
                self._collect_namespace(module, stmt.orelse, primary=False)
            elif isinstance(stmt, (*_FUNCTION_NODES, ast.ClassDef)):
                bound = _bind(module, stmt.name, ("def", stmt), primary)
                if bound and isinstance(stmt, ast.ClassDef):
                    self.classes[(module.name, stmt.name)] = _Class(
                        module.name, stmt.name, stmt, _collect_methods(stmt)
                    )
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if alias.asname:
                        _bind(module, alias.asname, ("import", ("module", alias.name)), primary)
                    else:
                        head = alias.name.split(".", 1)[0]
                        _bind(module, head, ("import", ("module", head)), primary)
            elif isinstance(stmt, ast.ImportFrom):
                source = self._absolute_module(module, stmt.module, stmt.level)
                if source is None:
                    continue
                for alias in stmt.names:
                    if alias.name == "*":
                        module.stars.append(source)
                    else:
                        ref = ("from", source, alias.name)
                        _bind(module, alias.asname or alias.name, ("import", ref), primary)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        _bind(module, target.id, ("value", stmt.value), primary)

    def _absolute_module(self, module: _Module, name: str | None, level: int) -> str | None:
        if level == 0:
            return name
        parts = module.name.split(".")
        package = parts if module.info.is_package else parts[:-1]
        drop = level - 1
        if drop >= len(package):
            return None  # 최상위 밖으로 나가는 상대 Import
        base = package[: len(package) - drop]
        return ".".join(base + ([name] if name else []))

    # -- 이름 해석 ----------------------------------------------------------

    def _in_library(self, dotted: str) -> bool:
        return dotted.split(".", 1)[0] in self.top_level

    def lookup(self, module_name: str, name: str, seen: set[tuple[str, str]] | None = None) -> _Ref | None:
        """모듈 네임스페이스에서 이름을 정적으로 찾는다. 찾지 못하면 None."""
        seen = set() if seen is None else seen
        if (module_name, name) in seen:
            return None
        seen.add((module_name, name))
        module = self.modules.get(module_name)
        if module is None:
            submodule = f"{module_name}.{name}"
            if submodule in self.modules:
                return ("module", submodule)
            if self._in_library(module_name):
                return None
            return ("external", submodule)
        binding = module.bindings.get(name)
        if binding is not None:
            kind, value = binding
            if kind == "def":
                return ("class" if isinstance(value, ast.ClassDef) else "function", module_name, name)
            if kind == "import":
                if value[0] == "from":
                    return self.lookup(value[1], value[2], seen)
                target = value[1]
                if target in self.modules or self._in_library(target):
                    return ("module", target)
                return ("external", target)
            if isinstance(value, (ast.Name, ast.Attribute)):
                return self.resolve_expr(module_name, value, seen)
            return None  # 호출 결과 등 정적으로 알 수 없는 값에 묶인 이름
        submodule = f"{module_name}.{name}"
        if submodule in self.modules:
            return ("module", submodule)
        for star in module.stars:
            found = self.lookup(star, name, seen)
            if found is not None:
                return found
        if name in _BUILTIN_NAMES:
            return ("external", f"builtins.{name}")
        return None

    def resolve_expr(self, module_name: str, expr: ast.expr, seen: set[tuple[str, str]] | None = None) -> _Ref | None:
        if isinstance(expr, ast.Name):
            return self.lookup(module_name, expr.id, seen)
        if isinstance(expr, ast.Attribute):
            base = self.resolve_expr(module_name, expr.value, seen)
            if base is None:
                return None
            if base[0] == "module":
                return self.lookup(base[1], expr.attr, seen)
            if base[0] == "external":
                return ("external", f"{base[1]}.{expr.attr}")
            return None
        if isinstance(expr, ast.Subscript):
            return self.resolve_expr(module_name, expr.value, seen)
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            try:
                parsed = ast.parse(expr.value, mode="eval").body
            except SyntaxError:
                return None
            return self.resolve_expr(module_name, parsed, seen)
        return None

    def resolve_dotted_class(self, dotted: str) -> _ClassKey | None:
        parts = dotted.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            module_name = ".".join(parts[:cut])
            if module_name in self.modules:
                ref: _Ref | None = ("module", module_name)
                for attr in parts[cut:]:
                    if ref is None or ref[0] != "module":
                        return None
                    ref = self.lookup(ref[1], attr)
                return (ref[1], ref[2]) if ref and ref[0] == "class" else None
        return None

    # -- 상속 ---------------------------------------------------------------

    def bases(self, key: _ClassKey) -> tuple[list[_ClassKey], list[str], list[str]]:
        """(라이브러리 기반 클래스, 외부 기반 이름, unresolved 사유)."""
        if key in self._base_cache:
            return self._base_cache[key]
        cls = self.classes[key]
        library: list[_ClassKey] = []
        external: list[str] = []
        reasons: list[str] = []
        for base in cls.node.bases:
            text = ast.unparse(base)
            if isinstance(base, ast.Call):
                reasons.append(f"기반 클래스가 호출식이라 정적으로 확정할 수 없음: {text}")
                continue
            ref = self.resolve_expr(cls.module, base)
            if ref is None:
                reasons.append(f"기반 클래스를 정적으로 확인할 수 없음: {text}")
            elif ref[0] == "class":
                library.append((ref[1], ref[2]))
            elif ref[0] == "external":
                if ref[1] != "builtins.object":
                    external.append(ref[1])
            else:
                reasons.append(f"기반 클래스가 클래스가 아님: {text}")
        for name in ("__getattr__", "__getattribute__"):
            if name in cls.methods:
                reasons.append(f"{name}로 속성 접근을 동적으로 위임함")
        self._base_cache[key] = (library, external, reasons)
        return self._base_cache[key]

    def mro(self, key: _ClassKey, stack: tuple[_ClassKey, ...] = ()) -> list[_ClassKey]:
        """라이브러리 안 클래스만으로 C3 선형화한 MRO. 외부 기반은 포함하지 않는다."""
        if key in self._mro_cache:
            return self._mro_cache[key]
        if key in stack:
            return [key]
        library = self.bases(key)[0]
        sequences = [self.mro(base, stack + (key,)) for base in library] + [list(library)]
        merged = _c3_merge(sequences)
        if merged is None:
            merged = list(dict.fromkeys(k for seq in sequences for k in seq))
        result = [key] + [k for k in merged if k != key]
        self._mro_cache[key] = result
        return result

    def class_unresolved_reasons(self, key: _ClassKey) -> list[str]:
        reasons = list(self.bases(key)[2])
        for ancestor in self.mro(key)[1:]:
            if self.bases(ancestor)[2]:
                reasons.append(f"기반 클래스 {ancestor[0]}.{ancestor[1]}가 unresolved임")
        return reasons

    def find_method(self, key: _ClassKey, name: str) -> tuple[_ClassKey, ast.FunctionDef | ast.AsyncFunctionDef] | None:
        for owner in self.mro(key):
            method = self.classes[owner].methods.get(name)
            if method is not None:
                return owner, method
        return None

    # -- 레코드 생성 --------------------------------------------------------

    def build_records(self) -> None:
        for module in self.modules.values():
            if not _is_public_path(module.name):
                continue
            if module.info.kind == "native":
                self._add_module_record(
                    module,
                    resolution="unresolved",
                    limitations=["네이티브 확장 모듈이라 정적 분석으로 API를 확인할 수 없음"],
                )
                continue
            if module.tree is None:
                self._add_module_record(
                    module,
                    resolution="unresolved",
                    limitations=[f"소스를 분석하지 못해 인덱싱하지 않음: {module.error}"],
                )
                continue
            self._add_module_record(module, resolution="resolved", limitations=[])
            for name, (kind, node) in module.bindings.items():
                if kind != "def" or name.startswith("_"):
                    continue
                if isinstance(node, ast.ClassDef):
                    self._add_class_records((module.name, name))
                else:
                    self._add_function_record(module.name, name, node, owner=None, inherited_from=None)

    def _add_module_record(self, module: _Module, *, resolution: str, limitations: list[str]) -> None:
        description = ""
        if module.tree is not None:
            description = ast.get_docstring(module.tree, clean=True) or ""
        line = 1 if module.info.kind == "source" else None
        record = self._new_record(
            module.name,
            "",
            kind="module",
            signature="",
            description=description,
            path=module.info.relpath,
            line=line,
            capabilities=[],
            limitations=limitations,
            resolution=resolution,
        )
        if module.tree is not None:
            self.spans[record.api_id] = [module.info.relpath, 1, len(self.texts[module.info.relpath].split("\n"))]

    def _add_class_records(self, key: _ClassKey) -> None:
        cls = self.classes[key]
        library, external, _ = self.bases(key)
        reasons = self.class_unresolved_reasons(key)
        limitations = list(reasons)
        limitations += [f"외부 기반 클래스 {name}의 상속 멤버는 인덱싱하지 않음" for name in external]
        decorator_notes = _decorator_limitations(cls.node)
        limitations += decorator_notes
        init = self.find_method(key, "__init__")
        if init is not None:
            signature = _format_signature(init[1].args, None, drop_first=True)
        elif not (external or reasons or cls.node.decorator_list):
            signature = "()"
        else:
            signature = ""
        record = self._new_record(
            cls.module,
            cls.name,
            kind="class",
            signature=signature,
            description=ast.get_docstring(cls.node, clean=True) or "",
            path=self.modules[cls.module].info.relpath,
            line=cls.node.lineno,
            capabilities=[],
            limitations=limitations,
            resolution="unresolved" if reasons else "resolved",
        )
        self.spans[record.api_id] = self._span(cls.module, cls.node)

        seen: set[str] = set()
        for owner in self.mro(key):
            for name, node in self.classes[owner].methods.items():
                if name.startswith("_") or name in seen:
                    continue
                seen.add(name)
                inherited_from = None
                if owner != key:
                    origin = self.classes[owner]
                    if _is_public_path(origin.module) and not origin.name.startswith("_"):
                        inherited_from = make_api_id(self.library_id, origin.module, f"{origin.name}.{name}")
                self._add_function_record(
                    cls.module,
                    f"{cls.name}.{name}",
                    node,
                    owner=owner,
                    inherited_from=inherited_from,
                )

    def _add_function_record(
        self,
        module_name: str,
        qualname: str,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        owner: _ClassKey | None,
        inherited_from: str | None,
    ) -> None:
        # 상속 레코드도 원 정의의 위치·시그니처·설명을 그대로 쓴다
        defined_in = owner[0] if owner else module_name
        record = self._new_record(
            module_name,
            qualname,
            kind="method" if owner else "function",
            signature=_format_signature(node.args, node.returns),
            description=ast.get_docstring(node, clean=True) or "",
            path=self.modules[defined_in].info.relpath,
            line=node.lineno,
            capabilities=_capabilities(node),
            limitations=_decorator_limitations(node),
            resolution="resolved",
            inherited_from=inherited_from,
        )
        self.spans[record.api_id] = self._span(defined_in, node)

    def _new_record(
        self,
        module: str,
        qualname: str,
        *,
        kind: str,
        signature: str,
        description: str,
        path: str,
        line: int | None,
        capabilities: list[str],
        limitations: list[str],
        resolution: str,
        inherited_from: str | None = None,
    ) -> ApiRecord:
        api_id = make_api_id(self.library_id, module, qualname)
        record = ApiRecord(
            api_id=api_id,
            library_id=self.library_id,
            module=module,
            qualname=qualname,
            kind=kind,
            signature=signature,
            description=description,
            source_location={"path": path, "line": line},
            capabilities=capabilities,
            limitations=limitations,
            catalog_revision=self.source_revision,
            verification_status="unverified",
            resolution=resolution,
            inherited_from=inherited_from,
            access_paths=[],
        )
        self.records[api_id] = record
        return record

    def _span(self, module_name: str, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> list[Any]:
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        return [self.modules[module_name].info.relpath, start, node.end_lineno or node.lineno]

    # -- Factory 진입점 -----------------------------------------------------

    def apply_entrypoints(self, entrypoints: list[dict[str, Any]], *, strict: bool) -> None:
        for entrypoint in entrypoints:
            try:
                returned = self._resolve_entrypoint(entrypoint)
            except LibraryError as exc:
                if strict:
                    raise
                self.issues.append(
                    {"code": "INVALID_ENTRYPOINT", "entrypoint": entrypoint["id"], "message": exc.message}
                )
                continue
            if returned is None:
                self.issues.append(
                    {
                        "code": "ENTRYPOINT_UNRESOLVED",
                        "entrypoint": entrypoint["id"],
                        "message": "Factory 반환 클래스를 정적으로 추론하지 못했고 returns도 지정되지 않았습니다.",
                    }
                )
                continue
            prefix = f"{entrypoint['module']}.{entrypoint['factory']}()"
            self._attach_access_paths(returned, prefix)
            for attr, service in self._service_attributes(returned):
                self._attach_access_paths(service, f"{prefix}.{attr}")

    def _resolve_entrypoint(self, entrypoint: dict[str, Any]) -> _ClassKey | None:
        module_name = entrypoint["module"]
        module = self.modules.get(module_name)
        if module is None:
            raise _entrypoint_error(f"라이브러리에 모듈이 없습니다: {module_name}", entrypoint)
        if module.tree is None:
            raise _entrypoint_error(f"모듈을 정적으로 분석할 수 없습니다: {module_name}", entrypoint)

        parts = entrypoint["factory"].split(".")
        ref = self.lookup(module_name, parts[0])
        inferred: _ClassKey | None = None
        if len(parts) == 1:
            if ref is not None and ref[0] == "class":
                inferred = (ref[1], ref[2])
            elif ref is not None and ref[0] == "function":
                node = self.modules[ref[1]].bindings[ref[2]][1]
                inferred = self._infer_return(ref[1], node, owner=None)
            else:
                raise _entrypoint_error(f"Factory 함수를 찾지 못했습니다: {entrypoint['factory']}", entrypoint)
        else:
            if ref is None or ref[0] != "class":
                raise _entrypoint_error(f"Factory 클래스를 찾지 못했습니다: {parts[0]}", entrypoint)
            owner = (ref[1], ref[2])
            found = self.find_method(owner, parts[1])
            if found is None:
                raise _entrypoint_error(f"Factory 메서드를 찾지 못했습니다: {entrypoint['factory']}", entrypoint)
            inferred = self._infer_return(found[0][0], found[1], owner=owner)

        declared: _ClassKey | None = None
        if "returns" in entrypoint:
            declared = self.resolve_dotted_class(entrypoint["returns"])
            if declared is None:
                raise _entrypoint_error(f"returns 클래스를 찾지 못했습니다: {entrypoint['returns']}", entrypoint)
        return inferred or declared

    def _infer_return(
        self,
        module_name: str,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        owner: _ClassKey | None,
        depth: int = 0,
    ) -> _ClassKey | None:
        """반환 annotation, ``return Cls(...)``, 싱글톤 대입 순으로 반환 클래스를 추론한다."""
        if node.returns is not None:
            annotated = self._annotation_classes(module_name, node.returns)
            if len(annotated) == 1:
                return annotated.pop()

        owner_name = owner[1] if owner else None
        assigns: dict[str, ast.expr] = {}
        returns: list[ast.expr] = []
        for sub in _walk_body(node):
            if isinstance(sub, ast.Assign):
                for target in sub.targets:
                    assigns[_target_key(target, owner_name)] = sub.value
            elif isinstance(sub, ast.AnnAssign) and sub.value is not None:
                assigns[_target_key(sub.target, owner_name)] = sub.value
            elif isinstance(sub, ast.Return) and sub.value is not None:
                if not (isinstance(sub.value, ast.Constant) and sub.value.value is None):
                    returns.append(sub.value)

        found: set[_ClassKey] = set()
        for value in returns:
            cls = self._value_class(module_name, value, assigns, owner, owner_name, depth, set())
            if cls is None:
                return None
            found.add(cls)
        return found.pop() if len(found) == 1 else None

    def _value_class(
        self,
        module_name: str,
        expr: ast.expr,
        assigns: dict[str, ast.expr],
        owner: _ClassKey | None,
        owner_name: str | None,
        depth: int,
        visiting: set[str],
    ) -> _ClassKey | None:
        if isinstance(expr, ast.Call):
            return self._call_class(module_name, expr, owner, depth)
        if isinstance(expr, (ast.Name, ast.Attribute)):
            key = _target_key(expr, owner_name)
            if key in visiting:
                return None
            if key in assigns:
                return self._value_class(
                    module_name, assigns[key], assigns, owner, owner_name, depth, visiting | {key}
                )
            if isinstance(expr, ast.Name):
                binding = self.modules[module_name].bindings.get(expr.id)
                if binding is not None and binding[0] == "value" and isinstance(binding[1], ast.Call):
                    return self._call_class(module_name, binding[1], None, depth)
        return None

    def _call_class(
        self, module_name: str, call: ast.Call, owner: _ClassKey | None, depth: int
    ) -> _ClassKey | None:
        func = call.func
        if owner is not None and isinstance(func, ast.Name) and func.id == "cls":
            return owner
        ref = self.resolve_expr(module_name, func)
        if ref is None:
            return None
        if ref[0] == "class":
            return (ref[1], ref[2])
        if ref[0] == "function" and depth < 3:
            node = self.modules[ref[1]].bindings[ref[2]][1]
            return self._infer_return(ref[1], node, owner=None, depth=depth + 1)
        return None

    def _annotation_classes(self, module_name: str, annotation: ast.expr) -> set[_ClassKey]:
        if isinstance(annotation, ast.Constant):
            if not isinstance(annotation.value, str):
                return set()
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return set()
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return self._annotation_classes(module_name, annotation.left) | self._annotation_classes(
                module_name, annotation.right
            )
        if isinstance(annotation, ast.Subscript):
            ref = self.resolve_expr(module_name, annotation.value)
            if ref is not None and ref[0] == "external" and ref[1].rsplit(".", 1)[-1] in ("Optional", "Union"):
                inner = annotation.slice
                elements = inner.elts if isinstance(inner, ast.Tuple) else [inner]
                result: set[_ClassKey] = set()
                for element in elements:
                    result |= self._annotation_classes(module_name, element)
                return result
        ref = self.resolve_expr(module_name, annotation)
        return {(ref[1], ref[2])} if ref is not None and ref[0] == "class" else set()

    def _service_attributes(self, key: _ClassKey) -> list[tuple[str, _ClassKey]]:
        """``__init__``에서 ``self.<attr> = Cls(...)``로 보관한 공개 서비스 인스턴스."""
        found: dict[str, _ClassKey] = {}
        for owner in self.mro(key):
            init = self.classes[owner].methods.get("__init__")
            if init is None:
                continue
            for sub in _walk_body(init):
                if isinstance(sub, ast.Assign):
                    targets, value = sub.targets, sub.value
                elif isinstance(sub, ast.AnnAssign) and sub.value is not None:
                    targets, value = [sub.target], sub.value
                else:
                    continue
                if not isinstance(value, ast.Call):
                    continue
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and not target.attr.startswith("_")
                        and target.attr not in found
                    ):
                        service = self._call_class(owner[0], value, None, 0)
                        if service is not None:
                            found[target.attr] = service
            if not _calls_super_init(init):
                break
        return list(found.items())

    def _attach_access_paths(self, key: _ClassKey, prefix: str) -> None:
        cls = self.classes.get(key)
        if cls is None:
            return
        head = f"{cls.name}."
        for record in self.records.values():
            if record.kind == "method" and record.module == cls.module and record.qualname.startswith(head):
                method = record.qualname[len(head) :]
                if "." in method:
                    continue
                path = f"{prefix}.{method}"
                if path not in record.access_paths:
                    record.access_paths.append(path)


def _bind(module: _Module, name: str, value: tuple[str, Any], primary: bool) -> bool:
    if primary or name not in module.bindings:
        module.bindings[name] = value
        return True
    return False


def _collect_methods(node: ast.ClassDef) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for stmt in node.body:
        if not isinstance(stmt, _FUNCTION_NODES):
            continue
        # property setter/deleter는 같은 이름의 getter를 대체하지 않는다
        if stmt.name in methods and any(
            isinstance(d, ast.Attribute) and d.attr in _PROPERTY_ACCESSORS for d in stmt.decorator_list
        ):
            continue
        methods[stmt.name] = stmt
    return methods


def _walk_body(node: ast.AST) -> Iterable[ast.AST]:
    """중첩 함수·클래스·람다 안은 제외하고 함수 본문 노드를 순회한다."""
    stack = list(reversed(getattr(node, "body", [])))
    while stack:
        current = stack.pop()
        yield current
        for child in reversed(list(ast.iter_child_nodes(current))):
            if not isinstance(child, _SCOPE_NODES):
                stack.append(child)


def _target_key(expr: ast.expr, owner_name: str | None) -> str:
    """``cls._x``·``self._x``·``Owner._x``를 같은 대상으로 보도록 정규화한다."""
    if isinstance(expr, ast.Attribute):
        return f"{_target_key(expr.value, owner_name)}.{expr.attr}"
    if isinstance(expr, ast.Name):
        if expr.id in ("cls", "self") or (owner_name is not None and expr.id == owner_name):
            return "<owner>"
        return expr.id
    return ast.dump(expr)


def _calls_super_init(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for sub in _walk_body(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "__init__":
            return True
    return False


def _c3_merge(sequences: list[list[_ClassKey]]) -> list[_ClassKey] | None:
    pending = [list(seq) for seq in sequences if seq]
    result: list[_ClassKey] = []
    while pending:
        for seq in pending:
            head = seq[0]
            if not any(head in other[1:] for other in pending):
                break
        else:
            return None
        result.append(head)
        pending = [seq[1:] if seq[0] == head else seq for seq in pending]
        pending = [seq for seq in pending if seq]
    return result


def _is_public_path(dotted: str) -> bool:
    return not any(part.startswith("_") for part in dotted.split("."))


def _decorator_name(decorator: ast.expr) -> str:
    if isinstance(decorator, ast.Call):
        decorator = decorator.func
    if isinstance(decorator, ast.Attribute):
        return decorator.attr
    if isinstance(decorator, ast.Name):
        return decorator.id
    return ""


def _capabilities(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    capabilities: list[str] = []
    if isinstance(node, ast.AsyncFunctionDef):
        capabilities.append("async")
    names = {_decorator_name(d) for d in node.decorator_list}
    if "classmethod" in names:
        capabilities.append("classmethod")
    if "staticmethod" in names:
        capabilities.append("staticmethod")
    if names & {"property", "cached_property"}:
        capabilities.append("property")
    if "abstractmethod" in names:
        capabilities.append("abstract")
    return capabilities


def _decorator_limitations(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    notes = []
    for decorator in node.decorator_list:
        if _decorator_name(decorator) not in _KNOWN_DECORATORS:
            notes.append(f"데코레이터 @{ast.unparse(decorator)}가 실제 시그니처·동작을 바꿀 수 있음")
    return notes


def _format_signature(args: ast.arguments, returns: ast.expr | None, *, drop_first: bool = False) -> str:
    """소스 표기 그대로의 시그니처 문자열을 만든다."""
    positional = list(args.posonlyargs) + list(args.args)
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    posonly_count = len(args.posonlyargs)
    if drop_first and positional:
        positional, defaults = positional[1:], defaults[1:]
        posonly_count = max(posonly_count - 1, 0)

    params: list[str] = []
    for index, (arg, default) in enumerate(zip(positional, defaults)):
        params.append(_format_param(arg, default))
        if posonly_count and index == posonly_count - 1:
            params.append("/")
    if args.vararg is not None:
        params.append("*" + _format_param(args.vararg, None))
    elif args.kwonlyargs:
        params.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        params.append(_format_param(arg, default))
    if args.kwarg is not None:
        params.append("**" + _format_param(args.kwarg, None))

    signature = f"({', '.join(params)})"
    if returns is not None:
        signature += f" -> {ast.unparse(returns)}"
    return signature


def _format_param(arg: ast.arg, default: ast.expr | None) -> str:
    text = arg.arg
    if arg.annotation is not None:
        text += f": {ast.unparse(arg.annotation)}"
    if default is not None:
        text += (" = " if arg.annotation is not None else "=") + ast.unparse(default)
    return text
