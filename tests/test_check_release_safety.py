"""check_release_safety 单元测试（纯逻辑，不用 git/网络）。

注意：本文件在 CONTENT_SCAN_SKIP 中，可安全包含示例密钥字面量。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_release_safety",
    Path(__file__).resolve().parent.parent / "scripts" / "check_release_safety.py",
)
crs = importlib.util.module_from_spec(_SPEC)
sys.modules["check_release_safety"] = crs
_SPEC.loader.exec_module(crs)


def _kinds(findings):
    return {f.kind for f in findings}


# ---------- 路径类 ----------

def test_tracked_env_flagged():
    assert "tracked-env" in _kinds(crs.scan_paths([".env"]))
    assert "tracked-env" in _kinds(crs.scan_paths([".env.local"]))
    assert "tracked-env" in _kinds(crs.scan_paths([".env.bak2"]))


def test_env_example_and_frontend_env_not_flagged():
    # .env.example 与前端 VITE 的 .env 都不是后端真实 .env
    assert crs.scan_paths([".env.example"]) == []
    assert crs.scan_paths(["frontend/.env"]) == []
    assert crs.scan_paths(["frontend/.env.prod"]) == []


def test_misplaced_tracked_flagged():
    assert "misplaced-tracked" in _kinds(crs.scan_paths(["samples/a.pdf"]))
    assert "misplaced-tracked" in _kinds(crs.scan_paths(["node_modules/x/y.js"]))
    assert "misplaced-tracked" in _kinds(crs.scan_paths([".run_logs/api.log"]))
    assert "misplaced-tracked" in _kinds(crs.scan_paths(["foo/bar.output"]))
    assert crs.scan_paths(["app/api/documents.py"]) == []


# ---------- 密钥格式 ----------

def test_detects_openai_style_key():
    f = crs.scan_text("x.py", 'KEY = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"')
    assert "sk-key" in _kinds(f)
    # 脱敏：不完整打印
    assert all("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123" not in fd.snippet for fd in f)


def test_detects_jwt_private_key_aws_bearer():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJ"
    assert "jwt" in _kinds(crs.scan_text("a", jwt))
    assert "private-key" in _kinds(crs.scan_text("a", "-----BEGIN RSA PRIVATE KEY-----"))
    assert "aws-akid" in _kinds(crs.scan_text("a", "AKIAIOSFODNN7EXAMPLE"))
    assert "bearer-token" in _kinds(crs.scan_text("a", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"))


def test_plain_words_not_flagged():
    f = crs.scan_text("a", "this token is a key and a password field")
    assert f == []


def test_placeholder_line_skipped():
    assert crs.scan_text("a", "EMBEDDING_API_KEY=<your-dashscope-key>") == []
    assert crs.scan_text("a", "RERANK_API_KEY=          # 本地留空") == []


# ---------- 个人路径 ----------

def test_personal_paths_flagged():
    assert "personal-path" in _kinds(crs.scan_text("a", 'p = "/home/alice/project/x"'))
    assert "personal-path" in _kinds(crs.scan_text("a", 'p = "/Users/jane/repo/y"'))
    assert "windows-path" in _kinds(crs.scan_text("a", r'p = "C:\Users\Bob\Desktop\z"'))


def test_route_home_not_false_positive():
    # 相对路由路径 views/home/ 不应被当作 /home/ 个人目录
    assert crs.scan_text("a", 'home: () => import("@/views/home/index.vue")') == []


# ---------- collect_findings 集成（注入文件清单 + 读取器，不依赖 git） ----------

def test_collect_skips_self_but_path_checks_apply():
    files = ["scripts/check_release_safety.py", ".env", "app/clean.py", "samples/x.pdf"]
    contents = {
        "scripts/check_release_safety.py": 'sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123',  # 被内容豁免
        "app/clean.py": "print('ok')",
        "samples/x.pdf": "binary-ish",
        ".env": "DB_PASSWORD=whatever",
    }
    findings = crs.collect_findings(files, read_text=lambda p: contents.get(p))
    kinds = _kinds(findings)
    assert "tracked-env" in kinds            # .env 路径命中
    assert "misplaced-tracked" in kinds      # samples/ 路径命中
    # 自身脚本的 sk- 内容被豁免，不应产生 sk-key
    assert "sk-key" not in kinds


def test_collect_flags_secret_in_normal_file():
    files = ["app/leak.py"]
    findings = crs.collect_findings(
        files, read_text=lambda p: 'TOKEN = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"',
    )
    assert "sk-key" in _kinds(findings)


def test_clean_repo_no_findings():
    files = ["app/a.py", "docs/x.md", ".env.example"]
    contents = {"app/a.py": "x=1", "docs/x.md": "# hi", ".env.example": "KEY=<your-key>"}
    assert crs.collect_findings(files, read_text=lambda p: contents.get(p, "")) == []
