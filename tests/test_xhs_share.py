"""`xhs/share.py` 的回归测试：multipart 上传 + `/api/xiaoji/share` JSON
POST。跟 `tests/test_daemon_api.py`/`tests/test_home_phone.py` 一个路数，
打桩 `urllib.request.urlopen`（全进程只有一份，谁打桩都是同一份）；`share()`
经 `daemon_api.post_json` 走，额外打桩 `daemon_api.post_json` 本身即可，
不用重复测传输层（那是 `test_daemon_api.py` 的事）。`upload_files()`/
`share()` 的三类 daemon_api 异常翻译收进共用的 `share._call`（HTTPError
但响应体不是 JSON 时 `DaemonResponseError.http_status` 带着状态码，文案
仍是"发送失败，daemon 返回 HTTP N"，不会退化成 daemon_api 内部的通用
兜底文案——2026-09-22 code-review 指出并修复，见 daemon_api.py/xhs/share.py
模块注释）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import daemon_api
from xhs import XhsError, share


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload, ensure_ascii=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self):
        return self.body


class FakeHTTPError(daemon_api.urllib.error.HTTPError):
    def __init__(self, code, body: bytes):
        super().__init__("http://x", code, "err", {}, None)
        self._body = body

    def read(self):
        return self._body


class UploadFilesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.token_file = Path(self.tempdir.name) / "token"
        self.token_file.write_text("real-token")
        self.token_patcher = patch.object(daemon_api, "WEB_AUTH_TOKEN_FILE", self.token_file)
        self.token_patcher.start()
        self.addCleanup(self.token_patcher.stop)
        self.base_patcher = patch.object(daemon_api, "DAEMON_API_BASE", "http://api.test")
        self.base_patcher.start()
        self.addCleanup(self.base_patcher.stop)

        self.calls = []
        self.response = {"ok": True, "attachments": [{"id": "a1"}, {"id": "a2"}]}

        def fake_urlopen(request, *, timeout):
            self.calls.append(request)
            if isinstance(self.response, Exception):
                raise self.response
            return FakeResponse(self.response)

        self.urlopen_patcher = patch.object(daemon_api.urllib.request, "urlopen", side_effect=fake_urlopen)
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)

        self.img1 = Path(self.tempdir.name) / "a.jpg"
        self.img1.write_bytes(b"fake-jpeg-bytes")
        self.img2 = Path(self.tempdir.name) / "b.png"
        self.img2.write_bytes(b"fake-png-bytes")

    def test_empty_list_returns_empty_without_request(self):
        self.assertEqual(share.upload_files([]), [])
        self.assertEqual(self.calls, [])

    def test_uploads_files_as_multipart_and_returns_ids(self):
        ids = share.upload_files([self.img1, self.img2])
        self.assertEqual(ids, ["a1", "a2"])
        request = self.calls[-1]
        self.assertEqual(request.full_url, "http://api.test/api/upload")
        self.assertEqual(request.get_header("Cookie"), "xy_auth=real-token")
        content_type = request.get_header("Content-type")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        body = request.data
        self.assertIn(b'name="target"', body)
        self.assertIn(b"chat", body)
        self.assertIn(b'filename="a.jpg"', body)
        self.assertIn(b'filename="b.png"', body)
        self.assertIn(b"fake-jpeg-bytes", body)
        self.assertIn(b"fake-png-bytes", body)

    def test_missing_token_raises_xhs_error(self):
        self.token_file.unlink()
        with self.assertRaises(XhsError):
            share.upload_files([self.img1])
        self.assertEqual(self.calls, [])

    def test_not_ok_response_raises_xhs_error_with_server_message(self):
        self.response = {"ok": False, "error": "文件太大"}
        with self.assertRaisesRegex(XhsError, "文件太大"):
            share.upload_files([self.img1])

    def test_connection_error_raises_xhs_error(self):
        self.response = daemon_api.urllib.error.URLError("boom")
        with self.assertRaises(XhsError):
            share.upload_files([self.img1])

    def test_http_error_without_json_body_raises_with_status_code(self):
        # daemon_api.send_request 抛 DaemonResponseError(http_status=500)，
        # share._call 翻成带状态码的原版文案（见模块顶部注释）。
        self.response = FakeHTTPError(500, b"not json at all")
        with self.assertRaisesRegex(XhsError, "发送失败，daemon 返回 HTTP 500"):
            share.upload_files([self.img1])

    def test_ids_without_id_field_are_skipped(self):
        self.response = {"ok": True, "attachments": [{"id": "a1"}, {"name": "no-id-here"}]}
        self.assertEqual(share.upload_files([self.img1]), ["a1"])


class ShareTests(unittest.TestCase):
    def test_no_ids_and_no_link_raises_without_calling_post_json(self):
        with patch.object(daemon_api, "post_json") as mocked:
            with self.assertRaises(XhsError):
                share.share()
            mocked.assert_not_called()

    def test_ids_only_posts_expected_payload(self):
        with patch.object(daemon_api, "post_json", return_value={"ok": True, "count": 2}) as mocked:
            share.share(ids=["a1", "a2"], note="小红书：发原图 1,2")
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], share.SHARE_PATH)
        self.assertEqual(args[1], {
            "attachments": ["a1", "a2"], "link": None, "source": "xhs", "note": "小红书：发原图 1,2",
        })

    def test_link_only_posts_link_payload(self):
        link = {"url": "https://x", "title": "t", "text": "text"}
        with patch.object(daemon_api, "post_json", return_value={"ok": True, "count": 1}) as mocked:
            share.share(link=link, note="小红书：分享链接")
        args, _kwargs = mocked.call_args
        self.assertEqual(args[1]["attachments"], [])
        self.assertEqual(args[1]["link"], link)

    def test_not_ok_response_raises_xhs_error(self):
        with patch.object(daemon_api, "post_json", return_value={"ok": False, "error": "服务端拒绝"}):
            with self.assertRaisesRegex(XhsError, "服务端拒绝"):
                share.share(ids=["a1"])

    def test_daemon_auth_error_mapped_to_xhs_error(self):
        with patch.object(daemon_api, "post_json", side_effect=daemon_api.DaemonAuthError("x")):
            with self.assertRaises(XhsError):
                share.share(ids=["a1"])

    def test_daemon_connection_error_mapped_to_xhs_error(self):
        with patch.object(daemon_api, "post_json", side_effect=daemon_api.DaemonConnectionError("x")):
            with self.assertRaises(XhsError):
                share.share(ids=["a1"])

    def test_daemon_response_error_mapped_to_xhs_error(self):
        with patch.object(daemon_api, "post_json", side_effect=daemon_api.DaemonResponseError("x")):
            with self.assertRaises(XhsError):
                share.share(ids=["a1"])


if __name__ == "__main__":
    unittest.main()
