"""The consumer path, end to end: `asas new` → pip install from the real
pinned tags → the generated app boots.

This is the one thing the local-path CI jobs (per-package matrix, selfcheck,
reference-host) structurally cannot cover — they never install from a
`git+https://...@asas-<pkg>/vX.Y.Z` URL, so a bad tag, a broken subdirectory
pin, or scaffold output that doesn't survive a clean install would otherwise
only be found by a real consumer. Network use is opt-in (ASAS_NETWORK_TESTS=1,
set by the CI `cli` job) so the suite stays runnable offline.
"""

import os
import subprocess
import sys

import pytest

from asas_cli.cli import main

pytestmark = pytest.mark.skipif(
    not os.environ.get("ASAS_NETWORK_TESTS"),
    reason="installs from the live remote — set ASAS_NETWORK_TESTS=1 to run",
)

_BOOT_CHECK = (
    "import main\n"
    "from fastapi.testclient import TestClient\n"
    "with TestClient(main.app) as client:\n"
    "    assert client.get('/docs').status_code == 200\n"
)


def test_scaffolded_project_installs_and_boots(tmp_path):
    # ratelimit + validation: both table-less, so the boot sequence needs no
    # database beyond the sqlite default — the leanest real install there is.
    assert main(["new", "smoke", "--with", "ratelimit,validation", "--dir", str(tmp_path)]) == 0
    project = tmp_path / "smoke"

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    pip = venv / "bin" / "pip"
    python = venv / "bin" / "python"

    # Non-editable install straight from the generated pyproject: resolves the
    # asas packages from their pinned tags on the real remote. httpx on top so
    # the boot check can use fastapi.testclient.
    subprocess.run(
        [str(pip), "install", str(project), "httpx"],
        check=True, capture_output=True, text=True, timeout=600,
    )

    result = subprocess.run(
        [str(python), "-c", _BOOT_CHECK],
        cwd=project, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"generated app failed to boot:\n{result.stderr}"
