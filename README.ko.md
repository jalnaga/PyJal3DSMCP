# PyJal3DSMCP

[English README](README.md)

Windows 작업용 PC에서 Claude Desktop, Codex 또는 ChatGPT 데스크톱을 Autodesk 3ds Max에 연결합니다. 아티스트는 대화로 장면을 살펴보고, 이름으로 오브젝트를 찾거나, 재질과 모디파이어를 다룰 수 있습니다. [3dsmax-mcp](https://github.com/cl0nazepamm/3dsmax-mcp)의 장면 도구에 로컬 Python 라이브러리를 찾아 Max 안에서 실행하는 도구 11개를 추가했습니다.

## 에이전트에게 설치 요청하기

3ds Max가 설치된 PC에서 PowerShell을 실행할 수 있는 에이전트에게 아래 문장을 보내세요. 사용할 앱에 따라 **Claude Desktop**을 **Codex** 또는 **ChatGPT Desktop**으로 바꾸면 됩니다.

> 이 Windows PC의 Claude Desktop에서 쓸 수 있게 https://github.com/jalnaga/PyJal3DSMCP 를 설치해줘. README를 따라 해당 앱에 서버를 등록해줘. Max를 닫아야 하면 장면을 먼저 저장할 수 있게 알려줘. 재시작 후에는 장면 개요를 읽어서 연결을 확인해줘.

Windows, **3ds Max 2023–2027**, 로컬 MCP를 지원하는 데스크톱 앱이 필요합니다. ChatGPT 데스크톱에서는 **Settings → MCP servers** 메뉴가 있는 로컬 Work 세션을 사용하세요. 앱이 같은 PC에서 서버를 실행하고 표준 입출력(stdio)으로 통신합니다. [OpenAI 데스크톱 MCP 설정](https://learn.chatgpt.com/docs/extend/mcp)

## Windows 설치

1. Max와 클라이언트를 사용하는 Windows 계정으로 PowerShell을 엽니다. `git --version`과 `uv --version`을 확인하세요. 없는 도구에 해당하는 명령만 실행한 뒤 PowerShell을 다시 엽니다. uv가 프로젝트에 필요한 Python 3.12를 내려받을 수 있습니다. [uv 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)

   ```powershell
   winget install --id Git.Git -e
   winget install --id astral-sh.uv -e
   ```

2. 브리지를 처음 설치한다면 장면을 저장한 뒤 3ds Max를 닫습니다. 같은 버전의 브리지가 이미 설치돼 있다면 Max를 열어 둬도 됩니다. 업데이트할 때는 기존 서버 실행 파일을 사용 중인 클라이언트를 닫으세요. 업데이트 때 다시 쓸 폴더에 저장소를 복제합니다. 이미 복제했다면 해당 폴더로 이동하세요.

   ```powershell
   git clone https://github.com/jalnaga/PyJal3DSMCP.git
   Set-Location PyJal3DSMCP
   ```

3. 사용할 앱에 해당하는 명령을 실행합니다. 아래 명령은 선택 사항인 에이전트 스킬을 생략하므로 설치 중 스킬 위치를 묻지 않습니다. Autodesk 애플리케이션 패키지를 쓸 때 Windows에서 관리자 권한 승인을 요청할 수 있습니다.

   | 클라이언트 | PowerShell 명령 |
   | --- | --- |
   | Claude Desktop | `uv run --python 3.12 pyjal3dsmcp-install --client claude-desktop --skip-skill` |
   | Codex | `uv run --python 3.12 pyjal3dsmcp-install --client codex --skip-skill` |
   | ChatGPT 데스크톱(로컬 Work) | `uv run --python 3.12 pyjal3dsmcp-install --client chatgpt-desktop --skip-skill` |

   세 앱 모두에 등록하려면 한 명령에 `--client claude-desktop --client codex`를 지정하세요. Codex와 ChatGPT 데스크톱은 설정 항목 하나를 공유하며, Codex CLI 설치 없이도 등록됩니다. `--client`를 생략하면 Claude Code, Gemini CLI, Cursor, OpenCode도 찾아서 등록합니다.

   설치기는 `uv tool install`로 서버를 설치하고 `pyjal3dsmcp.exe`의 절대 경로를 앱 설정에 기록합니다. 명령이 정상 종료했는지 확인하세요. 실행 파일이 사용 중이라는 오류가 나오면 기존 서버를 쓰는 앱을 종료하고 PowerShell에서 다시 실행하세요.

## 앱에서 연결 확인

Max를 시작하고 클라이언트를 다시 불러온 뒤 새 대화를 엽니다. 서버 이름은 **`pyjal3dsmcp`**입니다.

| 클라이언트 | 확인 방법 |
| --- | --- |
| Claude Desktop | 앱을 완전히 종료한 뒤 다시 엽니다. **Settings → Developer → Edit Config**에서 등록된 실행 명령을 확인할 수 있습니다. 설치기는 일반 설치판과 Microsoft Store판의 설정 위치를 모두 처리합니다. [Claude Desktop MCP 설정](https://modelcontextprotocol.io/docs/develop/connect-local-servers) |
| Codex | 새 로컬 세션에서 `/mcp`를 입력합니다. CLI가 있다면 `codex mcp get pyjal3dsmcp`로 등록한 명령도 확인할 수 있습니다. |
| ChatGPT 데스크톱 | **Settings → MCP servers**에서 필요한 경우 서버를 활성화하고 **Restart**를 선택합니다. 로컬 Work 대화에서 `/mcp`를 입력합니다. [OpenAI 데스크톱 MCP 설정](https://learn.chatgpt.com/docs/extend/mcp) |

> PyJal3DSMCP로 실행 중인 3ds Max 인스턴스를 찾아줘. 하나면 장면 개요를 읽어서 오브젝트 개수를 알려줘. 여러 개면 어느 인스턴스를 볼지 물어봐줘. 장면은 그대로 유지해줘.

`query_scene`에 `action="overview"`를 전달해 장면 개요가 돌아오면 앱에서 Max까지 연결된 것입니다. 서버가 보이는데 Max를 찾지 못하면 설치 출력에 사용 중인 연도의 `mcp_bridge_YYYY.gup`가 있는지 확인하고 Max를 다시 시작하세요. 서버가 보이지 않으면 앱이 설치기에 표시된 설정 경로를 사용하는지 확인하세요. Codex와 ChatGPT의 기본 경로는 `%USERPROFILE%\.codex\config.toml`이며, `CODEX_HOME`을 지정한 경우 그 폴더의 설정을 사용합니다.

## 업데이트와 제거

복제한 폴더에서 실행하세요. 업데이트할 때는 최초 설치와 같은 클라이언트 옵션을 사용합니다. 예를 들어 `uv run pyjal3dsmcp-update --client claude-desktop --skip-skill`은 `git pull --ff-only`로 새 코드를 받은 뒤 다시 설치합니다. 현재 코드로 재설치하려면 `--no-pull`을 추가하세요.

`uv run pyjal3dsmcp-uninstall --dry-run`으로 제거할 목록을 확인합니다. 제거하려면 장면을 저장하고 Max와 서버를 사용하는 앱을 닫은 뒤 `uv run pyjal3dsmcp-uninstall`을 실행하세요. 이때 모든 클라이언트와 원본 `3dsmax-mcp` 서버가 함께 사용하는 Max 브리지도 제거됩니다. 설치기는 기존 앱 설정 파일을 처음 수정할 때 `.pyjal3dsmcp.bak`으로 백업합니다.

## Max에서 Python 라이브러리 사용

일반 장면 작업에는 Max 전용 도구를 사용하세요. 개인 라이브러리를 쓸 때는 대상 Max PID를 선택해 `library_attach`로 연결하고, `library_search`와 `library_describe`로 API를 확인한 뒤 `library_execute`로 실행합니다. 개인 라이브러리는 `library_register`로 등록하며 인스턴스별로 import와 실행을 승인합니다. 서버는 항상 전체 도구 프로필을 사용합니다. 설치기는 Python 실행을 위해 새 Max 설정에 `safe_mode = false`를 기록하며 기존 설정값은 유지합니다.

팀에서는 설치기를 실행하는 폴더에 `pyjal3dsmcp-team.toml`을 두어 공유 라이브러리와 신뢰 루트를 지정할 수 있습니다. `PYJAL_DLEL_TEAM_PROFILE` 환경 변수가 프로필 위치보다 우선합니다. 신뢰 루트 아래 라이브러리는 import와 실행 승인을 상속합니다. [라이브러리 에이전트 가이드](docs/pyjal/DLEL-AGENT-GUIDE.md)는 MCP 리소스 `resource://pyjal3dsmcp/agent-guide`로도 읽을 수 있습니다.

## 라이선스

[MIT 라이선스](LICENSE). 원본 3dsmax-mcp에는 [해당 프로젝트의 라이선스](https://github.com/cl0nazepamm/3dsmax-mcp/blob/82f555275c4d030e0125898526c9a93e19532a1e/LICENSE)가 적용됩니다.
