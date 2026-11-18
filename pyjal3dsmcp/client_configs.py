"""앱 설정 파일(Claude Desktop·Cursor·OpenCode)의 ``pyjal3dsmcp`` 항목을 추가·갱신·제거한다.

파일만 다루고 외부 명령은 실행하지 않는다(외부 명령은 ``installer.py``에만 있다). 파일 위치는 호출하는 쪽이 정한다.

규칙:

- 기본은 **이미 있는 파일만** 고친다. 호출부가 허용한 Claude Desktop 설정은 없는 파일·부모 폴더를 만든다.
- 자기 항목만 바꾸고 다른 서버 항목·최상위 키·키 순서는 보존한다. 이미 있는 자기 항목에서는 실행 명령 관련 키만 바꾸고
  ``env`` 등 사용자가 넣은 다른 키는 둔다.
- 내용이 바뀔 때만 쓴다. 쓰기 직전에 ``<파일명>.pyjal3dsmcp.bak``이 없으면 원본 바이트를 백업한다. 쓰기는 같은 폴더의
  임시 파일에 쓴 뒤 교체한다. 신규 파일에는 원본 백업을 만들지 않는다. 원본에 UTF-8 BOM이 있었으면 BOM을 유지한다.
- JSON으로 읽을 수 없거나(주석·중복 키 포함) 최상위·서버 묶음이 객체가 아니면 파일을 건드리지 않는다.
"""

from __future__ import annotations

import codecs
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SERVER_NAME = "pyjal3dsmcp"
BACKUP_SUFFIX = ".pyjal3dsmcp.bak"

# 처리 결과
MISSING = "missing"  # 파일 없음(만들지 않음)
UNCHANGED = "unchanged"  # 바뀔 내용 없음(쓰지 않음)
UPDATED = "updated"  # 항목 추가·갱신
CREATED = "created"  # 설정 파일 신규 생성
REMOVED = "removed"  # 항목 제거
ABSENT = "absent"  # 제거할 항목 없음
UNREADABLE = "unreadable"  # 읽을 수 없어 건드리지 않음
FAILED = "failed"  # 쓰기 실패


@dataclass(frozen=True)
class AppConfig:
    """앱 하나의 설정 파일. ``opencode``면 서버 묶음이 ``mcp``이고 항목 모양이 다르다."""

    label: str
    path: Path
    opencode: bool = False
    create_missing: bool = False

    @property
    def servers_key(self) -> str:
        return "mcp" if self.opencode else "mcpServers"


@dataclass(frozen=True)
class Outcome:
    status: str
    message: str = ""


def entry_for(app: AppConfig, command: list[str]) -> dict[str, Any]:
    """그 앱에 넣을 ``pyjal3dsmcp`` 항목(새로 만들 때의 모양)."""
    if app.opencode:
        return {"type": "local", "command": list(command), "enabled": True}
    entry: dict[str, Any] = {"command": command[0]}
    if len(command) > 1:
        entry["args"] = list(command[1:])
    return entry


def manual_snippet(app: AppConfig, command: list[str]) -> str:
    """사용자가 직접 넣을 내용(JSON 문자열)."""
    return json.dumps({app.servers_key: {SERVER_NAME: entry_for(app, command)}}, indent=2, ensure_ascii=False)


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # 중복 키를 그대로 다시 쓰면 앞의 값이 사라진다
            raise ValueError(f"중복 키 {key!r}")
        result[key] = value
    return result


def _load(app: AppConfig) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """파일 원본 바이트·전체 설정·서버 묶음. 읽을 수 없으면 ``ValueError``, 파일 접근 실패는 ``OSError``."""
    raw = app.path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"UTF-8이 아님: {exc}") from exc
    data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    if not isinstance(data, dict):
        raise ValueError("최상위가 JSON 객체가 아님")
    servers = data.get(app.servers_key, {})
    if not isinstance(servers, dict):
        raise ValueError(f'"{app.servers_key}"가 JSON 객체가 아님')
    return raw, data, servers


def _write(app: AppConfig, original: bytes | None, data: dict[str, Any]) -> None:
    """기존 원본만 처음 한 번 백업하고 임시 파일을 거쳐 교체한다."""
    content = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if original is not None and original.startswith(codecs.BOM_UTF8):
        content = codecs.BOM_UTF8 + content
    if original is None:
        app.path.parent.mkdir(parents=True, exist_ok=True)
    else:
        backup = backup_path(app.path)
        if not backup.exists():
            backup.write_bytes(original)
    fd, temporary = tempfile.mkstemp(prefix=".pyjal3dsmcp-", suffix=".tmp", dir=app.path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.replace(temporary, app.path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_servers(app: AppConfig) -> dict[str, Any] | None:
    """그 파일의 서버 묶음(읽기만 한다). 파일이 없거나 읽을 수 없으면 None."""
    try:
        return _load(app)[2]
    except (OSError, ValueError):
        return None


def register(app: AppConfig, command: list[str]) -> Outcome:
    """``pyjal3dsmcp`` 항목을 추가·갱신하고 허용된 앱은 없는 설정도 만든다."""
    if not app.path.is_file():
        if not app.create_missing:
            return Outcome(MISSING)
        if app.path.exists():
            return Outcome(UNREADABLE, "설정 경로가 파일이 아님")
        original, data, servers = None, {}, {}
    else:
        try:
            original, data, servers = _load(app)
        except (OSError, ValueError) as exc:
            return Outcome(UNREADABLE, str(exc))

    current = servers.get(SERVER_NAME)
    desired = entry_for(app, command)
    if isinstance(current, dict):
        updated = dict(current)
        updated.update(desired)
        if not app.opencode and "args" not in desired:
            updated.pop("args", None)
    else:
        updated = desired
    if current == updated:
        return Outcome(UNCHANGED)

    servers = dict(servers)
    servers[SERVER_NAME] = updated
    data = dict(data)
    data[app.servers_key] = servers
    try:
        _write(app, original, data)
    except OSError as exc:
        return Outcome(FAILED, str(exc))
    return Outcome(CREATED if original is None else UPDATED)


def unregister(app: AppConfig) -> Outcome:
    """``pyjal3dsmcp`` 항목만 지운다. 항목이 없으면 파일을 바꾸지 않는다."""
    if not app.path.is_file():
        return Outcome(MISSING)
    try:
        original, data, servers = _load(app)
    except (OSError, ValueError) as exc:
        return Outcome(UNREADABLE, str(exc))
    if SERVER_NAME not in servers:
        return Outcome(ABSENT)

    data = dict(data)
    data[app.servers_key] = {name: value for name, value in servers.items() if name != SERVER_NAME}
    try:
        _write(app, original, data)
    except OSError as exc:
        return Outcome(FAILED, str(exc))
    return Outcome(REMOVED)
