#!/usr/bin/env python3
"""token 粗估：跟具体业务模块无关的纯函数，供 `morning_paper`、`xhs` 等按
同一口径估算"这段文字直接读要占小机多少上下文"。原属 `morning_paper.py`
（2026-09-22 搬出，供 xhs 包复用，避免第二处各写各的估算口径）。
"""

from __future__ import annotations

import math
import re

_CJK_RE = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")


def estimate_tokens(text: str) -> int:
    """token 粗估：中日韩字符按 1 字 1 token，其余按 4 字符 1 token。偏保守
    （中文实际略少于 1 字 1 token），用来标"直接读要占多少上下文"够用。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return cjk + math.ceil((len(text) - cjk) / 4)
