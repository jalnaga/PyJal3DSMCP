"""DLEL 저장 위치 해석.

Catalog는 소스 코드가 아닌 설치 환경의 외부 등록 데이터이므로 사용자 프로필의
로컬 앱 데이터 아래에 둔다. 위치 규칙은 이 모듈 한 곳에서만 정한다.

팀 프로필의 파일 이름·환경 변수와 로컬 설정 위치도 여기서 정한다.
로컬 설정은 ``PYJAL_DLEL_CATALOG_DIR``와 무관하게 항상 ``%LOCALAPPDATA%\\PyJal3DSMCP\\dlel\\settings.json``이다.
"""

from __future__ import annotations

import os
from pathlib import Path

CATALOG_DIR_ENV = "PYJAL_DLEL_CATALOG_DIR"
# 팀 프로필 파일 경로(1순위). 비어 있지 않으면 로컬 설정의 기록보다 앞선다.
TEAM_PROFILE_ENV = "PYJAL_DLEL_TEAM_PROFILE"
# 배포 폴더에 두는 팀 프로필 파일 이름
TEAM_PROFILE_FILE_NAME = "pyjal3dsmcp-team.toml"
LOCAL_SETTINGS_FILE_NAME = "settings.json"
_APP_DIR_PARTS = ("PyJal3DSMCP", "dlel")


def _app_dir() -> Path:
    """``%LOCALAPPDATA%\\PyJal3DSMCP\\dlel``. ``LOCALAPPDATA``가 없으면 Upstream ``MaxClient``와 같이 ``~/AppData/Local``."""
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    return base.joinpath(*_APP_DIR_PARTS)


def default_catalog_dir() -> Path:
    """기본 Catalog 위치를 돌려준다. 디렉터리는 만들지 않는다.

    ``PYJAL_DLEL_CATALOG_DIR``가 비어 있지 않으면 그 경로, 없으면
    ``%LOCALAPPDATA%\\PyJal3DSMCP\\dlel\\catalog``다. ``LOCALAPPDATA``가 없으면 Upstream
    ``MaxClient``와 같이 ``~/AppData/Local``을 대신 쓴다.
    """
    explicit = os.environ.get(CATALOG_DIR_ENV, "").strip()
    if explicit:
        return Path(explicit)
    return _app_dir() / "catalog"


def local_settings_path() -> Path:
    """로컬 설정 파일(``team_profile``·개인 ``libraries``·``disabled_libraries``) 위치. 파일·디렉터리는 만들지 않는다."""
    return _app_dir() / LOCAL_SETTINGS_FILE_NAME


__all__ = [
    "CATALOG_DIR_ENV",
    "LOCAL_SETTINGS_FILE_NAME",
    "TEAM_PROFILE_ENV",
    "TEAM_PROFILE_FILE_NAME",
    "default_catalog_dir",
    "local_settings_path",
]
