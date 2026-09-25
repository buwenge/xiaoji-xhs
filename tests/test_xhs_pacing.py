"""`xhs/pacing.py`（真人节奏，9/26）测试：假 page/鼠标只记账，所有停顿打桩成不睡。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from xhs import XhsError, pacing, paths


class FakeMouse:
    def __init__(self):
        self.wheel_calls: list[tuple[int, int]] = []
        self.moves: list[tuple[float, float, int]] = []
        self.clicks: list[tuple[float, float]] = []

    def wheel(self, dx, dy):
        self.wheel_calls.append((dx, dy))

    def move(self, x, y, steps=1):
        self.moves.append((x, y, steps))

    def click(self, x, y, delay=0):
        self.clicks.append((x, y))


class FakeKeyboard:
    def __init__(self):
        self.typed: list[str] = []
        self.presses: list[str] = []

    def type(self, text):
        self.typed.append(text)

    def press(self, key):
        self.presses.append(key)


class FakePage:
    def __init__(self, height=1000):
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.viewport_size = {"width": 1000, "height": height}


class BoxElement:
    """`bounding_box` 返回 `box_fn()`——滚动后位置怎么变由测试决定。"""

    def __init__(self, box_fn):
        self.box_fn = box_fn
        self.click_calls = 0
        self.scroll_into_view_calls = 0

    def bounding_box(self):
        return self.box_fn()

    def click(self):
        self.click_calls += 1

    def scroll_into_view_if_needed(self):
        self.scroll_into_view_calls += 1


class PacingTestCase(unittest.TestCase):
    def setUp(self):
        sleep_patcher = patch.object(pacing, "_sleep", lambda *a, **k: None)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)


class AdmitTests(PacingTestCase):
    def setUp(self):
        super().setUp()
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        dir_patcher = patch.object(paths, "XHS_DIR", Path(tempdir.name))
        dir_patcher.start()
        self.addCleanup(dir_patcher.stop)

    def _at(self, text):
        return datetime.fromisoformat(text).replace(tzinfo=pacing.TZ)

    def test_counts_up_within_a_day(self):
        pacing.admit(self._at("2026-09-26T10:00:00"))
        pacing.admit(self._at("2026-09-26T11:00:00"))
        data = json.loads(paths.browser_usage_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"day": "2026-09-26", "count": 2})

    def test_refuses_after_daily_limit_and_resets_next_day(self):
        paths.browser_usage_path().write_text(
            json.dumps({"day": "2026-09-26", "count": pacing.DAILY_BROWSER_LIMIT}), encoding="utf-8"
        )
        with self.assertRaises(XhsError) as ctx:
            pacing.admit(self._at("2026-09-26T20:00:00"))
        self.assertIn("链接", str(ctx.exception))
        pacing.admit(self._at("2026-09-27T10:00:00"))
        data = json.loads(paths.browser_usage_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"day": "2026-09-27", "count": 1})

    def test_no_quiet_hours(self):
        # 没有深夜禁用时段，半夜照样能开。
        pacing.admit(self._at("2026-09-26T03:30:00"))
        data = json.loads(paths.browser_usage_path().read_text(encoding="utf-8"))
        self.assertEqual(data["count"], 1)

    def test_corrupt_file_starts_fresh(self):
        paths.browser_usage_path().write_text("{oops", encoding="utf-8")
        pacing.admit(self._at("2026-09-26T10:00:00"))
        data = json.loads(paths.browser_usage_path().read_text(encoding="utf-8"))
        self.assertEqual(data["count"], 1)


class ScrollTests(PacingTestCase):
    def test_splits_into_small_steps_summing_to_total(self):
        page = FakePage()
        pacing.scroll(page, 1600)
        dys = [dy for _, dy in page.mouse.wheel_calls]
        self.assertGreater(len(dys), 3)
        self.assertEqual(sum(dys), 1600)
        self.assertTrue(all(0 < dy <= pacing.SCROLL_STEP_MAX for dy in dys))

    def test_negative_scrolls_up(self):
        page = FakePage()
        pacing.scroll(page, -500)
        self.assertEqual(sum(dy for _, dy in page.mouse.wheel_calls), -500)


class BringIntoViewTests(PacingTestCase):
    def test_scrolls_until_element_visible(self):
        page = FakePage(height=1000)
        state = {"y": 2500.0}

        def fake_scroll(_page, dy):
            state["y"] -= dy

        element = BoxElement(lambda: {"x": 100, "y": state["y"], "width": 200, "height": 300})
        with patch.object(pacing, "scroll", fake_scroll):
            pacing.bring_into_view(page, element)
        self.assertGreaterEqual(state["y"], 0)
        self.assertLessEqual(state["y"] + 300, 1000)
        self.assertEqual(element.scroll_into_view_calls, 0)

    def test_stops_when_element_does_not_move(self):
        # 顶栏搜索框是固定定位，滚了也不动——不能一直滚到上限。
        page = FakePage()
        scrolls = []
        element = BoxElement(lambda: {"x": 300, "y": -5, "width": 400, "height": 40})
        with patch.object(pacing, "scroll", lambda _p, dy: scrolls.append(dy)):
            pacing.bring_into_view(page, element)
        self.assertEqual(len(scrolls), 1)


class ClickTests(PacingTestCase):
    def test_moves_mouse_in_steps_then_clicks_inside_box(self):
        page = FakePage()
        element = BoxElement(lambda: {"x": 100, "y": 200, "width": 200, "height": 100})
        pacing.click(page, element)
        self.assertEqual(len(page.mouse.moves), 1)
        x, y, steps = page.mouse.moves[0]
        self.assertGreaterEqual(steps, 12)
        self.assertEqual(page.mouse.clicks, [(x, y)])
        self.assertTrue(100 <= x <= 300 and 200 <= y <= 300)
        self.assertEqual(element.click_calls, 0)

    def test_falls_back_to_element_click_without_box(self):
        page = FakePage()
        element = BoxElement(lambda: None)
        pacing.click(page, element)
        self.assertEqual(element.click_calls, 1)
        self.assertEqual(page.mouse.clicks, [])


class TypeAndLingerTests(PacingTestCase):
    def test_types_one_char_at_a_time(self):
        page = FakePage()
        pacing.type_text(page, "秋天穿搭")
        self.assertEqual(page.keyboard.typed, ["秋", "天", "穿", "搭"])

    def test_linger_flips_at_most_images_minus_one(self):
        class Note:
            desc = "字" * 300
            images = [object(), object()]

        page = FakePage()
        pacing.linger_on_note(page, Note())
        self.assertEqual(page.keyboard.presses, ["ArrowRight"])

    def test_linger_single_image_does_not_flip(self):
        class Note:
            desc = ""
            images = [object()]

        page = FakePage()
        pacing.linger_on_note(page, Note())
        self.assertEqual(page.keyboard.presses, [])


if __name__ == "__main__":
    unittest.main()
