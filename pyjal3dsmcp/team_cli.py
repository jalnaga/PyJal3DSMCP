"""사용자가 관리하는 프로필 및 이 PC의 설정을 편집한다."""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib

import tomlkit

from pyjal_extensions import team_profile as profiles
from pyjal_extensions.discovery import build_index, normalize_entrypoints
from pyjal_extensions.locations import TEAM_PROFILE_ENV, local_settings_path


def current_bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def replace_checked(path, before, after, *, backup=False):
    if before == after:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(after)
    try:
        if current_bytes(path) != before:
            raise ValueError(f"파일이 변경되었습니다: {path}")
        if backup and before is not None:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".bak", delete=False) as saved:
                saved.write(before)
                print(saved.name)
        if before is None:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate(path, *, strict=False, raw=None):
    warnings = []
    loaded = profiles._load_profile(path, warnings, raw)
    if not loaded or warnings:
        raise ValueError(str(warnings))
    if strict:
        data = tomllib.loads((raw if raw is not None else path.read_bytes()).decode())
        if len(data.get("preconditions", [])) != len(loaded.preconditions):
            raise ValueError("사전 조건의 형식 또는 중복 ID를 확인하세요.")
        for values in ([r.root_id for r in loaded.trust_roots], [s.library_id for s in loaded.libraries if s.library_id]):
            if len(values) != len(set(values)):
                raise ValueError("중복 ID입니다.")
        for root in loaded.trust_roots:
            if not Path(root.location).is_dir():
                raise ValueError(f"루트에 접근할 수 없습니다: {root.location}")
        for item in loaded.libraries:
            scan = profiles.scan_source(item.source_path)
            entries = normalize_entrypoints(item.entrypoints)
            build_index(scan, item.library_id or scan.package or "", "", entries, strict_entrypoints=True)
    return loaded


def settings_change(change):
    path = local_settings_path()
    before = current_bytes(path)
    data = json.loads(before) if before is not None else {}
    if not isinstance(data, dict):
        raise ValueError("로컬 설정은 JSON 객체여야 합니다.")
    original = data.copy()
    change(data)
    if original != data:
        replace_checked(path, before, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


def link(path):
    path = path.resolve(strict=True)
    validate(path)
    settings_change(lambda data: data.update(team_profile=str(path)))
    print(path)


def show():
    config = profiles.load_team_config()
    settings, error = profiles.read_local_settings()
    override = os.environ.get(TEAM_PROFILE_ENV, "").strip()
    return dict(source="environment" if override else "settings" if config.profile_path else "none",
                roots=[dict(id=r.root_id, path=r.location, accessible=Path(r.location).is_dir()) for r in config.trust_roots],
                path=config.profile_path, sha256=config.profile_sha256,
                environment=os.environ.get(TEAM_PROFILE_ENV), recorded=(settings or {}).get("team_profile"),
                warnings=config.warnings + profiles.trust_root_warnings(config))


def edit(args):
    source = args.file.resolve(strict=True)
    target = source if args.personal_file else args.work_copy.resolve(strict=True)
    if not args.personal_file and source.samefile(target):
        raise ValueError("팀 원본과 작업 사본이 같습니다.")
    original = source.read_bytes()
    before = target.read_bytes()
    document = tomlkit.parse(before.decode())
    section, key = {"trust-root": ("trust_roots", "id"), "library": ("libraries", "library_id"),
                    "precondition": ("preconditions", "library_id")}[args.kind]
    if args.action == "set" and args.kind != "precondition":
        raise ValueError("set은 사전 조건에서 사용할 수 있습니다.")
    items = document.setdefault(section, tomlkit.aot())
    path_key = "path" if args.kind == "trust-root" else "source_path"
    relative = os.path.relpath(args.path.resolve(), source.parent).replace(os.sep, "/") if args.path else None
    matches = [i for i, entry in enumerate(items) if entry.get(key) == args.id or
               (args.kind == "library" and not entry.get(key) and relative and
                (target.parent / entry[path_key]).resolve() == (target.parent / relative).resolve())]
    if len(matches) > 1:
        raise ValueError("중복 ID 또는 경로입니다.")
    if args.action == "remove":
        if matches:
            del items[matches[0]]
    else:
        entry = {key: args.id}
        if args.kind == "precondition":
            if not args.requires_loaded or args.message is None:
                raise ValueError("--requires-loaded 및 --message가 필요합니다.")
            entry.update(requires_loaded=args.requires_loaded, message=args.message)
        else:
            if relative is None:
                raise ValueError("--path가 필요합니다.")
            entry[path_key] = relative
            if args.entrypoints:
                entry["entrypoints"] = json.loads(args.entrypoints)
        if matches:
            existing = items[matches[0]]
            if args.action == "add" and any(existing.get(k) != v for k, v in entry.items()):
                raise ValueError("동일 ID에 다른 값이 있습니다.")
            existing.update(entry)
        else:
            items.append(entry)
    after = tomlkit.dumps(document).replace("\r\n", "\n")
    if b"\r\n" in before:
        after = after.replace("\n", "\r\n")
    result = after.encode()
    validate(target, strict=True, raw=result)
    effective = show()
    fields = [str(source), str(target), original.hex(), before.hex(), result.hex(), effective]
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    print("".join(difflib.unified_diff(before.decode().splitlines(True), after.splitlines(True),
                                      fromfile=str(target), tofile=str(target))))
    if args.kind == "trust-root":
        print("신뢰 루트는 하위 라이브러리의 import·실행 승인에 영향을 줍니다.")
    elif args.kind == "library":
        print("이 변경은 라이브러리 자동 활성화 및 entrypoint에 영향을 줍니다.")
    else:
        print("이 변경은 보호 도구의 호출 사전 조건에 영향을 줍니다.")
    print("plan_sha256", digest)
    if args.apply:
        if args.expect_sha256 != digest or source.read_bytes() != original:
            raise ValueError("검토한 계획과 현재 파일이 다릅니다.")
        replace_checked(target, before, result, backup=True)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "set", "validate", "clear", "show", "personal", "edit"):
        cmd = commands.add_parser(name)
        if name in {"init", "set", "validate"}:
            cmd.add_argument("path", type=Path, **({"nargs": "?"} if name == "validate" else {}))
        if name == "init":
            cmd.add_argument("--anchor", required=True, type=Path)
        if name == "show":
            cmd.add_argument("--json", action="store_true")
        if name == "personal":
            sub = cmd.add_subparsers(dest="action", required=True)
            for action in ("enable", "disable", "library"):
                op = sub.add_parser(action)
                if action == "library":
                    op.add_argument("verb", choices=["add", "remove"])
                    op.add_argument("--path", type=Path)
                    op.add_argument("--entrypoints")
                op.add_argument("id")
        if name == "edit":
            cmd.add_argument("--file", required=True, type=Path)
            scope = cmd.add_mutually_exclusive_group(required=True)
            scope.add_argument("--personal-file", action="store_true")
            scope.add_argument("--work-copy", type=Path)
            cmd.add_argument("kind", choices=["library", "precondition", "trust-root"])
            cmd.add_argument("action", choices=["add", "remove", "set"])
            cmd.add_argument("id")
            cmd.add_argument("--path", type=Path)
            cmd.add_argument("--entrypoints")
            cmd.add_argument("--requires-loaded", action="append")
            cmd.add_argument("--message")
            cmd.add_argument("--apply", action="store_true")
            cmd.add_argument("--expect-sha256")
    args = parser.parse_args()
    try:
        if args.command == "init":
            path = args.path.absolute()
            anchor = args.anchor.resolve(strict=True)
            if not anchor.is_dir():
                raise ValueError("anchor 디렉터리가 필요합니다.")
            template = Path(__file__).parent / "docs/pyjal3dsmcp-team.example.toml"
            if not template.is_file():
                template = Path(__file__).resolve().parents[1] / "docs/pyjal/examples" / template.name
            doc = tomlkit.parse(template.read_text(encoding="utf-8"))
            doc["anchor"] = os.path.relpath(anchor, path.parent).replace(os.sep, "/")
            replace_checked(path, None, tomlkit.dumps(doc).encode())
            validate(path)
        elif args.command == "set":
            link(args.path)
        elif args.command == "clear":
            settings_change(lambda d: d.pop("team_profile", None))
        elif args.command == "validate":
            path = args.path or show()["path"]
            if not path:
                raise ValueError("연결된 프로필이 없습니다.")
            validate(Path(path))
        elif args.command == "edit":
            edit(args)
        elif args.command == "personal":
            def change(data):
                if args.action == "library":
                    items = list(data.get("libraries", []))
                    others = [item for item in items if item.get("library_id") != args.id]
                    if args.verb == "add":
                        if not args.path:
                            raise ValueError("--path가 필요합니다.")
                        entry = {"library_id": args.id, "source_path": str(args.path.resolve(strict=True))}
                        if args.entrypoints:
                            entry["entrypoints"] = normalize_entrypoints(json.loads(args.entrypoints))
                        scan = profiles.scan_source(entry["source_path"])
                        build_index(scan, args.id, "", entry.get("entrypoints", []), strict_entrypoints=True)
                        if len(items) != len(others) and entry not in items:
                            raise ValueError("동일 ID에 다른 라이브러리가 있습니다.")
                        if entry not in items:
                            items.append(entry)
                        data["libraries"] = items
                    else:
                        data["libraries"] = others
                else:
                    values = list(data.get("disabled_libraries", []))
                    if args.action == "disable" and args.id not in values:
                        values.append(args.id)
                    if args.action == "enable" and args.id in values:
                        values.remove(args.id)
                    data["disabled_libraries"] = values
            settings_change(change)
        print(json.dumps(show(), ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError, profiles.LibraryError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
