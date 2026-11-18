"""PyJal3DSMCP DLEL(Dynamic Library Extension Layer) 코어.

Max 외부의 MCP 서버 Python에서 동작하며 표준 라이브러리만 사용한다.
등록·인덱싱·검색 어느 경로에서도 대상 라이브러리를 Import·실행하지 않는다.
"""

from __future__ import annotations

from typing import Any

__all__ = ["LibraryError"]


class LibraryError(Exception):
    """DLEL 공개 오류.

    ``code``는 MCP Tool이 오류 분류로 그대로 전달하는 값이다.
    """

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}
