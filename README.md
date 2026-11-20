# PyJal3DSMCP

[한국어 README](README.ko.md)

Connect Claude Desktop, Codex, or ChatGPT Desktop to Autodesk 3ds Max on your Windows workstation. Ask your assistant to inspect a scene, find objects by name, or work with materials and modifiers. PyJal3DSMCP includes the tools from [3dsmax-mcp](https://github.com/cl0nazepamm/3dsmax-mcp) and adds 12 tools for discovering and running your own Python libraries inside Max.

## Ask an agent to install it

Copy this into an agent that can run PowerShell on the PC where you use Max. Replace **Claude Desktop** with **Codex** or **ChatGPT Desktop** if that is the app you use.

> Install https://github.com/jalnaga/PyJal3DSMCP for Claude Desktop on this Windows PC. Follow the README and register the server for that app. Let me save my scene if Max needs to close. After restart, verify the connection by reading a scene overview.

You need Windows, **3ds Max 2023–2027**, and a desktop client with local MCP support. For ChatGPT Desktop, use a local Work session with **Settings → MCP servers**. The client launches the server locally over standard input/output (stdio). [OpenAI desktop MCP setup](https://learn.chatgpt.com/docs/extend/mcp)

## Installation and updates

On Windows, install uv and Git and choose the full 40-character commit of the public release. The isolated launcher prepares its own cache. Review the plan, save open scenes and close any apps listed as holding a lock before applying it.

~~~powershell
$sha = ((git ls-remote https://github.com/jalnaga/PyJal3DSMCP HEAD) -split '\s+')[0]
if ($sha -notmatch '^[a-f0-9]{40}$') { throw 'Cannot resolve release commit' }
$source = "git+https://github.com/jalnaga/PyJal3DSMCP@$sha"
uvx --isolated --from $source pyjal3dsmcp-install --status --json
uvx --isolated --from $source pyjal3dsmcp-install --client claude-desktop --dry-run --json
uvx --isolated --from $source pyjal3dsmcp-install --client claude-desktop --yes --json
~~~

Repeat `--client` to select `codex` or `chatgpt-desktop`. Those two clients share one TOML entry. Use `--all-clients` to discover all supported clients, and `--all-clients --no-apps` for CLI registration only. Unattended runs skip skills by default. Select `--skill global` or `--skill project` explicitly. If the plan requires administrator access, review the destination and use `--allow-elevation` to permit the Windows approval dialog.

~~~powershell
$plan = uvx --isolated --from $source pyjal3dsmcp-update --ref master --client claude-desktop --dry-run --json | ConvertFrom-Json
$plan
$reviewed = $plan.build.target.commit
uvx --isolated --from $source pyjal3dsmcp-update --ref $reviewed --track-ref master --client claude-desktop --yes --json
uvx --isolated --from $source pyjal3dsmcp-uninstall --dry-run --json
uvx --isolated --from $source pyjal3dsmcp-uninstall --yes --json
~~~

Exit 0 means complete, 1 means an operation failed, 2 means preflight blocked the operation, and 3 means follow-up is required. An uninstaller running inside the installed tool may return an `after_exit` command. Execute it after the process exits, then check status from the isolated launcher. Normal removal preserves personal settings. Add `--purge` only to remove local settings and the catalog.

## Git-free release bundle

For Windows x64 with the Python version listed in the release, compare the ZIP SHA-256 with the digest in the release notes, extract it into a persistent local directory and run these commands there. For an update use the newly verified bundle and repeat the plan and apply. For removal use this bundle's isolated launcher.

~~~powershell
$wheel = (Get-Item .\pyjal3dsmcp-*.whl).FullName
$env:UV_PYTHON = '3.13'
$env:UV_OFFLINE = 'true'
$env:UV_NO_INDEX = 'true'
$env:UV_FIND_LINKS = [uri]::new((Get-Location).Path).AbsoluteUri
$env:UV_OVERRIDE = (Get-Item .\overrides.txt).FullName
uvx --isolated --from $wheel pyjal3dsmcp-install --status --json
uvx --isolated --from $wheel pyjal3dsmcp-install --wheel $wheel --client codex --dry-run --json
uvx --isolated --from $wheel pyjal3dsmcp-install --wheel $wheel --client codex --yes --json
uvx --isolated --from $wheel pyjal3dsmcp-update --wheel $wheel --client codex --dry-run --json
uvx --isolated --from $wheel pyjal3dsmcp-update --wheel $wheel --client codex --yes --json
uvx --isolated --from $wheel pyjal3dsmcp-uninstall --dry-run --json
uvx --isolated --from $wheel pyjal3dsmcp-uninstall --yes --json
uvx --isolated --from $wheel pyjal3dsmcp-install --status --json
~~~

## Personal and team profiles

Create an empty profile in your own directory or connect a team profile that your team already maintains. The environment variable `PYJAL_DLEL_TEAM_PROFILE` overrides this PC's saved pointer. Inspect the effective path and signature with `show`.

~~~powershell
$file = "$HOME\MaxLibraries\personal.toml"
New-Item -ItemType Directory -Force "$HOME\MaxLibraries"
pyjal3dsmcp-team init $file --anchor "$HOME\MaxLibraries"
pyjal3dsmcp-team validate $file
pyjal3dsmcp-team set $file
pyjal3dsmcp-team show --json
pyjal3dsmcp-team edit --file $file --personal-file trust-root add sample --path "$HOME\MaxLibraries\sample"
~~~

Trust-root changes grant permission to import and execute libraries beneath the root. Review the diff, then append `--apply --expect-sha256 <plan hash>` to the same edit command. For a team profile replace `--personal-file` with `--work-copy <path>` and use a copy with the same relative directory structure. The team maintainer reviews and publishes that copy. After a change, restart the MCP server, run `query_scene(action="overview")` and compare `library_attach(expected_pid=PID)` with the signature from `show`. To disconnect only the saved PC pointer, run `pyjal3dsmcp-team clear`.

The first ordinary `library_*` call and each `library_attach` promote old registrations whose ID and resolved source path match a discovered trusted surface. The next successful attach reports their IDs once in `auto_migrated`. Before migration, use `library_diagnose_surfaces` to compare registered and discovered paths without writing the catalog or connecting to Max.

## Verify from your app

Start Max and open a new conversation after reloading the client. The server name is **`pyjal3dsmcp`**.

| Client | Where to check |
| --- | --- |
| Claude Desktop | Fully quit and reopen Claude. Open **Settings → Developer → Edit Config** to see the registered command. The installer handles both classic and Microsoft Store config locations. [Claude Desktop MCP setup](https://modelcontextprotocol.io/docs/develop/connect-local-servers) |
| Codex | Open a new local session and type `/mcp`. If the CLI is installed, `codex mcp get pyjal3dsmcp` also shows the registered command. |
| ChatGPT Desktop | Open **Settings → MCP servers**, enable the server if needed, select **Restart**, then type `/mcp` in a local Work conversation. [OpenAI desktop MCP setup](https://learn.chatgpt.com/docs/extend/mcp) |

> Use PyJal3DSMCP to list running 3ds Max instances. If there is one, read its scene overview and tell me the object count. If there are several, ask which one I want. Leave the scene as it is.

A successful `query_scene` call with `action="overview"` confirms the connection all the way to Max. If the server is listed but no Max instance appears, check the installer's output for your year's `mcp_bridge_YYYY.gup` and restart Max. If the server is missing, check that the app is using the config path printed by the installer. For Codex and ChatGPT this defaults to `%USERPROFILE%\.codex\config.toml` and follows `CODEX_HOME` when set.

## Using Python libraries inside Max

For ordinary scene work, use the dedicated Max tools. For a custom library, select the intended Max PID, run `library_attach`, then use `library_search` and `library_describe` to inspect the API before `library_execute`. Register personal libraries with `library_register` and approve import and execution per instance. The server uses the full tool profile. The installer sets `safe_mode = false` in a new Max configuration to permit Python execution and respects an existing setting.

Teams can put `pyjal3dsmcp-team.toml` in the installer's working directory to register shared libraries and trusted roots. `PYJAL_DLEL_TEAM_PROFILE` overrides that profile location. Libraries under a trusted root inherit import and execution approval. The [library agent guide (Korean)](docs/pyjal/DLEL-AGENT-GUIDE.md) is also available as the MCP resource `resource://pyjal3dsmcp/agent-guide`.

## License

[MIT License](https://github.com/jalnaga/PyJal3DSMCP/blob/master/LICENSE). The upstream dependency is distributed under its [own license](https://github.com/cl0nazepamm/3dsmax-mcp/blob/82f555275c4d030e0125898526c9a93e19532a1e/LICENSE).
