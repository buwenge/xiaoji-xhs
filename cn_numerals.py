"""中文数字（一~十及组合，含"两"）与阿拉伯数字的互认解析。

纯搬运自 `home.py`（先例 commit `a315269`，`home 提醒` 的日期/时刻解析最早
引入）——2026-09-11 晨报打分也要认中文数字编号，抽成共用模块而不是再抄
一份。**标准库 only**：被 `home.py`（venv）与 `morning_feedback.py`（系统
`/usr/bin/python3`）双解释器共用，不许引入任何第三方依赖。
"""

from __future__ import annotations


def chinese_number(value: str) -> int | None:
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value in digits:
        return digits[value]
    if "十" not in value or value.count("十") != 1:
        return None
    left, right = value.split("十")
    tens = 1 if not left else digits.get(left)
    ones = 0 if not right else digits.get(right)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


CN_NUM_PATTERN = "[零一二两三四五六七八九十]{1,3}"


def to_int(value: str) -> int | None:
    return int(value) if value.isdigit() else chinese_number(value)
