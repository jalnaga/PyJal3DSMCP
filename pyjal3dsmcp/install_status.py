"""설치기가 확인하는 디스크 상태와 Windows 프로세스 정보."""

import ast
import configparser
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import zipfile

from . import client_configs, team_cli


def processes(tool, launcher):
    if os.name != "nt":
        return [], "unknown"
    try:
        import win32api
        import win32security
        from win32com.client import GetObject

        service = GetObject("winmgmts:root/cimv2")
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), 8)
        sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
        result = []
        for proc in service.ExecQuery("SELECT * FROM Win32_Process WHERE Name='pyjal3dsmcp.exe' OR Name='python.exe'"):
            path = Path(proc.ExecutablePath or "")
            if not (launcher and path == launcher or tool and path == tool / "Scripts/pyjal3dsmcp.exe" or
                    tool and path == tool / "Scripts/python.exe" and
                    re.search(r"(?:^|\s)-m\s+pyjal3dsmcp\.server(?:\s|$)", proc.CommandLine or "")):
                continue
            info = proc.ExecMethod_("GetOwnerSid")
            parent = next(iter(service.ExecQuery(f"SELECT Name FROM Win32_Process WHERE ProcessId={int(proc.ParentProcessId)}")), None)
            result.append(dict(pid=int(proc.ProcessId), path=str(path), start=proc.CreationDate,
                               ownership="confirmed" if info.Sid == sid else "other_or_unknown",
                               parent_pid=int(proc.ParentProcessId), parent_app=parent.Name if parent else None,
                               session=int(proc.SessionId)))
        return result, "checked"
    except Exception:
        return [], "unknown"


def inventory(api, installer):
    uv = shutil.which("uv")
    root = api._uv_tool_dir(uv, bin_dir=False) if uv else None
    bin_dir = api._uv_tool_dir(uv, bin_dir=True) if uv else None
    tool = root / api.SERVER_NAME if root else None
    launcher = bin_dir / ("pyjal3dsmcp.exe" if os.name == "nt" else api.SERVER_NAME) if bin_dir else None
    procs, process_state = processes(tool, launcher)
    dest = installer.APPLICATION_PACKAGE_DST
    differences = []
    bridge = "absent"
    if dest.exists():
        try:
            with tempfile.TemporaryDirectory() as tmp:
                stage = Path(tmp)
                _, missing = installer.stage_bundle(stage)
                differences = installer.package_differences(stage, dest)
                bridge = "different" if missing or differences else "matched"
        except (RuntimeError, OSError):
            bridge = "unknown"
    clients = []
    for app in api.app_configs(installer):
        entries = client_configs.read_servers(app)
        state = ("missing_file" if not app.path.exists() else "unreadable" if entries is None else
                 "present" if api.SERVER_NAME in entries else "absent")
        entry = (entries or {}).get(api.SERVER_NAME)
        entry = entry if isinstance(entry, dict) else {}
        command = entry.get("command")
        executable = command[0] if isinstance(command, list) and command else command
        executable = executable if isinstance(executable, str) else None
        # 임의의 등록 인수에는 인증 정보가 있을 수 있어 알려진 실행 형식만 출력한다.
        args = entry.get("args", [])
        argv = None
        if executable and Path(executable).stem.lower() in ("uv", "python", api.SERVER_NAME):
            if not args or args == ["-m", "pyjal3dsmcp.server"] or (
                isinstance(args, list) and len(args) == 4 and args[:2] == ["run", "--directory"] and args[-1] == api.SERVER_NAME
            ):
                argv = [executable, *args]
        environment = entry.get("env", {})
        override = environment.get("PYJAL_DLEL_TEAM_PROFILE") if isinstance(environment, dict) else None
        clients.append(dict(variant=app.label, config_path=str(app.path), state=state, argv=argv,
                            executable_exists=Path(executable).is_file() if executable and Path(executable).is_absolute() else None,
                            team_profile=override if isinstance(override, str) else None))
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(installer.CONFIG_DST, encoding="utf-8-sig")
    except (configparser.Error, OSError, UnicodeError):
        config.clear()
    return dict(uv_tool=dict(environment=str(tool) if tool else None, launcher=str(launcher) if launcher else None,
                             installed=bool(launcher and launcher.is_file())), caller_prefix=sys.prefix,
                cli_clients=[dict(id=a, executable=shutil.which(a), registered=api.SERVER_NAME in api.registered_servers(a)) for a in api.AGENTS],
                server_processes=procs, process_inspection=process_state, clients=clients,
                max_package=dict(path=str(dest), state=bridge, differences=differences),
                max_processes=[dict(description=p, pids=[int(n) for n in re.findall(r"pid\s+(\d+)", p, re.I)],
                                    unsaved_changes=None, save_state_reason="no_verified_pid_bound_connection") for p in installer.running_max_processes()],
                ini=dict(path=str(installer.CONFIG_DST), safe_mode=config.get("mcp", "safe_mode", fallback=None),
                         tool_profile=config.get("mcp", "tool_profile", fallback=None), effective_tool_profile="full"),
                team_profile=team_cli.show())


def build(direct, version):
    direct = direct or {}
    vcs = direct.get("vcs_info", {})
    return dict(version=version, source_kind="git" if vcs else "wheel" if "archive_info" in direct else
                "checkout" if "dir_info" in direct else "unknown", commit=vcs.get("commit_id"),
                requested_ref=vcs.get("requested_revision"), dirty=None,
                wheel_sha256=direct.get("archive_info", {}).get("hashes", {}).get("sha256"))


def build_info(source):
    fields = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            field = {"COMMIT": "commit", "DIRTY": "dirty"}.get(node.targets[0].id)
            if field and isinstance(node.value, ast.Constant) and node.value.value is not None:
                fields[field] = node.value.value
    return fields


def wheel_target(path, package_dir):
    """wheel 안의 빌드 근거와 호출 설치기의 배포 코드 일치를 읽기 전용으로 확인한다."""
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        info = "pyjal3dsmcp/_build_info.py"
        recorded = build_info(archive.read(info)) if info in members else {}
        modules = [name for name in members if name.startswith(("pyjal3dsmcp/", "pyjal_extensions/")) and name.endswith(".py")]
        same = bool(modules)
        for name in modules:
            target = package_dir.parent / name
            if ".." in Path(name).parts or not target.is_file() or target.read_bytes() != archive.read(name):
                same = False
                break
    return recorded, same


def installed_build(tool):
    if tool:
        for metadata in Path(tool).glob("Lib/site-packages/pyjal3dsmcp-*.dist-info"):
            path = metadata / "direct_url.json"
            direct = json.loads(path.read_bytes()) if path.is_file() else {}
            version = metadata.name.removeprefix("pyjal3dsmcp-").removesuffix(".dist-info")
            result = build(direct, version)
            info = metadata.parent / "pyjal3dsmcp/_build_info.py"
            if info.is_file():
                result.update(build_info(info.read_bytes()))
            receipt = Path(tool) / "pyjal3dsmcp-source.json"
            if receipt.is_file():
                saved = json.loads(receipt.read_bytes())
                if saved.get("commit") == result["commit"]:
                    result["requested_ref"] = saved.get("requested_ref")
                    if saved.get("source_url") == direct.get("url") and saved.get("wheel_sha256"):
                        result["wheel_sha256"] = saved["wheel_sha256"]
            return result, direct
    return build({}, None), {}
