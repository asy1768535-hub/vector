"""权限授予 / 撤销 / 查询的高层封装。被 admin 接口调用。"""
from __future__ import annotations

from typing import Iterable

from app.casbin.enforcer import get_enforcer

VALID_ACTIONS = ("read", "insert", "delete", "admin")


def _obj(library_slug: str) -> str:
    return f"library:{library_slug}"


def grant(user_id: str, library_slug: str, actions: Iterable[str]) -> list[tuple[str, str, str]]:
    """添加 (sub=user_id, obj=library:<slug>, act=action) 策略。返回新增的策略列表。"""
    enforcer = get_enforcer()
    added: list[tuple[str, str, str]] = []
    for action in actions:
        if action not in VALID_ACTIONS:
            raise ValueError(f"invalid action: {action}")
        if enforcer.add_policy(user_id, _obj(library_slug), action):
            added.append((user_id, _obj(library_slug), action))
    return added


def revoke(user_id: str, library_slug: str, actions: Iterable[str] | None = None) -> int:
    """撤销策略。actions 为 None 则撤销该用户对该库的所有策略。返回删除条数。"""
    enforcer = get_enforcer()
    obj = _obj(library_slug)
    if actions is None:
        return _revoke_all(enforcer, user_id, obj)
    removed = 0
    for action in actions:
        if enforcer.remove_policy(user_id, obj, action):
            removed += 1
    return removed


def _revoke_all(enforcer, user_id: str, obj: str) -> int:
    removed = 0
    for action in VALID_ACTIONS:
        if enforcer.remove_policy(user_id, obj, action):
            removed += 1
    return removed


def list_user_permissions(user_id: str) -> dict[str, list[str]]:
    """返回 {library_slug: [actions...]}。"""
    enforcer = get_enforcer()
    result: dict[str, list[str]] = {}
    for policy in enforcer.get_filtered_policy(0, user_id):
        if len(policy) < 3:
            continue
        _, obj, act = policy[0], policy[1], policy[2]
        if not obj.startswith("library:"):
            continue
        slug = obj[len("library:") :]
        result.setdefault(slug, []).append(act)
    return result


def list_library_grantees(library_slug: str) -> dict[str, list[str]]:
    """返回 {user_id: [actions...]}，便于 UI 「按库视图」展示。"""
    enforcer = get_enforcer()
    obj = _obj(library_slug)
    result: dict[str, list[str]] = {}
    for policy in enforcer.get_filtered_policy(1, obj):
        if len(policy) < 3:
            continue
        sub, _, act = policy[0], policy[1], policy[2]
        result.setdefault(sub, []).append(act)
    return result
