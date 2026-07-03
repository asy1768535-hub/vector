from __future__ import annotations

from pathlib import Path

from app.main import resolve_console_ui_dir


def test_console_ui_defaults_to_admin_ui_even_when_frontend_dist_exists(tmp_path, monkeypatch):
    root = tmp_path
    admin_ui = root / "admin-ui"
    frontend_dist = root / "frontend" / "dist"
    admin_ui.mkdir()
    frontend_dist.mkdir(parents=True)

    from app.config import settings
    monkeypatch.setattr(settings, "console_ui_dir", "admin-ui")

    assert resolve_console_ui_dir(root) == admin_ui


def test_console_ui_can_be_explicitly_pointed_at_frontend_dist(tmp_path, monkeypatch):
    root = tmp_path
    frontend_dist = root / "frontend" / "dist"
    frontend_dist.mkdir(parents=True)

    from app.config import settings
    monkeypatch.setattr(settings, "console_ui_dir", "frontend/dist")

    assert resolve_console_ui_dir(root) == frontend_dist


def test_console_ui_missing_directory_returns_none(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "console_ui_dir", "admin-ui")

    assert resolve_console_ui_dir(Path(tmp_path)) is None
