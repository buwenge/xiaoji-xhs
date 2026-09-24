"""requests 跟短链跳转 + 从页面 HTML 抽 `window.__INITIAL_STATE__`——xhs
包里唯一联网的地方（S1 阶段；S3 才引入真浏览器）。手机 UA，不登录，匿名
能看到正文/图片/首屏评论。
"""

from __future__ import annotations

import json
import re
import urllib.parse

import requests

from xhs import XhsError

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148"
)
FETCH_TIMEOUT = 15
FETCH_RETRIES = 1

_STATE_MARKER = "window.__INITIAL_STATE__"
# 只在"JS 字面量位置"（紧跟 `:`/`,`/`[`）替换 undefined，不动引号里的普通
# 文本——技术类笔记正文/代码片段里出现字面"undefined"这个词不算罕见
# （这仓库自己的 fixture 笔记就是讲部署的），不能被正则一杆子换成 null。
_UNDEFINED_RE = re.compile(r"([:,\[]\s*)undefined(\s*)(?=[,}\]])")
_LOGIN_WALL_RE = re.compile(r"/login|/404/sec_")

LOGIN_REQUIRED_MESSAGE = "这篇要登录才能看，等浏览器那期上线再试"
UNREACHABLE_MESSAGE = "这条链接打不开，检查一下链接或者晚点再试"


def resolve_and_fetch(url: str) -> tuple[str, str]:
    """手机 UA 跟短链跳转，拿最终页面。返回 `(final_url, html)`。链接打
    不开或被跳到登录墙，统一转成一句中文的 `XhsError`。"""
    response = None
    last_exc: Exception | None = None
    for attempt in range(FETCH_RETRIES + 1):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": MOBILE_UA},
                allow_redirects=True,
                timeout=FETCH_TIMEOUT,
            )
            break
        except requests.RequestException as exc:
            last_exc = exc
            response = None
    if response is None:
        raise XhsError(UNREACHABLE_MESSAGE) from last_exc
    final_url = response.url
    if response.status_code >= 400 or _is_login_wall(final_url):
        raise XhsError(LOGIN_REQUIRED_MESSAGE)
    return final_url, response.text


def _is_login_wall(url: str) -> bool:
    return bool(_LOGIN_WALL_RE.search(urllib.parse.urlsplit(url).path))


def extract_xsec_token(url: str) -> str | None:
    query = urllib.parse.urlsplit(url).query
    values = urllib.parse.parse_qs(query).get("xsec_token")
    return values[0] if values else None


def _extract_state_json_text(html: str) -> str | None:
    """从 HTML 里摘出 `window.__INITIAL_STATE__` 后面那段 JSON 源文本，用
    花括号配对计数扫描，不是正则找 `</script>` 当结尾——技术类笔记正文/
    代码块里完全可能字面出现 `</script>` 这几个字符，非贪婪正则会在那里
    提前截断，把本来完整的 JSON 切坏。"""
    marker_pos = html.find(_STATE_MARKER)
    if marker_pos < 0:
        return None
    eq_pos = html.find("=", marker_pos + len(_STATE_MARKER))
    if eq_pos < 0:
        return None
    start = eq_pos + 1
    length = len(html)
    while start < length and html[start].isspace():
        start += 1
    if start >= length or html[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    i = start
    while i < length:
        ch = html[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return html[start : i + 1]
        i += 1
    return None


def extract_initial_state(html: str) -> dict:
    """摘出 `window.__INITIAL_STATE__=…` 后面配对花括号内的 JSON 源文本，
    `undefined` 字面量换成 `null` 再 `json.loads`。抓不到/解析不出来一律
    当成"要登录才能看"（公开笔记页面里这段必然存在，抓不到多半是被跳到
    了登录/风控页而不是别的原因）。"""
    raw = _extract_state_json_text(html or "")
    if raw is None:
        raise XhsError(LOGIN_REQUIRED_MESSAGE)
    raw = _UNDEFINED_RE.sub(r"\1null\2", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise XhsError(LOGIN_REQUIRED_MESSAGE) from exc
    if not isinstance(data, dict):
        raise XhsError(LOGIN_REQUIRED_MESSAGE)
    return data
