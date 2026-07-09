from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path(request) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)
    path = Path.cwd() / "pytest_tmp_files" / "tmp_path" / f"{safe_name}-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path.resolve()
