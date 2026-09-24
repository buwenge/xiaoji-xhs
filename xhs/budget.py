"""计数、三档提醒、5 小时回锅——全部纯函数（喂 dict/数字，不碰文件），
只有 `apply()` 顺手把新 state 算出来，真正落盘是调用方（`xhs.cli`）的事。

提醒文案（第 8 节，逐字，不许改写）：
- 10000 档带上当次累计数（"读了{n} token了"）；15000/20000 档是固定文案，
  不嵌数字。
- 计数口径：只算脚本返回给小机的文本，按 Asia/Shanghai 零点归零；一次
  调用跨两档就两句都报；20000 报过之后当天不再报任何档。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

THRESHOLDS = (10000, 15000, 20000)
REBOOT_THRESHOLD = THRESHOLDS[1]  # 回锅门槛就是"15k 档"，跟三档提醒共用同一个数字来源
REBOOT_COOLDOWN = timedelta(hours=5)

REBOOT_TEXT = "确定还要刷吗？今天已经刷过一次了哦。"


def _notice_text(threshold: int, tokens_after: int) -> str:
    if threshold == 10000:
        return f"预制提醒：宝宝你已经读了{tokens_after} token了！注意注意！不要狂吃上下文！但是真的还想刷也可以继续刷！"
    if threshold == 15000:
        return "预制提醒：15k了！！怎么还在看呀！"
    if threshold == 20000:
        return "预制提醒：20k了！！不要刷了不要刷了！"
    raise ValueError(f"未知档位：{threshold}")


def today_str(now: datetime) -> str:
    return now.date().isoformat()


def reset_if_new_day(state: dict[str, Any], now: datetime) -> dict[str, Any]:
    """跨日先归零。不原地改传入的 dict，返回一份新的，调用方自己决定要
    不要落盘。"""
    state = dict(state)
    day = today_str(now)
    if state.get("day") != day:
        state["day"] = day
        state["tokens_today"] = 0
        state["reported"] = []
    return state


def should_reboot(state: dict[str, Any], now: datetime) -> bool:
    """执行命令前先判断的"回锅"：今天已经攒够 15000 且距上次调用超过
    5 小时。两个条件缺一不触发；state 里的 day 不是今天（说明今天还没刷
    过）也不算回锅。"""
    if state.get("day") != today_str(now):
        return False
    if state.get("tokens_today", 0) < REBOOT_THRESHOLD:
        return False
    last_call_at = state.get("last_call_at")
    if not last_call_at:
        return False
    try:
        last = datetime.fromisoformat(last_call_at)
    except (TypeError, ValueError):
        return False
    return (now - last) > REBOOT_COOLDOWN


def touch_last_call(state: dict[str, Any], now: datetime) -> dict[str, Any]:
    """回锅句挡下命令时仍要更新 last_call_at（不然立刻重试还是会被挡）；
    day 在进这个分支前已经确认是今天，这里不用再跨日归零。"""
    state = dict(state)
    state["last_call_at"] = now.isoformat()
    return state


def apply(state: dict[str, Any], estimate: int, now: datetime) -> tuple[dict[str, Any], list[str]]:
    """真正执行了一次命令之后调用：跨日归零 → tokens_today += estimate →
    逐档判断"上次累计 < 档 ≤ 这次累计 且 档不在 reported"→ 记进 notices
    与 reported → 更新 last_call_at。返回 (新 state, 本次要追加的提醒
    文案列表)。"""
    state = reset_if_new_day(state, now)
    before = state.get("tokens_today", 0)
    after = before + max(estimate, 0)
    state["tokens_today"] = after
    state["last_call_at"] = now.isoformat()
    reported = list(state.get("reported", []))
    notices: list[str] = []
    for threshold in THRESHOLDS:
        if before < threshold <= after and threshold not in reported:
            notices.append(_notice_text(threshold, after))
            reported.append(threshold)
    state["reported"] = reported
    return state, notices
