"""B9-2：本地预览工具（``tools/settings-ui-harness``）的分组资源路由。

它是个纯开发工具，不进生产镜像，但路由写错会让真实页面上的「关键词回复」与
「定时消息」永远拿不到数据、单条 DELETE 掉进 404——父代理在真实浏览器里实测到了。

这里用**真 HTTP** 打一个真跑起来的 harness 进程：只绑 loopback、只读合成夹具、
不读 ``.env``、不连 Telegram、不碰生产库。断言三件事：

1. 四类资源（``keyword-replies`` / ``scheduled-messages`` / ``rules`` / ``memories``）
   的集合 GET 都命中集合分支并返回列表；
2. 单条 DELETE 返回与已上线契约一致的三态信封，重复删同一个 id 幂等（``no_change``），
   从未存在的 id 是 404；
3. 删掉的行真的从后续 GET 里消失，且只影响本群本资源。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = _ROOT / "tools" / "settings-ui-harness" / "harness.py"

GROUP_ID = -1001234567890
OTHER_GROUP_ID = -1009876543210
KEYWORD_REPLY_ID = 20
SCHEDULED_MESSAGE_ID = 30
RULE_ID = 1
MEMORY_ID = 10

#: ``RESOURCE_TYPES`` 的 URL 段名 → 响应数组键（都是复数/连字符真实形状）。
COLLECTIONS = (
    ("keyword-replies", "keyword_replies"),
    ("scheduled-messages", "scheduled_messages"),
    ("rules", "rules"),
    ("memories", "memories"),
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _HarnessClient:
    """最小 HTTP 客户端：只用标准库，避免给测试引入额外依赖。"""

    def __init__(self, base: str) -> None:
        self.base = base

    def request(self, method: str, path: str, payload: dict | None = None):
        data = None
        headers: dict[str, str] = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
                return response.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            try:
                parsed = json.loads(body) if body else None
            except json.JSONDecodeError:  # pragma: no cover - 诊断用
                parsed = {"raw": body}
            return error.code, parsed

    def get(self, path: str):
        return self.request("GET", path)

    def delete(self, path: str):
        return self.request("DELETE", path, payload={})


class SettingsHarnessRouteTests(unittest.TestCase):
    """真子进程 + 真 HTTP，一个进程服务整个类，用 ``/harness/reset`` 复位。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not _HARNESS.is_file():  # pragma: no cover - 仓库形状回归
            raise AssertionError(f"缺少预览工具：{_HARNESS}")
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env.pop("BOT_TOKEN", None)
        cls.process = subprocess.Popen(
            [
                sys.executable,
                str(_HARNESS),
                "--port",
                str(cls.port),
                "--host",
                "127.0.0.1",
            ],
            cwd=str(_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        cls.client = _HarnessClient(cls.base)
        deadline = time.time() + 25
        last_error: Exception | None = None
        while time.time() < deadline:
            if cls.process.poll() is not None:
                break
            try:
                status, _body = cls.client.get("/api/v1/session")
                if status == 200:
                    return
            except Exception as exc:  # 还没起来
                last_error = exc
            time.sleep(0.2)
        output = ""
        if cls.process.poll() is not None and cls.process.stdout is not None:
            output = cls.process.stdout.read()
        cls.tearDownClass()
        raise AssertionError(
            f"预览工具没有在 25 秒内就绪（last_error={last_error!r}）\n{output}"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        process = getattr(cls, "process", None)
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
                process.kill()
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        cls.process = None

    def setUp(self) -> None:
        status, body = self.client.request("POST", "/harness/reset", payload={})
        self.assertEqual(status, 200, f"复位预览夹具失败：{body!r}")

    def collection(self, group_id: int, segment: str, key: str) -> list:
        """读一个集合 GET，并把「200 + 真有这个数组」断言清楚。

        直接索引响应体会在旧实现上抛 ``KeyError``（404 的 error 文档里没有该键），
        那样报的是测试自己的形状问题，不是业务断言。这里先断言再取值。
        """

        status, body = self.client.get(f"/api/v1/groups/{group_id}/{segment}")
        self.assertEqual(status, 200, f"{segment} 的集合 GET 失败：{body!r}")
        self.assertIsInstance(body, dict)
        self.assertIn(key, body, f"{segment} 的集合 GET 必须返回 {key} 数组：{body!r}")
        self.assertIsInstance(body[key], list)
        return body[key]

    # ---- 1. 集合 GET ----

    def test_every_resource_collection_get_returns_rows(self) -> None:
        for segment, key in COLLECTIONS:
            with self.subTest(resource=segment):
                self.collection(GROUP_ID, segment, key)

    def test_hyphenated_collections_are_not_read_as_single_records(self) -> None:
        """回归父代理实测的缺陷：``keyword-replies`` 被当成记录 id 直接 404。"""

        for segment, key in (
            ("keyword-replies", "keyword_replies"),
            ("scheduled-messages", "scheduled_messages"),
        ):
            with self.subTest(resource=segment):
                rows = self.collection(GROUP_ID, segment, key)
                self.assertTrue(rows, f"{segment} 夹具应当有行")

    def test_single_record_get_still_works(self) -> None:
        status, body = self.client.get(
            f"/api/v1/groups/{GROUP_ID}/keyword-replies/{KEYWORD_REPLY_ID}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], KEYWORD_REPLY_ID)

    # ---- 2. 单条 DELETE 三态 ----

    def test_single_delete_returns_the_three_state_envelope(self) -> None:
        status, body = self.client.delete(
            f"/api/v1/groups/{GROUP_ID}/keyword-replies/{KEYWORD_REPLY_ID}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {"ok": True, "deleted": True, "changed": True, "status": "deleted"},
        )

    def test_single_delete_is_idempotent(self) -> None:
        path = f"/api/v1/groups/{GROUP_ID}/scheduled-messages/{SCHEDULED_MESSAGE_ID}"
        first = self.client.delete(path)
        second = self.client.delete(path)

        self.assertEqual(first[0], 200)
        self.assertEqual(first[1]["status"], "deleted")
        self.assertEqual(second[0], 200)
        self.assertEqual(
            second[1],
            {"ok": True, "deleted": False, "changed": False, "status": "no_change"},
        )

    def test_unknown_record_id_is_a_404(self) -> None:
        """真实 API 对「从未存在」的 id 回 404，而不是假装删掉了一行。"""

        status, body = self.client.delete(
            f"/api/v1/groups/{GROUP_ID}/keyword-replies/999999"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_collection_level_delete_is_a_no_change_envelope(self) -> None:
        """没有指定记录的 DELETE 不报 500，也不再回空对象。"""

        for segment in ("rules", "memories"):
            with self.subTest(resource=segment):
                status, body = self.client.delete(
                    f"/api/v1/groups/{GROUP_ID}/{segment}"
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    body,
                    {
                        "ok": True,
                        "deleted": False,
                        "changed": False,
                        "status": "no_change",
                    },
                )

    # ---- 3. 删除真的生效、且不越界 ----

    def test_deleted_row_disappears_from_later_collection_gets(self) -> None:
        for segment, key, record_id in (
            ("keyword-replies", "keyword_replies", KEYWORD_REPLY_ID),
            ("scheduled-messages", "scheduled_messages", SCHEDULED_MESSAGE_ID),
            ("rules", "rules", RULE_ID),
            ("memories", "memories", MEMORY_ID),
        ):
            with self.subTest(resource=segment):
                before = self.collection(GROUP_ID, segment, key)
                self.assertIn(record_id, [row["id"] for row in before])

                status, body = self.client.delete(
                    f"/api/v1/groups/{GROUP_ID}/{segment}/{record_id}"
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["status"], "deleted")

                after = self.collection(GROUP_ID, segment, key)
                self.assertNotIn(record_id, [row["id"] for row in after])
                self.assertEqual(len(after), len(before) - 1)

    def test_deleting_in_one_group_leaves_the_other_group_alone(self) -> None:
        status, _body = self.client.delete(
            f"/api/v1/groups/{GROUP_ID}/keyword-replies/{KEYWORD_REPLY_ID}"
        )
        self.assertEqual(status, 200)

        # 另一个群本来就没有行；关键是它没被写入、没报错、也没被删到别人头上。
        self.assertEqual(
            self.collection(OTHER_GROUP_ID, "keyword-replies", "keyword_replies"), []
        )

        # 复位后原群的行必须回来（复位走 State.__init__，删除标记也必须清掉）。
        self.client.request("POST", "/harness/reset", payload={})
        restored = self.collection(GROUP_ID, "keyword-replies", "keyword_replies")
        self.assertIn(KEYWORD_REPLY_ID, [row["id"] for row in restored])
        again = self.client.delete(
            f"/api/v1/groups/{GROUP_ID}/keyword-replies/{KEYWORD_REPLY_ID}"
        )
        self.assertEqual(
            again[1]["status"], "deleted", "复位后删除标记必须清空，否则会误报 no_change"
        )

    def test_unknown_resource_segment_is_still_a_404(self) -> None:
        """不在 ``RESOURCE_TYPES`` 里的段名不该被这次修复顺手"接住"。"""

        status, body = self.client.delete(
            f"/api/v1/groups/{GROUP_ID}/not-a-resource/1"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
