# PyJal3DSMCP

[한국어 README](README.ko.md)

Connect Claude Desktop, Codex, or ChatGPT Desktop to Autodesk 3ds Max on your Windows workstation. Ask your assistant to inspect a scene, find objects by name, or work with materials and modifiers. PyJal3DSMCP includes the tools from [3dsmax-mcp](https://github.com/cl0nazepamm/3dsmax-mcp) and adds 11 tools for discovering and running your own Python libraries inside Max.

## Ask an agent to install it

Copy this into an agent that can run PowerShell on the PC where you use Max. Replace **Claude Desktop** with **Codex** or **ChatGPT Desktop** if that is the app you use.

> Install https://github.com/jalnaga/PyJal3DSMCP for Claude Desktop on this Windows PC. Follow the README and register the server for that app. Let me save my scene if Max needs to close. After restart, verify the connection by reading a scene overview.

You need Windows, **3ds Max 2023–2027**, and a desktop client with local MCP support. For ChatGPT Desktop, use a local Work session with **Settings → MCP servers**. The client launches the server locally over standard input/output (stdio). [OpenAI desktop MCP setup](https://learn.chatgpt.com/docs/extend/mcp)

## Install on Windows

1. Open PowerShell as the Windows user who runs Max and the client. Check `git --version` and `uv --version`. If either command is missing, install the corresponding package below and reopen PowerShell. uv can download Python 3.12 for this project. [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/)

   ```powershell
   winget install --id Git.Git -e
   winget install --id astral-sh.uv -e
   ```

2. Save your scene and close 3ds Max before installing the bridge for the first time. If the matching bridge is already installed, Max can stay open. For an update, close any clients using the previous server executable. Clone the repository into a folder you can keep for updates, or open the existing checkout.

   ```powershell
   git clone https://github.com/jalnaga/PyJal3DSMCP.git
   Set-Location PyJal3DSMCP
   ```

3. Run the command for your app. The examples skip the optional agent skill so the install runs without a skill-selection prompt. Windows may ask you to approve writing the Autodesk application package.

   | Client | PowerShell command |
   | --- | --- |
   | Claude Desktop | `uv run --python 3.12 pyjal3dsmcp-install --client claude-desktop --skip-skill` |
   | Codex | `uv run --python 3.12 pyjal3dsmcp-install --client codex --skip-skill` |
   | ChatGPT Desktop (local Work) | `uv run --python 3.12 pyjal3dsmcp-install --client chatgpt-desktop --skip-skill` |

   To configure Claude and both OpenAI clients in one run, pass `--client claude-desktop --client codex`. Codex and ChatGPT Desktop share one entry, even on a PC without the Codex CLI. Omitting `--client` also enables discovery of Claude Code, Gemini CLI, Cursor and OpenCode.

   The installer uses `uv tool install` and writes the absolute path to `pyjal3dsmcp.exe` into the client settings. Check that the command exits successfully. If it reports a locked executable, quit the app using the old server and retry from PowerShell.

## Verify from your app

Start Max and open a new conversation after reloading the client. The server name is **`pyjal3dsmcp`**.

| Client | Where to check |
| --- | --- |
| Claude Desktop | Fully quit and reopen Claude. Open **Settings → Developer → Edit Config** to see the registered command. The installer handles both classic and Microsoft Store config locations. [Claude Desktop MCP setup](https://modelcontextprotocol.io/docs/develop/connect-local-servers) |
| Codex | Open a new local session and type `/mcp`. If the CLI is installed, `codex mcp get pyjal3dsmcp` also shows the registered command. |
| ChatGPT Desktop | Open **Settings → MCP servers**, enable the server if needed, select **Restart**, then type `/mcp` in a local Work conversation. [OpenAI desktop MCP setup](https://learn.chatgpt.com/docs/extend/mcp) |

> Use PyJal3DSMCP to list running 3ds Max instances. If there is one, read its scene overview and tell me the object count. If there are several, ask which one I want. Leave the scene as it is.

A successful `query_scene` call with `action="overview"` confirms the connection all the way to Max. If the server is listed but no Max instance appears, check the installer's output for your year's `mcp_bridge_YYYY.gup` and restart Max. If the server is missing, check that the app is using the config path printed by the installer. For Codex and ChatGPT this defaults to `%USERPROFILE%\.codex\config.toml` and follows `CODEX_HOME` when set.

## Updates and removal

Run these from the checkout. To update, use the same client flag as at install time, for example `uv run pyjal3dsmcp-update --client claude-desktop --skip-skill`. This pulls with `git pull --ff-only` and reinstalls. Add `--no-pull` to reinstall the current checkout.

Preview removal with `uv run pyjal3dsmcp-uninstall --dry-run`. To proceed, save your scene, close Max and clients using the server, and run `uv run pyjal3dsmcp-uninstall`. This removes the Max bridge. Any clients using the upstream `3dsmax-mcp` server also depend on that bridge. Client config files get a `.pyjal3dsmcp.bak` backup on their first edit.

## Using Python libraries inside Max

For ordinary scene work, use the dedicated Max tools. For a custom library, select the intended Max PID, run `library_attach`, then use `library_search` and `library_describe` to inspect the API before `library_execute`. Register personal libraries with `library_register` and approve import and execution per instance. The server uses the full tool profile. The installer sets `safe_mode = false` in a new Max configuration to permit Python execution and respects an existing setting.

Teams can put `pyjal3dsmcp-team.toml` in the installer's working directory to register shared libraries and trusted roots. `PYJAL_DLEL_TEAM_PROFILE` overrides that profile location. Libraries under a trusted root inherit import and execution approval. The [library agent guide (Korean)](docs/pyjal/DLEL-AGENT-GUIDE.md) is also available as the MCP resource `resource://pyjal3dsmcp/agent-guide`.

## License

[MIT License](LICENSE). The upstream dependency is distributed under its [own license](https://github.com/cl0nazepamm/3dsmax-mcp/blob/82f555275c4d030e0125898526c9a93e19532a1e/LICENSE).
