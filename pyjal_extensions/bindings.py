"""Instance Binding: 3ds Max 인스턴스 식별과 인스턴스별 활성 Library Set 저장.

``MaxInstanceIdentity``는 PID 단독이 아니라 PID·프로세스 생성 시각·bridge 대상(pipe)으로 Max 프로세스 하나를
가리킨다. 같은 PID라도 생성 시각이 다르면(재시작·PID 재사용) 다른 인스턴스이며 과거 기록을 쓰지 않는다.

저장 구조(모든 쓰기는 ``<catalog_dir>/bindings/`` 아래)::

    <catalog_dir>/bindings/instance-<pid>-<start>.json

파일 하나가 인스턴스 하나의 Binding·Runtime 상태·마지막 Operation·DLEL이 넣은 경로와 인스턴스 로드 기억
(``loaded_modules``)을 담는다. 로드 기억은 라이브러리 기록이 아니라 그 Max 프로세스의 ``sys.modules`` 사실이므로
라이브러리 해제·재등록·``library_id``와 무관하게 인스턴스가 살아 있는 동안 유지된다.
에이전트 표면 항목은 같은 소스(같은 ``import_root``·최상위 패키지)의 패키지 항목과 revision 범위가 다르므로
기억에 범위(``scope`` = 표면 모듈 이름)를 더해 서로의 옛 revision으로 오인하지 않는다.
같은 프로세스 안의 읽기·쓰기는 모듈 잠금으로 직렬화한다. 다른 MCP 서버 프로세스와의 동시 쓰기는
잠그지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from pyjal_extensions import LibraryError

BINDINGS_DIR = "bindings"
BINDINGS_SCHEMA_VERSION = 1
# Upstream ``select_max_instance``와 같은 PID별 native bridge pipe 이름 규칙
PIPE_TEMPLATE = "\\\\.\\pipe\\3dsmax-mcp-pid-{pid}"

_FILE_PATTERN = re.compile(r"instance-(\d+)-(\d+)\.json")

# 같은 프로세스의 모든 LibraryRuntime·BindingStore가 공유하는 잠금(MCP Tool은 호출마다 객체를 새로 만든다)
STORE_LOCK = threading.RLock()


def bridge_target_for(pid: int) -> str:
    return PIPE_TEMPLATE.format(pid=pid)


@dataclass(frozen=True)
class MaxInstanceIdentity:
    """실제 Max 프로세스 하나. 세 필드가 모두 같아야 같은 인스턴스다."""

    pid: int
    process_start_time_utc: int
    bridge_target: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "process_start_time_utc": self.process_start_time_utc,
            "bridge_target": self.bridge_target,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MaxInstanceIdentity:
        return cls(
            pid=int(data["pid"]),
            process_start_time_utc=int(data["process_start_time_utc"]),
            bridge_target=str(data["bridge_target"]),
        )

    @property
    def key(self) -> str:
        """프로세스 안 직렬화와 파일 이름에 쓰는 키."""
        return f"{self.pid}-{self.process_start_time_utc}"


def default_library_state() -> dict[str, Any]:
    """기록이 없는 라이브러리의 인스턴스 상태."""
    return {"binding": "disabled", "runtime_state": "not_loaded", "loaded_source_revision": None, "last_operation": None}


class BindingStore:
    """``<catalog_dir>/bindings/``의 인스턴스별 기록. 호출자는 ``STORE_LOCK`` 안에서 읽고 쓴다."""

    def __init__(self, catalog_dir: str | os.PathLike[str]) -> None:
        self._dir = Path(catalog_dir) / BINDINGS_DIR

    # -- 인스턴스 기록 ------------------------------------------------------

    def load(self, identity: MaxInstanceIdentity) -> dict[str, Any]:
        """인스턴스 기록. 없으면 빈 기록(파일은 만들지 않음)."""
        data = self._read(self._path(identity))
        if data is None:
            return self._empty(identity)
        data.setdefault("libraries", {})
        data.setdefault("owned_paths", [])
        # 로드 기억이 없는 기록은 빈 기억으로 읽는다
        if not isinstance(data.get("loaded_modules"), list):
            data["loaded_modules"] = []
        return data

    def save(self, identity: MaxInstanceIdentity, record: dict[str, Any]) -> None:
        record["schema_version"] = BINDINGS_SCHEMA_VERSION
        record["identity"] = identity.to_dict()
        self._write(self._path(identity), record)

    def library_state(self, identity: MaxInstanceIdentity, library_id: str) -> dict[str, Any]:
        state = default_library_state()
        state.update(self.load(identity)["libraries"].get(library_id, {}))
        return state

    def update_library(self, identity: MaxInstanceIdentity, library_id: str, **changes: Any) -> dict[str, Any]:
        """한 라이브러리의 상태 일부만 바꿔 저장한다(다른 필드는 유지)."""
        record = self.load(identity)
        state = default_library_state()
        state.update(record["libraries"].get(library_id, {}))
        state.update(changes)
        record["libraries"][library_id] = state
        self.save(identity, record)
        return state

    def add_owned_path(self, identity: MaxInstanceIdentity, path: str) -> None:
        record = self.load(identity)
        if not any(_same_path(path, owned) for owned in record["owned_paths"]):
            record["owned_paths"].append(path)
            self.save(identity, record)

    def owns_path(self, identity: MaxInstanceIdentity, path: str) -> bool:
        return any(_same_path(path, owned) for owned in self.load(identity)["owned_paths"])

    # -- 인스턴스 로드 기억 ---------------------------------------------

    def remember_loaded(
        self,
        identity: MaxInstanceIdentity,
        import_root: str,
        names: list[str],
        revision: str,
        scope: str | None = None,
    ) -> None:
        """(최상위 이름, ``import_root``, 범위)마다 기억이 없을 때만 로드 revision을 기록한다.

        DLEL은 reload하지 않으므로 Max에 남은 코드는 처음 로드된 것이다. 이미 있는 기억은 덮어쓰지 않는다.
        ``scope``는 표면 항목의 표면 모듈 이름이며 일반 항목은 None이다.
        """
        record = self.load(identity)
        memory = record["loaded_modules"]
        changed = False
        for name in names:
            if _find_loaded(memory, name, import_root, scope) is None:
                item = {"name": name, "import_root": import_root, "source_revision": revision}
                if scope is not None:
                    item["scope"] = scope
                memory.append(item)
                changed = True
        if changed:
            self.save(identity, record)

    def loaded_revisions(
        self, identity: MaxInstanceIdentity, import_root: str, names: list[str], scope: str | None = None
    ) -> dict[str, str]:
        """그 인스턴스에 같은 ``import_root``·범위로 로드가 확인된 이름 → 로드 revision."""
        memory = self.load(identity)["loaded_modules"]
        found = {}
        for name in names:
            item = _find_loaded(memory, name, import_root, scope)
            if item is not None:
                found[name] = item["source_revision"]
        return found

    # -- 다른 인스턴스 기록 -------------------------------------------------

    def previous_identities(self, identity: MaxInstanceIdentity) -> list[MaxInstanceIdentity]:
        """같은 PID의 다른 시작 시각 기록(재시작·PID 재사용 이전 인스턴스). 최근 시작 순."""
        found = [
            other
            for other in self.identities()
            if other.pid == identity.pid and other.process_start_time_utc != identity.process_start_time_utc
        ]
        return sorted(found, key=lambda other: other.process_start_time_utc, reverse=True)

    def forget_previous(self, identity: MaxInstanceIdentity) -> None:
        """같은 PID의 이전 인스턴스 기록을 지운다. 그 프로세스는 이미 끝났으므로 다시 쓰이지 않는다."""
        for other in self.previous_identities(identity):
            self.discard(other)

    def identities(self) -> list[MaxInstanceIdentity]:
        result = []
        for path in self._files():
            data = self._read(path)
            if isinstance(data, dict) and isinstance(data.get("identity"), dict):
                try:
                    result.append(MaxInstanceIdentity.from_dict(data["identity"]))
                except (KeyError, TypeError, ValueError):
                    continue
        return result

    def records(self) -> Iterator[tuple[MaxInstanceIdentity, dict[str, Any]]]:
        for identity in self.identities():
            yield identity, self.load(identity)

    def remove_library(self, library_id: str) -> None:
        """모든 인스턴스 기록에서 라이브러리를 지운다(해제 후 같은 ID 재등록 시 부활 방지).

        인스턴스의 로드 기억·``owned_paths``는 라이브러리 기록이 아니므로 그대로 둔다.
        """
        for identity, record in list(self.records()):
            if library_id in record["libraries"]:
                del record["libraries"][library_id]
                self.save(identity, record)

    def discard(self, identity: MaxInstanceIdentity) -> None:
        """인스턴스 기록 파일 하나를 지운다(``bindings/`` 안의 DLEL 기록 이름만 대상)."""
        try:
            self._path(identity).unlink()
        except FileNotFoundError:
            pass

    # -- 내부 ---------------------------------------------------------------

    def _empty(self, identity: MaxInstanceIdentity) -> dict[str, Any]:
        return {
            "schema_version": BINDINGS_SCHEMA_VERSION,
            "identity": identity.to_dict(),
            "owned_paths": [],
            "loaded_modules": [],
            "libraries": {},
        }

    def _path(self, identity: MaxInstanceIdentity) -> Path:
        return self._dir / f"instance-{identity.key}.json"

    def _files(self) -> list[Path]:
        try:
            return sorted(p for p in self._dir.iterdir() if _FILE_PATTERN.fullmatch(p.name))
        except FileNotFoundError:
            return []

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LibraryError("CATALOG_CORRUPT", f"Binding 파일을 읽지 못했습니다: {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise LibraryError("CATALOG_CORRUPT", f"Binding 파일 형식이 잘못되었습니다: {path}")
        return data

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        """임시 파일에 쓴 뒤 교체해 중단 시 반쯤 쓰인 파일이 남지 않게 한다."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _find_loaded(memory: list[Any], name: str, import_root: str, scope: str | None = None) -> dict[str, Any] | None:
    for item in memory:
        if (
            isinstance(item, dict)
            and item.get("name") == name
            and item.get("scope") == scope
            and isinstance(item.get("import_root"), str)
            and isinstance(item.get("source_revision"), str)
            and _same_path(import_root, item["import_root"])
        ):
            return item
    return None


__all__ = [
    "BINDINGS_DIR",
    "BindingStore",
    "MaxInstanceIdentity",
    "STORE_LOCK",
    "bridge_target_for",
    "default_library_state",
]
