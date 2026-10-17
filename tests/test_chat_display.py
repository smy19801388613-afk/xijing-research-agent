"""Run real frontend display and asynchronous attachment regression checks."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_chat_document_loading_and_tool_display():
    root=Path(__file__).resolve().parents[1]
    node=shutil.which('node')
    if not node or not (root/'web/node_modules/typescript').exists():
        pytest.skip('Frontend dependency installation and Node are required for the display regressions')
    result=subprocess.run([node,'--test','tests/chat-documents.test.mjs','tests/recommendation-display.test.mjs'],cwd=root/'web',
                          capture_output=True,text=True,encoding='utf-8',timeout=45)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
