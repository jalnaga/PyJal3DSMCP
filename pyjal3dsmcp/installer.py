"""PyJal3DSMCP 설치·업데이트·제거 wrapper(console script ``pyjal3dsmcp-install``·``-update``·``-uninstall``).

설치(``main``)는 Upstream 설치기(``maxmcp.installer``)의 **개별 단계**만 호출한다.

0. 고정 설치(기본): 다른 단계보다 먼저 ``uv tool install --force --reinstall <소스>``를 실행하고, 그 tool 환경의
   ``pyjal3dsmcp.exe`` 절대 경로를 서버 실행 명령으로 쓴다. 소스는 실행 중인 checkout 루트 또는 배포 메타데이터
   (``direct_url.json``)로 정한다. ``uv``가 없거나 설치가 실패하면 아무것도 바꾸지 않고 끝난다.
   ``--editable``이면 고정 설치 없이 ``uv run --directory <checkout 루트> pyjal3dsmcp``를 등록한다.
1. 사용자 설정: ``deploy_config(tool_profile="full")``. 설치 전에 사용자 INI가 없었을 때만 ``safe_mode``를 소문자
   ``false``로 쓴다. 이미 있던 INI의 ``safe_mode``는 건드리지 않는다.
2. Max 애플리케이션 패키지: ``deploy_application_package()``.
3. skill: ``build_skills()``.
4. ``--client``로 지정한 데스크톱 앱에만 등록한다. 기본 자동 등록에는 Claude Code, Gemini, Cursor, OpenCode도 포함된다.
5. Claude Desktop은 JSON, Codex와 ChatGPT Desktop은 공유 TOML에 등록한다. ``--no-apps``는 CLI 등록만 수행한다.
6. 팀 프로필: 현재 작업 폴더(프로필 배포 폴더)에 ``pyjal3dsmcp-team.toml``이 있으면 그 절대 경로를 DLEL 로컬 설정
   (``%LOCALAPPDATA%\\PyJal3DSMCP\\dlel\\settings.json``)의 ``team_profile``에 기록한다. 없으면 기존 기록을 그대로 둔다.
   고정 설치 준비가 실패하면 다른 단계와 마찬가지로 기록하지 않는다.

Upstream의 ``main()``·``register_agents()``·``register_app_mcp_configs()``·``register_opencode()``는 호출하지 않는다.
서버를 ``3dsmax-mcp`` 이름으로 등록하기 때문이다. 기존 ``3dsmax-mcp`` 등록은 지우지 않고 경고와 제거 명령만 출력한다.

업데이트(``update_main``)는 checkout이면 ``git pull --ff-only`` 뒤 새 프로세스(checkout의 ``.venv``를 동기화하지 않는
``uv run --no-project --with-editable <루트>``)로 설치를 실행하고, checkout이 아니면 같은
프로세스에서 설치를 실행한다. 제거(``uninstall_main``)는 이 wrapper가 만든 것만 지운다.

외부 명령(``subprocess``)은 이 모듈에만 있다. 파일 쓰기 위치는 Upstream 설치기가 import 시점의
``LOCALAPPDATA``·``ProgramData``로 정한다.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import hashlib
import re
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from unittest.mock import patch
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from pyjal_extensions.locations import TEAM_PROFILE_FILE_NAME, local_settings_path
from pyjal_extensions.team_profile import read_local_settings

from . import _build_info, client_configs, install_status, team_cli, upstream

SERVER_NAME = "pyjal3dsmcp"
SERVER_MODULE = "pyjal3dsmcp.server"
INSTALL_SCRIPT = "pyjal3dsmcp-install"
LEGACY_SERVER_NAME = "3dsmax-mcp"
UPSTREAM_DISTRIBUTION = "3dsmax-mcp"
SKILL_NAME = "3dsmax-mcp-dev"
AGENTS = ("claude", "codex", "gemini")
DESKTOP_CLIENTS = ("claude-desktop", "codex", "chatgpt-desktop")
AGENT_COMMAND_TIMEOUT_SECONDS = 30
# Max 애플리케이션 패키지로 지워도 되는 경로의 마지막 세 요소(Upstream APPLICATION_PACKAGE_DST)
PACKAGE_PATH_TAIL = ("autodesk", "applicationplugins", "3dsmax-mcp")
ELEVATED_DELETE_TIMEOUT_SECONDS = 300

PACKAGE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# 서버 실행 명령과 설치 소스
# ---------------------------------------------------------------------------


def checkout_root() -> Path | None:
    """실행 중인 ``pyjal3dsmcp`` 패키지가 저장소 checkout 안에 있으면 그 루트, 아니면 None.

    가상환경에 무엇이 설치돼 있는지가 아니라 **지금 실행 중인 패키지의 위치**로 판단한다. 패키지 옆에 이 프로젝트의
    ``pyproject.toml``이 있으면 checkout이고, wheel로 설치된 패키지(site-packages) 옆에는 없다.
    """
    root = PACKAGE_DIR.parent
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")).get("project", {})
    except (OSError, ValueError):
        return None
    return root if project.get("name") == SERVER_NAME else None


def console_script_path() -> Path | None:
    """이 인터프리터 환경에 설치된 ``pyjal3dsmcp`` console script.

    실행 중인 패키지가 이 환경의 site-packages에 있을 때만 돌려준다. 다른 위치의 사본을 실행 중이면 같은 환경의
    console script는 다른 코드를 띄우므로 쓰지 않는다.
    """
    site_dirs = {Path(sysconfig.get_path(name)).resolve() for name in ("purelib", "platlib")}
    if PACKAGE_DIR.parent not in site_dirs:
        return None
    suffix = ".exe" if os.name == "nt" else ""
    script = Path(sysconfig.get_path("scripts")) / f"{SERVER_NAME}{suffix}"
    return script if script.is_file() else None


def server_command() -> list[str]:
    """지금 실행 중인 코드를 그대로 띄우는 서버 실행 명령(고정 설치를 하지 않을 때 쓴다).

    checkout에서는 ``uv run --directory <저장소 루트> pyjal3dsmcp``(``--editable``), wheel 설치에서는 설치된
    console script이고 console script를 찾지 못하면 ``<python> -m pyjal3dsmcp.server``다.
    """
    root = checkout_root()
    if root is not None:
        return [shutil.which("uv") or "uv", "run", "--directory", str(root), SERVER_NAME]
    script = console_script_path()
    if script is not None:
        return [str(script)]
    return [sys.executable, "-m", SERVER_MODULE]


def server_entry(command: list[str]) -> dict[str, Any]:
    """앱 설정 파일의 ``mcpServers`` 항목 모양."""
    entry: dict[str, Any] = {"command": command[0]}
    if len(command) > 1:
        entry["args"] = command[1:]
    return entry


def _direct_url() -> dict[str, Any] | None:
    """실행 중인 패키지와 같은 위치에 설치된 배포의 ``direct_url.json``. 없으면 None."""
    for distribution in metadata.distributions(path=[str(PACKAGE_DIR.parent)]):
        if (distribution.metadata["Name"] or "").lower() != SERVER_NAME:
            continue
        text = distribution.read_text("direct_url.json")
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    return None


def _file_url_path(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path = url2pathname(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = "\\\\" + parsed.netloc + path  # UNC 공유 폴더
    return path


def install_source() -> str | None:
    """``uv tool install``에 넘길 소스. 알 수 없으면(예: wheel 파일 설치) None.

    checkout이면 그 루트, 아니면 ``direct_url.json``의 ``dir_info``(로컬 경로)나 git ``vcs_info``
    (``git+<url>[@requested_revision]``)를 쓴다.
    """
    root = checkout_root()
    if root is not None:
        return str(root)
    direct = _direct_url()
    if not direct or not isinstance(direct.get("url"), str):
        return None
    url = direct["url"]
    if isinstance(direct.get("archive_info"), dict):
        file = _file_url_path(url)
        if file and Path(file).suffix == ".whl" and Path(file).is_file():
            return file
    if isinstance(direct.get("dir_info"), dict):
        return _file_url_path(url)
    vcs = direct.get("vcs_info")
    if isinstance(vcs, dict) and vcs.get("vcs") == "git":
        revision = vcs.get("commit_id")
        return f"git+{url}@{revision}" if revision else f"git+{url}"
    return None


# ---------------------------------------------------------------------------
# 외부 명령
# ---------------------------------------------------------------------------


def _run(command: list[str], *, capture: bool = False, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """``uv``·``git`` 실행. ``capture``가 아니면 출력은 그대로 콘솔로 간다(진행 상황을 보여 준다)."""
    sys.stdout.flush()
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE if capture else sys.stderr,
        stderr=subprocess.PIPE if capture else sys.stderr,
        stdin=subprocess.DEVNULL, timeout=180,
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0", UV_NO_PROGRESS="1"),
        text=True if capture else None,
        encoding="utf-8" if capture else None,
        errors="replace" if capture else None,
    )


def _uv_tool_dir(uv: str, *, bin_dir: bool) -> Path | None:
    try:
        completed = _run([uv, "tool", "dir", *(["--bin"] if bin_dir else [])], capture=True)
    except OSError:
        return None
    text = completed.stdout.strip() if completed.returncode == 0 else ""
    return Path(text.splitlines()[-1].strip()) if text else None


def pin_install(source: str) -> list[str] | None:
    """``uv tool install --force --reinstall <소스>``를 실행하고 그 실행 파일 명령을 돌려준다. 실패하면 None."""
    print("\n[준비] 고정 설치 (uv tool install)")
    retry = "실행 중인 MCP 서버와 AI 클라이언트(Claude Desktop·Cursor 등)를 모두 닫고 다시 실행하세요."
    uv = shutil.which("uv")
    if not uv:
        print("  실패: PATH에서 'uv'를 찾지 못했습니다. uv를 설치하거나 PATH에 추가한 뒤 다시 실행하세요.")
        print("  아무것도 바꾸지 않았습니다.")
        return None
    print(f"  소스: {source}")
    try:
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        completed = _run([uv, "tool", "install", "--python", python_version, "--force", "--reinstall", source])
    except OSError as exc:
        print(f"  실패: uv를 실행하지 못했습니다 ({exc}). 아무것도 바꾸지 않았습니다.")
        return None
    if completed.returncode != 0:
        print(f"  실패: uv tool install이 종료 코드 {completed.returncode}로 끝났습니다. 아무것도 바꾸지 않았습니다.")
        print(f"  {retry}")
        return None
    bin_dir = _uv_tool_dir(uv, bin_dir=True)
    suffix = ".exe" if os.name == "nt" else ""
    executable = bin_dir / f"{SERVER_NAME}{suffix}" if bin_dir else None
    if executable is None or not executable.is_file():
        print(f"  실패: 고정 설치한 실행 파일을 찾지 못했습니다 ({executable or 'uv tool dir --bin 실패'}).")
        print(f"  {retry}")
        return None
    print(f"  OK: {executable}")
    return [str(executable)]


# ---------------------------------------------------------------------------
# 클라이언트 CLI
# ---------------------------------------------------------------------------


def add_command(agent: str, command: list[str], env: dict[str, str] | None = None) -> list[str]:
    """``<cli> mcp add``. 인수 문법은 Upstream 설치기와 같고 등록 이름만 다르다."""
    if agent == "claude":
        # -e는 값을 여러 개 받으므로 서버 이름 뒤에 둔다.
        env_args = [item for key, value in (env or {}).items() for item in ("-e", f"{key}={value}")]
        return [agent, "mcp", "add", "--scope", "user", SERVER_NAME, *env_args, "--", *command]
    if agent == "codex":
        return [agent, "mcp", "add", SERVER_NAME, "--", *command]
    return [agent, "mcp", "add", "--scope", "user", SERVER_NAME, *command]


def remove_command(agent: str, name: str) -> list[str]:
    """``<cli> mcp remove``(``add_command``와 같은 scope)."""
    if agent == "claude":
        return [agent, "mcp", "remove", name, "--scope", "user"]
    if agent == "codex":
        return [agent, "mcp", "remove", name]
    return [agent, "mcp", "remove", "--scope", "user", name]


def run_agent_command(command: list[str]) -> str | None:
    """클라이언트 CLI 명령을 실행한다. 성공하면 None, 실패하면 CLI가 낸 메시지를 돌려준다."""
    try:
        # npm으로 설치한 클라이언트 CLI는 Windows에서 .cmd shim이라 shell을 거쳐야 실행된다. 인수는 한 번만 인용한다.
        subprocess.run(
            subprocess.list2cmdline(command),
            shell=True,
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=AGENT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "timed out"
    except subprocess.CalledProcessError as exc:
        parts = [part.decode("utf-8", "replace") for part in (exc.stderr, exc.stdout) if part]
        return "\n".join(parts).strip() or f"exit code {exc.returncode}"
    except OSError as exc:
        return str(exc)
    return None


def registered_servers(agent: str) -> dict[str, Any]:
    """그 클라이언트의 사용자 범위 설정에 등록된 MCP 서버(이름 → 항목). 읽기만 한다.

    읽는 파일은 ``claude``: ``~/.claude.json``, ``codex``: ``~/.codex/config.toml``, ``gemini``: ``~/.gemini/settings.json``
    이다(이전 설치기가 등록하던 범위). 프로젝트·로컬 범위 등록과 앱 설정 파일은 보지 않는다. 파일이 없거나 읽을 수
    없으면 빈 dict다.
    """
    home = Path.home()
    try:
        if agent == "claude":
            base = os.environ.get("CLAUDE_CONFIG_DIR")
            path = (Path(base) if base else home) / ".claude.json"
            servers = json.loads(path.read_text(encoding="utf-8-sig")).get("mcpServers")
        elif agent == "codex":
            base = os.environ.get("CODEX_HOME")
            path = (Path(base) if base else home / ".codex") / "config.toml"
            servers = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("mcp_servers")
        else:
            path = home / ".gemini" / "settings.json"
            servers = json.loads(path.read_text(encoding="utf-8-sig")).get("mcpServers")
    except (OSError, ValueError, AttributeError):
        return {}
    return servers if isinstance(servers, dict) else {}


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), text)


def _registered_env(agent: str) -> dict[str, str]:
    """다시 등록할 때 이어 갈 기존 ``pyjal3dsmcp`` 항목의 환경 변수(``claude``만 등록 명령으로 넘길 수 있다)."""
    if agent != "claude":
        return {}
    entry = registered_servers(agent).get(SERVER_NAME)
    env = entry.get("env") if isinstance(entry, dict) else None
    return {str(key): str(value) for key, value in env.items()} if isinstance(env, dict) else {}


def register_agents(command: list[str], candidates: tuple[str, ...] = AGENTS) -> list[str]:
    """``PATH``에 있는 클라이언트 CLI에 ``pyjal3dsmcp``를 등록한다. 찾은 CLI 이름을 돌려준다."""
    print("\n[4/4] MCP 클라이언트 등록 (CLI)")
    agents = [agent for agent in candidates if shutil.which(agent)]
    if not agents:
        print(f"  PATH에서 클라이언트 CLI({', '.join(candidates)})를 찾지 못했습니다.")
        print("  CLI를 설치한 뒤 아래 명령으로 직접 등록할 수 있습니다:")
        for agent in candidates:
            print(f"    {subprocess.list2cmdline(add_command(agent, command))}")
        return agents

    successful = []
    for agent in agents:
        add = add_command(agent, command)
        error = run_agent_command(add)
        if error and "already exists" in error.lower():
            # add는 기존 항목을 덮어쓰지 않으므로 예전 실행 명령이 남는다. 자기 이름의 항목만 지우고 다시 넣는다.
            add = add_command(agent, command, _registered_env(agent))
            error = run_agent_command(remove_command(agent, SERVER_NAME))
            if not error:
                error = run_agent_command(add)
                if error:
                    print(f"  실패: {agent} - 기존 {SERVER_NAME} 항목을 지웠지만 다시 등록하지 못했습니다 (CLI 오류)")
                    print(f"    직접 실행: {subprocess.list2cmdline(add_command(agent, command))}")
                    continue
        if error:
            print(f"  건너뜀: {agent} (CLI 오류)")
            print(f"    직접 실행: {subprocess.list2cmdline(add_command(agent, command))}")
        else:
            successful.append(agent)
            print(f"  OK: {agent} ({SERVER_NAME})")
    return successful


def report_legacy_registrations() -> list[str]:
    """기존 ``3dsmax-mcp`` 등록이 남아 있는 클라이언트를 알린다. 자동으로 지우지 않는다."""
    found = [agent for agent in AGENTS if LEGACY_SERVER_NAME in registered_servers(agent)]
    for agent in found:
        print(f"\n  경고: {agent} 에 기존 '{LEGACY_SERVER_NAME}' 등록이 남아 있습니다 (자동으로 지우지 않습니다).")
        print(f"  {SERVER_NAME}와 함께 등록되어 있으면 Tool이 중복되고 같은 3ds Max 브리지를 두 서버 프로세스가 잡습니다.")
        print("  확인한 뒤 아래 명령으로 제거하세요:")
        print(f"    {subprocess.list2cmdline(remove_command(agent, LEGACY_SERVER_NAME))}")
    return found


# ---------------------------------------------------------------------------
# 앱 설정 파일
# ---------------------------------------------------------------------------


def app_configs(installer: Any, clients: tuple[str, ...] = ()) -> list[client_configs.AppConfig]:
    """앱 설정 파일 위치(파일이 있는지는 보지 않는다). 위치 규칙은 Upstream 경로 도우미를 따른다."""
    apps = []
    if not clients or "claude-desktop" in clients:
        for path in dict.fromkeys(installer.claude_desktop_config_paths()):
            store = any(part.lower() == "packages" for part in path.parts)
            apps.append(client_configs.AppConfig(
                "Claude Desktop (Microsoft Store)" if store else "Claude Desktop", path, create_missing=True,
            ))
    if not clients or {"codex", "chatgpt-desktop"}.intersection(clients):
        base = os.environ.get("CODEX_HOME")
        path = (Path(base) if base else Path.home() / ".codex") / "config.toml"
        apps.append(client_configs.AppConfig("Codex / ChatGPT Desktop", path, create_missing=True, codex=True))
    if clients:
        return apps
    apps.append(client_configs.AppConfig("Cursor", Path.home() / ".cursor" / "mcp.json"))
    apps.append(client_configs.AppConfig("OpenCode", installer.resolve_opencode_config(Path.home()), opencode=True))
    return apps


def _print_manual_app_entry(app: client_configs.AppConfig, command: list[str]) -> None:
    print(f"    {app.path} 에 아래 항목을 직접 넣으세요:")
    for line in client_configs.manual_snippet(app, command).splitlines():
        print(f"      {line}")


def register_app_configs(installer: Any, command: list[str], clients: tuple[str, ...] = ()) -> bool:
    """Register the selected desktop clients and report configuration errors to the caller."""
    print("\n[4/4] MCP desktop clients")
    ok = True
    for app in app_configs(installer, clients):
        outcome = client_configs.register(app, command)
        if outcome.status == client_configs.MISSING:
            continue
        if outcome.status == client_configs.CREATED:
            print(f"  OK: {app.label} ({app.path}) - 설정 파일 신규 생성")
        elif outcome.status == client_configs.UPDATED:
            print(f"  OK: {app.label} ({app.path})")
            print(f"    원본 백업: {client_configs.backup_path(app.path)}")
        elif outcome.status == client_configs.UNCHANGED:
            print(f"  이미 최신: {app.label} ({app.path})")
        else:
            ok = False
            reason = "읽을 수 없어 바꾸지 않았습니다" if outcome.status == client_configs.UNREADABLE else "쓰지 못했습니다"
            print(f"  건너뜀: {app.label} - {reason} ({outcome.message})")
            _print_manual_app_entry(app, command)
    return ok


def print_manual_registration(command: list[str]) -> None:
    """그 밖의 MCP 클라이언트(설정 파일에 직접 넣는 앱)에 넣을 내용을 출력한다."""
    servers = json.dumps({"mcpServers": {SERVER_NAME: server_entry(command)}}, indent=2, ensure_ascii=False)
    print("\n  수동 등록 (그 밖의 MCP 클라이언트)")
    print("  설정 파일에 직접 넣는 앱은 아래 항목을 쓰세요:")
    for line in servers.splitlines():
        print(f"      {line}")
    print(f'  예전 설치기가 앱 설정 파일에 넣은 "{LEGACY_SERVER_NAME}" 항목이 있으면 그 항목은 지우세요.')


# ---------------------------------------------------------------------------
# 설치
# ---------------------------------------------------------------------------


def deploy_user_config(installer: Any) -> None:
    """Upstream 사용자 설정 단계를 실행하고, 새로 만든 INI에만 ``safe_mode = false``를 쓴다.

    native bridge는 값을 대소문자 구분으로 비교하므로 소문자 ``false`` 한 단어로 쓴다(같은 줄 주석 없음).
    """
    existed = installer.CONFIG_DST.exists()
    installer.deploy_config(tool_profile=upstream.FULL_TOOL_PROFILE)
    if existed:
        print("  safe_mode:       기존 사용자 설정 유지")
    else:
        installer.set_ini_value(installer.CONFIG_DST, "mcp", "safe_mode", "false")
        print("  safe_mode:       false (새 사용자 설정)")


def _version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "unknown"


def _prepare_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        # 한글을 표현하지 못하는 콘솔 인코딩에서도 멈추지 않게 하고, 하위 프로세스 출력과 순서를 맞춘다.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace", line_buffering=True)


def _add_install_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--editable", action="store_true",
        help="개발용: 고정 설치 없이 이 checkout을 'uv run --directory <checkout> pyjal3dsmcp'로 등록합니다.",
    )
    parser.add_argument(
        "--skip-skill", "--skip-skills", action="store_true", help="skill 빌드·설치 단계를 건너뜁니다."
    )
    parser.add_argument("--all-clients", action="store_true")
    parser.add_argument("--skill", choices=("global", "project", "both", "skip"))
    parser.add_argument("--team-profile", type=Path)
    parser.add_argument("--force-reinstall", action="store_true")
    parser.add_argument("--ref")
    parser.add_argument("--track-ref", help="전체 SHA와 함께 보존할 원래 업데이트 ref")
    parser.add_argument("--wheel", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--no-apps", action="store_true", help="Use CLI registration only.")
    group.add_argument("--client", choices=DESKTOP_CLIENTS, action="append", default=[],
                       help="Register only this desktop client (repeat to select several).")


def deploy_bridge(installer, allow_elevation):
    """고정 Upstream의 RunAs 재시도 전에 호출자의 상승 허용 여부를 확인한다."""
    dest = installer.APPLICATION_PACKAGE_DST
    expected = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "Autodesk/ApplicationPlugins/3dsmax-mcp"
    if dest != expected or dest.resolve() != dest.absolute():
        raise ValueError(f"브리지 경로를 확인하세요: {dest}")

    class BridgeSubprocess:
        def __getattr__(self, name):
            return getattr(subprocess, name)

        def run(self, argv, **options):
            if "runas" in str(argv).lower():
                if not allow_elevation:
                    raise PermissionError("브리지 권한을 확인하고 --allow-elevation으로 재시도하세요.")
                with tempfile.TemporaryDirectory() as tmp:
                    staged = Path(tmp) / "bridge"
                    installer.stage_bundle(staged)
                    elevated_package(dest, staged)
                return subprocess.CompletedProcess(argv, 0)
            return subprocess.run(argv, **options)

    with patch.object(installer, "subprocess", BridgeSubprocess()):
        return installer.deploy_application_package()


def install(args: argparse.Namespace) -> int:
    """설치 절차. 고정 설치·``--editable`` 준비가 실패하면 아무것도 바꾸지 않고 2, 패키지 단계 실패는 1, 성공은 0."""
    installer = upstream.load_installer()
    print(f"PyJal3DSMCP 설치기 v{_version(SERVER_NAME)} (Upstream 3dsmax-mcp {_version(UPSTREAM_DISTRIBUTION)})")

    running = args.report["inventory"]["max_processes"]
    if args.editable:
        root = checkout_root()
        if root is None:
            print("\n  오류: --editable은 저장소 checkout에서 실행할 때만 쓸 수 있습니다. 아무것도 바꾸지 않았습니다.")
            print("  checkout 폴더에서 'uv run --directory <폴더> pyjal3dsmcp-install --editable'로 실행하세요.")
            return 2
        command = server_command()
        print(f"\n[준비] 고정 설치: 건너뜀 (--editable, checkout {root}를 직접 실행)")
    else:
        source = args.source
        if args.reuse:
            command = [args.report["inventory"]["uv_tool"]["launcher"]]
        elif source is None:
            if "archive-v0" in Path(sys.prefix).parts or Path(sys.prefix, "uv-receipt.toml").exists():
                raise ValueError("안정된 실행기를 확인할 수 없습니다. wheel 경로에서 uv tool install을 먼저 실행하세요.")
            command = server_command()
            print("\n[준비] 고정 설치(uv tool install): 건너뜀 - 설치 소스를 알 수 없습니다(예: wheel 파일로 설치).")
            print("  지금 실행 중인 환경의 서버를 등록합니다.")
        else:
            pinned = pin_install(source)
            if pinned is None:
                return 2
            command = pinned
            target = args.report["build"]["target"]
            if target["commit"] or target["wheel_sha256"]:
                tool_path = Path(args.report["inventory"]["uv_tool"]["environment"])
                recorded = dict(target)
                if target["wheel_sha256"]:
                    wheel = Path(args.source)
                    if hashlib.sha256(wheel.read_bytes()).hexdigest() != target["wheel_sha256"]:
                        raise OSError("설치 중 wheel이 변경되었습니다.")
                    recorded["source_url"] = wheel.resolve().as_uri()
                receipt = tool_path / "pyjal3dsmcp-source.json"
                team_cli.replace_checked(receipt, team_cli.current_bytes(receipt), json.dumps(recorded).encode())

    args.report["changed"] = True
    deploy_user_config(installer)
    if args.profile:
        team_cli.link(args.profile)
    profile = team_cli.show()
    previous = args.report["inventory"]["team_profile"]
    disposition = ("environment_override" if profile["environment"] else "recorded" if
                   profile["recorded"] != previous["recorded"] else "retained" if profile["recorded"] else "absent")
    args.report["team_profile"] = dict(profile, disposition=disposition)
    descriptions = {"environment_override": "환경 변수 우선", "recorded": "새로 기록",
                    "retained": "기존 기록 유지", "absent": "없음"}
    location = f" ({profile['path']})" if profile["path"] else ""
    print(f"팀 프로필: {descriptions[disposition]}{location}")
    package_ok = bool(running) or deploy_bridge(installer, args.allow_elevation)
    if args.skip_skill or args.skill == "skip" or (args.skill is None and (args.yes or not sys.stdin.isatty())):
        print("\n[3/4] skill: 건너뜀 (--skip-skill)")
    else:
        if args.skill in ("both", "project"):
            src = Path(installer.ROOT) / "skills" / SKILL_NAME
            dest = Path.cwd() / ".agents/skills" / SKILL_NAME
            dest.mkdir(parents=True, exist_ok=True)
            for file in src.glob("*.md"):
                shutil.copy2(file, dest / file.name)
        if args.skill in ("global", "both"):
            console = sys.stdin
            try:
                sys.stdin = io.StringIO("2\n")
                if installer.build_skills() is False:
                    raise OSError("전역 skill 설치에 실패했습니다.")
            finally:
                sys.stdin = console
        elif args.skill is None:
            installer.build_skills()

    clients = tuple(args.client)
    cli_ok = True
    if not clients:
        # With app registration enabled, the shared TOML entry also configures the Codex CLI.
        candidates = AGENTS if args.no_apps else ("claude", "gemini")
        expected = {agent for agent in candidates if shutil.which(agent)}
        cli_ok = set(register_agents(command, candidates)) == expected
    if command[0] == "uv" and not shutil.which("uv"):
        print("  경고: PATH에서 'uv'를 찾지 못했습니다. 등록한 실행 명령이 uv를 쓰므로 uv를 설치하거나 PATH에 추가하세요.")
    apps_ok = True
    if args.no_apps:
        print("\n[4/4] 앱 설정 파일: 건너뜀 (--no-apps)")
    else:
        apps_ok = register_app_configs(installer, command, clients)
    if not clients:
        print_manual_registration(command)
    report_legacy_registrations()

    print("\n" + "=" * 60)
    if package_ok and apps_ok:
        print("  Installation complete. Reload the client and check pyjal3dsmcp in a new local session.")
        if not running:
            print("  Start 3ds Max to load the bridge.")
    elif not package_ok:
        print("  애플리케이션 패키지 설치에 실패했습니다. 위 메시지를 확인하고 3ds Max를 닫은 뒤 다시 실행하세요.")
    else:
        print("  Client registration failed. Check the configuration path and error above, then retry.")
    print(f"  MCP 서버는 AI 클라이언트가 연결할 때 시작됩니다 (등록 이름: {SERVER_NAME}, Tool 프로필: full 고정).")
    print(f"  서버 실행 명령: {subprocess.list2cmdline(command)}")
    print("=" * 60)
    return 1 if not package_ok else 0 if apps_ok and cli_ok else 3


def checkout_target(root):
    prefix = ["git", "-C", str(root)]
    branch = _run([*prefix, "symbolic-ref", "-q", "HEAD"], capture=True)
    if branch.returncode:
        return None
    branch_ref = branch.stdout.strip()
    tracking = _run([*prefix, "for-each-ref", "--format=%(upstream:remotename)%09%(upstream:remoteref)", branch_ref], capture=True)
    fields = tracking.stdout.strip().split("\t")
    if len(fields) != 2:
        return None
    remote, ref = fields
    result = _run([*prefix, "ls-remote", "--exit-code", "--", remote, ref], capture=True)
    matches = [line.split()[0] for line in result.stdout.splitlines() if line.endswith("\t" + ref)]
    if result.returncode or len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{40}", matches[0]):
        raise ValueError("추적 브랜치의 원격 커밋을 확인하지 못했습니다.")
    return remote, ref, matches[0]


def _git_pull(root: Path, tracking) -> bool:
    """checkout이 git 저장소이고 추적 브랜치가 있으면 ``git pull --ff-only``. 실패면 False(건너뛴 경우는 True)."""
    if not (root / ".git").exists():
        print("  git pull: 건너뜀 (git 저장소가 아님, 예: 공유 폴더 사본)")
        return True
    git = shutil.which("git")
    if not git:
        print("  실패: PATH에서 'git'을 찾지 못했습니다. git을 설치하거나, 받지 않고 다시 설치하려면 --no-pull을 쓰세요.")
        return False
    upstream_branch = _run([git, "-C", str(root), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], capture=True)
    if upstream_branch.returncode != 0:
        print("  git pull: 건너뜀 (추적 브랜치가 없음)")
        return True
    print(f"  git pull --ff-only ({upstream_branch.stdout.strip()})")
    pins = [tracking[0], tracking[2]] if tracking else []
    completed = _run([git, "-C", str(root), "pull", "--ff-only", *pins])
    if completed.returncode != 0:
        print(f"  실패: git pull --ff-only가 종료 코드 {completed.returncode}로 끝났습니다. 설치를 실행하지 않았습니다.")
        print(f"  {root} 에서 변경 사항을 정리한 뒤 다시 실행하거나, 받지 않고 다시 설치하려면 --no-pull을 쓰세요.")
        return False
    return True


def forwarded_flags(args):
    flags = (["--all-clients"] if args.all_clients else []) + [flag for flag, on in (("--editable", args.editable), ("--skip-skill", args.skip_skill),
                                   ("--no-apps", args.no_apps)) if on]
    for flag, value in (("--team-profile", args.team_profile), ("--skill", args.skill)):
        if value is not None:
            flags.extend([flag, str(value)])
    for flag, enabled in (("--yes", args.yes), ("--allow-elevation", args.allow_elevation)):
        if enabled:
            flags.append(flag)
    for client in args.client:
        flags.extend(["--client", client])
    return flags


def run_update_child(command, args):
    if not args.json:
        return _run(command).returncode
    previous = args.report["changed"]
    args.report["changed"] = True if previous else None
    child = _run([*command, "--json"], capture=True)
    args.report["child_exit_code"] = child.returncode
    try:
        result = json.loads(child.stdout)
    except ValueError:
        result = {}
    if isinstance(result, dict) and result.get("schema_version") == 1:
        args.report["changed"] = True if previous else result.get("changed")
        for field in ("issues", "next_actions"):
            args.report[field].extend(result.get(field, []))
        if "team_profile" in result:
            args.report["team_profile"] = result["team_profile"]
    return child.returncode if child.returncode in (0, 1, 2, 3) else 1


def update(args):
    root = checkout_root()
    builds = args.report["build"]
    wheel_switch = bool(args.wheel and not args.wheel_matches_caller)
    git_switch = not args.wheel and builds["target"]["commit"] != builds["caller"]["commit"]
    if (root is None and git_switch) or wheel_switch:
        flags = forwarded_flags(args)
        if args.wheel:
            flags.extend(["--wheel", args.source])
        else:
            flags.extend(["--track-ref", builds["target"]["requested_ref"]])
        cmd = [shutil.which("uv") or "uv", "tool", "run", "--isolated", "--from", args.source, INSTALL_SCRIPT, *flags]
        return run_update_child(cmd, args)
    if root is None:
        print("PyJal3DSMCP 업데이트: checkout이 아니므로 이 프로세스에서 다시 설치합니다.")
        return install(args)

    print(f"PyJal3DSMCP 업데이트: {root}")
    uv = shutil.which("uv")
    if not uv:
        print("  실패: PATH에서 'uv'를 찾지 못했습니다. uv를 설치하거나 PATH에 추가한 뒤 다시 실행하세요. 아무것도 바꾸지 않았습니다.")
        return 2
    if args.no_pull:
        print("  git pull: 건너뜀 (--no-pull)")
    elif not _git_pull(root, args.tracking):
        return 1

    if args.tracking:
        actual = _run(["git", "-C", str(root), "rev-parse", "HEAD"], capture=True).stdout.strip()
        args.report["changed"] = actual != builds["caller"]["commit"]
        if actual != args.tracking[2]:
            raise ValueError("계획한 커밋과 checkout이 달라 재설치를 보류했습니다.")
    flags = forwarded_flags(args)
    # 받은 코드와 의존성으로 설치하도록 새 프로세스에서 실행한다(이 프로세스는 받기 전 코드다).
    # --no-project: 프로젝트 모드의 uv run은 pull로 버전이 바뀐 checkout의 .venv를 다시 동기화하는데, 지금 실행 중인
    # .venv\Scripts\pyjal3dsmcp-update.exe가 잠겨 있어 교체하지 못할 수 있다(Windows).
    # 그래서 .venv를 건드리지 않고 임시 환경에 checkout을 --with-editable로 넣어 받은 코드의 설치기를 실행한다.
    command = [uv, "run", "--no-project", "--with-editable", str(root), "--directory", str(root), INSTALL_SCRIPT, *flags]
    print(f"  설치 실행: {subprocess.list2cmdline(command)}")
    try:
        return run_update_child(command, args)
    except OSError as exc:
        print(f"  실패: 설치 프로세스를 시작하지 못했습니다 ({exc}).")
        return 1


# ---------------------------------------------------------------------------
# 제거
# ---------------------------------------------------------------------------


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def _skill_dirs() -> list[Path]:
    home = Path.home()
    return [home / ".claude" / "skills" / SKILL_NAME, home / ".agents" / "skills" / SKILL_NAME]


def _user_data_dirs(installer: Any) -> list[Path]:
    """``--purge``로만 지우는 사용자 데이터: Upstream 사용자 설정 폴더와 PyJal 라이브러리 카탈로그 폴더."""
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return [Path(installer.CONFIG_DST).parent, base / "PyJal3DSMCP"]


def _remove_tree(path: Path, label: str) -> bool:
    if path.resolve() != path.absolute():
        print(f"  경로가 리디렉션되어 건너뜁니다: {path}")
        return False
    if not path.exists():
        print(f"  없음: {label} ({path})")
        return True
    try:
        shutil.rmtree(path)
    except OSError as exc:
        print(f"  실패: {label} ({path}): {exc}")
        print("    사용 중인 프로그램을 닫고 그 폴더를 직접 지우세요.")
        return False
    print(f"  삭제: {label} ({path})")
    return True


def elevated_package(path, staged=None):
    """승인된 패키지 경로에 한해 PowerShell 파일 API로 설치 또는 제거한다."""
    powershell = shutil.which("powershell") or str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    expected = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "Autodesk/ApplicationPlugins/3dsmax-mcp"
    if expected != path or path.resolve() != path.absolute():
        raise ValueError("상승 대상 경로가 예상과 다릅니다.")
    quoted = str(path).replace("'", "''")
    script = ("$ErrorActionPreference='Stop'; "
              f"$dst='{quoted}'; "
              "$p=$dst; while($p){if((Test-Path -LiteralPath $p) -and "
              "((Get-Item -Force -LiteralPath $p).Attributes -band [IO.FileAttributes]::ReparsePoint)){exit 2}; "
              "$p=[IO.Path]::GetDirectoryName($p)}; "
              "if(Test-Path -LiteralPath $dst){Remove-Item -LiteralPath $dst -Recurse -Force}; ")
    if staged is not None:
        source = str(staged).replace("'", "''")
        script += f"Copy-Item -LiteralPath '{source}' -Destination $dst -Recurse -Force"
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    launch = ("$ErrorActionPreference='Stop'; "
              f"$p=Start-Process powershell -ArgumentList '-NoProfile -EncodedCommand {encoded}' -Verb RunAs -Wait -PassThru; exit $p.ExitCode")
    proc = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", launch],
                          capture_output=True, timeout=ELEVATED_DELETE_TIMEOUT_SECONDS)
    if proc.returncode:
        raise PermissionError("elevation_denied")
    return not path.exists() if staged is None else path.is_dir()


def _remove_package(installer: Any, args) -> bool:
    """Max 애플리케이션 패키지 폴더를 지운다. 경로가 예상과 다르거나 3ds Max가 실행 중이면 지우지 않고 실패."""
    path = Path(installer.APPLICATION_PACKAGE_DST)
    label = "Max 애플리케이션 패키지"
    if (path != Path(os.environ.get("ProgramData", "C:/ProgramData")).joinpath("Autodesk", "ApplicationPlugins", "3dsmax-mcp")
            or path.resolve() != path.absolute()):
        print(f"  실패: {label} 경로가 예상(...\\Autodesk\\ApplicationPlugins\\3dsmax-mcp)과 달라 지우지 않았습니다: {path}")
        return False
    if path.resolve() != path.absolute():
        print(f"  경로가 리디렉션되어 건너뜁니다: {path}")
        return False
    if not path.exists():
        print(f"  없음: {label} ({path})")
        return True
    running = installer.running_max_processes()
    if running:
        print(f"  실패: 3ds Max가 실행 중이라 {label}를 지우지 않았습니다 ({', '.join(running)}).")
        print("    3ds Max를 닫고 제거를 다시 실행하세요.")
        return False
    try:
        shutil.rmtree(path)
    except PermissionError:
        if args.allow_elevation and elevated_package(path):
            print(f"  삭제: {label} ({path}, 관리자 권한)")
            return True
        print(f"  실패: {label}를 지울 권한이 없습니다. 관리자 권한으로 이 폴더를 직접 지우세요: {path}")
        return False
    except OSError as exc:
        print(f"  실패: {label} ({path}): {exc}")
        print("    사용 중인 프로그램을 닫고 그 폴더를 직접 지우세요.")
        return False
    print(f"  삭제: {label} ({path})")
    return True


def _remove_cli_registrations(agents: list[str]) -> bool:
    ok = True
    for agent in agents:
        # 일부 CLI(codex·gemini)는 없는 이름을 remove해도 0으로 끝나므로, 종료 코드가 아니라
        # 사용자 범위 설정 파일로 등록 여부를 먼저 확인한다.
        if SERVER_NAME not in registered_servers(agent):
            print(f"  없음: {agent} 등록 ({SERVER_NAME})")
            continue
        command = remove_command(agent, SERVER_NAME)
        error = run_agent_command(command)
        if error is None:
            print(f"  삭제: {agent} 등록 ({SERVER_NAME})")
        else:
            ok = False
            print(f"  실패: {agent} 등록 (CLI 오류)")
            print(f"    직접 실행: {subprocess.list2cmdline(command)}")
    return ok


def _remove_app_entries(apps: list[client_configs.AppConfig]) -> bool:
    ok = True
    for app in apps:
        outcome = client_configs.unregister(app)
        if outcome.status == client_configs.REMOVED:
            print(f"  삭제: {app.label} 항목 ({app.path}, 원본 백업 {client_configs.backup_path(app.path)})")
        elif outcome.status in (client_configs.ABSENT, client_configs.MISSING):
            print(f"  없음: {app.label} 항목 ({app.path})")
        elif outcome.status == client_configs.UNREADABLE:
            ok = False
            print(f"  건너뜀: {app.label} - 읽을 수 없어 바꾸지 않았습니다 ({outcome.message})")
            print(f'    {app.path} 에서 "{app.servers_key}"의 "{SERVER_NAME}" 항목을 직접 지우세요.')
        else:
            ok = False
            print(f"  실패: {app.label} ({app.path}): {outcome.message}")
    return ok


def _running_inside_tool_environment(tool_dir: Path | None) -> bool:
    return tool_dir is not None and _same_path(Path(sys.prefix), tool_dir / SERVER_NAME)


def _uninstall_tool() -> bool:
    """``uv tool uninstall pyjal3dsmcp``. 이미 없으면 건너뛴다. 그 tool 환경 안에서 실행 중이면 명령만 안내한다."""
    manual = f"uv tool uninstall {SERVER_NAME}"
    uv = shutil.which("uv")
    if not uv:
        print(f"  건너뜀: PATH에서 'uv'를 찾지 못했습니다. uv tool로 설치했다면 '{manual}'을 직접 실행하세요.")
        return True
    if _running_inside_tool_environment(_uv_tool_dir(uv, bin_dir=False)):
        print("  실행하지 않음: 이 제거 명령이 지울 uv tool 환경 안에서 실행 중입니다.")
        print(f"    이 창이 끝난 뒤 직접 실행하세요: {manual}")
        return False
    try:
        completed = _run([uv, "tool", "uninstall", SERVER_NAME], capture=True)
    except OSError as exc:
        print(f"  실패: uv를 실행하지 못했습니다 ({exc}). 직접 실행하세요: {manual}")
        return False
    if completed.returncode == 0:
        print(f"  삭제: uv tool ({SERVER_NAME})")
        return True
    listing = _run([uv, "tool", "list"], capture=True)
    installed = any(line.split()[:1] == [SERVER_NAME] for line in listing.stdout.splitlines())
    if listing.returncode == 0 and not installed:
        print(f"  없음: uv tool ({SERVER_NAME})")
        return True
    print(f"  실패: uv tool uninstall ({_first_line(completed.stderr or completed.stdout)})")
    print(f"    실행 중인 MCP 서버와 AI 클라이언트를 닫고 직접 실행하세요: {manual}")
    return False


def _legacy_users(agents: list[str], apps: list[client_configs.AppConfig]) -> list[str]:
    users = [agent for agent in agents if LEGACY_SERVER_NAME in registered_servers(agent)]
    for app in apps:
        servers = client_configs.read_servers(app)
        if servers and LEGACY_SERVER_NAME in servers:
            users.append(app.label)
    return users


def _confirm() -> bool:
    try:
        answer = input("\n위 항목을 지울까요? [y/N]: ")
    except EOFError:
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


def uninstall(args):
    installer = upstream.load_installer()
    # The app cleanup removes the shared Codex/ChatGPT entry even without the CLI.
    agents = [agent for agent in AGENTS if shutil.which(agent)]
    if "url" in (registered_servers("codex").get(SERVER_NAME) or {}):
        agents = [a for a in agents if a != "codex"]
    apps = [app for app in app_configs(installer) if app.path.is_file()]
    package = Path(installer.APPLICATION_PACKAGE_DST)
    skills = _skill_dirs()
    user_data = _user_data_dirs(installer)

    print(f"PyJal3DSMCP 제거기 v{_version(SERVER_NAME)}")
    print("\n지울 대상:")
    print(f"  - CLI 등록 '{SERVER_NAME}': {', '.join(agents) if agents else '(PATH에 CLI 없음)'}")
    for app in apps:
        print(f"  - 앱 설정의 '{SERVER_NAME}' 항목: {app.label} ({app.path})")
    print(f"  - Max 애플리케이션 패키지: {package}")
    for skill in skills:
        print(f"  - skill: {skill}")
    print(f"  - uv tool: {SERVER_NAME} (uv tool uninstall {SERVER_NAME})")
    for path in user_data:
        print(f"  - {'사용자 데이터' if args.purge else '남김(--purge면 삭제)'}: {path}")
    print(f"  '{LEGACY_SERVER_NAME}' 이름의 등록은 지우지 않습니다.")

    if args.dry_run:
        print("\n--dry-run: 아무것도 바꾸지 않았습니다.")
        return 0
    if not args.yes and not _confirm():
        print("취소했습니다. 아무것도 바꾸지 않았습니다.")
        return 1

    args.report["changed"] = True
    legacy = _legacy_users(agents, apps)
    results = []
    print("\n[1/5] CLI 등록")
    results.append(_remove_cli_registrations(agents))
    print("\n[2/5] 앱 설정 파일")
    if "codex" in agents and SERVER_NAME in registered_servers("codex"):
        apps = [app for app in apps if not app.codex]
    results.append(_remove_app_entries(apps))
    print("\n[3/5] Max 애플리케이션 패키지")
    results.append(_remove_package(installer, args))
    print("\n[4/5] skill")
    results.extend(_remove_tree(skill, "skill") for skill in skills)
    print("\n[5/5] uv tool")
    tool = args.report["inventory"]["uv_tool"]
    inside = tool["environment"] and _same_path(Path(tool["environment"]), Path(sys.prefix))
    if inside:
        print(f"이 창이 끝난 뒤 직접 실행하세요: uv tool uninstall {SERVER_NAME}")
        args.report["next_actions"].append(dict(code="UV_TOOL_SELF_REMOVAL_PENDING", actor="agent", when="after_exit", required=True,
                                                argv=["uv", "tool", "uninstall", SERVER_NAME]))
    else:
        results.append(_uninstall_tool())
        if any(p and Path(p).exists() for p in (tool["environment"], tool["launcher"])):
            results.append(False)
    if args.purge:
        print("\n[--purge] 사용자 데이터")
        results.extend(_remove_tree(path, "사용자 데이터") for path in user_data)

    if legacy:
        print(f"\n  경고: 기존 '{LEGACY_SERVER_NAME}' 등록이 남아 있습니다 ({', '.join(legacy)}). 이 제거기는 지우지 않았습니다.")
        print("  그 등록도 같은 Max 애플리케이션 패키지를 쓰므로 패키지를 지운 뒤에는 동작하지 않습니다.")
        print("  쓰지 않는다면 그 클라이언트에서 직접 지우세요.")

    print("\n" + "=" * 60)
    if all(results):
        print("  제거를 마쳤습니다.")
    else:
        print("  일부 항목을 지우지 못했습니다. 위의 '실패'·'실행하지 않음' 안내를 확인하세요.")
    print("=" * 60)
    return 1 if not all(results) else 3 if inside else 0


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def resolve_revision(url, revision):
    if re.fullmatch(r"[a-fA-F0-9]{40}", revision):
        return revision.lower()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._/-]*", revision):
        raise ValueError("지원하지 않는 Git ref입니다.")
    git = shutil.which("git")
    if not git:
        raise ValueError("Git ref를 확인하려면 git이 필요합니다.")
    refs = [f"refs/tags/{revision}^{{}}", f"refs/tags/{revision}", f"refs/heads/{revision}"]
    if revision == "HEAD":
        refs.append("HEAD")
    proc = _run([git, "ls-remote", "--", url, *refs], capture=True)
    lines = dict((name, sha) for sha, name in (line.split() for line in proc.stdout.splitlines()))
    for name in refs:
        if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", lines.get(name, "")):
            return lines[name]
    raise ValueError("원격 ref를 확인할 수 없습니다.")


def lifecycle(operation):
    report = dict(schema_version=1, operation=operation, mode="apply", inventory={}, build={}, steps=[], issues=[],
                  next_actions=[], changed=False, outcome="blocked")
    wants_json = "--json" in sys.argv[1:]
    parser = UsageParser(prog=f"pyjal3dsmcp-{operation}")
    for flag in ("status", "dry-run", "json", "yes", "allow-elevation"):
        parser.add_argument("--" + flag, action="store_true")
    if operation == "uninstall":
        parser.add_argument("--purge", action="store_true")
    else:
        _add_install_flags(parser)
        if operation == "update":
            parser.add_argument("--no-pull", action="store_true")
    code = 2
    try:
        args = parser.parse_args()
        args.report = report
        with contextlib.redirect_stdout(sys.stderr if wants_json else sys.stdout):
            code = execute(operation, args)
    except (ValueError, OSError, RuntimeError, install_status.zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        reason = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "elevation_denied" if isinstance(exc, PermissionError) else "preflight_error"
        report["issues"].append(dict(code=reason, message=str(exc)))
        code = 1 if report["changed"] else 2
    if code not in (0, 1, 2, 3):
        report["child_exit_code"] = code
        code = 1
    report["exit_code"] = code
    if code:
        if not report["issues"]:
            report["issues"].append(dict(code={1: "operation_failed", 2: "blocked", 3: "follow_up_required"}[code]))
        if not report["next_actions"]:
            report["next_actions"].append(dict(code="REVIEW_ISSUE", actor="human", when="before_retry", required=True, instruction="표시된 설정과 권한을 확인하고 다시 실행하세요."))
    if wants_json:
        print(json.dumps(report, ensure_ascii=False))
    elif report["issues"] or report["mode"] != "apply":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


def execute(operation, args):
    report = args.report
    direct = _direct_url() or {}
    parsed = urlparse(direct.get("url") or "")
    if parsed.password or (parsed.username and parsed.scheme in ("https", "http")) or parsed.query:
        raise ValueError("인증 정보를 포함하지 않은 저장소 URL이 필요합니다.")
    state = install_status.inventory(sys.modules[__name__], upstream.load_installer())
    if state["process_inspection"] == "unknown" and os.name == "nt":
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        query = ("$ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process "
                 "-Filter \"Name='python.exe' OR Name='pyjal3dsmcp.exe'\" | "
                 "Select-Object ProcessId,ParentProcessId,ExecutablePath,Name,CommandLine) | ConvertTo-Json -Compress")
        check = _run([str(shell), "-NoProfile", "-NonInteractive", "-Command", query], capture=True)
        if check.returncode == 0:
            rows = json.loads(check.stdout or "[]")
            tool = state["uv_tool"]
            known = [Path(tool["launcher"])] if tool["launcher"] else []
            if tool["environment"]:
                known.append(Path(tool["environment"]) / "Scripts/pyjal3dsmcp.exe")
            for row in rows if isinstance(rows, list) else [rows]:
                path = Path(row.get("ExecutablePath") or "")
                if path in known or (tool["environment"] and path == Path(tool["environment"]) / "Scripts/python.exe"
                                      and " -m pyjal3dsmcp.server" in (row.get("CommandLine") or "")):
                    state["server_processes"].append(dict(pid=row["ProcessId"], parent_pid=row["ParentProcessId"],
                                                         parent_app=None, ownership="unknown"))
            state["process_inspection"] = "checked_fallback"
    report["inventory"] = state
    report["mode"] = "status" if args.status else "plan" if args.dry_run else "apply"
    tool = state["uv_tool"]
    installed, direct_installed = install_status.installed_build(tool["environment"])
    parsed = urlparse(direct_installed.get("url") or "")
    if parsed.password or (parsed.username and parsed.scheme in ("https", "http")) or parsed.query:
        raise ValueError("설치된 소스 URL의 인증 정보를 제거하세요.")
    caller = install_status.build(direct, _version(SERVER_NAME))
    if not caller["commit"]:
        caller.update(commit=_build_info.COMMIT, dirty=_build_info.DIRTY)
    root = checkout_root()
    if root:
        caller["source_kind"] = "checkout"
        if (root / ".git").exists() and shutil.which("git"):
            head = _run(["git", "-C", str(root), "rev-parse", "HEAD"], capture=True)
            if head.returncode == 0:
                caller.update(commit=head.stdout.strip(), dirty=bool(_run(["git", "-C", str(root), "status", "--porcelain"], capture=True).stdout))
    target = caller.copy()
    report["build"] = dict(caller=caller, installed=installed, target=target)
    if args.status and args.dry_run:
        raise ValueError("--status와 --dry-run 중 하나를 선택하세요.")
    if args.status:
        report["outcome"] = "status"
        return 0
    if operation != "uninstall":
        if (args.client and args.all_clients) or (not args.all_clients and not args.client and
                                                  (args.yes or not sys.stdin.isatty())):
            raise ValueError("클라이언트를 --client 또는 --all-clients로 지정하세요.")
        args.profile = args.team_profile
        if args.profile is None and (Path.cwd() / TEAM_PROFILE_FILE_NAME).is_file():
            args.profile = Path.cwd() / TEAM_PROFILE_FILE_NAME
        if args.profile:
            team_cli.validate(args.profile)
        args.source = install_source()
        if args.profile:
            settings, error = read_local_settings()
            if error:
                raise ValueError(error)
        if args.track_ref:
            target["requested_ref"] = args.track_ref
        if args.wheel:
            wheel = args.wheel.resolve(strict=True)
            if not wheel.name.startswith("pyjal3dsmcp-") or wheel.suffix != ".whl":
                raise ValueError("제품 wheel 경로가 필요합니다.")
            args.source = str(wheel)
            target.update(source_kind="wheel", requested_ref=None, commit=None, dirty=None)

        args.tracking = None
        if operation == "update" and root and not args.no_pull and (root / ".git").exists() and shutil.which("git"):
            args.tracking = checkout_target(root)
            if args.tracking:
                target.update(commit=args.tracking[2], requested_ref=args.tracking[1])
        if operation == "update" and not args.wheel and not root and (direct_installed or direct).get("vcs_info"):
            source = direct_installed or direct
            revision = args.ref or installed.get("requested_ref") or caller["requested_ref"] or "HEAD"
            target.update(commit=resolve_revision(source["url"], revision), requested_ref=args.track_ref or revision, source_kind="git")
            args.source = f"git+{source['url']}@{target['commit']}"
        elif args.ref:
            raise ValueError("--ref는 Git URL로 설치된 환경의 업데이트에 사용하세요.")
        if args.source and Path(args.source).suffix.lower() == ".whl" and Path(args.source).is_file():
            path = Path(args.source)
            provenance, args.wheel_matches_caller = install_status.wheel_target(path, PACKAGE_DIR)
            target.update(provenance)
            target["wheel_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        args.reuse = bool(tool["installed"] and target["commit"] and installed["commit"] == target["commit"]
                          and direct_installed.get("url") == direct.get("url") and not args.force_reinstall)
        if root and target["source_kind"] == "checkout":
            original_path = _file_url_path(direct_installed.get("url", ""))
            args.reuse = bool(original_path and _same_path(Path(original_path), root)
                              and tool["installed"] and target["commit"] and installed["commit"] == target["commit"]
                              and caller["dirty"] is False and installed["dirty"] is False and not args.force_reinstall)
        if (tool["installed"] and target["wheel_sha256"] and installed["wheel_sha256"] == target["wheel_sha256"]
                and not args.force_reinstall):
            args.reuse = True
        if (tool["installed"] and tool["environment"] and _same_path(Path(sys.prefix), Path(tool["environment"]))
                and operation == "install" and target["source_kind"] == "wheel" and not args.force_reinstall):
            args.reuse = True
        if operation == "update" and args.reuse and not args.profile and (not root or caller["commit"] == target["commit"]):
            report["outcome"] = "no_change"
            return 0
    if operation == "update" and (target["commit"] != caller["commit"] or
                                  (args.wheel and not args.wheel_matches_caller)):
        state["max_package"]["state"] = "unknown"
        state["max_package"]["differences"] = []
    dest = Path(state["max_package"]["path"])
    report["steps"] = [dict(action=operation, target=tool["environment"], elevation="not_required"),
                       dict(action="bridge", target=str(dest), elevation="possible"),
                       dict(action="clients", targets=[c["config_path"] for c in state["clients"]], elevation="not_required")]
    apps = app_configs(upstream.load_installer(), tuple(getattr(args, "client", [])))
    if getattr(args, "no_apps", False):
        apps = []
    report["steps"][-1].update(targets=[str(app.path) for app in apps], backups=[
        str(client_configs.backup_path(app.path)) for app in apps if app.path.is_file()])
    if operation != "uninstall":
        report["steps"].append(dict(action="configuration", target=state["ini"]["path"], elevation="not_required"))
        if args.profile:
            report["steps"].append(dict(action="link_profile", source=str(args.profile.absolute()),
                                        target=str(local_settings_path()), elevation="not_required"))
        skills = []
        if args.skill in ("global", "both"):
            skills.extend(_skill_dirs())
        if args.skill in ("project", "both"):
            skills.append(Path.cwd() / ".agents/skills" / SKILL_NAME)
        if skills:
            report["steps"].append(dict(action="skill", targets=list(map(str, skills)), elevation="not_required"))
    if operation == "uninstall":
        report["steps"].append(dict(action="remove_skills", targets=list(map(str, _skill_dirs())), elevation="not_required"))
        report["steps"].append(dict(action="purge" if args.purge else "preserve",
                                    targets=list(map(str, _user_data_dirs(upstream.load_installer())))))
    pids = [p["pid"] for p in state["server_processes"]]
    if state["max_processes"] and (operation == "uninstall" or state["max_package"]["state"] != "matched"):
        pids += [pid for proc in state["max_processes"] for pid in proc["pids"]]
        report["issues"].append(dict(code="max_running"))
    if pids or report["issues"]:
        report["next_actions"].append(dict(code="SAVE_AND_CLOSE_MAX" if state["max_processes"] and report["issues"] else "CLOSE_CLIENT", actor="human", when="before_retry", required=True, pids=pids, instruction="작업을 저장하고 해당 앱을 닫아 주세요."))
        return 2
    if operation == "uninstall" and args.purge:
        purge_dirs = _user_data_dirs(upstream.load_installer())
        pointers = state["team_profile"]
        for text in (pointers.get("recorded"), pointers.get("path")):
            if isinstance(text, str) and any(Path(text).resolve().is_relative_to(folder.resolve()) for folder in purge_dirs):
                raise ValueError("연결한 프로필이 제거 대상 안에 있습니다. 프로필을 보존 위치로 옮기세요.")
    if args.dry_run:
        if operation == "uninstall" and not args.json:
            uninstall(args)
        report["outcome"] = "planned"
        return 0
    if operation == "uninstall" and not args.yes and not sys.stdin.isatty():
        if not args.json:
            args.dry_run = True
            uninstall(args)
        raise ValueError("비대화식 제거에는 --yes가 필요합니다.")
    if operation != "uninstall" and not args.reuse and not args.editable and tool["environment"] and _same_path(Path(tool["environment"]), Path(sys.prefix)):
        raise ValueError("같은 uv tool 환경을 교체하려면 uvx --isolated에서 실행하세요.")
    if os.name == "nt" and state["process_inspection"] == "unknown":
        raise ValueError("서버 프로세스 잠금 상태를 확인할 수 없습니다.")
    if tool["installed"] and state["process_inspection"] == "checked":
        active, inspected = install_status.processes(Path(tool["environment"]) if tool["environment"] else None,
                                                     Path(tool["launcher"]) if tool["launcher"] else None)
        if active or inspected != "checked":
            report["inventory"]["server_processes"] = active
            report["next_actions"].append(dict(code="CLOSE_CLIENT", actor="human", when="before_retry", required=True, pids=[p["pid"] for p in active],
                                                instruction="앱을 닫고 잠금을 다시 확인하세요."))
            return 2
    code = {"install": install, "update": update, "uninstall": uninstall}[operation](args)
    if code == 0 and operation == "install":
        report["changed"] = True
    if args.json:
        after, _ = install_status.installed_build(tool["environment"])
        report["verification"] = dict(installed=after, environment_exists=bool(tool["environment"] and Path(tool["environment"]).exists()),
                                      launcher_exists=bool(tool["launcher"] and Path(tool["launcher"]).is_file()))
        if code == 0 and operation != "uninstall" and target["commit"] and after["commit"] and target["commit"] != after["commit"]:
            report["issues"].append(dict(code="installed_build_mismatch"))
            code = 1
    if code == 3 and not report["next_actions"]:
        report["next_actions"].append(dict(code="REGISTER_CLIENT", actor="human", when="after_exit", required=True, instruction="클라이언트 설정 경로를 확인하고 수동 등록하거나 다시 실행하세요."))
    report["outcome"] = "complete" if code == 0 else "partial" if code == 3 else "failed"
    if code == 0 and operation != "uninstall":
        report["next_actions"].append(dict(code="VERIFY_CONNECTION", actor="agent", when="after_reconnect", required=False, instruction="재연결 후 query_scene와 library_attach로 확인하세요."))
    return code


def main():
    return lifecycle("install")


def update_main():
    return lifecycle("update")


def uninstall_main():
    return lifecycle("uninstall")


if __name__ == "__main__":
    raise SystemExit(main())
