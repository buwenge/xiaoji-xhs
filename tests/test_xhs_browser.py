"""`xhs/browser.py` 测试：`page` 全部打桩（假对象），验证"UI 动作序列 +
从 state 取数"，不连真浏览器/9223（设计文档 S3）。看守进程连接
（`ensure`/`_alive_keeper`）的 os.kill/urlopen/subprocess 也全部打桩。
"""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from xhs import XhsError, browser, pacing, paths, selectors


def setUpModule():
    # 真人节奏（9/26）的停顿全经 `pacing._sleep`，测试一律不真睡。
    global _PACING_SLEEP_PATCHER
    _PACING_SLEEP_PATCHER = patch.object(pacing, "_sleep", lambda *a, **k: None)
    _PACING_SLEEP_PATCHER.start()


def tearDownModule():
    _PACING_SLEEP_PATCHER.stop()


class ScrollRecorder:
    """把 `pacing.scroll` 换成只记账：一次调用 = 一"轮"滚动（真实实现会拆成
    好几下 `mouse.wheel`，按 wheel 次数数轮数就不对了）。"""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, page, dy):
        self.calls.append(dy)


class FakeElement:
    def __init__(self, attrs: dict | None = None, children: dict | None = None):
        self.attrs = attrs or {}
        # S3.1 审查意见 1.1：`expand_comment` 在一个 `.parent-comment`
        # 元素内部再找"展开 N 条回复"按钮——`children` 是那个元素自己的
        # `query_selector` 结果表（跟 `FakePage.dom_elements` 同一个套路，
        # 只是作用域是这一个元素而不是整个页面）。
        self.children = children or {}
        self.screenshot_calls: list[str] = []
        self.fill_calls: list[str] = []
        self.click_calls = 0
        self.scroll_into_view_calls = 0
        self._attr_call_counts: dict[str, int] = {}

    def get_attribute(self, name):
        value = self.attrs.get(name)
        if callable(value):
            idx = self._attr_call_counts.get(name, 0)
            self._attr_call_counts[name] = idx + 1
            return value(idx)
        return value

    def screenshot(self, path=None, **kwargs):
        self.screenshot_calls.append(path)
        Path(path).write_bytes(b"fake-element-png")

    def fill(self, value):
        self.fill_calls.append(value)

    def click(self):
        self.click_calls += 1
        hook = getattr(self, "on_click", None)
        if hook is not None:
            hook()

    def scroll_into_view_if_needed(self):
        self.scroll_into_view_calls += 1

    def query_selector(self, selector):
        return self.children.get(selector)


class FakeMouse:
    def __init__(self):
        self.wheel_calls: list[tuple[int, int]] = []

    def wheel(self, dx, dy):
        self.wheel_calls.append((dx, dy))


class FakeKeyboard:
    def __init__(self):
        self.typed: list[str] = []
        self.presses: list[str] = []

    def type(self, text):
        self.typed.append(text)

    def press(self, key):
        self.presses.append(key)


class FakePage:
    def __init__(self):
        self.goto_calls: list[str] = []
        self.evaluate_calls: list[tuple[str, object]] = []
        self.screenshot_calls: list[str] = []
        self.query_selector_calls: list[str] = []
        self.query_selector_all_calls: list[str] = []
        self.hover_calls: list[str] = []
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.focused_id = None  # `document.activeElement.id`
        self.state_responses: dict[str, object] = {}
        self._state_call_counts: dict[str, int] = {}
        self.dom_elements: dict[str, object] = {}
        self.dom_element_lists: dict[str, list] = {}
        # S3.1 审查意见 1.4/1.5：`_ensure_on_note`/`load_more` 都要看
        # `page.url`——真 Playwright 是个只读属性，`goto` 之后自动更新；
        # 假 page 手动在 `goto` 里同步。默认空字符串（"哪儿都不在"）。
        self.url = ""
        # 统一时序日志（除了各自的 *_calls 列表外，额外记一份跨方法的
        # 先后顺序）——单测"_check_risk 是不是排在 goto 之后"这类顺序
        # bug（2026-09-22 code-review 抓到过一次）时用得上。
        self.call_log: list[tuple[str, str]] = []

    def goto(self, url, **kwargs):
        self.call_log.append(("goto", url))
        self.goto_calls.append(url)
        self.url = url

    def hover(self, selector, **kwargs):
        self.hover_calls.append(selector)

    def evaluate(self, script, arg=None):
        self.evaluate_calls.append((script, arg))
        if "activeElement" in script:
            return self.focused_id
        if script != selectors.READ_STATE_JS:
            return None
        resp = self.state_responses.get(arg)
        if callable(resp):
            idx = self._state_call_counts.get(arg, 0)
            self._state_call_counts[arg] = idx + 1
            return resp(idx)
        return resp

    def query_selector(self, selector):
        self.call_log.append(("query_selector", selector))
        self.query_selector_calls.append(selector)
        return self.dom_elements.get(selector)

    def query_selector_all(self, selector):
        self.query_selector_all_calls.append(selector)
        return self.dom_element_lists.get(selector, [])

    def screenshot(self, path=None, **kwargs):
        self.screenshot_calls.append(path)
        Path(path).write_bytes(b"fake-page-png")


def _fresh_xhs_dir():
    tempdir = tempfile.TemporaryDirectory()
    return tempdir, Path(tempdir.name)


FEED_ITEMS_RAW = [
    {"id": "n1", "modelType": "note", "xsecToken": "t1", "noteCard": {"displayTitle": "标题1", "type": "normal", "user": {"nickname": "u1"}, "interactInfo": {"likedCount": "10"}}},
    {"id": "n2", "modelType": "note", "xsecToken": "t2", "noteCard": {"displayTitle": "标题2", "type": "video", "user": {"nickname": "u2"}, "interactInfo": {"likedCount": "20"}}},
]


class ReadStateTests(unittest.TestCase):
    def test_parses_json_string(self):
        page = FakePage()
        page.state_responses["feed.feeds"] = json.dumps(FEED_ITEMS_RAW)
        result = browser.read_state(page, "feed.feeds")
        self.assertEqual(result, FEED_ITEMS_RAW)
        self.assertEqual(page.evaluate_calls[0], (selectors.READ_STATE_JS, "feed.feeds"))

    def test_passes_through_native_object_for_test_convenience(self):
        page = FakePage()
        page.state_responses["feed.feeds"] = FEED_ITEMS_RAW
        self.assertEqual(browser.read_state(page, "feed.feeds"), FEED_ITEMS_RAW)

    def test_none_stays_none(self):
        page = FakePage()
        self.assertIsNone(browser.read_state(page, "note.noteDetailMap.missing"))


class IsLoggedInTests(unittest.TestCase):
    def test_true_from_state(self):
        page = FakePage()
        page.state_responses["user.loggedIn"] = True
        self.assertTrue(browser.is_logged_in(page))

    def test_false_from_state(self):
        page = FakePage()
        page.state_responses["user.loggedIn"] = False
        self.assertFalse(browser.is_logged_in(page))

    def test_dom_fallback_when_state_unavailable(self):
        page = FakePage()
        page.state_responses["user.loggedIn"] = None
        page.dom_elements[selectors.DOM["login_modal"]] = None
        page.dom_elements[selectors.DOM["login_btn_sidebar"]] = None
        self.assertTrue(browser.is_logged_in(page))

    def test_dom_fallback_detects_login_modal(self):
        page = FakePage()
        page.state_responses["user.loggedIn"] = None
        page.dom_elements[selectors.DOM["login_modal"]] = FakeElement()
        self.assertFalse(browser.is_logged_in(page))


class CheckRiskTests(unittest.TestCase):
    def test_no_hints_found_is_silent(self):
        page = FakePage()
        browser._check_risk(page)  # 不抛异常即通过

    def test_raises_when_hint_found(self):
        page = FakePage()
        hint = selectors.RISK_PAGE_HINTS[0]
        page.dom_elements[hint] = FakeElement()
        with self.assertRaises(XhsError):
            browser._check_risk(page)


class KeeperConnectionTests(unittest.TestCase):
    def test_alive_keeper_none_when_json_missing(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            self.assertIsNone(browser._alive_keeper())

    def test_alive_keeper_none_when_pid_dead(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            paths.ensure_dirs()
            paths.keeper_json_path().write_text(
                json.dumps({"pid": 999999999, "cdp_port": 9223}), encoding="utf-8"
            )
            self.assertIsNone(browser._alive_keeper())

    def test_alive_keeper_none_when_cdp_unreachable(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            paths.ensure_dirs()
            paths.keeper_json_path().write_text(
                json.dumps({"pid": os.getpid(), "cdp_port": 1}), encoding="utf-8"
            )
            with patch.object(browser, "_cdp_alive", return_value=False):
                self.assertIsNone(browser._alive_keeper())

    def test_alive_keeper_returns_info_when_both_alive(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            paths.ensure_dirs()
            info = {"pid": os.getpid(), "cdp_port": 9223}
            paths.keeper_json_path().write_text(json.dumps(info), encoding="utf-8")
            # `_pid_alive` 现在还核验 `/proc/<pid>/cmdline` 含 "xhs.keeper"
            # （S3.1 审查意见 1.3）——pytest 自己的进程当然不含，这里打桩掉
            # 身份核验本身，单独测"两边都活着就返回 info"这条逻辑。
            with patch.object(browser, "_cdp_alive", return_value=True), patch.object(
                browser, "_pid_alive", return_value=True
            ):
                self.assertEqual(browser._alive_keeper(), info)

    def test_keeper_info_none_when_json_missing(self):
        # S4：`live_api` 复用的公开入口，跟 `_alive_keeper` 同一套
        # `_read_keeper_json`/`_pid_alive`，唯一区别是不连 CDP。
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            self.assertIsNone(browser.keeper_info())

    def test_keeper_info_none_when_pid_dead(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            paths.ensure_dirs()
            paths.keeper_json_path().write_text(
                json.dumps({"pid": 999999999, "cdp_port": 9223}), encoding="utf-8"
            )
            self.assertIsNone(browser.keeper_info())

    def test_keeper_info_none_when_json_malformed(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            paths.ensure_dirs()
            paths.keeper_json_path().write_text("not json", encoding="utf-8")
            self.assertIsNone(browser.keeper_info())

    def test_keeper_info_returns_info_without_checking_cdp(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            paths.ensure_dirs()
            info = {"pid": os.getpid(), "cdp_port": 1, "started_at": "t0", "last_used_at": "t1"}
            paths.keeper_json_path().write_text(json.dumps(info), encoding="utf-8")
            with patch.object(browser, "_pid_alive", return_value=True), patch.object(
                browser, "_cdp_alive"
            ) as cdp_mock:
                self.assertEqual(browser.keeper_info(), info)
                cdp_mock.assert_not_called()

    def test_pid_alive_false_for_dead_pid(self):
        self.assertFalse(browser._pid_alive(999999999))

    def test_pid_alive_false_for_live_pid_without_keeper_cmdline(self):
        # S3.1 审查意见 1.3：pid 活着但不是 xhs.keeper 进程（比如陈旧
        # keeper.json 里的 pid 被系统另派给了别的进程）——不能被判定成
        # "看守进程还活着"。用当前测试进程自己的 pid（真实存在但 cmdline
        # 是 pytest，不含 "xhs.keeper"）。
        self.assertFalse(browser._pid_alive(os.getpid()))

    def test_ensure_reuses_alive_keeper_without_spawning(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir), patch.object(
            browser, "_alive_keeper", return_value={"pid": 1, "cdp_port": 9223}
        ), patch.object(browser, "_spawn_keeper") as spawn_mock:
            info = browser.ensure()
            self.assertEqual(info["cdp_port"], 9223)
            spawn_mock.assert_not_called()

    def test_ensure_spawns_and_waits_until_alive(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        calls = {"n": 0}

        def fake_alive():
            calls["n"] += 1
            if calls["n"] < 3:
                return None
            return {"pid": 1, "cdp_port": 9223}

        with patch.object(paths, "XHS_DIR", xhs_dir), patch.object(
            browser, "_alive_keeper", side_effect=fake_alive
        ), patch.object(browser, "_spawn_keeper") as spawn_mock, patch.object(
            time, "sleep", return_value=None
        ):
            info = browser.ensure(timeout=5.0)
            spawn_mock.assert_called_once()
            self.assertEqual(info["cdp_port"], 9223)

    def test_ensure_raises_on_timeout(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir), patch.object(
            browser, "_alive_keeper", return_value=None
        ), patch.object(browser, "_spawn_keeper"), patch.object(
            time, "monotonic", side_effect=[0, 100]
        ):
            with self.assertRaises(XhsError):
                browser.ensure(timeout=1.0)

    def test_ensure_timeout_message_includes_keeper_log_tail(self):
        # 2026-09-22 code-review 指出：超时错误原来是裸的"启动超时"，看不
        # 出具体原因（比如 Xvfb/Chrome 缺失）；真正的原因写在 keeper.log
        # 里，超时消息里带上最后几行。
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            paths.ensure_dirs()
            paths.keeper_log_path().write_text(
                "一些早期日志\nFileNotFoundError: 'Xvfb'\n", encoding="utf-8"
            )
        with patch.object(paths, "XHS_DIR", xhs_dir), patch.object(
            browser, "_alive_keeper", return_value=None
        ), patch.object(browser, "_spawn_keeper"), patch.object(
            time, "monotonic", side_effect=[0, 100]
        ):
            with self.assertRaises(XhsError) as ctx:
                browser.ensure(timeout=1.0)
        self.assertIn("Xvfb", str(ctx.exception))

    def test_spawn_keeper_wraps_popen_oserror(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir), patch.object(
            browser.subprocess, "Popen", side_effect=OSError("拒绝访问")
        ):
            with self.assertRaises(XhsError):
                browser._spawn_keeper()

    def test_close_keeper_no_json_message(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            self.assertEqual(browser.close_keeper(), "现在没有开着的浏览器")

    def test_close_keeper_sends_sigterm_to_recorded_pid(self):
        # `_pid_alive` 现在真的去读 `/proc/4242/cmdline`（大概率不存在，
        # 大概率就算存在也不是 xhs.keeper）——身份核验本身在
        # `_pid_alive` 自己的测试里测过，这里打桩掉它，专测"身份核验通过
        # 之后才发 SIGTERM"这条逻辑（S3.1 审查意见 1.3）。
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            paths.ensure_dirs()
            paths.keeper_json_path().write_text(json.dumps({"pid": 4242}), encoding="utf-8")
            with patch.object(browser, "_pid_alive", return_value=True), patch("os.kill") as kill_mock:
                message = browser.close_keeper()
            kill_mock.assert_called_once_with(4242, browser.signal.SIGTERM)
            self.assertEqual(message, "已经放下了")

    def test_close_keeper_pid_reused_by_unrelated_process(self):
        # S3.1 审查意见 1.3：keeper.json 里的 pid 现在指向一个真实存在、但
        # 不是 xhs.keeper 的进程（用当前测试进程自己的 pid 模拟"陈旧 pid
        # 被复用"）——不该发 SIGTERM，该把 keeper.json 当过期数据删掉。
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            paths.ensure_dirs()
            paths.keeper_json_path().write_text(
                json.dumps({"pid": os.getpid()}), encoding="utf-8"
            )
            with patch("os.kill") as kill_mock:
                message = browser.close_keeper()
            kill_mock.assert_not_called()
            self.assertEqual(message, "浏览器好像已经关了")
            self.assertFalse(paths.keeper_json_path().exists())

    def test_close_keeper_already_dead_pid(self):
        tempdir, xhs_dir = _fresh_xhs_dir()
        self.addCleanup(tempdir.cleanup)
        with patch.object(paths, "XHS_DIR", xhs_dir):
            paths.ensure_dirs()
            paths.keeper_json_path().write_text(json.dumps({"pid": 999999999}), encoding="utf-8")
            self.assertEqual(browser.close_keeper(), "浏览器好像已经关了")


class ListActionTests(unittest.TestCase):
    def setUp(self):
        self.pause_patcher = patch.object(browser, "human_pause", lambda *a, **k: None)
        self.pause_patcher.start()
        self.addCleanup(self.pause_patcher.stop)

    def test_open_feed_goes_to_explore_and_parses(self):
        page = FakePage()
        page.state_responses["feed.feeds"] = FEED_ITEMS_RAW
        items = browser.open_feed(page)
        self.assertEqual(page.goto_calls, [selectors.EXPLORE_URL])
        self.assertEqual([i.title for i in items], ["标题1", "标题2"])

    def test_open_feed_checks_risk_after_navigating_not_before(self):
        # 2026-09-22 code-review 抓到：_check_risk 原来排在 page.goto 之
        # 前，检查的是上一页而不是刚导航到的这一页，撞验证码/风控页时永
        # 远查不到。goto 必须先于第一次 query_selector（_check_risk 用的
        # 就是 query_selector）。
        page = FakePage()
        page.state_responses["feed.feeds"] = FEED_ITEMS_RAW
        browser.open_feed(page)
        first_goto = next(i for i, c in enumerate(page.call_log) if c[0] == "goto")
        first_query = next(i for i, c in enumerate(page.call_log) if c[0] == "query_selector")
        self.assertLess(first_goto, first_query)

    def test_search_without_search_box_arrives_home_then_falls_back_to_url(self):
        # 真人节奏（9/26）：浏览器还停在空白页时先进首页；页面上找不到搜索
        # 框才退回直接跳搜索网址。
        page = FakePage()
        page.state_responses["search.feeds"] = FEED_ITEMS_RAW
        items = browser.search(page, "猫咪零食")
        self.assertEqual(page.goto_calls, [selectors.EXPLORE_URL, selectors.search_url("猫咪零食")])
        self.assertEqual(len(items), 2)

    def test_search_types_into_search_box_instead_of_jumping(self):
        page = FakePage()
        page.url = selectors.EXPLORE_URL
        box = FakeElement()
        page.dom_elements[selectors.DOM["search_input"]] = box

        def press(key):
            page.keyboard.presses.append(key)
            if key == "Enter":
                page.url = selectors.search_url("猫咪")
        page.keyboard.press = press
        page.focused_id = "search-input"
        page.state_responses["search.feeds"] = FEED_ITEMS_RAW
        items = browser.search(page, "猫咪")
        self.assertEqual(page.goto_calls, [])
        self.assertEqual(box.click_calls, 1)
        self.assertEqual(page.keyboard.typed, ["猫", "咪"])
        self.assertEqual(page.keyboard.presses, ["Control+A", "Backspace", "Enter"])
        self.assertEqual(len(items), 2)

    def test_search_box_not_focused_falls_back_without_select_all(self):
        # 9/26 匿名实测：有遮罩时点不进搜索框，再按 Ctrl+A 会选中整页。
        page = FakePage()
        page.url = selectors.EXPLORE_URL
        page.dom_elements[selectors.DOM["search_input"]] = FakeElement()
        page.state_responses["search.feeds"] = FEED_ITEMS_RAW
        browser.search(page, "猫咪")
        self.assertEqual(page.keyboard.presses, [])
        self.assertEqual(page.goto_calls, [selectors.search_url("猫咪")])

    def test_search_checks_risk_after_navigating_not_before(self):
        page = FakePage()
        page.state_responses["search.feeds"] = FEED_ITEMS_RAW
        browser.search(page, "猫咪零食")
        first_goto = next(i for i, c in enumerate(page.call_log) if c[0] == "goto")
        first_query = next(i for i, c in enumerate(page.call_log) if c[0] == "query_selector")
        self.assertLess(first_goto, first_query)

    def test_search_empty_and_not_logged_in_raises(self):
        page = FakePage()
        page.state_responses["search.feeds"] = []
        page.state_responses["user.loggedIn"] = False
        with self.assertRaises(XhsError):
            browser.search(page, "猫咪零食")

    def test_search_empty_but_logged_in_returns_empty_list(self):
        page = FakePage()
        page.state_responses["search.feeds"] = []
        page.state_responses["user.loggedIn"] = True
        self.assertEqual(browser.search(page, "猫咪零食"), [])

    def test_load_more_search_requires_login(self):
        page = FakePage()
        page.state_responses["user.loggedIn"] = False
        with self.assertRaises(XhsError):
            browser.load_more(page, "search", current_count=2)

    def test_load_more_feed_scrolls_and_returns_new_tail(self):
        page = FakePage()
        page.url = selectors.EXPLORE_URL  # S3.1 审查意见 1.5：feed 得先"在首页"
        page.state_responses["feed.feeds"] = FEED_ITEMS_RAW + [
            {"id": "n3", "modelType": "note", "xsecToken": "t3", "noteCard": {"displayTitle": "标题3", "type": "normal", "user": {"nickname": "u3"}, "interactInfo": {"likedCount": "5"}}}
        ]
        new_items = browser.load_more(page, "feed", current_count=2)
        self.assertEqual(len(new_items), 1)
        self.assertEqual(new_items[0].index, 3)
        self.assertEqual(new_items[0].title, "标题3")
        self.assertGreaterEqual(len(page.mouse.wheel_calls), 1)

    def test_load_more_checks_risk_before_and_after_scrolling(self):
        # 2026-09-22 code-review 抓到：load_more 原来完全不查风控，撞到
        # 拦截页会"硬滚"三次再默默回空列表——每滚一次都得查一次。风控检查
        # 排在"在不在列表页"检查之前，page.url 默认空字符串也不影响这条。
        page = FakePage()
        hint = selectors.RISK_PAGE_HINTS[0]
        page.dom_elements[hint] = FakeElement()
        recorder = ScrollRecorder()
        with patch.object(pacing, "scroll", recorder), self.assertRaises(XhsError):
            browser.load_more(page, "feed", current_count=0)
        self.assertIn(hint, page.query_selector_calls)
        # 撞到风控就该立刻停，不该还是滚了 3 次才发现。
        self.assertLessEqual(len(recorder.calls), 1)

    def test_load_more_gives_up_after_three_scrolls_with_no_growth(self):
        page = FakePage()
        page.url = selectors.EXPLORE_URL
        page.state_responses["feed.feeds"] = FEED_ITEMS_RAW  # 长度一直不变
        recorder = ScrollRecorder()
        with patch.object(pacing, "scroll", recorder):
            self.assertEqual(browser.load_more(page, "feed", current_count=2), [])
        self.assertEqual(len(recorder.calls), 3)

    def test_load_more_unknown_kind_raises(self):
        page = FakePage()
        with self.assertRaises(XhsError):
            browser.load_more(page, "bogus", current_count=0)

    def test_load_more_feed_raises_when_not_on_explore_page(self):
        # S3.1 审查意见 1.5：首页推荐每次刷新都是新一批，接不上原来的序号
        # ——不在首页时不该继续滚，直接报错让宝宝重新说一次"首页"。
        page = FakePage()  # page.url 默认空字符串，不在 /explore
        with self.assertRaises(XhsError):
            browser.load_more(page, "feed", current_count=0)
        self.assertEqual(page.mouse.wheel_calls, [])

    def test_load_more_search_navigates_when_not_on_results_page(self):
        # S3.1 审查意见 1.5：不在搜索结果页时先导航回去（带着原来的关键词）
        # 再滚，不是直接报错——跟 feed 不同，搜索结果页可以重新打开。
        page = FakePage()
        page.state_responses["user.loggedIn"] = True
        page.state_responses["search.feeds"] = FEED_ITEMS_RAW + [
            {"id": "n3", "modelType": "note", "xsecToken": "t3", "noteCard": {"displayTitle": "标题3", "type": "normal", "user": {"nickname": "u3"}, "interactInfo": {"likedCount": "5"}}}
        ]
        new_items = browser.load_more(page, "search", current_count=2, query="猫咪零食")
        self.assertIn(selectors.search_url("猫咪零食"), page.goto_calls)
        self.assertEqual(len(new_items), 1)

    def test_load_more_search_already_on_results_page_does_not_renavigate(self):
        page = FakePage()
        page.url = selectors.search_url("猫咪零食")
        page.state_responses["user.loggedIn"] = True
        page.state_responses["search.feeds"] = FEED_ITEMS_RAW + [
            {"id": "n3", "modelType": "note", "xsecToken": "t3", "noteCard": {"displayTitle": "标题3", "type": "normal", "user": {"nickname": "u3"}, "interactInfo": {"likedCount": "5"}}}
        ]
        browser.load_more(page, "search", current_count=2, query="猫咪零食")
        self.assertEqual(page.goto_calls, [])


DETAIL_ENTRY = {
    "note": {
        "noteId": "n1",
        "title": "标题",
        "desc": "正文",
        "user": {"nickname": "作者"},
        "interactInfo": {"likedCount": "1", "collectedCount": "2", "commentCount": "3", "shareCount": "4"},
        "time": 1700000000000,
        "imageList": [],
    },
    "comments": {"list": [], "hasMore": False},
}


class DetailActionTests(unittest.TestCase):
    def setUp(self):
        self.pause_patcher = patch.object(browser, "human_pause", lambda *a, **k: None)
        self.pause_patcher.start()
        self.addCleanup(self.pause_patcher.stop)

    def test_open_detail_navigates_to_correct_url(self):
        page = FakePage()
        page.state_responses["note.noteDetailMap.n1"] = DETAIL_ENTRY
        note, page_data = browser.open_detail(page, "n1", "TOKEN", "pc_search")
        self.assertEqual(page.goto_calls, [selectors.detail_url("n1", "TOKEN", "pc_search")])
        self.assertEqual(note.title, "标题")
        self.assertEqual(page_data.comments, [])

    def test_open_detail_checks_risk_after_navigating_not_before(self):
        # 9/26 起 goto 之前会先查"有没有弹层/列表上有没有这张卡片"，所以
        # 这里只看风控检查那几个选择器是不是都排在 goto 之后。
        page = FakePage()
        page.state_responses["note.noteDetailMap.n1"] = DETAIL_ENTRY
        browser.open_detail(page, "n1", "TOKEN")
        first_goto = next(i for i, c in enumerate(page.call_log) if c[0] == "goto")
        first_risk = next(
            i for i, c in enumerate(page.call_log)
            if c[0] == "query_selector" and c[1] in selectors.RISK_PAGE_HINTS
        )
        self.assertLess(first_goto, first_risk)

    def test_open_detail_clicks_card_on_list_page_instead_of_jumping(self):
        page = FakePage()
        page.url = selectors.EXPLORE_URL
        cover = FakeElement()
        cover.on_click = lambda: setattr(page, "url", selectors.detail_url("n1", "TOKEN"))
        page.dom_elements[selectors.note_card("n1")] = FakeElement(children={selectors.DOM["note_card_cover"]: cover})
        page.state_responses["note.noteDetailMap.n1"] = DETAIL_ENTRY
        note, _ = browser.open_detail(page, "n1", "TOKEN")
        self.assertEqual(page.goto_calls, [])
        self.assertEqual(cover.click_calls, 1)
        self.assertEqual(note.note_id, "n1")

    def test_open_detail_closes_open_modal_before_clicking_next_card(self):
        page = FakePage()
        page.url = selectors.detail_url("n0", "T0")
        close_btn = FakeElement()
        close_btn.on_click = lambda: page.dom_elements.pop(selectors.DOM["note_modal"], None)
        page.dom_elements[selectors.DOM["note_modal"]] = FakeElement()
        page.dom_elements[selectors.DOM["note_close"]] = close_btn
        cover = FakeElement()
        cover.on_click = lambda: setattr(page, "url", selectors.detail_url("n1", "TOKEN"))
        page.dom_elements[selectors.note_card("n1")] = FakeElement(children={selectors.DOM["note_card_cover"]: cover})
        page.state_responses["note.noteDetailMap.n1"] = DETAIL_ENTRY
        browser.open_detail(page, "n1", "TOKEN")
        self.assertEqual(close_btn.click_calls, 1)
        self.assertEqual(cover.click_calls, 1)
        self.assertEqual(page.goto_calls, [])

    def test_open_detail_falls_back_to_url_when_card_click_does_not_open(self):
        page = FakePage()
        page.url = selectors.EXPLORE_URL
        cover = FakeElement()  # 点了网址不变
        page.dom_elements[selectors.note_card("n1")] = FakeElement(children={selectors.DOM["note_card_cover"]: cover})
        page.state_responses["note.noteDetailMap.n1"] = DETAIL_ENTRY
        browser.open_detail(page, "n1", "TOKEN")
        self.assertEqual(cover.click_calls, 1)
        self.assertEqual(page.goto_calls, [selectors.detail_url("n1", "TOKEN")])

    def test_open_detail_retries_until_state_populated(self):
        page = FakePage()
        page.state_responses["note.noteDetailMap.n1"] = lambda call_index: (
            None if call_index == 0 else DETAIL_ENTRY
        )
        note, _ = browser.open_detail(page, "n1", "TOKEN")
        self.assertEqual(note.note_id, "n1")

    def test_open_detail_gives_up_after_three_tries(self):
        page = FakePage()
        page.state_responses["note.noteDetailMap.n1"] = None
        with self.assertRaises(XhsError):
            browser.open_detail(page, "n1", "TOKEN")


class EnsureOnNoteTests(unittest.TestCase):
    """S3.1 审查意见 1.4：截图/翻评论前先确认页面停在当前笔记的详情页。"""

    def setUp(self):
        self.pause_patcher = patch.object(browser, "human_pause", lambda *a, **k: None)
        self.pause_patcher.start()
        self.addCleanup(self.pause_patcher.stop)

    def test_already_on_note_does_not_navigate(self):
        page = FakePage()
        page.url = selectors.detail_url("n1", "TOKEN", "pc_feed")
        current = {"id": "n1", "xsec_token": "TOKEN", "xsec_source": "pc_feed"}
        browser._ensure_on_note(page, current)
        self.assertEqual(page.goto_calls, [])

    def test_not_on_note_navigates_to_detail(self):
        page = FakePage()
        page.state_responses["note.noteDetailMap.n1"] = DETAIL_ENTRY
        current = {"id": "n1", "xsec_token": "TOKEN", "xsec_source": "pc_search"}
        browser._ensure_on_note(page, current)
        self.assertEqual(page.goto_calls, [selectors.detail_url("n1", "TOKEN", "pc_search")])

    def test_url_attribute_access_failure_treated_as_not_on_note(self):
        # 假 page/真 Playwright 属性访问失败都当"不在"，不该让
        # `_ensure_on_note` 本身炸掉。
        class NoUrlPage(FakePage):
            @property
            def url(self):
                raise RuntimeError("boom")

            @url.setter
            def url(self, value):
                pass

        page = NoUrlPage()
        page.state_responses["note.noteDetailMap.n1"] = DETAIL_ENTRY
        current = {"id": "n1", "xsec_token": "TOKEN"}
        browser._ensure_on_note(page, current)
        self.assertEqual(page.goto_calls, [selectors.detail_url("n1", "TOKEN", "pc_feed")])


DETAIL_ENTRY_WITH_COMMENTS = {
    "note": DETAIL_ENTRY["note"],
    "comments": {
        "list": [
            {"id": "c1", "userInfo": {"nickname": "u1"}, "content": "t1", "subCommentCount": "2", "subComments": []},
        ],
        "hasMore": True,
    },
}


class LoadCommentsTests(unittest.TestCase):
    """S3.1 审查意见 1.1：`评论 N条` 缓存不够时打开浏览器滚 `.note-scroller`。"""

    def setUp(self):
        self.pause_patcher = patch.object(browser, "human_pause", lambda *a, **k: None)
        self.pause_patcher.start()
        self.addCleanup(self.pause_patcher.stop)

    def test_scrolls_until_enough_comments_then_stops(self):
        # 第一次读 state 只有 1 条、hasMore=True；滚一次之后变成 2 条、
        # hasMore=False——满足"够了"就该停，不再继续滚第三次。
        more_entry = {
            "note": DETAIL_ENTRY["note"],
            "comments": {
                "list": DETAIL_ENTRY_WITH_COMMENTS["comments"]["list"]
                + [{"id": "c2", "userInfo": {"nickname": "u2"}, "content": "t2", "subCommentCount": "0", "subComments": []}],
                "hasMore": False,
            },
        }
        page = FakePage()
        page.state_responses["note.noteDetailMap.n1"] = lambda i: (
            DETAIL_ENTRY_WITH_COMMENTS if i == 0 else more_entry
        )
        recorder = ScrollRecorder()
        with patch.object(pacing, "scroll", recorder):
            page_data = browser.load_comments(page, "n1", want=2)
        self.assertEqual(len(page_data.comments), 2)
        self.assertFalse(page_data.has_more)
        self.assertEqual(len(recorder.calls), 1)
        self.assertIn(selectors.DOM["note_scroller"], page.hover_calls)

    def test_stops_when_has_more_false_even_if_want_not_reached(self):
        page = FakePage()
        page.state_responses["note.noteDetailMap.n1"] = {
            "note": DETAIL_ENTRY["note"],
            "comments": {"list": DETAIL_ENTRY_WITH_COMMENTS["comments"]["list"], "hasMore": False},
        }
        page_data = browser.load_comments(page, "n1", want=99)
        self.assertEqual(len(page_data.comments), 1)
        self.assertEqual(page.mouse.wheel_calls, [])

    def test_gives_up_after_max_rounds(self):
        page = FakePage()
        page.state_responses["note.noteDetailMap.n1"] = DETAIL_ENTRY_WITH_COMMENTS  # 一直不变
        recorder = ScrollRecorder()
        with patch.object(pacing, "scroll", recorder):
            page_data = browser.load_comments(page, "n1", want=99)
        self.assertEqual(len(page_data.comments), 1)
        self.assertEqual(len(recorder.calls), browser.COMMENTS_SCROLL_MAX_ROUNDS)

    def test_checks_risk_while_scrolling(self):
        page = FakePage()
        page.state_responses["note.noteDetailMap.n1"] = DETAIL_ENTRY_WITH_COMMENTS
        hint = selectors.RISK_PAGE_HINTS[0]
        page.dom_elements[hint] = FakeElement()
        recorder = ScrollRecorder()
        with patch.object(pacing, "scroll", recorder), self.assertRaises(XhsError):
            browser.load_comments(page, "n1", want=99)
        self.assertLessEqual(len(recorder.calls), 1)


class ExpandCommentTests(unittest.TestCase):
    """S3.1 审查意见 1.1：点开一条评论的楼中楼，重复点到按钮消失/够了/次数
    上限为止。"""

    def setUp(self):
        self.pause_patcher = patch.object(browser, "human_pause", lambda *a, **k: None)
        self.pause_patcher.start()
        self.addCleanup(self.pause_patcher.stop)

    def _entry(self, sub_comments, sub_count="2"):
        return {
            "note": DETAIL_ENTRY["note"],
            "comments": {
                "list": [
                    {
                        "id": "c1",
                        "userInfo": {"nickname": "u1"},
                        "content": "t1",
                        "subCommentCount": sub_count,
                        "subComments": sub_comments,
                    }
                ],
                "hasMore": False,
            },
        }

    def test_clicks_until_show_more_button_gone(self):
        show_more = FakeElement()
        comment_el = FakeElement(children={selectors.DOM["comment_reply_show_more"]: show_more})
        page = FakePage()
        page.dom_element_lists[selectors.DOM["comment_item"]] = [comment_el]
        one_sub = [{"userInfo": {"nickname": "a"}, "content": "b"}]
        two_subs = one_sub + [{"userInfo": {"nickname": "c"}, "content": "d"}]
        entries = [self._entry([]), self._entry(one_sub), self._entry(two_subs)]
        page.state_responses["note.noteDetailMap.n1"] = lambda i: entries[min(i, len(entries) - 1)]

        def click():
            show_more.click_calls += 1
            if show_more.click_calls >= 2:
                comment_el.children.pop(selectors.DOM["comment_reply_show_more"], None)

        show_more.click = click

        comment = browser.expand_comment(page, "n1", 1)
        self.assertEqual(len(comment.sub_preview), 2)
        self.assertEqual(comment_el.scroll_into_view_calls, 1)

    def test_stops_when_enough_sub_comments_reached(self):
        comment_el = FakeElement(
            children={selectors.DOM["comment_reply_show_more"]: FakeElement()}
        )
        page = FakePage()
        page.dom_element_lists[selectors.DOM["comment_item"]] = [comment_el]
        page.state_responses["note.noteDetailMap.n1"] = self._entry(
            [{"userInfo": {"nickname": "a"}, "content": "b"}], sub_count="1"
        )
        comment = browser.expand_comment(page, "n1", 1)
        self.assertEqual(len(comment.sub_preview), 1)
        self.assertEqual(comment_el.children[selectors.DOM["comment_reply_show_more"]].click_calls, 0)

    def test_stops_after_max_clicks(self):
        show_more = FakeElement()
        comment_el = FakeElement(children={selectors.DOM["comment_reply_show_more"]: show_more})
        page = FakePage()
        page.dom_element_lists[selectors.DOM["comment_item"]] = [comment_el]
        page.state_responses["note.noteDetailMap.n1"] = self._entry([], sub_count="99")
        comment = browser.expand_comment(page, "n1", 1)
        self.assertEqual(show_more.click_calls, browser.COMMENT_EXPAND_MAX_CLICKS)
        self.assertEqual(len(comment.sub_preview), 0)

    def test_missing_comment_index_raises(self):
        page = FakePage()
        page.dom_element_lists[selectors.DOM["comment_item"]] = []
        with self.assertRaises(XhsError):
            browser.expand_comment(page, "n1", 1)

    def test_checks_risk_between_clicks(self):
        show_more = FakeElement()
        comment_el = FakeElement(children={selectors.DOM["comment_reply_show_more"]: show_more})
        page = FakePage()
        page.dom_element_lists[selectors.DOM["comment_item"]] = [comment_el]
        page.state_responses["note.noteDetailMap.n1"] = self._entry([], sub_count="99")
        hint = selectors.RISK_PAGE_HINTS[0]
        page.dom_elements[hint] = FakeElement()
        with self.assertRaises(XhsError):
            browser.expand_comment(page, "n1", 1)


class ScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.tempdir, self.xhs_dir = _fresh_xhs_dir()
        self.addCleanup(self.tempdir.cleanup)
        self.patcher = patch.object(paths, "XHS_DIR", self.xhs_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_screenshot_page(self):
        page = FakePage()
        path = browser.screenshot_page(page)
        self.assertTrue(path.exists())
        self.assertEqual(page.screenshot_calls, [str(path)])

    def test_screenshot_content_uses_note_content_selector(self):
        page = FakePage()
        element = FakeElement()
        page.dom_elements[selectors.DOM["note_content"]] = element
        path = browser.screenshot_content(page)
        self.assertTrue(path.exists())
        self.assertEqual(element.screenshot_calls, [str(path)])
        self.assertEqual(page.screenshot_calls, [])  # 没有退化成整页

    def test_screenshot_content_falls_back_to_full_page_when_not_found(self):
        page = FakePage()
        path = browser.screenshot_content(page)
        self.assertTrue(path.exists())
        self.assertEqual(page.screenshot_calls, [str(path)])

    def test_screenshot_comment_picks_nth_element(self):
        page = FakePage()
        elements = [FakeElement(), FakeElement(), FakeElement()]
        page.dom_element_lists[selectors.DOM["comment_item"]] = elements
        path = browser.screenshot_comment(page, 2)
        self.assertEqual(elements[1].screenshot_calls, [str(path)])
        self.assertEqual(elements[0].screenshot_calls, [])
        self.assertEqual(elements[2].screenshot_calls, [])

    def test_screenshot_comment_out_of_range_falls_back_to_page(self):
        page = FakePage()
        page.dom_element_lists[selectors.DOM["comment_item"]] = [FakeElement()]
        path = browser.screenshot_comment(page, 5)
        self.assertEqual(page.screenshot_calls, [str(path)])


def _fake_qr_data_uri(color: str = "red") -> str:
    image = Image.new("RGB", (2, 2), color=color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.tempdir, self.xhs_dir = _fresh_xhs_dir()
        self.addCleanup(self.tempdir.cleanup)
        self.patcher = patch.object(paths, "XHS_DIR", self.xhs_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.pause_patcher = patch.object(browser, "human_pause", lambda *a, **k: None)
        self.pause_patcher.start()
        self.addCleanup(self.pause_patcher.stop)

    def test_login_status_true(self):
        page = FakePage()
        page.state_responses["user.loggedIn"] = True
        self.assertTrue(browser.login_status(page))
        self.assertEqual(page.goto_calls, [selectors.EXPLORE_URL])

    def test_save_qrcode_upscales_and_writes_file(self):
        data_uri = _fake_qr_data_uri()
        path = browser._save_qrcode(data_uri)
        self.assertTrue(path.exists())
        with Image.open(path) as saved:
            self.assertEqual(saved.size, (8, 8))  # 2x2 放大 4 倍

    def test_save_qrcode_bad_uri_raises(self):
        with self.assertRaises(XhsError):
            browser._save_qrcode("not-a-data-uri")

    def test_run_login_flow_waits_for_qrcode_element_when_page_supports_it(self):
        """维护者 9/22 补丁：登录弹窗晚于页面出现，冷 profile 下 2 秒 pause 抓不到
        二维码（生产首跑扑空）。真 Playwright page 有 `wait_for_selector`，
        流程要先用它等二维码元素（最多 QR_APPEAR_TIMEOUT_MS）；超时也不炸。"""

        class WaitingPage(FakePage):
            def __init__(self):
                super().__init__()
                self.waits = []

            def wait_for_selector(self, selector, timeout=None):
                self.waits.append((selector, timeout))
                raise RuntimeError("timeout")  # 超时也只是静默，不影响后面的判断

        page = WaitingPage()
        page.state_responses["user.loggedIn"] = False
        message = browser.run_login_flow(page, share_fn=lambda p: None, sleep_fn=lambda s: None)
        self.assertEqual(page.waits, [(selectors.DOM["qrcode_img"], browser.QR_APPEAR_TIMEOUT_MS)])
        self.assertIn("没找到登录二维码", message)

    def test_run_login_flow_already_logged_in_short_circuits(self):
        page = FakePage()
        page.state_responses["user.loggedIn"] = True
        shared = []
        message = browser.run_login_flow(page, share_fn=shared.append, sleep_fn=lambda s: None)
        self.assertEqual(message, "已经是登录状态了")
        self.assertEqual(shared, [])

    def test_run_login_flow_shares_qrcode_and_detects_success(self):
        page = FakePage()
        data_uri = _fake_qr_data_uri()
        page.dom_elements[selectors.DOM["qrcode_img"]] = FakeElement({"src": data_uri})
        # 第一次调用 is_logged_in 是循环外的"已经登录了？"预检，第二次是
        # 第一轮扫码后的检查——两次之后就该判定成功。
        login_states = iter([False, True])
        page.state_responses["user.loggedIn"] = lambda _i: next(login_states)
        shared = []
        sleeps = []
        message = browser.run_login_flow(page, share_fn=shared.append, sleep_fn=sleeps.append)
        self.assertEqual(message, "已登录")
        self.assertEqual(len(shared), 1)  # 二维码没变，只发一次
        # S3.1 审查意见 2.3：50 秒拆成 5 秒一步，扫完码几秒内就该返回——这里
        # 扫码后第一次 5 秒查询就命中，只睡了一步，不是整段 50 秒。
        self.assertEqual(sleeps, [browser.LOGIN_POLL_STEP])

    def test_run_login_flow_touches_keeper_each_iteration(self):
        # 2026-09-22 code-review 指出：一轮可能整整睡 50 秒，不续命的话
        # 把 XHS_KEEPER_IDLE_TIMEOUT 调低就会被看守进程从 CDP 会话底下
        # 拆掉。
        page = FakePage()
        page.dom_elements[selectors.DOM["qrcode_img"]] = FakeElement({"src": _fake_qr_data_uri()})
        login_states = iter([False, True])
        page.state_responses["user.loggedIn"] = lambda _i: next(login_states)
        self.assertFalse(paths.keeper_touch_path().exists())
        browser.run_login_flow(page, share_fn=lambda p: None, sleep_fn=lambda s: None)
        self.assertTrue(paths.keeper_touch_path().exists())

    def test_run_login_flow_resends_only_when_qrcode_changes(self):
        page = FakePage()
        uri_a = _fake_qr_data_uri("red")
        uri_b = _fake_qr_data_uri("blue")
        srcs = [uri_a, uri_a, uri_b, uri_b, uri_b]  # 5 轮：a 不变、变成 b、b 不变到底
        page.dom_elements[selectors.DOM["qrcode_img"]] = FakeElement(
            {"src": lambda i: srcs[min(i, len(srcs) - 1)]}
        )
        page.state_responses["user.loggedIn"] = False
        shared = []
        message = browser.run_login_flow(page, share_fn=shared.append, sleep_fn=lambda s: None)
        self.assertEqual(len(shared), 2)  # a 发一次，变成 b 再发一次，之后不变不重发
        self.assertIn("等了几分钟", message)

    def test_run_login_flow_gives_up_after_max_attempts(self):
        page = FakePage()
        page.dom_elements[selectors.DOM["qrcode_img"]] = FakeElement({"src": _fake_qr_data_uri()})
        page.state_responses["user.loggedIn"] = False
        message = browser.run_login_flow(page, share_fn=lambda p: None, sleep_fn=lambda s: None)
        self.assertIn("等了几分钟", message)

    def test_run_login_flow_continues_when_share_fn_raises(self):
        # daemon 没跑时 share_fn 会抛 XhsError——登录流程不该被这个打断，
        # 二维码已经落在本地了，给不了分享就把路径写进最终那句话里
        # （设计文档 S3 现场补充："分享失败只报一行 warning 不中止"）。
        page = FakePage()
        page.dom_elements[selectors.DOM["qrcode_img"]] = FakeElement({"src": _fake_qr_data_uri()})
        page.state_responses["user.loggedIn"] = False

        def failing_share(_path):
            raise XhsError("本机还没登录，发不出去，等会儿再试")

        message = browser.run_login_flow(page, share_fn=failing_share, sleep_fn=lambda s: None)
        self.assertIn("等了几分钟", message)
        self.assertIn("二维码图片", message)
        self.assertIn(".png", message)

    def test_run_login_flow_no_qrcode_and_not_logged_in(self):
        page = FakePage()  # 没放 qrcode_img 元素，query_selector 返回 None
        page.state_responses["user.loggedIn"] = False
        message = browser.run_login_flow(page, share_fn=lambda p: None, sleep_fn=lambda s: None)
        self.assertIn("没找到登录二维码", message)

    def test_run_login_flow_polls_every_step_within_a_round(self):
        # S3.1 审查意见 2.3：一整轮最多 LOGIN_POLL_INTERVAL 秒，拆成
        # LOGIN_POLL_STEP 秒一步查一次；扫码发生在第 3 步才生效的场景。
        page = FakePage()
        page.dom_elements[selectors.DOM["qrcode_img"]] = FakeElement({"src": _fake_qr_data_uri()})
        login_states = iter([False, False, False, True])
        page.state_responses["user.loggedIn"] = lambda _i: next(login_states)
        sleeps = []
        message = browser.run_login_flow(page, share_fn=lambda p: None, sleep_fn=sleeps.append)
        self.assertEqual(message, "已登录")
        self.assertEqual(sleeps, [browser.LOGIN_POLL_STEP] * 3)

    def test_submit_code_no_input_element(self):
        page = FakePage()
        self.assertEqual(browser.submit_code(page, "123456"), "现在没有验证码框")

    def test_submit_code_fills_and_clicks_then_checks_login(self):
        page = FakePage()
        input_el = FakeElement()
        submit_el = FakeElement()
        page.dom_elements[selectors.DOM["code_input"]] = input_el
        page.dom_elements[selectors.DOM["submit_btn"]] = submit_el
        page.state_responses["user.loggedIn"] = True
        message = browser.submit_code(page, "123456")
        self.assertEqual(input_el.fill_calls, ["123456"])
        self.assertEqual(submit_el.click_calls, 1)
        self.assertEqual(message, "已登录")

    def test_submit_code_prefers_placeholder_hint_selector(self):
        # S3.1 审查意见 2.2：先找可见的 `input[placeholder*='验证码']`，找
        # 得到就不再退回现有的 `code_input`。
        page = FakePage()
        hint_el = FakeElement()
        fallback_el = FakeElement()
        page.dom_elements[selectors.DOM["code_input_placeholder_hint"]] = hint_el
        page.dom_elements[selectors.DOM["code_input"]] = fallback_el
        page.state_responses["user.loggedIn"] = True
        browser.submit_code(page, "654321")
        self.assertEqual(hint_el.fill_calls, ["654321"])
        self.assertEqual(fallback_el.fill_calls, [])

    def test_submit_code_not_logged_in_after_submit(self):
        page = FakePage()
        page.dom_elements[selectors.DOM["code_input"]] = FakeElement()
        page.state_responses["user.loggedIn"] = False
        message = browser.submit_code(page, "000000")
        self.assertEqual(message, "验证码提交了，还没看到登录成功")


if __name__ == "__main__":
    unittest.main()
