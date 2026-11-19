import codecs
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pyjal3dsmcp import client_configs as configs
from pyjal3dsmcp.installer import app_configs


class DesktopRegistrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.command = [str(self.root / "Artist Tools" / "작업" / "pyjal3dsmcp.exe")]

    def test_openai_clients_share_one_config_without_a_cli(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex")}):
            apps = app_configs(Mock(spec=[]), ("codex", "chatgpt-desktop"))
        self.assertEqual(len(apps), 1)
        app = apps[0]
        self.assertEqual(app.path, self.root / "codex" / "config.toml")
        self.assertEqual(configs.register(app, self.command).status, configs.CREATED)
        servers = tomllib.loads(app.path.read_text("utf-8"))["mcp_servers"]
        self.assertEqual(servers, {"pyjal3dsmcp": {"command": self.command[0]}})

    def test_toml_update_retry_and_removal_preserve_user_settings(self):
        app = configs.AppConfig("Codex", self.root / "config.toml", codex=True)
        original = (
            '# Artist preferences\n[mcp_servers.other]\ncommand = "keep"\n'
            '[mcp_servers.pyjal3dsmcp]\ncommand = "uv"\nargs = ["run"]\n'
            'tool_timeout_sec = 90 # keep this override\n'
        ).encode()
        app.path.write_bytes(original)
        self.assertEqual(configs.register(app, self.command).status, configs.UPDATED)
        first = app.path.read_bytes()
        self.assertIn(b"# keep this override", first)
        entry = tomllib.loads(first.decode())["mcp_servers"]["pyjal3dsmcp"]
        self.assertEqual(entry, {"command": self.command[0], "tool_timeout_sec": 90})
        self.assertEqual(configs.register(app, self.command).status, configs.UNCHANGED)
        self.assertEqual(app.path.read_bytes(), first)
        self.assertEqual(configs.unregister(app).status, configs.REMOVED)
        self.assertEqual(configs.backup_path(app.path).read_bytes(), original)
        self.assertEqual(tomllib.loads(app.path.read_text("utf-8"))["mcp_servers"], {"other": {"command": "keep"}})

    def test_invalid_toml_is_reported_without_overwriting_it(self):
        app = configs.AppConfig("Codex", self.root / "config.toml", codex=True)
        app.path.write_bytes(b"[mcp_servers")
        self.assertEqual(configs.register(app, self.command).status, configs.UNREADABLE)
        self.assertEqual(app.path.read_bytes(), b"[mcp_servers")
        self.assertFalse(configs.backup_path(app.path).exists())

    def test_claude_only_preserves_other_servers_and_bom(self):
        installer = Mock()
        installer.claude_desktop_config_paths.return_value = [self.root / "claude_desktop_config.json"]
        app, = app_configs(installer, ("claude-desktop",))
        original = codecs.BOM_UTF8 + b'{"mcpServers":{"other":{"command":"keep"}}}'
        app.path.write_bytes(original)
        self.assertEqual(configs.register(app, self.command).status, configs.UPDATED)
        self.assertTrue(app.path.read_bytes().startswith(codecs.BOM_UTF8))
        servers = json.loads(app.path.read_bytes())["mcpServers"]
        self.assertEqual(servers["pyjal3dsmcp"], {"command": self.command[0]})
        self.assertEqual(servers["other"], {"command": "keep"})
        self.assertEqual(configs.backup_path(app.path).read_bytes(), original)
