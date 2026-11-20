"""PyJal3DSMCP MCP 서버 진입점(console script ``pyjal3dsmcp``, ``python -m pyjal3dsmcp.server``).

Upstream ``3dsmax-mcp`` 서버를 그대로 쓰고 그 위에 ``library_*`` Tool 12개·에이전트 가이드 resource·안내 prompt만 더한다.
Upstream Tool·prompt·resource는 수정하지 않는다.

프로필은 항상 ``full``이다. 환경 변수(``MCP_TOOL_PROFILE``·``THREEDSMAX_MCP_TOOL_PROFILE``)나 사용자 INI의
``tool_profile``이 ``core``·``progressive``여도 따르지 않는다(core·progressive 프로필은 제공하지 않는다).
"""

from __future__ import annotations

from . import upstream

# 순서가 계약이다: Upstream 서버 모듈을 불러오기 전에 프로필을 고정한다.
upstream.pin_full_tool_profile()

# import하면 Upstream 서버를 불러오고 PyJal Tool·resource·prompt를 그 서버에 등록한다.
# 이 모듈을 거꾸로 import하지 않으므로 ``-m``으로 실행해도 등록이 두 번 일어나지 않는다.
from . import library_tools  # noqa: E402,F401


def main() -> None:
    """Upstream의 stdio 서버 실행을 호출한다. 클라이언트가 연결을 닫으면 Upstream이 프로세스를 끝낸다."""
    upstream.load_server().main()


if __name__ == "__main__":
    main()
