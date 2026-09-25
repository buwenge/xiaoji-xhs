"""小红书站点的 JS 状态路径 / DOM 选择器 / URL 模板——全部放这一个字典，
改版只改这里，不许散落到 `browser.py`/`keeper.py` 各处（设计文档第 3 节第
6 条）。

来源：2026-09-22 05:00 维护者匿名实测（Chrome 153 + Xvfb :99），
`tests/fixtures/xhs_browser_state.README.md` 有完整来源说明与 DOM 普查
表；登录态下的选择器/文案标了"待核对"，尚未真机验证。
"""

from __future__ import annotations

import os
import urllib.parse

CDP_PORT = 9223


def cdp_port() -> int:
    """Chrome 远程调试端口。默认 9223；`XHS_CDP_PORT` 可改——给测试用：
    子进程级测试起的假看守进程如果也写 9223，而此刻机器上恰好有一个真
    看守进程（小机正在刷、或维护者在 smoke）在监听 9223，`browser._cdp_alive`
    会把那个真 Chrome 当成自己的，测试就会顶包连上真实站点（S3.1 验收
    单待裁决第 6 条撞到过一次）。测试改用别的端口就物理隔开了。函数内
    当轮取 env，不在 import 时冻结。"""
    raw = os.environ.get("XHS_CDP_PORT", "").strip()
    if raw.isdigit():
        return int(raw)
    return CDP_PORT

EXPLORE_URL = "https://www.xiaohongshu.com/explore"


def search_url(keyword: str) -> str:
    return (
        "https://www.xiaohongshu.com/search_result?"
        f"keyword={urllib.parse.quote(keyword)}&source=web_explore_feed"
    )


def detail_url(note_id: str, xsec_token: str, xsec_source: str = "pc_feed") -> str:
    return (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        f"?xsec_token={urllib.parse.quote(xsec_token)}&xsec_source={xsec_source}"
    )


# ---------------------------------------------------------------------------
# window.__INITIAL_STATE__ 是 Vue 响应式对象：`feed.feeds`/`search.feeds`/
# `note.currentNoteId`/`user.loggedIn` 是 ref（真值 `._rawValue`，备用
# `._value`），`note.noteDetailMap` 是普通 reactive 对象。这一段 JS 通过
# `browser.read_state(page, path)` 统一调用：先沿点分路径逐级解 ref 取到
# 目标值，再用 JSON.stringify 的 replacer 递归解掉值内部可能还藏着的 ref
# （设计文档 S3 现场补充：「一段通用 JS 取值函数」，不许各处手写
# `_rawValue`）。JSON.stringify 本身不会产出裸 `undefined` 字面量（会跳过
# undefined 的属性、把数组里的 undefined 变成 null），不需要再像
# `fetch_light.py` 那样做 `undefined→null` 的正则替换。
# ---------------------------------------------------------------------------
READ_STATE_JS = """
(function (path) {
  function unref(v) {
    if (v && typeof v === 'object' && v.__v_isRef) {
      return ('_rawValue' in v) ? v._rawValue : v._value;
    }
    return v;
  }
  var cur = window.__INITIAL_STATE__;
  var parts = (path || '').split('.').filter(function (p) { return p.length > 0; });
  for (var i = 0; i < parts.length; i++) {
    cur = unref(cur);
    if (cur == null) return null;
    cur = cur[parts[i]];
  }
  cur = unref(cur);
  if (cur === undefined) return null;
  return JSON.stringify(cur, function (key, value) { return unref(value); });
})
"""

STATE_PATHS = {
    "feed": "feed.feeds",
    "search": "search.feeds",
    "logged_in": "user.loggedIn",
    "current_note_id": "note.currentNoteId",
}


def note_detail_path(note_id: str) -> str:
    return f"note.noteDetailMap.{note_id}"


# ---------------------------------------------------------------------------
# DOM（截图/滚动/登录表单用；结构化数据一律从 state 读，DOM 只用于"人眼
# 看到什么"这类操作）。登录态下的差异、验证码/风控页选择器均未核对，见
# `xhs_browser_state.README.md`。
# ---------------------------------------------------------------------------
DOM = {
    # ↓ 本阶段代码真的在读的（browser.py 按名字引用）。
    "login_modal": ".login-modal",
    "login_btn_sidebar": "button#login-btn",
    "qrcode_img": ".login-modal img.qrcode-img",
    "code_input": ".login-container .right form label.auth-code input",
    # S3.1 审查意见 2.2：`submit_code` 优先用这个更宽松的选择器（表单结构
    # 稍微改版也不容易失效），找不到再退回 "code_input"。原来的值是裸的
    # 中文提示文本 "验证码"（不是可用的 CSS 选择器），S3 时从来没人真的
    # 用它构造过选择器——现在直接给一个可用的属性选择器。
    "code_input_placeholder_hint": "input[placeholder*='验证码']",
    "submit_btn": ".login-container .right form button.submit",
    "note_content": ".note-content",
    "comment_item": ".parent-comment",
    # S3.1 审查意见 1.1：`browser.load_comments` 滚评论前先 hover 到这个
    # 容器上（鼠标不在它上面 `mouse.wheel` 滚的是外层页面）。
    "note_scroller": ".note-scroller",
    # S3.1 审查意见 1.1：`browser.expand_comment` 点这个按钮展开楼中楼。
    "comment_reply_show_more": ".reply-container .show-more",
    # 9/26 真人节奏：从搜索框打字搜、从列表点卡片进笔记、关弹层再点下一
    # 张。`search_input`/卡片的 `data-note-id`/`a.cover` 9/26 匿名实测核对
    # 过（原来记的 `.search-layout .search-input` 已经对不上）；弹层与关闭
    # 按钮匿名点不开卡片，登录态下待核对，找不到时 browser.py 退回
    # Esc 键/直接跳网址。
    "search_input": "#search-input",
    "note_card_cover": "a.cover",
    "note_modal": ".note-detail-mask",
    "note_close": ".close-circle",
    # ↓ 同一次 DOM 普查记下来但本阶段代码没接线（S4 实时观看会用到；
    # 改版时这些一起核对，不要漏掉——2026-09-22 code-review 指出：不标
    # 出来的话，下一个人分不清哪些是"现在真依赖的"、哪些只是记录）。
    "login_container": ".login-container",
    "phone_input": ".login-container .right form input",
    "note_container": "#noteContainer",
    "comments_container": ".comments-el > .comments-container",
    "comments_total": ".comments-el .total",
    "feeds_container": ".feeds-page .feeds-container",
    "note_item": "section.note-item",
}


def note_card(note_id: str) -> str:
    """列表页（首页/搜索结果）里某条笔记的卡片。"""
    return f"section.note-item[data-note-id='{note_id}']"

# 验证码/风控页：匿名实测未撞到，无真实选择器，先放一个候选列表；
# `browser.py` 撞到其中任意一个就当"需要人来处理"，立刻停并截图，不硬闯
# 不重试（设计文档第 3 节第 5 条）。
RISK_PAGE_HINTS = (
    "text=请完成安全验证",
    "text=访问频率异常",
    ".captcha-modal",
    "iframe[src*='captcha']",
)
