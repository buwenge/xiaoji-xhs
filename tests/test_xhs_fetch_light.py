import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from xhs import XhsError, fetch_light

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "xhs_note_state.json"
REAL_STATE_JSON = FIXTURE_PATH.read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, url: str, text: str, status_code: int = 200):
        self.url = url
        self.text = text
        self.status_code = status_code


class ResolveAndFetchTests(unittest.TestCase):
    def test_success_returns_final_url_and_html(self):
        with patch.object(
            fetch_light.requests,
            "get",
            return_value=FakeResponse("https://www.xiaohongshu.com/discovery/item/abc?xsec_token=T", "<html>ok</html>"),
        ):
            final_url, html = fetch_light.resolve_and_fetch("https://xhslink.cn/o/abc")
        self.assertEqual(final_url, "https://www.xiaohongshu.com/discovery/item/abc?xsec_token=T")
        self.assertEqual(html, "<html>ok</html>")

    def test_network_error_raises_xhs_error(self):
        with patch.object(fetch_light.requests, "get", side_effect=requests.RequestException("boom")):
            with self.assertRaises(XhsError):
                fetch_light.resolve_and_fetch("https://xhslink.cn/o/abc")

    def test_retries_once_before_raising(self):
        calls = {"n": 0}

        def fake_get(*args, **kwargs):
            calls["n"] += 1
            raise requests.Timeout("timeout")

        with patch.object(fetch_light.requests, "get", side_effect=fake_get):
            with self.assertRaises(XhsError):
                fetch_light.resolve_and_fetch("https://xhslink.cn/o/abc")
        self.assertEqual(calls["n"], fetch_light.FETCH_RETRIES + 1)

    def test_redirect_to_login_raises_xhs_error(self):
        with patch.object(
            fetch_light.requests,
            "get",
            return_value=FakeResponse("https://www.xiaohongshu.com/login?redirect=x", "<html></html>"),
        ):
            with self.assertRaises(XhsError):
                fetch_light.resolve_and_fetch("https://xhslink.cn/o/abc")

    def test_redirect_to_sec_wall_raises_xhs_error(self):
        with patch.object(
            fetch_light.requests,
            "get",
            return_value=FakeResponse("https://www.xiaohongshu.com/404/sec_verify", "<html></html>"),
        ):
            with self.assertRaises(XhsError):
                fetch_light.resolve_and_fetch("https://xhslink.cn/o/abc")

    def test_4xx_status_raises_xhs_error(self):
        with patch.object(
            fetch_light.requests,
            "get",
            return_value=FakeResponse("https://www.xiaohongshu.com/explore/abc", "<html></html>", status_code=404),
        ):
            with self.assertRaises(XhsError):
                fetch_light.resolve_and_fetch("https://xhslink.cn/o/abc")


class ExtractXsecTokenTests(unittest.TestCase):
    def test_extracts_token(self):
        url = "https://www.xiaohongshu.com/discovery/item/abc?xsec_token=AbC123&xsec_source=pc"
        self.assertEqual(fetch_light.extract_xsec_token(url), "AbC123")

    def test_missing_token_returns_none(self):
        self.assertIsNone(fetch_light.extract_xsec_token("https://www.xiaohongshu.com/explore/abc"))


class ExtractInitialStateTests(unittest.TestCase):
    def test_extracts_real_fixture_state(self):
        html = f"<html><script>window.__INITIAL_STATE__={REAL_STATE_JSON}</script></html>"
        data = fetch_light.extract_initial_state(html)
        self.assertEqual(
            data["noteData"]["data"]["noteData"]["noteId"], "6aae7f7300000000290165a0"
        )

    def test_replaces_undefined_literal_with_null(self):
        html = '<html><script>window.__INITIAL_STATE__={"a": undefined, "b": 1}</script></html>'
        data = fetch_light.extract_initial_state(html)
        self.assertEqual(data, {"a": None, "b": 1})

    def test_trailing_semicolon_is_stripped(self):
        html = '<html><script>window.__INITIAL_STATE__={"a": 1};</script></html>'
        data = fetch_light.extract_initial_state(html)
        self.assertEqual(data, {"a": 1})

    def test_missing_state_raises_xhs_error(self):
        with self.assertRaises(XhsError):
            fetch_light.extract_initial_state("<html>登录才能看哦</html>")

    def test_malformed_json_raises_xhs_error(self):
        html = "<html><script>window.__INITIAL_STATE__={not valid json</script></html>"
        with self.assertRaises(XhsError):
            fetch_light.extract_initial_state(html)

    def test_non_dict_json_raises_xhs_error(self):
        html = "<html><script>window.__INITIAL_STATE__=[1,2,3]</script></html>"
        with self.assertRaises(XhsError):
            fetch_light.extract_initial_state(html)

    def test_balanced_but_invalid_json_raises_xhs_error(self):
        # 花括号配对成功（不再走"找 </script>"截断），但内容本身不是合法
        # JSON（单引号）——确认走的是 json.loads 失败这条分支，不是"找不
        # 到闭合括号"那条，两条分支都要有覆盖。
        html = "<html><script>window.__INITIAL_STATE__={'a': 1}</script></html>"
        with self.assertRaises(XhsError):
            fetch_light.extract_initial_state(html)

    def test_truncated_no_closing_brace_raises_xhs_error(self):
        html = "<html><script>window.__INITIAL_STATE__={\"a\": 1, \"b\": "
        with self.assertRaises(XhsError):
            fetch_light.extract_initial_state(html)

    def test_literal_undefined_word_inside_string_value_is_preserved(self):
        # 笔记正文/代码片段里出现字面"undefined"这个词（技术类笔记很常
        # 见，这仓库自己的 fixture 笔记就是讲部署的）不能被当成 JS 字面
        # 量误换成 null——只有紧跟 `:`/`,`/`[` 的裸 undefined 才该换。
        html = (
            '<html><script>window.__INITIAL_STATE__='
            '{"desc": "this variable is undefined in Python", "flag": undefined}'
            "</script></html>"
        )
        data = fetch_light.extract_initial_state(html)
        self.assertEqual(data["desc"], "this variable is undefined in Python")
        self.assertIsNone(data["flag"])

    def test_literal_script_close_tag_inside_string_value_does_not_truncate(self):
        # 正文里出现字面 "</script>" 这几个字符（同样是技术类笔记的常见
        # 场景）不该把 JSON 提前截断——花括号配对扫描，不是找 </script>。
        html = (
            '<html><script>window.__INITIAL_STATE__='
            '{"desc": "use </script> to close the tag", "ok": 1}'
            "</script></html>"
        )
        data = fetch_light.extract_initial_state(html)
        self.assertEqual(data["desc"], "use </script> to close the tag")
        self.assertEqual(data["ok"], 1)

    def test_escaped_quote_inside_string_value_does_not_break_brace_matching(self):
        html = (
            '<html><script>window.__INITIAL_STATE__='
            '{"desc": "she said \\"hi }\\" to me", "ok": 2}'
            "</script></html>"
        )
        data = fetch_light.extract_initial_state(html)
        self.assertEqual(data["desc"], 'she said "hi }" to me')
        self.assertEqual(data["ok"], 2)


if __name__ == "__main__":
    unittest.main()
