"""Execute the same recovery helper used by the research overview."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_research_overview_status_recovery():
    root=Path(__file__).resolve().parents[1]
    node=shutil.which('node')
    if not node or not (root/'web/node_modules/typescript').exists():
        pytest.skip('Frontend dependencies and Node are required for display regressions')
    result=subprocess.run([node,'--test','tests/run-status.test.mjs'],cwd=root/'web',
                          capture_output=True,text=True,encoding='utf-8',timeout=45)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
