"""Skills/Recipe: API 호출 예시와 검증 상태.

API Catalog가 기계적으로 수집한 사실이라면 Recipe는 사람이 쓴 호출 예시다.
새 Recipe는 항상 ``unverified``이며, 실제 Max 검증을 통한 ``verified`` 승격은
Runtime 단계의 범위라 여기서 제공하지 않는다.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, replace
from typing import Any

from pyjal_extensions import LibraryError


@dataclass
class Recipe:
    recipe_id: str
    api_id: str
    library_id: str
    title: str
    code: str
    notes: str
    status: str
    catalog_revision: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Recipe:
        return cls(**data)


def new_recipe(*, api_id: str, library_id: str, title: Any, code: Any, notes: Any, catalog_revision: str) -> Recipe:
    """현재 Catalog revision에 묶인 ``unverified`` Recipe를 만든다."""
    for name, value in (("title", title), ("code", code)):
        if not isinstance(value, str) or not value.strip():
            raise LibraryError("INVALID_ARGUMENT", f"Recipe {name}이(가) 비었습니다.", {name: value})
    if not isinstance(notes, str):
        raise LibraryError("INVALID_ARGUMENT", "Recipe notes는 문자열이어야 합니다.", {"notes": notes})
    return Recipe(
        recipe_id=f"rcp_{uuid.uuid4().hex}",
        api_id=api_id,
        library_id=library_id,
        title=title,
        code=code,
        notes=notes,
        status="unverified",
        catalog_revision=catalog_revision,
    )


def with_effective_status(recipe: Recipe, *, current_revision: str, api_exists: bool) -> Recipe:
    """저장된 Recipe에 현재 Catalog 기준 상태를 반영한 사본을 돌려준다.

    revision이 바뀌었거나 API가 사라졌으면 ``invalidated``다. 저장값을 고치지 않고
    매번 계산하므로 같은 Catalog를 연 다른 인스턴스도 같은 상태를 본다.
    """
    if recipe.catalog_revision != current_revision or not api_exists:
        return replace(recipe, status="invalidated")
    return replace(recipe)
