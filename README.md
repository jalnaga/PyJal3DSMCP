# PyJal3DSMCP

3ds Max를 AI 클라이언트에서 사용하는 MCP 서버입니다. [3dsmax-mcp](https://github.com/cl0nazepamm/3dsmax-mcp)에 DLEL(Dynamic Library Extension Layer) 도구 11개를 추가하여 로컬 Python 라이브러리를 검색하고, 선택한 3ds Max 인스턴스에서 실행합니다. 일반적인 장면 작업에는 기존 전용 도구를 먼저 사용합니다.

## 설치

Windows, Python 3.12 이상, Git, [uv](https://docs.astral.sh/uv/)와 3ds Max가 필요합니다. PowerShell에서 저장소를 복제하고 설치합니다.

```powershell
git clone https://github.com/jalnaga/PyJal3DSMCP.git
Set-Location PyJal3DSMCP
uv run pyjal3dsmcp-install
```

설치 전에 실행 중인 3ds Max와 MCP 서버를 사용하는 클라이언트를 종료하세요. 설치 후 3ds Max와 클라이언트를 다시 시작합니다.

설치기는 서버를 `uv tool install`로 고정 설치하고 사용자 설정, Max 애플리케이션 패키지, skill을 준비합니다. `PATH`에 있는 Claude Code·Codex·Gemini CLI와 Claude Desktop, 기존 설정 파일이 있는 Cursor·OpenCode에 `pyjal3dsmcp` 이름으로 등록합니다. Claude Desktop은 설정이 없으면 생성합니다. 기존 앱 설정은 최초 변경 시 `.pyjal3dsmcp.bak`으로 백업하며 `--no-apps`로 앱 설정 변경을 건너뛸 수 있습니다.

도구 프로필은 항상 `full`입니다. 사용자 설정을 처음 만들 때 `safe_mode = false`로 설정합니다. 기존 설정의 `safe_mode`는 유지됩니다. Python bridge를 쓰는 라이브러리 도구는 `safe_mode=false`가 필요하며 에이전트가 임의로 설정을 바꾸지 않습니다. 기존 `3dsmax-mcp` 등록은 자동 제거하지 않으므로 중복 서버가 보이면 설치기가 안내하는 제거 명령을 확인하세요.

## 업데이트와 제거

복제한 저장소 폴더에서 실행합니다.

```powershell
uv run pyjal3dsmcp-update
uv run pyjal3dsmcp-uninstall --dry-run
uv run pyjal3dsmcp-uninstall
```

업데이트는 `git pull --ff-only` 후 새 코드로 다시 설치합니다. 로컬 변경 때문에 pull이 실패하면 변경을 정리하세요. `--no-pull`은 내려받기 없이 재설치합니다. 제거는 목록과 확인 절차를 거쳐 이 서버의 등록·설치물을 정리합니다.

## 라이브러리 사용

대상 3ds Max의 PID를 확인하고 `library_attach`를 호출합니다. `library_search` → `library_describe`로 정확한 API를 읽은 뒤 `library_execute`를 사용합니다. 개인 라이브러리는 사용자 요청에 따라 `library_register`로 등록하고 인스턴스별 활성화와 import·실행 승인을 받습니다.

[에이전트 사용 가이드](docs/pyjal/DLEL-AGENT-GUIDE.md)는 MCP resource `resource://pyjal3dsmcp/agent-guide`로도 제공됩니다. 라이브러리 원본은 정적 분석하며 자동 reload는 하지 않습니다. 로드된 코드와 원본 revision이 다르면 해당 Max를 다시 시작해야 합니다.

공유 프로필을 사용하는 환경에서는 `pyjal3dsmcp-team.toml`로 신뢰 루트와 라이브러리를 지정할 수 있습니다. 설치기를 이 파일이 있는 폴더에서 실행하면 로컬 설정에 프로필 위치를 기록합니다. `PYJAL_DLEL_TEAM_PROFILE` 환경 변수는 로컬 설정의 `team_profile`보다 우선합니다. 프로필 안 경로는 프로필 폴더 기준 상대 경로이며 그 폴더 밖으로 나갈 수 없습니다. 신뢰 루트의 라이브러리는 프로필 배포자의 import·실행 승인을 상속합니다. 신뢰하는 코드만 포함하세요.

## 라이선스

[MIT License](LICENSE). 의존하는 3dsmax-mcp의 라이선스는 해당 프로젝트를 확인하세요.
