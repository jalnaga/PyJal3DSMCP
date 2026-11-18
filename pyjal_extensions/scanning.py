"""Max에서 실행할 부작용 없는 직계 라이브러리 후보 탐색 스크립트.

``ATTACH_SCRIPT``(``library_attach``)는 ``SCAN_SCRIPT`` 뒤에 붙어 Catalog 항목의 최상위 이름 아래 로드된 모듈의 실제 파일·
수정 시각과 사전 조건 모듈의 로드 여부를 더 모은다. ``sys.path``·``sys.modules``를 바꾸지 않고 아무것도 import하지 않으며
모듈 속성 접근 훅을 실행하지 않는다(``__dict__``만 직접 읽음).
"""

SCAN_SCRIPT = r'''
import os
import sys
import types
import zipimport


def _scan_norm(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _scan_warning(path, exc):
    _scan_warnings.append({"path": path, "code": type(exc).__name__,
                           "message": str(exc) or type(exc).__name__})


def _scan_add(name, path, kind, evidence, index=None):
    path = _scan_norm(path)
    row = _scan_rows.get(path)
    if row is None:
        row = {"name": name, "path": path, "kind": kind, "evidence": [],
               "sys_path_indices": [], "loaded": False,
               "registered_library_ids": [], "source_path": None,
               "unsupported_reason": "이 후보 종류는 단독 소스 등록을 지원하지 않습니다."}
        if kind == "package":
            init = os.path.join(path, "__init__.py")
            try:
                if not os.path.basename(path).isidentifier() or os.path.basename(path) != os.path.normcase(name):
                    raise ValueError("실제 패키지 디렉터리 이름이 발견된 import 이름과 다릅니다.")
                if not os.path.isfile(init):
                    raise FileNotFoundError(init)
                if os.path.commonpath([path, _scan_norm(init)]) != path:
                    raise ValueError("패키지 초기화 파일이 원본 경계를 벗어납니다.")
                with open(init, "rb"):
                    pass
                row["source_path"] = path
                row["unsupported_reason"] = None
            except (OSError, ValueError) as exc:
                row["unsupported_reason"] = str(exc)
                _scan_warning(path, exc)
        _scan_rows[path] = row
        if kind != "zip" and not os.path.exists(path):
            _scan_warning(path, FileNotFoundError("발견된 원본 경로가 더는 존재하지 않습니다."))
    if evidence not in row["evidence"]:
        row["evidence"].append(evidence)
    if index is not None and index not in row["sys_path_indices"]:
        row["sys_path_indices"].append(index)
    if evidence == "sys.modules":
        row["loaded"] = True


def _scan_file(name):
    if name.endswith(".py") and name[:-3].isidentifier():
        return name[:-3], "module"
    if name.endswith((".pyd", ".so")) and name.split(".", 1)[0].isidentifier():
        return name.split(".", 1)[0], "native"
    return None


def _scan_archive(path):
    # ZIP 경로 뒤에 붙은 내부 디렉터리도 추출 없이 식별한다.
    archive = path
    while not os.path.isfile(archive):
        parent = os.path.dirname(archive)
        if parent == archive:
            return None
        archive = parent
    try:
        zipimport.zipimporter(archive)
    except zipimport.ZipImportError:
        return None
    prefix = path[len(archive):].replace(os.sep, "/").strip("/")
    return archive, (prefix + "/" if prefix else "")


def _scan_zip_members(filename):
    # sys.path가 stdlib을 포함하지 않아도 동작하도록 중앙 디렉터리만 읽는다.
    # 압축 데이터·라이브러리 코드는 열거나 추출하지 않는다.
    with open(filename, "rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 65557))
        tail = handle.read()
        end = tail.rfind(b"PK\x05\x06")
        while end >= 0:
            if len(tail) - end >= 22 and int.from_bytes(tail[end + 20:end + 22], "little") == len(tail) - end - 22:
                break
            end = tail.rfind(b"PK\x05\x06", 0, end)
        if end < 0 or len(tail) - end < 22:
            raise ValueError("ZIP 중앙 디렉터리 종료 레코드가 없습니다.")
        record = tail[end:]
        number = lambda data: int.from_bytes(data, "little")
        if number(record[4:8]) != 0:
            raise ValueError("분할 ZIP 탐색은 지원하지 않습니다.")
        directory_size = number(record[12:16])
        directory_end = size - len(tail) + end
        if directory_size == 0xffffffff or number(record[10:12]) == 0xffff:
            handle.seek(directory_end - 20)
            locator = handle.read(20)
            if locator[:4] != b"PK\x06\x07":
                raise ValueError("ZIP64 위치 레코드가 없습니다.")
            directory_end = number(locator[8:16])
            handle.seek(directory_end)
            extended = handle.read(56)
            if extended[:4] != b"PK\x06\x06" or len(extended) != 56:
                raise ValueError("ZIP64 종료 레코드가 잘못되었습니다.")
            directory_size = number(extended[40:48])
        start = directory_end - directory_size
        if start < 0:
            raise ValueError("ZIP 중앙 디렉터리 범위가 잘못되었습니다.")
        handle.seek(start)
        while handle.tell() < directory_end:
            header = handle.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise ValueError("ZIP 중앙 디렉터리 항목이 잘못되었습니다.")
            length = number(header[28:30])
            name = handle.read(length)
            if len(name) != length:
                raise ValueError("ZIP 파일 이름이 잘렸습니다.")
            encoding = "utf-8" if number(header[8:10]) & 0x800 else "cp437"
            yield name.decode(encoding).replace("\\", "/")
            handle.seek(number(header[30:32]) + number(header[32:34]), 1)
            if handle.tell() > directory_end:
                raise ValueError("ZIP 항목이 중앙 디렉터리 범위를 벗어납니다.")


def _scan_path(path, index):
    if os.path.isdir(path):
        with os.scandir(path) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            try:
                if entry.name == "__pycache__":
                    continue
                if entry.is_dir() and entry.name.isidentifier():
                    kind = "package" if os.path.isfile(os.path.join(entry.path, "__init__.py")) else "namespace"
                    _scan_add(entry.name, entry.path, kind, "sys.path", index)
                elif entry.is_file():
                    found = _scan_file(entry.name)
                    if found:
                        _scan_add(found[0], entry.path, found[1], "sys.path", index)
            except (OSError, ValueError) as exc:
                _scan_warning(entry.path, exc)
        return
    archive = _scan_archive(path)
    if archive is None:
        raise FileNotFoundError("탐색 가능한 디렉터리 또는 ZIP 경로가 없습니다: " + path)
    filename, prefix = archive
    members = set(_scan_zip_members(filename))
    for member in sorted(members):
        if not member.startswith(prefix):
            continue
        relative = member[len(prefix):]
        parts = relative.split("/")
        if len(parts) == 2 and parts[1] == "__init__.py" and parts[0].isidentifier():
            _scan_add(parts[0], os.path.join(filename, prefix, parts[0]), "zip", "sys.path", index)
        elif len(parts) == 1:
            found = _scan_file(relative)
            if found:
                _scan_add(found[0], os.path.join(filename, member), "zip", "sys.path", index)


_scan_rows = {}
_scan_warnings = []
for _scan_index, _scan_entry in enumerate(list(sys.path)):
    if not isinstance(_scan_entry, str):
        continue
    try:
        _scan_path(_scan_norm(_scan_entry), _scan_index)
    except (OSError, ValueError, zipimport.ZipImportError) as _scan_exc:
        _scan_warning(_scan_entry, _scan_exc)

for _scan_name, _scan_module in list(sys.modules.items()):
    # 모듈 서브클래스와 임의 프록시의 속성 접근 훅을 실행하지 않는다.
    if not isinstance(_scan_name, str) or not _scan_name.isidentifier() or type(_scan_module) is not types.ModuleType:
        continue
    _scan_meta = types.ModuleType.__getattribute__(_scan_module, "__dict__")
    _scan_file_path = _scan_meta.get("__file__")
    _scan_paths = _scan_meta.get("__path__")
    try:
        if isinstance(_scan_file_path, str) and _scan_file_path:
            _scan_location = _scan_norm(_scan_file_path)
            _scan_zip = _scan_archive(_scan_location)
            if _scan_zip is not None:
                if os.path.basename(_scan_location) in ("__init__.py", "__init__.pyc"):
                    _scan_location = os.path.dirname(_scan_location)
                _scan_kind = "zip"
            elif "__path__" in _scan_meta:
                _scan_location = os.path.dirname(_scan_location)
                _scan_kind = "package"
            else:
                _scan_found = _scan_file(os.path.basename(_scan_location))
                if _scan_found is None:
                    continue
                _scan_kind = _scan_found[1]
            _scan_add(_scan_name, _scan_location, _scan_kind, "sys.modules")
        else:
            # 실제 _NamespacePath 순회는 경로 재계산·finder 훅을 부를 수 있다.
            # importlib이 보관한 경로 스냅샷만 직접 읽는다.
            _scan_external = sys.modules.get("_frozen_importlib_external")
            _scan_namespace_type = None
            if type(_scan_external) is types.ModuleType:
                _scan_namespace_type = types.ModuleType.__getattribute__(
                    _scan_external, "__dict__").get("_NamespacePath")
            if _scan_namespace_type is not None and type(_scan_paths) is _scan_namespace_type:
                _scan_paths = object.__getattribute__(_scan_paths, "_path")
            if type(_scan_paths) not in (list, tuple):
                continue
            for _scan_location in _scan_paths:
                if isinstance(_scan_location, str):
                    _scan_add(_scan_name, _scan_location, "namespace", "sys.modules")
    except (OSError, ValueError, zipimport.ZipImportError) as _scan_exc:
        _scan_warning(_scan_file_path or _scan_name, _scan_exc)

_scan_registered_keys = {}
for _scan_registration in _SCAN_REGISTERED:
    _scan_registered_keys[_scan_registration["library_id"]] = [_scan_norm(path) for path in _scan_registration["paths"]]
_scan_matched = set()
for _scan_row in _scan_rows.values():
    for _scan_registration in _SCAN_REGISTERED:
        if _scan_row["path"] in _scan_registered_keys[_scan_registration["library_id"]]:
            _scan_row["registered_library_ids"].append(_scan_registration["library_id"])
            _scan_matched.add(_scan_registration["library_id"])

# 서버(Catalog)와 Max가 같은 폴더를 다른 표기(로컬 드라이브와 UNC)로 볼 때: 문자열로 맞지 않은 등록 경로만 파일 ID로 다시 맞춘다.
# 로드된 후보만 대상이며 등록 경로가 이 Max에서 보이지 않으면 건너뛴다
_scan_registered_stats = []
for _scan_registration in _SCAN_REGISTERED:
    if _scan_registration["library_id"] in _scan_matched:
        continue
    for _scan_key in _scan_registered_keys[_scan_registration["library_id"]]:
        try:
            _scan_stat = os.stat(_scan_key)
        except (OSError, ValueError):
            continue
        if _scan_stat.st_ino:
            _scan_registered_stats.append((_scan_registration["library_id"], _scan_stat))
if _scan_registered_stats:
    for _scan_row in _scan_rows.values():
        if not _scan_row["loaded"] or _scan_row["registered_library_ids"]:
            continue
        try:
            _scan_stat = os.stat(_scan_row["path"])
        except (OSError, ValueError):
            continue
        for _scan_library_id, _scan_registered_stat in _scan_registered_stats:
            if os.path.samestat(_scan_stat, _scan_registered_stat) and _scan_library_id not in _scan_row["registered_library_ids"]:
                _scan_row["registered_library_ids"].append(_scan_library_id)
result = {"dlel": {"modules": [], "candidates": list(_scan_rows.values()), "warnings": _scan_warnings}}
'''

# 앞에 ``_ATTACH_NAMES``(최상위 이름 목록)·``_ATTACH_REQUIRED``(사전 조건 모듈 목록)를 정의하고 SCAN_SCRIPT 뒤에 붙인다.
ATTACH_SCRIPT = r'''
def _attach_location(meta):
    location = meta.get("__file__")
    if isinstance(location, str) and location:
        return location
    paths = meta.get("__path__")
    _attach_external = sys.modules.get("_frozen_importlib_external")
    if type(_attach_external) is types.ModuleType:
        _attach_namespace = types.ModuleType.__getattribute__(_attach_external, "__dict__").get("_NamespacePath")
        if _attach_namespace is not None and type(paths) is _attach_namespace:
            paths = object.__getattribute__(paths, "_path")
    if type(paths) in (list, tuple):
        for item in paths:
            if isinstance(item, str) and item:
                return item
    return None


_attach_names = set(_ATTACH_NAMES)
_attach_modules = []
for _attach_name, _attach_module in list(sys.modules.items()):
    if not isinstance(_attach_name, str) or _attach_name.split(".", 1)[0] not in _attach_names:
        continue
    _attach_row = {"name": _attach_name, "location": None, "file": None, "mtime_ns": None, "missing": False}
    _attach_modules.append(_attach_row)
    if type(_attach_module) is not types.ModuleType:
        continue
    try:
        _attach_meta = types.ModuleType.__getattribute__(_attach_module, "__dict__")
        _attach_row["location"] = _attach_location(_attach_meta)
        _attach_file = _attach_meta.get("__file__")
        if isinstance(_attach_file, str) and _attach_file:
            _attach_row["file"] = _attach_file
            try:
                _attach_row["mtime_ns"] = os.stat(_attach_file).st_mtime_ns
            except OSError:
                _attach_row["missing"] = True
    except (OSError, ValueError, TypeError) as _attach_exc:
        _scan_warning(_attach_name, _attach_exc)

_attach_stdlib = []
_attach_os_file = types.ModuleType.__getattribute__(os, "__dict__").get("__file__")
if isinstance(_attach_os_file, str) and _attach_os_file:
    _attach_stdlib.append(_scan_norm(os.path.dirname(_attach_os_file)))
for _attach_prefix in (getattr(sys, "base_prefix", None), getattr(sys, "prefix", None)):
    if isinstance(_attach_prefix, str) and _attach_prefix:
        _attach_stdlib.append(_scan_norm(os.path.join(_attach_prefix, "DLLs")))

result["dlel"]["attach"] = {
    "modules": _attach_modules,
    "required_loaded": [name for name in _ATTACH_REQUIRED if name in sys.modules],
    "stdlib": _attach_stdlib,
}
'''
