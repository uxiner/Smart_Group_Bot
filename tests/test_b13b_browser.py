"""B13b/C4 第二子批：真实发布页 + 合成运营后端的浏览器回归。

与 `tests/test_b11_browser.py` 同构：起**真实**回环 harness（逐字发布
`bot/web/static/index.html`，只把 Telegram SDK 与后端换成合成实现），用真实
Chrome/Blink 点真按钮，驱动 `tools/settings-ui-harness/checks/b13b-operations.mjs`。
"""
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


class B13bOperationsBrowserTests(unittest.TestCase):
    def test_operations_policy_and_runs_real_browser(self):
        temporary = ROOT / ".b13b-tmp" / "temp"
        temporary.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="b13b-browser-", dir=temporary) as raw:
            directory = Path(raw)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = dict(
                os.environ,
                TMPDIR=raw,
                PYTHONDONTWRITEBYTECODE="1",
                B13B_ARTIFACTS=raw,
                HARNESS=f"http://127.0.0.1:{port}",
            )
            from test_sibling_delete_dom import _find_browser

            browser = _find_browser()
            self.assertIsNotNone(browser, "real Chrome/Blink required, never skip")
            env["OPERATIONS_BROWSER"] = browser
            with (directory / "harness.log").open("w") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(ROOT / "tools/settings-ui-harness/harness.py"),
                        "--port",
                        str(port),
                        "--real-page",
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                try:
                    for _ in range(100):
                        if process.poll() is not None:
                            self.fail(
                                "harness exited: "
                                + (directory / "harness.log").read_text()[-2000:]
                            )
                        try:
                            with urlopen(env["HARNESS"] + "/settings", timeout=1) as response:
                                self.assertEqual(
                                    response.read(),
                                    (ROOT / "bot/web/static/index.html").read_bytes(),
                                )
                                break
                        except OSError:
                            time.sleep(0.1)
                    completed = subprocess.run(
                        [
                            "node",
                            str(ROOT / "tools/settings-ui-harness/checks/b13b-operations.mjs"),
                        ],
                        cwd=ROOT,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=600,
                    )
                    result = directory / "browser-result.json"
                    # 详细证据只落在工作区忽略目录里，便于失败复盘。
                    evidence = ROOT / ".b13b-tmp"
                    evidence.mkdir(exist_ok=True)
                    (evidence / "browser-driver.log").write_text(
                        completed.stdout + "\n" + completed.stderr
                    )
                    if result.exists():
                        (evidence / "browser-result.json").write_text(result.read_text())
                    self.assertEqual(
                        completed.returncode, 0, completed.stdout + "\n" + completed.stderr
                    )
                    payload = json.loads(result.read_text())
                    self.assertEqual(payload["errors"], [])
                    self.assertEqual([x for x in payload["checks"] if not x["ok"]], [])
                    self.assertGreaterEqual(len(payload["checks"]), 90)
                    for width in (320, 390, 1280):
                        self.assertTrue(
                            any(x["label"].startswith(str(width)) for x in payload["checks"]),
                            f"missing {width}px viewport checks",
                        )
                    print(completed.stdout)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    unittest.main()
