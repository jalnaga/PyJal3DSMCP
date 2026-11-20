"""wheel 및 sdist에 빌드 시점의 Git 출처를 기록한다."""

from pathlib import Path
import re
import subprocess
import tempfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class ProvenanceHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        info = {"commit": None, "dirty": None}
        if (root / ".git").exists():
            def git(*args):
                return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
            commit = git("rev-parse", "HEAD")
            if re.fullmatch("[a-f0-9]{40}", commit):
                info.update(commit=commit, dirty=bool(git("status", "--porcelain")))
        if info["commit"] is None:
            return
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as stream:
            stream.write(f"COMMIT = {info['commit']!r}\nDIRTY = {info['dirty']!r}\n")
            self.generated = Path(stream.name)
        build_data["force_include"][str(self.generated)] = "pyjal3dsmcp/_build_info.py"

    def finalize(self, version, build_data, artifact_path):
        if hasattr(self, "generated"):
            self.generated.unlink(missing_ok=True)
