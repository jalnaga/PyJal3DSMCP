"""PyJal3DSMCP: Upstream ``3dsmax-mcp``를 의존성으로 가져와 DLEL 라이브러리 Tool을 더한 MCP 서버.

- ``upstream``: Upstream(Import 이름 ``maxmcp``) 접점. 저장소에서 ``maxmcp``를 import하는 유일한 모듈.
- ``server``: 진입점(``pyjal3dsmcp``). 프로필을 ``full``로 고정하고 Upstream 서버에 PyJal Tool을 등록한다.
- ``library_tools``: ``library_*`` 고정 Tool 12개, 에이전트 가이드 resource, 안내 prompt.
- ``installer``: 설치·업데이트·제거 wrapper(``pyjal3dsmcp-install``·``-update``·``-uninstall``).
- ``client_configs``: 앱 설정 파일(Claude Desktop·Cursor·OpenCode)의 ``pyjal3dsmcp`` 항목 처리.

이 패키지를 import하는 것만으로는 Upstream 서버를 불러오지 않는다.
"""
