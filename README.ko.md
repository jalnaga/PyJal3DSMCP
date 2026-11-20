# PyJal3DSMCP

[English README](README.md)

Windows 작업용 PC에서 Claude Desktop, Codex 또는 ChatGPT 데스크톱을 Autodesk 3ds Max에 연결합니다. 아티스트는 대화로 장면을 살펴보고, 이름으로 오브젝트를 찾거나, 재질과 모디파이어를 다룰 수 있습니다. [3dsmax-mcp](https://github.com/cl0nazepamm/3dsmax-mcp)의 장면 도구에 로컬 Python 라이브러리를 찾아 Max 안에서 실행하는 도구 12개를 추가했습니다.

## 에이전트에게 설치 요청하기

3ds Max가 설치된 PC에서 PowerShell을 실행할 수 있는 에이전트에게 아래 문장을 보내세요. 사용할 앱에 따라 **Claude Desktop**을 **Codex** 또는 **ChatGPT Desktop**으로 바꾸면 됩니다.

> 이 Windows PC의 Claude Desktop에서 쓸 수 있게 https://github.com/jalnaga/PyJal3DSMCP 를 설치해줘. README를 따라 해당 앱에 서버를 등록해줘. Max를 닫아야 하면 장면을 먼저 저장할 수 있게 알려줘. 재시작 후에는 장면 개요를 읽어서 연결을 확인해줘.

Windows, **3ds Max 2023–2027**, 로컬 MCP를 지원하는 데스크톱 앱이 필요합니다. ChatGPT 데스크톱에서는 **Settings → MCP servers** 메뉴가 있는 로컬 Work 세션을 사용하세요. 앱이 같은 PC에서 서버를 실행하고 표준 입출력(stdio)으로 통신합니다. [OpenAI 데스크톱 MCP 설정](https://learn.chatgpt.com/docs/extend/mcp)

## 설치와 업데이트

Windows에 uv와 Git을 준비하고 공개 릴리스의 전체 40자리 커밋을 선택합니다. 격리 실행기는 자체 캐시를 준비합니다. 계획에서 브리지 교체나 실행기 잠금이 표시되면 장면을 저장하고 표시된 앱을 닫은 뒤 적용합니다.

~~~powershell
$sha = ((git ls-remote https://github.com/jalnaga/PyJal3DSMCP HEAD) -split '\s+')[0]
if ($sha -notmatch '^[a-f0-9]{40}$') { throw 'Cannot resolve release commit' }
$source = "git+https://github.com/jalnaga/PyJal3DSMCP@$sha"
uvx --isolated --from $source pyjal3dsmcp-install --status --json
uvx --isolated --from $source pyjal3dsmcp-install --client claude-desktop --dry-run --json
uvx --isolated --from $source pyjal3dsmcp-install --client claude-desktop --yes --json
~~~

Codex와 ChatGPT Desktop은 같은 TOML 항목을 사용합니다. `--client codex` 또는 `--client chatgpt-desktop`을 선택하며 여러 클라이언트는 옵션을 반복합니다. 전체 탐색은 `--all-clients`, CLI 전용은 `--all-clients --no-apps`입니다. 무인 실행은 기본적으로 skill을 건너뜁니다. 설치하려면 `--skill global` 또는 `--skill project`를 지정합니다. 관리자 쓰기가 필요하면 대상 경로를 확인하고 `--allow-elevation`으로 Windows 승인 창을 허용합니다.

~~~powershell
$plan = uvx --isolated --from $source pyjal3dsmcp-update --ref master --client claude-desktop --dry-run --json | ConvertFrom-Json
$plan
$reviewed = $plan.build.target.commit
uvx --isolated --from $source pyjal3dsmcp-update --ref $reviewed --track-ref master --client claude-desktop --yes --json
uvx --isolated --from $source pyjal3dsmcp-uninstall --dry-run --json
uvx --isolated --from $source pyjal3dsmcp-uninstall --yes --json
~~~

종료 코드 0은 완료, 1은 실행 실패, 2는 사전 검사 차단, 3은 후속 조치 필요입니다. 설치된 환경의 제거기가 반환한 `after_exit` 명령은 제거기가 종료된 후 실행하고 격리 실행기에서 상태를 다시 확인합니다. 일반 제거 후 개인 설정은 유지됩니다. 로컬 설정과 카탈로그를 함께 제거할 때만 `--purge`를 지정합니다.

## Git 없는 Windows 배포

릴리스에서 지정한 Python 버전과 Windows x64에서 ZIP의 SHA-256을 릴리스에 게시된 값과 비교합니다. 로컬의 지속 폴더에 압축을 풀고 그 위치에서 실행합니다. 업데이트는 새 묶음의 해시를 확인한 뒤 같은 계획·적용 절차를 따릅니다. 제거는 묶음의 격리 실행기를 사용합니다.

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

## 개인 및 팀 프로필

개인 폴더에 프로필을 만들거나 팀이 관리하는 기존 파일에 연결합니다. 환경 변수 `PYJAL_DLEL_TEAM_PROFILE`이 이 PC에 저장한 포인터보다 우선합니다. 유효 경로와 디스크 서명은 `show`에서 확인합니다.

~~~powershell
$file = "$HOME\MaxLibraries\personal.toml"
New-Item -ItemType Directory -Force "$HOME\MaxLibraries"
pyjal3dsmcp-team init $file --anchor "$HOME\MaxLibraries"
pyjal3dsmcp-team validate $file
pyjal3dsmcp-team set $file
pyjal3dsmcp-team show --json
pyjal3dsmcp-team edit --file $file --personal-file trust-root add sample --path "$HOME\MaxLibraries\sample"
~~~

신뢰 루트 변경은 하위 라이브러리의 import와 실행 권한에 영향을 줍니다. diff를 확인한 뒤 같은 편집 명령에 `--apply --expect-sha256 <계획 해시>`를 추가합니다. 팀 원본이면 `--personal-file` 대신 `--work-copy <사본 경로>`로 동일한 상대 트리의 작업 사본을 지정합니다. 팀 배포 담당자가 사본을 검토해 게시합니다. MCP 서버 재시작 뒤 `query_scene(action="overview")`로 연결을 확인하고 `library_attach(expected_pid=PID)`의 서명을 show와 비교합니다. 로컬 포인터 해제는 `pyjal3dsmcp-team clear`입니다.

첫 일반 `library_*` 호출과 매 `library_attach`는 신뢰 루트에서 발견한 표면의 ID와 실제 원본 경로가 기존 등록과 일치하면 표면 색인을 갱신합니다. 첫 성공한 attach의 `auto_migrated`에 승격된 ID가 한 번 표시됩니다. 변경 전 상태는 `library_diagnose_surfaces`로 확인할 수 있습니다. 이 진단은 Max 연결이나 카탈로그 쓰기 없이 등록 경로와 발견 경로를 비교합니다.

## 앱에서 연결 확인

Max를 시작하고 클라이언트를 다시 불러온 뒤 새 대화를 엽니다. 서버 이름은 **`pyjal3dsmcp`**입니다.

| 클라이언트 | 확인 방법 |
| --- | --- |
| Claude Desktop | 앱을 완전히 종료한 뒤 다시 엽니다. **Settings → Developer → Edit Config**에서 등록된 실행 명령을 확인할 수 있습니다. 설치기는 일반 설치판과 Microsoft Store판의 설정 위치를 모두 처리합니다. [Claude Desktop MCP 설정](https://modelcontextprotocol.io/docs/develop/connect-local-servers) |
| Codex | 새 로컬 세션에서 `/mcp`를 입력합니다. CLI가 있다면 `codex mcp get pyjal3dsmcp`로 등록한 명령도 확인할 수 있습니다. |
| ChatGPT 데스크톱 | **Settings → MCP servers**에서 필요한 경우 서버를 활성화하고 **Restart**를 선택합니다. 로컬 Work 대화에서 `/mcp`를 입력합니다. [OpenAI 데스크톱 MCP 설정](https://learn.chatgpt.com/docs/extend/mcp) |

> PyJal3DSMCP로 실행 중인 3ds Max 인스턴스를 찾아줘. 하나면 장면 개요를 읽어서 오브젝트 개수를 알려줘. 여러 개면 어느 인스턴스를 볼지 물어봐줘. 장면은 그대로 유지해줘.

`query_scene`에 `action="overview"`를 전달해 장면 개요가 돌아오면 앱에서 Max까지 연결된 것입니다. 서버가 보이는데 Max를 찾지 못하면 설치 출력에 사용 중인 연도의 `mcp_bridge_YYYY.gup`가 있는지 확인하고 Max를 다시 시작하세요. 서버가 보이지 않으면 앱이 설치기에 표시된 설정 경로를 사용하는지 확인하세요. Codex와 ChatGPT의 기본 경로는 `%USERPROFILE%\.codex\config.toml`이며, `CODEX_HOME`을 지정한 경우 그 폴더의 설정을 사용합니다.

## Max에서 Python 라이브러리 사용

일반 장면 작업에는 Max 전용 도구를 사용하세요. 개인 라이브러리를 쓸 때는 대상 Max PID를 선택해 `library_attach`로 연결하고, `library_search`와 `library_describe`로 API를 확인한 뒤 `library_execute`로 실행합니다. 개인 라이브러리는 `library_register`로 등록하며 인스턴스별로 import와 실행을 승인합니다. 서버는 항상 전체 도구 프로필을 사용합니다. 설치기는 Python 실행을 위해 새 Max 설정에 `safe_mode = false`를 기록하며 기존 설정값은 유지합니다.

팀에서는 설치기를 실행하는 폴더에 `pyjal3dsmcp-team.toml`을 두어 공유 라이브러리와 신뢰 루트를 지정할 수 있습니다. `PYJAL_DLEL_TEAM_PROFILE` 환경 변수가 프로필 위치보다 우선합니다. 신뢰 루트 아래 라이브러리는 import와 실행 승인을 상속합니다. [라이브러리 에이전트 가이드](docs/pyjal/DLEL-AGENT-GUIDE.md)는 MCP 리소스 `resource://pyjal3dsmcp/agent-guide`로도 읽을 수 있습니다.

## 라이선스

[MIT 라이선스](https://github.com/jalnaga/PyJal3DSMCP/blob/master/LICENSE). 원본 3dsmax-mcp에는 [해당 프로젝트의 라이선스](https://github.com/cl0nazepamm/3dsmax-mcp/blob/82f555275c4d030e0125898526c9a93e19532a1e/LICENSE)가 적용됩니다.
