from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_feature_flag_defaults(monkeypatch) -> None:
    """Keep unit tests independent from deployment feature-flag values."""
    from app.config import Settings, settings

    for name, field in Settings.model_fields.items():
        default = field.default
        if isinstance(default, bool):
            monkeypatch.setenv(name.upper(), str(default).lower())
            monkeypatch.setattr(settings, name, default)
    # 提供 Worker 基线默认合法配置，保留负向测试中的 delenv 漏配阻断能力
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "1")

@pytest.fixture
def tmp_path(request) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)
    path = Path.cwd() / "pytest_tmp_files" / "tmp_path" / f"{safe_name}-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path.resolve()
