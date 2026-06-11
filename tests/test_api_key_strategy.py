"""API Key 生成/哈希/校验单元测试（不需要 DB）。"""
from __future__ import annotations

from app.auth.api_key import generate_api_key, hash_api_key, verify_api_key


def test_generate_api_key_shape():
    plain, prefix, hashed = generate_api_key()
    assert plain.startswith("vk_")
    assert len(plain) > 30
    assert prefix == plain[:12]
    assert hashed != plain
    # bcrypt 哈希总以 $2a/$2b/$2y 开头
    assert hashed.startswith("$2")


def test_verify_api_key_roundtrip():
    plain, _, hashed = generate_api_key()
    assert verify_api_key(plain, hashed) is True
    assert verify_api_key("wrong-key", hashed) is False
    assert verify_api_key(plain, "not-a-hash") is False  # 不应抛异常


def test_hash_is_unique_per_call():
    plain, _, h1 = generate_api_key()
    h2 = hash_api_key(plain)
    # 同明文不同 salt → 哈希不同
    assert h1 != h2
    # 但两个哈希都能校验通过
    assert verify_api_key(plain, h1)
    assert verify_api_key(plain, h2)


def test_two_keys_are_distinct():
    p1, _, _ = generate_api_key()
    p2, _, _ = generate_api_key()
    assert p1 != p2
