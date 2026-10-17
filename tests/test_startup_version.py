from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient
from app import main
from app.config import ROOT, VERSION


def test_shell_and_running_version_are_not_cached(tmp_path, monkeypatch):
    (tmp_path / 'index.html').write_text('<html>current shell</html>', encoding='utf-8')
    static = next(r.app for r in main.app.routes if getattr(r, 'name', None) == 'web')
    monkeypatch.setattr(static, 'directory', str(tmp_path))
    monkeypatch.setattr(static, 'all_directories', [str(tmp_path)])
    monkeypatch.setattr(main.connections, 'public_settings', lambda: {'version': VERSION})
    client = TestClient(main.app)
    for path in ('/', '/index.html', '/api/health', '/api/settings'):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
    assert client.get('/api/health').json()['version'] == VERSION


@pytest.mark.parametrize('shell', ['powershell.exe', 'pwsh.exe'])
@pytest.mark.parametrize('mismatch', [None, 'version', 'workspace', 'mode'])
def test_launcher_decodes_utf8_workspace_and_checks_server_identity(tmp_path, shell, mismatch):
    powershell = shutil.which(shell)
    if not powershell:
        pytest.skip(f'{shell} unavailable')
    project = tmp_path / '金融 AI 研究'
    (project / 'scripts').mkdir(parents=True)
    (project / 'app').mkdir()
    (project / 'web' / 'dist').mkdir(parents=True)
    (project / 'app' / 'config.py').write_text(f'VERSION = "{VERSION}"\n', encoding='utf-8')
    (project / 'web' / 'dist' / 'index.html').write_text('current shell', encoding='utf-8')
    script = project / 'scripts' / 'start.ps1'
    payload = {'service': 'Research Desk', 'competition': mismatch == 'mode',
               'data_directory': str(project / ('other' if mismatch == 'workspace' else 'var')),
               'version': '0.2.1' if mismatch == 'version' else VERSION}
    requests = []

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            # Match FastAPI's charset-less JSON; PS5 Invoke-RestMethod misdecodes it.
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = (ROOT / 'scripts' / 'start.ps1').read_text(encoding='utf-8')
    source = source.replace("$serviceUrl = 'http://127.0.0.1:8920'",
                            f"$serviceUrl = 'http://127.0.0.1:{server.server_port}'")
    script.write_text(source, encoding='utf-8')
    # Read real HTTP bytes; an already running service must not spawn or stop anything.
    command = ("$env:RESEARCH_COMPETITION_MODE='0'; Remove-Item Env:RESEARCH_DATA_DIR -ErrorAction SilentlyContinue; "
               "function Start-Process { throw 'unexpected process launch' }; "
               "function Stop-Process { throw 'unexpected process stop' }; "
               "& '" + str(script).replace("'", "''") + "' -NoBrowser")
    try:
        result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', command],
                                capture_output=True, timeout=15)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert requests == ['/api/health']
    output = result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace')
    if mismatch is None:
        assert result.returncode == 0, output
        assert f'Xijing AI v{VERSION} is running.' in output
        assert f'/?v={VERSION}&ui=' in output
        assert 'Project:' in output
    else:
        assert result.returncode != 0
        expected = {'version': 'Port 8920 is running v0.2.1',
                    'workspace': 'Port 8920 is running another data workspace',
                    'mode': 'Port 8920 is running another mode'}
        assert expected[mismatch] in output
        assert 'is running.' not in output
