"""Upstream ``3dsmax-mcp``(Import 이름 ``maxmcp``) 접점.

이 저장소에서 ``maxmcp``를 import하는 **유일한** 모듈이다. ``pyjal3dsmcp``의 다른 모듈과 ``pyjal_extensions``는
Upstream 심볼이 필요하면 이 모듈의 함수를 거친다. 고정된 Upstream 의존성과의 접점을 이 모듈에서 제공한다.

기대는 Upstream 심볼:

- ``maxmcp.server``의 ``mcp``(FastMCP, ``mcp.tool``은 structured envelope wrapper)·``main``(stdio 실행)
- ``maxmcp.tools.execute.execute_python``, ``maxmcp.max_client.expect_pid``
- ``maxmcp.process_health.process_start_time``
- 환경 변수 ``MCP_TOOL_PROFILE``(사용자 INI와 별칭 변수보다 우선)
- ``maxmcp.installer``의 설치 단계 함수와 경로 상수

모든 import를 함수 안에 둔 이유는 두 가지다. Upstream 서버 모듈은 import되는 순간 프로필을 읽어 Tool을 등록하므로
진입점이 프로필을 고정한 뒤에 불러와야 하고, 설치 wrapper는 서버를 불러오지 않고 설치기만 써야 한다.
"""

from __future__ import annotations

import json
import os
from types import ModuleType
from typing import Any

TOOL_PROFILE_ENV = "MCP_TOOL_PROFILE"
FULL_TOOL_PROFILE = "full"


def pin_full_tool_profile() -> None:
    """이 프로세스의 Upstream Tool 프로필을 ``full``로 고정한다.

    Upstream은 서버 모듈 import 때뿐 아니라 Tool을 등록할 때와 prompt를 만들 때마다 환경 변수를 다시 읽는다.
    그래서 값을 임시로 바꿨다 되돌리지 않고 프로세스가 끝날 때까지 둔다. 사용자 INI는 읽지도 고치지도 않는다.
    """
    os.environ[TOOL_PROFILE_ENV] = FULL_TOOL_PROFILE


def load_server() -> ModuleType:
    """Upstream 서버 모듈(``mcp``·``main``)을 돌려준다. 처음 부를 때 Upstream Tool이 등록된다.

    ``pin_full_tool_profile()`` 뒤에 불러야 한다. 순서는 진입점 ``pyjal3dsmcp.server``가 정한다.
    """
    from maxmcp import server

    return server


def process_start_time(pid: int) -> int | None:
    """프로세스 생성 시각(Windows FILETIME 정수). 프로세스가 없거나 조회할 수 없으면 None."""
    from maxmcp.process_health import process_start_time as upstream_process_start_time

    return upstream_process_start_time(pid)


def run_python_in_max(code: str, expected_pid: int) -> Any:
    """DLEL이 만든 Python을 Upstream ``execute_python`` 경로로 대상 Max에 한 번 보내고 runner payload를 돌려준다.

    ``library_*`` Tool은 ``expected_pid``를 직접 선언하므로 Upstream wrapper의 자동 대상 보호가 붙지 않는다.
    여기서 같은 보호(``expect_pid``)를 걸어, 그 PID의 Max에 닿지 않는 요청은 보내기 전에 거절되게 한다.
    Upstream 예외는 감싸지 않고 그대로 올린다(재전송 없음).
    """
    from maxmcp.max_client import expect_pid
    from maxmcp.tools.execute import execute_python

    with expect_pid(expected_pid):
        raw = execute_python(code)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def load_installer() -> ModuleType:
    """Upstream 설치기 모듈을 돌려준다.

    설치기는 import 시점의 ``LOCALAPPDATA``·``ProgramData``로 설치 위치를 정한다. 설치 wrapper는 단계 함수를
    호출할 때마다 이 모듈의 속성을 읽는다.
    """
    from maxmcp import installer

    return installer
