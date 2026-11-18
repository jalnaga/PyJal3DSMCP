# PyJal3DSMCP 에이전트 사용 가이드

일반 장면 작업에는 Upstream 전용 도구를 먼저 사용합니다. 등록된 Python 라이브러리가 필요한 작업에서 DLEL 도구를 사용하세요. 모든 도구가 직접 노출되며 프로필은 `full`로 고정됩니다.

## 시작 순서

1. 사용자가 지정한 3ds Max 인스턴스를 확인하고 PID를 정합니다. 임의로 다른 인스턴스로 전환하지 않습니다.
2. `library_attach(expected_pid=PID)`로 연결 상태를 조사합니다. 한 번의 읽기 전용 요청으로 `sys.path`, `sys.modules`, 로드 파일 시각을 읽으며 import와 경로 변경은 하지 않습니다. 신뢰 루트 라이브러리를 재색인하고 에이전트 표면을 자동 등록합니다. 신뢰 루트·프로필 라이브러리만 이 인스턴스에서 자동 활성화합니다.
3. `library_search(query="agentapi <검색어>")`로 검색하고 `library_describe(api_id=...)`로 서명·docstring·소스·접근 경로·revision을 읽습니다. 적합한 에이전트 함수가 있으면 우선 사용합니다.
4. `library_execute`에 정확한 라이브러리 ID, PID, `expected_source_revision`, 호출을 구별할 `operation_id`, 짧은 Python 코드를 전달합니다. JSON으로 표현 가능한 반환값을 `result`에 대입합니다. `operation_id`는 추적용이며 중복 실행을 막지 않습니다.

`library_attach`, `library_scan`, `library_probe`, `library_execute`는 Python bridge를 사용하므로 `safe_mode=false`가 필요합니다. 설정은 사용자에게 확인하며 자동으로 바꾸지 않습니다. PID와 프로세스 시작 시각을 함께 확인하므로 Max를 다시 시작하면 새 인스턴스로 취급합니다.

## 도구

| 도구 | 용도 |
|---|---|
| `library_attach` | 인스턴스 상태 분류, 프로필 항목 자동 활성화 |
| `library_scan` | 대상 Max에서 라이브러리 후보 발견, 자동 등록 없음 |
| `library_register` | 로컬 소스 루트 또는 설치된 패키지 폴더를 정적 분석해 등록 |
| `library_refresh` | 원본 재색인, 일반 라이브러리는 변경 시 저장된 승인 초기화 |
| `library_unregister` | 등록 정보 삭제, 원본 파일 유지 |
| `library_list` | Catalog·신뢰·revision, PID 지정 시 Binding·Runtime 상태 |
| `library_search` | API 검색, 에이전트 표면 우선, 최대 50개 |
| `library_describe` | API의 정확한 서명과 전체 설명 |
| `library_set_enabled` | 인스턴스별 활성화·비활성화 및 명시적 승인 저장 |
| `library_probe` | `find_spec`으로 위치 확인 또는 `import`로 준비 |
| `library_execute` | 승인된 라이브러리를 사용하는 관리형 Python 실행 |

등록·검색·설명·목록·재색인·활성화·해제는 Max에 요청하지 않습니다. 등록은 대상 라이브러리를 import·설치·빌드·실행하지 않으며 원본을 쓰지 않습니다. 설치된 패키지는 `__init__.py`가 있는 그 패키지 폴더 하나만 등록하세요. 전체 `site-packages` 폴더와 단독 파일·native·ZIP·namespace는 등록 대상으로 지원하지 않습니다.

## 승인과 실행

개인 라이브러리는 사용자 요청으로 등록하고 `library_set_enabled(library_id=..., enabled=true, expected_pid=PID)`로 활성화합니다. 신뢰 루트 밖의 import·실행 승인은 사용자의 명시적 지시가 있을 때만 `approve_import=true`, `approve_execution=true`로 기록합니다. 활성화는 import하지 않으며 비활성화해도 Max에 로드된 모듈을 내리지 않습니다. 실행 중 비활성화하면 진행 중 호출은 끝까지 진행하고 새 호출은 거절합니다.

프로필의 신뢰 루트 아래 항목은 두 승인을 상속합니다(`trust_source="root:<id>"`). 그 밖의 명시적 승인은 `trust_source="explicit"`입니다. 신뢰 루트 라이브러리는 목록·검색·설명·attach 때 변경된 원본을 자동 재색인합니다. 다른 라이브러리는 업데이트 후 `library_refresh`를 호출하고 승인 초기화 시 사용자에게 다시 확인합니다. 프로필 `libraries`에 있다는 사실만으로 신뢰 루트 밖 코드가 승인되지는 않습니다.

`library_probe(mode="find_spec")`는 모듈 코드를 실행하지 않습니다. `mode="import"`는 최상위 코드의 부작용이 있으며 import 승인이 필요합니다. 일반 소스와 사전 조건 없는 에이전트 표면은 등록된 import root 하나를 Max `sys.path` 끝에 추가할 수 있습니다. 설치된 패키지의 공용 부모 폴더는 추가하지 않습니다. 프로필에 사전 조건이 선언된 표면은 경로를 추가하지 않으며 필요한 모듈이 로드되지 않았다면 사용자가 툴을 정상적으로 열어야 합니다.

`library_execute`는 한 인스턴스에서 한 번에 하나만 실행하며 Max 주 스레드에서 undo를 지원합니다. 사용자 의도를 확인하고 부작용 범위를 검토하세요. 실패·응답 유실 뒤 자동 재전송하지 않습니다. `REQUEST_OUTCOME_UNKNOWN`, `MAX_BUSY`, `BLOCKED_BY_DIALOG`이면 장면과 작업 결과를 먼저 확인합니다. 검색 결과와 예제는 실행 성공의 증거가 아닙니다.

## 상태와 오류

| 상태 또는 오류 | 대응 |
|---|---|
| `current` / `not_loaded` | 검색·설명 후 사용, 필요 시 probe로 준비 |
| `restart_required` / `RUNTIME_RESTART_REQUIRED` | 로드 revision 차이가 확인됨. 사용자에게 해당 Max 재시작 요청, reload 금지 |
| `unverified` / `LOADED_CODE_UNVERIFIED` | 파일 시각만으로 최신 여부 불명. 실행은 허용되지만 변경이 중요하면 Max 재시작 권장 |
| `conflict` / `LIBRARY_VERSION_CONFLICT` | 같은 모듈의 다른 경로 또는 활성 버전 충돌. 경로 강제 변경·reload 금지 |
| `precondition_unmet` / `LIBRARY_PRECONDITION_UNMET` | `precondition.message`를 보여주고 사용자가 툴을 연 뒤 다시 attach |
| `unavailable` / `LIBRARY_STALE` | 원본 위치·상태 확인, 원본 복원 후 재색인 |
| `TEAM_TRUST_ROOT_UNAVAILABLE` | 프로필의 루트를 읽을 수 없음. 복사본·공유 위치 복원 후 재호출 |
| `LIBRARY_DISABLED` / `LIBRARY_NOT_APPROVED` | 대상 인스턴스 활성화·사용자 승인 확인 |
| `SAFE_MODE` / `TARGET_MISMATCH` | 사용자에게 설정·대상 상태 확인, 자동 변경·대상 전환 금지 |
| `OPERATION_IN_PROGRESS` | 진행 중 관리형 호출이 끝난 뒤 확인 |
| `PYTHON_ERROR` | `error.details.agent_error`가 있으면 `{code, message, details}`를 먼저 읽고 traceback 확인 |

외부에서 로드된 코드의 파일이 Max 시작 뒤 변경됐거나 읽히지 않는다는 사실은 옛 코드가 로드됐다는 확증이 아닙니다. DLEL이 기억한 로드 revision과 Catalog revision이 다를 때만 재시작을 필수로 요구합니다. 실패한 요청은 bridge 오류를 보존하며 자동 재시도하지 않습니다.

이 가이드는 `resource://pyjal3dsmcp/agent-guide`로 읽을 수 있습니다. 각 도구의 docstring에서 정확한 인자와 오류 계약을 확인하세요.
