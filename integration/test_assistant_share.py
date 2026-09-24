"""`assistant_share.py`（S2：`POST /api/xiaoji/share`）的回归测试。

HTTP 层用 `aiohttp.test_utils` 起真实 `web.Application`（写法照抄
`tests/test_chat_upload.py`），不启动 daemon、不连 3000/8765 端口。附件
id 直接塞进 `chat_upload._ATTACHMENTS`（跳过真实上传，`resolve_attachments`
内存优先命中），`history_store.HISTORY_FILE` 指到临时文件不碰生产
`chat_history.jsonl`。`ws_broadcast` 打桩验证广播的事件形状；
`log_and_broadcast` 让它真的跑（LOG_FILE 已由 conftest 全局改道），只验证
不炸、不额外断言日志内容。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import assistant_share
import chat_upload
import history_store


class AssistantShareHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="xiaoji-xhs-share-")
        root = pathlib.Path(self._tmp.name)
        self._history_file = root / "chat_history.jsonl"
        self._patches = [
            mock.patch.object(history_store, "HISTORY_FILE", self._history_file),
        ]
        for p in self._patches:
            p.start()
        chat_upload._ATTACHMENTS.clear()

        self._broadcasts = []

        async def fake_broadcast(event):
            self._broadcasts.append(event)

        self._broadcast_patch = mock.patch.object(assistant_share, "ws_broadcast", fake_broadcast)
        self._broadcast_patch.start()

        app = web.Application()
        app.router.add_post("/api/xiaoji/share", assistant_share.share_api)
        self._server = TestServer(app)
        self._client = TestClient(self._server)
        await self._client.start_server()

    async def asyncTearDown(self):
        await self._client.close()
        self._broadcast_patch.stop()
        for p in self._patches:
            p.stop()
        chat_upload._ATTACHMENTS.clear()
        self._tmp.cleanup()

    def _seed_attachment(self, aid: str, *, name="图.jpg", kind="image") -> dict:
        meta = {
            "id": aid, "kind": kind, "name": name, "size": 100,
            "url": f"/uploads/2026-09/{aid}.jpg", "thumb_url": f"/uploads/thumbs/2026-09/{aid}.jpg",
            "path": f"/tmp/{aid}.jpg",
        }
        chat_upload._ATTACHMENTS[aid] = {**meta, "_expire_at": time.time() + 3600}
        return meta

    def _last_history_entry(self) -> dict:
        lines = self._history_file.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    async def test_images_only_records_history_and_broadcasts(self):
        self._seed_attachment("aid1")
        self._seed_attachment("aid2")
        resp = await self._client.post("/api/xiaoji/share", json={
            "attachments": ["aid1", "aid2"], "link": None, "source": "xhs", "note": "小红书：发原图 1,2",
        })
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 2)

        entry = self._last_history_entry()
        self.assertEqual(entry["role"], "assistant")
        self.assertEqual(entry["text"], "")
        self.assertEqual(entry["sender"], "xiaoji")
        self.assertEqual(len(entry["attachments"]), 2)
        self.assertEqual(entry["attachments"][0]["kind"], "image")
        self.assertNotIn("id", entry["attachments"][0])  # history_attachments 裁剪掉内部字段

        self.assertEqual(len(self._broadcasts), 1)
        event = self._broadcasts[0]
        self.assertEqual(event["type"], "assistant_share")
        self.assertEqual(event["channel"], "xiaoji")
        self.assertEqual(event["sender"], "xiaoji")
        self.assertEqual(len(event["attachments"]), 2)

    async def test_link_only_records_link_attachment(self):
        link = {
            "url": "https://www.xiaohongshu.com/discovery/item/abc?xsec_token=T",
            "title": "标题",
            "text": "标题\nhttps://www.xiaohongshu.com/discovery/item/abc?xsec_token=T\n复制本条信息，打开【小红书】App查看精彩内容！",
        }
        resp = await self._client.post("/api/xiaoji/share", json={
            "attachments": [], "link": link, "source": "xhs", "note": "小红书：分享链接",
        })
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 1)

        entry = self._last_history_entry()
        att = entry["attachments"][0]
        self.assertEqual(att["kind"], "link")
        self.assertEqual(att["name"], "标题")
        self.assertEqual(att["url"], link["url"])
        self.assertEqual(att["text"], link["text"])

    async def test_both_images_and_link(self):
        self._seed_attachment("aid1")
        resp = await self._client.post("/api/xiaoji/share", json={
            "attachments": ["aid1"],
            "link": {"url": "https://x", "title": "t", "text": "text"},
            "source": "xhs", "note": "",
        })
        body = await resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 2)
        entry = self._last_history_entry()
        self.assertEqual(len(entry["attachments"]), 2)
        self.assertEqual(entry["attachments"][0]["kind"], "image")
        self.assertEqual(entry["attachments"][1]["kind"], "link")

    async def test_neither_attachments_nor_link_rejected(self):
        resp = await self._client.post("/api/xiaoji/share", json={
            "attachments": [], "link": None, "source": "xhs", "note": "",
        })
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertFalse(body["ok"])
        self.assertFalse(self._history_file.exists())
        self.assertEqual(self._broadcasts, [])

    async def test_unknown_attachment_ids_all_missing_rejected(self):
        resp = await self._client.post("/api/xiaoji/share", json={
            "attachments": ["ghost-id"], "link": None, "source": "xhs", "note": "",
        })
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertFalse(body["ok"])
        self.assertFalse(self._history_file.exists())

    async def test_link_as_non_dict_rejected_not_500(self):
        # 2026-09-22 code-review：非 dict 的 link（比如前端/CLI 出 bug 直接
        # 传了个字符串）之前会一路 AttributeError 到未处理的 500。
        resp = await self._client.post("/api/xiaoji/share", json={
            "attachments": [], "link": "https://x", "source": "xhs", "note": "",
        })
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertFalse(body["ok"])
        self.assertFalse(self._history_file.exists())

    async def test_attachments_as_non_list_rejected(self):
        resp = await self._client.post("/api/xiaoji/share", json={
            "attachments": "aid1", "link": None, "source": "xhs", "note": "",
        })
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertFalse(body["ok"])
        self.assertFalse(self._history_file.exists())

    async def test_malformed_json_body_rejected(self):
        resp = await self._client.post(
            "/api/xiaoji/share", data=b"not json", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
