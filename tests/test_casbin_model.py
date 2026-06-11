"""验证 app/casbin/model.conf 的 RBAC 模型逻辑（不依赖 DB）。

用临时 policy 文件构造 Enforcer，覆盖：
  - 直接策略授权（sub == p.sub）
  - 角色继承（g(user, role) 之后 role 的策略生效）
  - 未授权返回 False
"""
from __future__ import annotations

from pathlib import Path

import casbin


REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = REPO_ROOT / "app" / "casbin" / "model.conf"


def _make_enforcer(tmp_path: Path, policy_lines: list[str]) -> casbin.Enforcer:
    policy_file = tmp_path / "policy.csv"
    policy_file.write_text("\n".join(policy_lines) + "\n", encoding="utf-8")
    return casbin.Enforcer(str(MODEL_PATH), str(policy_file))


def test_direct_policy_allows(tmp_path):
    enf = _make_enforcer(tmp_path, [
        "p, user-a, library:medical, read",
        "p, user-a, library:medical, insert",
    ])
    assert enf.enforce("user-a", "library:medical", "read")
    assert enf.enforce("user-a", "library:medical", "insert")
    assert not enf.enforce("user-a", "library:medical", "delete")


def test_unrelated_user_denied(tmp_path):
    enf = _make_enforcer(tmp_path, [
        "p, user-a, library:medical, read",
    ])
    assert not enf.enforce("user-b", "library:medical", "read")


def test_library_isolation(tmp_path):
    enf = _make_enforcer(tmp_path, [
        "p, user-a, library:medical, read",
    ])
    # 不应跨库
    assert not enf.enforce("user-a", "library:legal", "read")


def test_role_inheritance(tmp_path):
    enf = _make_enforcer(tmp_path, [
        "p, role-reader, library:medical, read",
        "g, user-a, role-reader",
    ])
    assert enf.enforce("user-a", "library:medical", "read")
    assert not enf.enforce("user-a", "library:medical", "delete")
