"""真人节奏：让浏览器操作更接近真人习惯。

原来的动作很"机器"：直接拼网址跳搜索页/详情页、页面一开就读完、一次滚
一大截、鼠标瞬移点击。这里收着"像人一样"的几种动作，`browser.py` 各动作
函数调用：

- `scroll`：拆成一小段一小段滚，速度不均，偶尔停一下；
- `click`/`hover`：先把元素滚进视野，鼠标分几十步移过去再点；
- `type_text`：一个字一个字打；
- `linger_on_note`：进笔记后停几秒、往后翻几张图；
- `admit`：每天开浏览器次数上限。

每个动作对假 page（测试）缺的方法退回旧行为，不抛异常；所有等待都经
`_sleep`，测试整体打桩成不睡。这只能降低风险，不能消除——机房 IP 和
自动化浏览器的环境特征不在这里管。
"""

from __future__ import annotations

import contextlib
import random
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from file_io import locked, read_json, write_json_atomic
from xhs import XhsError, paths

TZ = ZoneInfo("Asia/Shanghai")

# 每天开浏览器（一条走浏览器的命令算一次）的上限。
DAILY_BROWSER_LIMIT = 60

SCROLL_STEP_MIN = 120
SCROLL_STEP_MAX = 360
# 元素顶端在视口里、且上半截离底边留这么多才算"看得见"，不然接着滚。
VIEW_BOTTOM_MARGIN = 60
BRING_INTO_VIEW_MAX_ROUNDS = 10

# 进笔记后停留：只停几秒、翻几张图就交还给小机——真正的"看"是他读结果、
# 想事情那几十秒，页面一直停在这篇上，不需要这里再干等（这些行为都由程序
# 自己完成，不给小机加活）。
LINGER_BASE = 1.5
LINGER_CHARS_PER_SEC = 40
LINGER_READ_CAP = 4.0
LINGER_MAX_FLIPS = 3


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _today(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def admit(now: datetime | None = None) -> None:
    """开浏览器前的门：当天超过上限不开，抛 `XhsError`（给
    小机看的一句话）。过了门就把当天次数 +1。"""
    now = now or datetime.now(TZ)
    path = paths.browser_usage_path()
    with locked(path):
        data = read_json(path, {})
        if not isinstance(data, dict) or data.get("day") != _today(now):
            data = {"day": _today(now), "count": 0}
        count = int(data.get("count") or 0)
        if count >= DAILY_BROWSER_LIMIT:
            raise XhsError(f"今天已经刷了 {count} 次了，明天再刷吧（发链接给我看不受影响）")
        data["count"] = count + 1
        write_json_atomic(path, data)


def scroll(page: Any, dy: float) -> None:
    """把一次 `dy` 拆成若干小段滚，每段之间停几十到两百毫秒，偶尔多停
    一下——真人拨滚轮/划触控板是一下一下的，不是一次瞬移 1600px。"""
    remaining = abs(int(dy))
    sign = 1 if dy >= 0 else -1
    while remaining > 0:
        step = min(remaining, random.randint(SCROLL_STEP_MIN, SCROLL_STEP_MAX))
        page.mouse.wheel(0, sign * step)
        remaining -= step
        _sleep(random.uniform(0.06, 0.22))
        if random.random() < 0.15:
            _sleep(random.uniform(0.5, 1.4))


def measure(element: Any) -> dict | None:
    getter = getattr(element, "bounding_box", None)
    if getter is None:
        return None
    try:
        box = getter()
    except Exception:  # noqa: BLE001 — 元素被替换/脱离文档都当"量不到"
        return None
    return box if isinstance(box, dict) and box.get("width") and box.get("height") else None


def _viewport_height(page: Any) -> float:
    size = getattr(page, "viewport_size", None)
    if isinstance(size, dict) and size.get("height"):
        return float(size["height"])
    with contextlib.suppress(Exception):
        value = page.evaluate("() => window.innerHeight")
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 900.0


def _can_move(page: Any) -> bool:
    return hasattr(getattr(page, "mouse", None), "move")


def _move_into(page: Any, box: dict) -> tuple[float, float]:
    """鼠标分十几到三十步移到元素里一个随机点（不是正中心）。"""
    x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    page.mouse.move(x, y, steps=random.randint(12, 30))
    _sleep(random.uniform(0.15, 0.5))
    return x, y


def bring_into_view(page: Any, element: Any) -> None:
    """一小段一小段滚到元素出现在视口里；量不到位置就退回
    `scroll_into_view_if_needed`。滚哪个容器取决于鼠标当前在哪（评论区要
    先 `hover` 到 `.note-scroller` 上，调用方负责）。"""
    height = _viewport_height(page)
    last_top = None
    for _ in range(BRING_INTO_VIEW_MAX_ROUNDS):
        box = measure(element)
        if box is None:
            break
        top = box["y"]
        if top >= 0 and top + min(box["height"], height / 2) <= height - VIEW_BOTTOM_MARGIN:
            return
        if last_top is not None and abs(top - last_top) < 1:
            return  # 滚了没动：固定定位的元素（顶栏搜索框）或者滚错了容器，别再滚
        last_top = top
        scroll(page, top - height * random.uniform(0.3, 0.45))
        _sleep(random.uniform(0.3, 0.8))
    with contextlib.suppress(Exception):
        element.scroll_into_view_if_needed()


def click(page: Any, element: Any) -> None:
    """滚进视野 → 鼠标移过去 → 停一下 → 按下松开。假 page/量不到位置
    时退回 `element.click()`。"""
    if not _can_move(page):
        element.click()
        return
    bring_into_view(page, element)
    box = measure(element)
    if box is None:
        element.click()
        return
    x, y = _move_into(page, box)
    page.mouse.click(x, y, delay=random.randint(50, 130))


def hover(page: Any, element: Any) -> None:
    """鼠标移到元素上（不瞬移）。量不到位置就什么都不做。"""
    if not _can_move(page):
        return
    box = measure(element)
    if box is not None:
        _move_into(page, box)


def type_text(page: Any, text: str) -> None:
    """一个字一个字打，字间隔不均，偶尔停顿想一想。"""
    for ch in text:
        page.keyboard.type(ch)
        _sleep(random.uniform(0.09, 0.28))
        if random.random() < 0.1:
            _sleep(random.uniform(0.4, 1.0))


def linger_on_note(page: Any, note: Any) -> None:
    """进笔记后像人一样看一眼：按正文长短停两三秒到五六秒，多图笔记往后
    翻一到三张（键盘右方向键）。只拖时间，不读东西——数据早就从 state
    读完了。"""
    desc = getattr(note, "desc", "") or ""
    read_seconds = min(len(desc) / LINGER_CHARS_PER_SEC, LINGER_READ_CAP)
    _sleep((LINGER_BASE + read_seconds) * random.uniform(0.7, 1.2))
    images = getattr(note, "images", None) or []
    keyboard = getattr(page, "keyboard", None)
    if len(images) > 1 and keyboard is not None:
        for _ in range(min(len(images) - 1, random.randint(1, LINGER_MAX_FLIPS))):
            with contextlib.suppress(Exception):
                keyboard.press("ArrowRight")
            _sleep(random.uniform(0.8, 1.8))
