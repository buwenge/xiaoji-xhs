"""`daemon_api.py`（S2 从 `home.py::phone_api` 抽出的通用骨架）的回归测试。

写法照抄 `tests/test_home_phone.py`（这个模块的抽取来源，那份测试被锁着
不能改）：打桩 `daemon_api.urllib.request.urlopen`，用一个假 response 对象
回放 JSON。`urllib.request` 全进程只有一份，谁打桩都是同一份——这也是
`home.py` 里那行"看起来没用"的 `import urllib.request` 必须留着的原因。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import daemon_api


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload, ensure_ascii=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class FakeHTTPError(daemon_api.urllib.error.HTTPError):
    def __init__(self, code, body: bytes | None):
        super().__init__("http://x", code, "err", {}, None)
        self._body = body if body is not None else b""

    def read(self):
        return self._body


class DaemonApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.token_file = Path(self.tempdir.name) / "token"
        self.token_file.write_text("real-token")
        self.token_patcher = patch.object(daemon_api, "WEB_AUTH_TOKEN_FILE", self.token_file)
        self.token_patcher.start()
        self.addCleanup(self.token_patcher.stop)


class ReadAuthTokenTests(DaemonApiTestCase):
    def test_returns_stripped_token(self):
        self.token_file.write_text("  padded-token  \n")
        self.assertEqual(daemon_api.read_auth_token(), "padded-token")

    def test_missing_file_raises_auth_error(self):
        missing = Path(self.tempdir.name) / "nope"
        with self.assertRaises(daemon_api.DaemonAuthError):
            daemon_api.read_auth_token(missing)

    def test_empty_file_raises_auth_error(self):
        self.token_file.write_text("   ")
        with self.assertRaises(daemon_api.DaemonAuthError):
            daemon_api.read_auth_token()

    def test_token_file_param_overrides_module_default(self):
        other = Path(self.tempdir.name) / "other-token"
        other.write_text("other-token-value")
        self.assertEqual(daemon_api.read_auth_token(other), "other-token-value")


class PostJsonTests(DaemonApiTestCase):
    def _urlopen(self, request, *, timeout):
        self.calls.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return FakeResponse(self.response)

    def setUp(self):
        super().setUp()
        self.calls = []
        self.response = {"ok": True}
        self.base = "http://api.test"
        self.urlopen_patcher = patch.object(
            daemon_api.urllib.request, "urlopen", side_effect=self._urlopen
        )
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)

    def test_success_builds_request_with_cookie_and_returns_body(self):
        data = daemon_api.post_json("/api/x", {"a": 1}, base=self.base)
        self.assertEqual(data, {"ok": True})
        request = self.calls[-1]
        self.assertEqual(request.full_url, "http://api.test/api/x")
        self.assertEqual(request.get_header("Cookie"), "xy_auth=real-token")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {"a": 1})

    def test_extra_headers_merge_without_dropping_defaults(self):
        daemon_api.post_json("/api/x", {}, base=self.base, headers={"X-Extra": "v"})
        request = self.calls[-1]
        self.assertEqual(request.get_header("X-extra"), "v")
        self.assertEqual(request.get_header("Cookie"), "xy_auth=real-token")

    def test_base_param_overrides_default_base(self):
        daemon_api.post_json("/api/x", {}, base="http://other.test")
        self.assertEqual(self.calls[-1].full_url, "http://other.test/api/x")

    def test_token_file_param_used_for_cookie(self):
        other = Path(self.tempdir.name) / "other-token"
        other.write_text("other-value")
        daemon_api.post_json("/api/x", {}, base=self.base, token_file=other)
        self.assertEqual(self.calls[-1].get_header("Cookie"), "xy_auth=other-value")

    def test_http_error_with_json_body_returns_parsed_body(self):
        self.response = FakeHTTPError(400, json.dumps({"ok": False, "error": "拒绝"}).encode())
        data = daemon_api.post_json("/api/x", {}, base=self.base)
        self.assertEqual(data, {"ok": False, "error": "拒绝"})

    def test_http_error_without_json_body_raises_response_error_with_status(self):
        # 2026-09-22 code-review 指出：这种情况原来退化成一个"看起来正常"
        # 的 dict 返回值，会绕过调用方（phone_api/xhs.share）自己的错误
        # 翻译层直接把通用文案泄漏出去——改成抛异常，带上状态码。
        self.response = FakeHTTPError(500, b"not json")
        with self.assertRaises(daemon_api.DaemonResponseError) as ctx:
            daemon_api.post_json("/api/x", {}, base=self.base)
        self.assertEqual(ctx.exception.http_status, 500)
        self.assertIn("500", str(ctx.exception))

    def test_connection_error_raises_daemon_connection_error(self):
        self.response = daemon_api.urllib.error.URLError("boom")
        with self.assertRaises(daemon_api.DaemonConnectionError):
            daemon_api.post_json("/api/x", {}, base=self.base)

    def test_timeout_raises_daemon_connection_error(self):
        self.response = TimeoutError("timed out")
        with self.assertRaises(daemon_api.DaemonConnectionError):
            daemon_api.post_json("/api/x", {}, base=self.base)

    def test_missing_auth_token_raises_before_any_request(self):
        self.token_file.unlink()
        with self.assertRaises(daemon_api.DaemonAuthError):
            daemon_api.post_json("/api/x", {}, base=self.base)
        self.assertEqual(self.calls, [])


class PostJsonBadJsonResponseTests(DaemonApiTestCase):
    def test_undecodable_success_body_raises_daemon_response_error(self):
        class BadResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self):
                return b"\xff\xfe not json at all"

        with patch.object(daemon_api.urllib.request, "urlopen", return_value=BadResponse()):
            with self.assertRaises(daemon_api.DaemonResponseError):
                daemon_api.post_json("/api/x", {}, base="http://api.test")


if __name__ == "__main__":
    unittest.main()
