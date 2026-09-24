"""识别/归一化小红书链接：短链 `xhslink.cn` 或正式链接
`xiaohongshu.com/explore/<id>`、`/discovery/item/<id>`。

只在 `request.raw_text` 上找（不是 `normalize()` 过的 `request.text`）——
URL 和 `xsec_token` 大小写敏感，`home.py` 的 `Request.text` 已经被
`normalize()` 动过大小写和空白。
"""

from __future__ import annotations

import re
import urllib.parse

_URL_RE = re.compile(r"https?://[^\s，。！？、；：:]+", re.IGNORECASE)
_XHS_HOST_RE = re.compile(r"(^|\.)(xhslink\.cn|xiaohongshu\.com)$", re.IGNORECASE)
_TRAILING_PUNCT = ".,!?，。！？、）】”\""


def is_xhs_link(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False
    return bool(_XHS_HOST_RE.search(host))


def find_link(text: str) -> str | None:
    """在原文里找第一个小红书链接，原样返回（不做大小写/空白归一）；
    找不到返回 None。"""
    if not text:
        return None
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_TRAILING_PUNCT)
        if is_xhs_link(url):
            return url
    return None
