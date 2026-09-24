import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from xhs import budget

TZ = ZoneInfo("Asia/Shanghai")


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


class ResetIfNewDayTests(unittest.TestCase):
    def test_cross_day_zeroes_out(self):
        state = {"day": "2026-09-21", "tokens_today": 9000, "reported": [10000]}
        new_state = budget.reset_if_new_day(state, dt("2026-09-22T08:00:00"))
        self.assertEqual(new_state["day"], "2026-09-22")
        self.assertEqual(new_state["tokens_today"], 0)
        self.assertEqual(new_state["reported"], [])

    def test_same_day_untouched(self):
        state = {"day": "2026-09-22", "tokens_today": 9000, "reported": [10000]}
        new_state = budget.reset_if_new_day(state, dt("2026-09-22T09:00:00"))
        self.assertEqual(new_state["tokens_today"], 9000)
        self.assertEqual(new_state["reported"], [10000])

    def test_does_not_mutate_input(self):
        state = {"day": "2026-09-21", "tokens_today": 9000, "reported": []}
        budget.reset_if_new_day(state, dt("2026-09-22T08:00:00"))
        self.assertEqual(state["day"], "2026-09-21")


class ApplyTierNoticeTests(unittest.TestCase):
    def _base_state(self):
        return {"day": "2026-09-22", "tokens_today": 0, "reported": [], "last_call_at": None}

    def test_crossing_10000_reports_once(self):
        state = self._base_state()
        state, notices = budget.apply(state, 9500, dt("2026-09-22T10:00:00"))
        self.assertEqual(len(notices), 0)
        state, notices = budget.apply(state, 600, dt("2026-09-22T10:01:00"))
        self.assertEqual(len(notices), 1)
        self.assertIn("10100", notices[0])
        self.assertIn("token了", notices[0])
        # 再来一次不应该重复报
        state, notices = budget.apply(state, 100, dt("2026-09-22T10:02:00"))
        self.assertEqual(notices, [])

    def test_crossing_15000_reports_fixed_text(self):
        state = {**self._base_state(), "tokens_today": 14900, "reported": [10000]}
        state, notices = budget.apply(state, 200, dt("2026-09-22T10:00:00"))
        self.assertEqual(notices, ["预制提醒：15k了！！怎么还在看呀！"])

    def test_crossing_20000_reports_fixed_text(self):
        state = {**self._base_state(), "tokens_today": 19900, "reported": [10000, 15000]}
        state, notices = budget.apply(state, 200, dt("2026-09-22T10:00:00"))
        self.assertEqual(notices, ["预制提醒：20k了！！不要刷了不要刷了！"])

    def test_no_more_reports_after_20000(self):
        state = {**self._base_state(), "tokens_today": 25000, "reported": [10000, 15000, 20000]}
        state, notices = budget.apply(state, 5000, dt("2026-09-22T10:00:00"))
        self.assertEqual(notices, [])
        self.assertEqual(state["tokens_today"], 30000)

    def test_single_call_crossing_two_tiers_reports_both(self):
        state = self._base_state()
        state, notices = budget.apply(state, 16000, dt("2026-09-22T10:00:00"))
        self.assertEqual(len(notices), 2)
        self.assertIn("token了", notices[0])
        self.assertEqual(notices[1], "预制提醒：15k了！！怎么还在看呀！")
        self.assertEqual(state["reported"], [10000, 15000])

    def test_apply_updates_last_call_at(self):
        state = self._base_state()
        now = dt("2026-09-22T10:00:00")
        state, _ = budget.apply(state, 100, now)
        self.assertEqual(state["last_call_at"], now.isoformat())


class RebootTests(unittest.TestCase):
    def test_reboot_triggers_when_both_conditions_met(self):
        state = {
            "day": "2026-09-22",
            "tokens_today": 16000,
            "last_call_at": dt("2026-09-22T08:00:00").isoformat(),
        }
        self.assertTrue(budget.should_reboot(state, dt("2026-09-22T14:00:01")))

    def test_no_reboot_when_tokens_below_threshold(self):
        state = {
            "day": "2026-09-22",
            "tokens_today": 12000,
            "last_call_at": dt("2026-09-22T08:00:00").isoformat(),
        }
        self.assertFalse(budget.should_reboot(state, dt("2026-09-22T14:00:01")))

    def test_no_reboot_when_gap_within_5h(self):
        state = {
            "day": "2026-09-22",
            "tokens_today": 16000,
            "last_call_at": dt("2026-09-22T10:00:00").isoformat(),
        }
        self.assertFalse(budget.should_reboot(state, dt("2026-09-22T14:00:00")))

    def test_no_reboot_when_day_is_stale(self):
        state = {
            "day": "2026-09-21",
            "tokens_today": 16000,
            "last_call_at": dt("2026-09-21T08:00:00").isoformat(),
        }
        self.assertFalse(budget.should_reboot(state, dt("2026-09-22T14:00:00")))

    def test_no_reboot_when_last_call_at_missing(self):
        state = {"day": "2026-09-22", "tokens_today": 16000, "last_call_at": None}
        self.assertFalse(budget.should_reboot(state, dt("2026-09-22T14:00:00")))

    def test_touch_last_call_then_retry_immediately_passes(self):
        state = {
            "day": "2026-09-22",
            "tokens_today": 16000,
            "last_call_at": dt("2026-09-22T08:00:00").isoformat(),
        }
        now = dt("2026-09-22T14:00:01")
        self.assertTrue(budget.should_reboot(state, now))
        touched = budget.touch_last_call(state, now)
        # 立刻重试：gap 几乎为 0，不再触发回锅
        self.assertFalse(budget.should_reboot(touched, now + timedelta(seconds=1)))


if __name__ == "__main__":
    unittest.main()
