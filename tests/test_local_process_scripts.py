import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_local_process_scripts_validate_expected_modules() -> None:
    start = Path("scripts/start_local.ps1").read_text(encoding="utf-8")
    stop = Path("scripts/stop_local.ps1").read_text(encoding="utf-8")
    status = Path("scripts/status_local.ps1").read_text(encoding="utf-8")

    for script in (start, stop, status):
        assert "Get-CimInstance Win32_Process" in script
        assert "identity mismatch" in script
        assert ".venv\\Scripts\\python.exe" in script

    assert 'Test-ProjectProcess $existingPid "app.main"' in start
    for label, module in (
        ("API", "app.main"),
        ("Import Worker", "app.workers.importer"),
        ("Embedder Worker", "app.workers.embedder"),
        ("Graph Extractor", "app.workers.graph_extractor"),
        ("Knowledge Artifact Worker", "app.workers.knowledge_artifacts"),
        ("Classification Worker", "app.workers.classifications"),
        ("Cleanup Worker", "app.workers.cleanup"),
    ):
        expected = f'"{label}" "{module}"'
        assert expected in start
        assert expected in stop
        assert expected in status


@pytest.mark.skipif(os.name != "nt", reason="PowerShell PID behavior is Windows-only")
def test_stop_script_does_not_kill_process_referenced_by_stale_pid(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    pid_dir = project / ".run_logs"
    scripts.mkdir(parents=True)
    pid_dir.mkdir()
    shutil.copy2("scripts/stop_local.ps1", scripts / "stop_local.ps1")
    (pid_dir / "importer.pid").write_text(str(os.getpid()), encoding="ascii")

    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(scripts / "stop_local.ps1"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "Import Worker : stale PID file ignored" in result.stdout
    assert not (pid_dir / "importer.pid").exists()
