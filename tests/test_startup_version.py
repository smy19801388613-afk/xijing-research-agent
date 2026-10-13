from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from app import main
from app.config import ROOT, VERSION


def test_shell_and_running_version_are_not_cached(tmp_path, monkeypatch):
    (tmp_path / 'index.html').write_text('<html>current shell</html>', encoding='utf-8')
    static = next(r.app for r in main.app.routes if r.name == 'web')
    monkeypatch.setattr(static, 'directory', str(tmp_path))
    monkeypatch.setattr(static, 'all_directories', [str(tmp_path)])
    monkeypatch.setattr(main.connections, 'public_settings', lambda: {'version': VERSION})
    client = TestClient(main.app)
    for path in ('/', '/index.html', '/api/health', '/api/settings'):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
    assert client.get('/api/health').json()['version'] == VERSION


@pytest.mark.parametrize('running_version,success', [(VERSION, True), ('0.2.1', False)])
def test_launcher_reports_live_version_and_rejects_old_server(tmp_path, running_version, success):
    powershell = shutil.which('powershell.exe')
    if not powershell:
        pytest.skip('Windows launcher')
    project = tmp_path / 'research app'
    (project / 'scripts').mkdir(parents=True)
    (project / 'app').mkdir()
    (project / 'web' / 'dist').mkdir(parents=True)
    (project / 'app' / 'config.py').write_text(f'VERSION = "{VERSION}"\n', encoding='utf-8')
    (project / 'web' / 'dist' / 'index.html').write_text('current shell', encoding='utf-8')
    script = project / 'scripts' / 'start.ps1'
    shutil.copyfile(ROOT / 'scripts' / 'start.ps1', script)
    # Fake only the health probe. A running-version check must neither spawn nor stop a process.
    data_dir=str(project/'var').replace("'", "''")
    command = ("$env:RESEARCH_COMPETITION_MODE='0'; Remove-Item Env:RESEARCH_DATA_DIR -ErrorAction SilentlyContinue; "
               "function Invoke-RestMethod { [pscustomobject]@{service='Research Desk';competition=$false;data_directory='"+data_dir+"';version='" + running_version + "'} }; "
               "function Start-Process { throw 'unexpected process launch' }; "
               "function Stop-Process { throw 'unexpected process stop' }; "
               "& '" + str(script).replace("'", "''") + "' -NoBrowser")
    result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', command],
                            capture_output=True, timeout=15)
    output = result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace')
    if success:
        assert result.returncode == 0, output
        assert f'Research Desk v{VERSION} is running.' in output
        assert f'/?v={VERSION}&ui=' in output
        assert 'Project:' in output
    else:
        assert result.returncode != 0
        assert 'Port 8920 is running v0.2.1' in output
        assert 'is running.' not in output
