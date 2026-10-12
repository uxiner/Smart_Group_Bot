"""Full shipped app/harness Chrome regression; missing browser is a failure."""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]

class B10PatchBrowserTests(unittest.TestCase):
    def test_real_pointer_patch_confirmation_three_viewports(self):
        with tempfile.TemporaryDirectory(prefix='b10-browser-') as raw:
            directory = Path(raw)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = dict(os.environ, TMPDIR=raw, PYTHONDONTWRITEBYTECODE='1',
                       B10_ARTIFACTS=raw, HARNESS=f'http://127.0.0.1:{port}')
            from test_sibling_delete_dom import _find_browser
            browser = _find_browser()
            self.assertIsNotNone(browser, 'real Chrome/Blink required, never skip')
            env['OPERATIONS_BROWSER'] = browser
            with (directory/'harness.log').open('w') as log:
                process = subprocess.Popen([sys.executable, str(ROOT/'tools/settings-ui-harness/harness.py'),
                                            '--port', str(port)], cwd=ROOT, env=env, stdout=log, stderr=log)
                try:
                    for _ in range(100):
                        if process.poll() is not None:
                            self.fail('harness exited: '+(directory/'harness.log').read_text()[-2000:])
                        try:
                            with urlopen(env['HARNESS']+'/settings', timeout=1):
                                break
                        except OSError:
                            time.sleep(.1)
                    completed = subprocess.run(['node', str(ROOT/'tools/settings-ui-harness/checks/b10-patch.mjs')],
                                               cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
                    result = directory/'browser-result.json'
                    # Retain detailed evidence outside fixture teardown, only in ignored workspace temp.
                    evidence = ROOT/'.b10-tmp'
                    evidence.mkdir(exist_ok=True)
                    (evidence/'browser-driver.log').write_text(completed.stdout+'\n'+completed.stderr)
                    if result.exists():
                        (evidence/'browser-result.json').write_text(result.read_text())
                    self.assertEqual(completed.returncode, 0, completed.stdout+'\n'+completed.stderr)
                    payload = json.loads(result.read_text())
                    self.assertEqual(payload['errors'], [])
                    self.assertEqual([x for x in payload['checks'] if not x['ok']], [])
                    self.assertGreaterEqual(len(payload['checks']), 170)
                    for width in (320,390,1280):
                        self.assertTrue(any(x['label'].startswith(str(width)) for x in payload['checks']))
                    print(completed.stdout)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
