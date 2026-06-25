"""发布安全检查：只扫描 git 已跟踪文件，发现疑似泄漏/误跟踪即非零退出。

检查项：
  1. 真实后端 .env 被跟踪（根级 .env / .env.local / .env.bak*；.env.example 与前端 VITE 配置不算）。
  2. 常见密钥格式：私钥块、JWT、OpenAI/DashScope `sk-` Key、AWS AKID、长 Bearer Token。
     —— 只按**格式**匹配，不把普通单词 token/key/password 当密钥；`<占位符>` 跳过。
  3. 个人绝对路径：`/home/<user>/`、`/Users/<user>/`、`C:\\Users\\<user>`（边界锚定，避免 views/home 误报）。
  4. 误跟踪：samples/、node_modules/、.run_logs/、*.output、.pytest_tmp/。

输出只给 文件:行号 + 类型 + 脱敏片段（不完整打印疑似密钥）。无问题退出 0；有问题退出 1。
纯标准库、跨平台。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# 内容扫描豁免：本脚本与其单测天然包含密钥“格式”与测试样例，扫描会自我误报。
# 路径类检查仍覆盖它们；仅“内容正则”跳过这两份。
CONTENT_SCAN_SKIP = {
    "scripts/check_release_safety.py",
    "tests/test_check_release_safety.py",
}

SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}")),
    ("sk-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("aws-akid", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
]

# 个人主目录绝对路径：要求 /home|/Users 前是边界字符（避免 "views/home/" 之类相对路径误报）。
PATH_PATTERNS = [
    ("personal-path", re.compile(r"""(?:^|[\s"'`=(,>])(?:/home|/Users)/[A-Za-z0-9._-]+/""")),
    ("windows-path", re.compile(r"\b[A-Za-z]:[\\/]+Users[\\/]+[A-Za-z0-9._ -]+")),
]

# 占位符（.env.example 等）：命中则该行不算泄漏。
PLACEHOLDER = re.compile(r"<[^>]+>|your-[a-z-]*-key|xxxx+|change-me|placeholder", re.IGNORECASE)

ENV_TRACKED = re.compile(r"^\.env(?:\.local|\.bak.*)?$")  # 根级后端 .env，排除 .env.example / 前端 .env
BAD_TRACKED = re.compile(r"(?:^|/)node_modules/|^samples/|^\.run_logs/|\.output$|(?:^|/)\.pytest_tmp/")


@dataclass
class Finding:
    path: str
    line: int          # 0 = 路径类（无具体行）
    kind: str
    snippet: str       # 已脱敏


def _mask(text: str) -> str:
    """脱敏：保留前 4 字符，其余以 *** 代替；过短则全 ***。"""
    t = text.strip()
    return (t[:4] + "***") if len(t) > 4 else "***"


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return [ln for ln in out.stdout.splitlines() if ln]


def _default_read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None  # 二进制/读不了 → 跳过内容扫描


def scan_text(path: str, text: str) -> list[Finding]:
    """对单个文件内容做密钥/路径格式扫描（行级）。占位符行跳过。"""
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if PLACEHOLDER.search(line):
            continue
        for kind, pat in SECRET_PATTERNS:
            m = pat.search(line)
            if m:
                findings.append(Finding(path, lineno, kind, _mask(m.group(0))))
        for kind, pat in PATH_PATTERNS:
            m = pat.search(line)
            if m:
                findings.append(Finding(path, lineno, kind, _mask(m.group(0))))
    return findings


def scan_paths(files: list[str]) -> list[Finding]:
    """路径类检查：跟踪了真实 .env / 误跟踪目录。"""
    findings: list[Finding] = []
    for f in files:
        if ENV_TRACKED.search(f):
            findings.append(Finding(f, 0, "tracked-env", f))
        if BAD_TRACKED.search(f):
            findings.append(Finding(f, 0, "misplaced-tracked", f))
    return findings


def collect_findings(files: list[str], read_text=_default_read) -> list[Finding]:
    findings = scan_paths(files)
    for f in files:
        if f in CONTENT_SCAN_SKIP:
            continue
        text = read_text(f)
        if text is None:
            continue
        findings.extend(scan_text(f, text))
    return findings


def main() -> int:
    files = tracked_files()
    findings = collect_findings(files)
    if not findings:
        print(f"[OK] 发布安全检查通过（扫描 {len(files)} 个已跟踪文件，无问题）")
        return 0
    print(f"发布安全检查发现 {len(findings)} 个问题：")
    for fd in findings:
        loc = f"{fd.path}:{fd.line}" if fd.line else fd.path
        print(f"  [{fd.kind}] {loc}  → {fd.snippet}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
