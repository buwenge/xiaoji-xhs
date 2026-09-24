"""Playwright over CDP：搜索/首页/详情/展开评论/截图/登录。

所有站点操作都接收一个已经打开的 `page`（真实调用走 `session()` 拿一个
连着看守进程 Chrome 的 Playwright Page；测试打桩一个假 page 对象，验证
"UI 动作序列 + 从 state 取数"，不连真浏览器/9223）。

结构化数据一律走 `read_state()`（`selectors.READ_STATE_JS`，唯一一处解
Vue ref 的地方）；DOM 只用于截图/滚动/登录表单这类"人眼看到什么"的操作
（设计文档第 3 节第 6 条：站点结构只放 `selectors.py` 一处）。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from PIL import Image

import log_store
from xhs import XhsError, paths, parse, selectors

REPO_ROOT = Path(__file__).resolve().parent.parent

HUMAN_PAUSE_MIN = 1.5
HUMAN_PAUSE_MAX = 4.0
LOGIN_POLL_INTERVAL = 50.0
# S3.1 审查意见 2.3：50 秒整段睡完才查一次 `is_logged_in`，扫完码的人最多
# 要多等 50 秒才看到"已登录"——拆成 5 秒一步，扫完码几秒内就能返回；轮次
# 上限（`LOGIN_MAX_ATTEMPTS`）和总时长（5 × 50s）不变。
LOGIN_POLL_STEP = 5.0
LOGIN_MAX_ATTEMPTS = 5
QR_UPSCALE = 4
# 登录弹窗是页面加载完之后才弹出来的，冷 profile 下 `human_pause` 那两秒经常
# 不够（9/22 生产首跑扑空一次）：先等二维码元素出现，最多这么久。
QR_APPEAR_TIMEOUT_MS = 15000

# S3.1 审查意见 1.2：五处 `page.goto(...)` 统一超时/等待策略，常量放一处——
# 小红书页面首屏渲染慢，等 `load` 事件可能迟迟不来，`domcontentloaded`
# 够用，后面已有的 `human_pause` 负责等真正渲染完。
GOTO_WAIT_UNTIL = "domcontentloaded"
GOTO_TIMEOUT_MS = 30000

# S3.1 审查意见 1.1：滚 `.note-scroller` 补评论，最多几轮、每轮滚多少。
COMMENTS_SCROLL_MAX_ROUNDS = 8
COMMENTS_SCROLL_DY = 1200
# 展开一条评论的楼中楼，最多点几次"展开 N 条回复"。
COMMENT_EXPAND_MAX_CLICKS = 5


def human_pause(base: float = 2.0) -> None:
    """反风控节奏：`base` 是本次动作的基准秒数（1.5–4 之间由调用方按动作
    权重挑），乘 `uniform(0.6, 1.4)` 抖动——不是固定间隔，不好被识别成
    脚本节奏。测试打桩成不睡（`patch.object(browser, "human_pause",
    lambda *a, **k: None)`），不为了测试真等这几秒。"""
    time.sleep(base * random.uniform(0.6, 1.4))


def read_state(page: Any, path: str) -> Any:
    """`selectors.READ_STATE_JS` 是唯一一处解 `window.__INITIAL_STATE__`
    里 Vue ref 的地方，所有读 state 的操作都经这里，不许各处手写
    `_rawValue`（设计文档 S3 现场补充）。真实 Playwright 返回的是
    `JSON.stringify` 之后的字符串，这里再 `json.loads` 一次；测试的假
    page 可以直接返回已经反序列化好的 Python 对象图省事。"""
    raw = page.evaluate(selectors.READ_STATE_JS, path)
    if raw is None:
        return None
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _safe_query(page: Any, selector: str) -> Any:
    """`page.query_selector` 的统一兜底：假 page（测试）/真 Playwright
    抛的异常类型不一样，找不到/读不到都按"没有这个元素"处理，不让一次
    DOM 探测炸掉整条命令。六处重复的 try/except 收成这一个（2026-09-22
    /simplify simplification 审查）——`is_logged_in` 例外：它原来是"两次
    查询共用一个 try，任何一次异常就整体判失败"，语义跟"每次查询各自独立
    兜底"不一样，保留原样不套这个助手，避免悄悄改行为。"""
    try:
        return page.query_selector(selector)
    except Exception:  # noqa: BLE001
        return None


def _safe_query_all(page: Any, selector: str) -> list:
    try:
        return page.query_selector_all(selector)
    except Exception:  # noqa: BLE001
        return []


def is_logged_in(page: Any) -> bool:
    """`user.loggedIn` 为准，DOM（登录弹窗/侧栏登录按钮）只做兜底
    （state 读不到时——比如页面还在加载）。"""
    value = read_state(page, selectors.STATE_PATHS["logged_in"])
    if isinstance(value, bool):
        return value
    try:
        modal = page.query_selector(selectors.DOM["login_modal"])
        sidebar_btn = page.query_selector(selectors.DOM["login_btn_sidebar"])
    except Exception:  # noqa: BLE001 — 假 page/真 Playwright 的异常类型不同，统一按"读不到"处理
        return False
    return modal is None and sidebar_btn is None


def _check_risk(page: Any) -> None:
    """撞到验证码/风控页立刻停，不硬闯不重试（红线）。候选选择器是
    `selectors.RISK_PAGE_HINTS`——本次匿名实测没撞到，无实测值，先占位。
    """
    for hint in selectors.RISK_PAGE_HINTS:
        if _safe_query(page, hint) is not None:
            log_store.write_log("warning", "xhs", f"撞到疑似验证码/风控页面：{hint}")
            raise XhsError("小红书要人来验证，我先放下手机了")


# ---------------------------------------------------------------------------
# 看守进程连接：ensure()/session()
# ---------------------------------------------------------------------------


def _read_keeper_json() -> dict | None:
    try:
        text = paths.keeper_json_path().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _pid_alive(pid: object) -> bool:
    """存活 + 身份核验：不只看 pid 还在不在，还要看 `/proc/<pid>/cmdline`
    里含不含 "xhs.keeper"（S3.1 审查意见 1.3）。看守进程被 `kill -9`/OOM
    杀死后 `keeper.json` 会留着一个陈旧 pid，操作系统随时可能把这个 pid
    另派给别的进程（小机的子进程/daemon 的子进程都可能）——不核验身份的
    话，`_alive_keeper()` 会误以为看守进程还活着去连一个根本不存在的
    CDP（还好），`close_keeper()` 会把 SIGTERM 发给这个无辜进程（不好）。
    用 `/proc` 文件系统读取，不用 `os.kill(pid, 0)` 探活——这样测试可以
    单独打桩 `os.kill` 验证"没有对着这个 pid 发送过任何信号"，不会被探活
    本身的调用污染断言。"""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    proc_dir = Path(f"/proc/{pid_int}")
    if not proc_dir.is_dir():
        return False
    try:
        cmdline = (proc_dir / "cmdline").read_bytes()
    except OSError:
        return False
    return b"xhs.keeper" in cmdline


def _cdp_alive(port: object) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/json/version", timeout=1):
            return True
    except (urllib.error.URLError, OSError, TypeError, ValueError):
        return False


def keeper_info() -> dict | None:
    """keeper.json + PID 身份核验（S3.1 审查意见 1.3 那套 `/proc/<pid>/cmdline`
    核验，见 `_pid_alive`），不连 CDP——`_alive_keeper()` 在这基础上再叠一层
    CDP 探活给"要真连浏览器"的调用方；S4 `xhs.live_api` 直接用这个更薄的
    版本，"是否在直播"只关心看守进程本身死没死，每 5 秒轮询一次没必要再多
    一次到 9223 的网络往返（也没必要在 daemon 侧引入"CDP 探活失败但看守
    进程其实活着"这种额外状态）。（2026-09-22 altitude 审查：原先
    `_alive_keeper` 和这个函数各自手写一遍"读 json + 核验 pid"，现在
    `_alive_keeper` 直接调这个当自己的身份核验那一步，不重复。）"""
    info = _read_keeper_json()
    if info is None or not _pid_alive(info.get("pid")):
        return None
    return info


def _alive_keeper() -> dict | None:
    info = keeper_info()
    if info is None:
        return None
    if not _cdp_alive(info.get("cdp_port", selectors.cdp_port())):
        return None
    return info


def _spawn_keeper() -> None:
    """`Popen(start_new_session=True)` 拉起，stdout/err 到 keeper.log；
    看守进程是独立进程组，本次 CLI 调用不等它跑完（设计文档 S3）。这一步
    本身（起 python 解释器）基本不会失败，但红线是"任何失败都变成一句
    中文，不吐 traceback"——万一真撞上（比如权限/资源限制），也不能让
    OSError 原样往上抛（2026-09-22 code-review 指出这里没兜底）。"""
    paths.ensure_dirs()
    try:
        log_handle = open(paths.keeper_log_path(), "a", encoding="utf-8")
    except OSError as exc:
        raise XhsError(f"看守进程日志文件打不开：{exc}") from exc
    try:
        subprocess.Popen(
            [sys.executable, "-m", "xhs.keeper"],
            cwd=str(REPO_ROOT),
            env={**os.environ, "XHS_DIR": str(paths.XHS_DIR)},
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise XhsError(f"看守进程起不来：{exc}") from exc
    finally:
        log_handle.close()


def _keeper_log_tail(max_chars: int = 200) -> str:
    """看守进程真正的失败原因（Xvfb/Chrome 缺失、CDP 一直不就绪…）只会
    写进 `keeper.log`，`ensure()` 超时时没人知道具体原因——把最后几行
    带上，比裸的"启动超时"有用（2026-09-22 code-review 指出）。"""
    try:
        text = paths.keeper_log_path().read_text(encoding="utf-8")
    except OSError:
        return ""
    tail = "\n".join(text.splitlines()[-3:]).strip()
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


def ensure(timeout: float = 20.0) -> dict:
    """确保看守进程活着、CDP 可连；必要时拉起它并等最多 `timeout` 秒。
    返回 keeper.json 的内容。"""
    info = _alive_keeper()
    if info is None:
        _spawn_keeper()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = _alive_keeper()
            if info is not None:
                break
            time.sleep(0.3)
        else:
            detail = _keeper_log_tail()
            suffix = f"（keeper.log：{detail}）" if detail else ""
            raise XhsError(f"小红书浏览器启动超时，稍后再试{suffix}")
    paths.touch_keeper()
    return info


@contextlib.contextmanager
def session():
    """真实生产用：拉起/连上看守进程的 Chrome，复用它唯一的默认标签页
    （不是每次命令都开一个新标签——"看"完接着"截图"用的是同一个页面）。
    `browser.close()` 在 `connect_over_cdp` 场景下只断开 CDP 会话，不会
    杀掉浏览器进程本身（浏览器不是 Playwright 启动的，生死由看守进程
    管）。这个函数不可能在 pytest 里单测（需要真 CDP），`ensure()`/各个
    动作函数已经把可测的逻辑都拆出去了；真实连通性只能靠真机 smoke。"""
    from playwright.sync_api import sync_playwright  # 惰性 import：只有真用到浏览器才需要这个依赖

    info = ensure()
    with sync_playwright() as pw:
        browser_handle = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{info['cdp_port']}")
        try:
            context = browser_handle.contexts[0] if browser_handle.contexts else browser_handle.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            yield page
        finally:
            browser_handle.close()
    paths.touch_keeper()


def _remove_stale_keeper_json() -> None:
    with contextlib.suppress(OSError):
        paths.keeper_json_path().unlink()


def close_keeper() -> str:
    """`放下`：给看守进程发 SIGTERM，让它自己走清理流程（不在这里等它
    退完——清理 Chrome/Xvfb 可能要几秒，CLI 命令不该为此卡住）。对 pid
    动手前先用 `_pid_alive` 核验身份（S3.1 审查意见 1.3）：pid 已经死了，
    或者活着但 `/proc/<pid>/cmdline` 里没有 "xhs.keeper"（陈旧 pid 被
    别的进程复用），都视为 `keeper.json` 过期——删掉它，不发信号。"""
    info = _read_keeper_json()
    if info is None:
        return "现在没有开着的浏览器"
    pid = info.get("pid")
    if not _pid_alive(pid):
        _remove_stale_keeper_json()
        return "浏览器好像已经关了"
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, TypeError, ValueError):
        _remove_stale_keeper_json()
        return "浏览器好像已经关了"
    return "已经放下了"


# ---------------------------------------------------------------------------
# 列表：搜索 / 首页 / 更多
# ---------------------------------------------------------------------------


def open_feed(page: Any) -> list[parse.ListItem]:
    page.goto(selectors.EXPLORE_URL, wait_until=GOTO_WAIT_UNTIL, timeout=GOTO_TIMEOUT_MS)
    human_pause(2.0)
    _check_risk(page)
    raw = read_state(page, selectors.STATE_PATHS["feed"])
    return parse.parse_list_items(raw)


def search(page: Any, keyword: str) -> list[parse.ListItem]:
    """匿名 `search.feeds` 是空数组、页面盖"登录后查看搜索结果"弹窗
    （S3 现场补充实测）——搜不到东西时先看是不是没登录，是的话给明确
    提示而不是"搜索结果是空的"。"""
    page.goto(selectors.search_url(keyword), wait_until=GOTO_WAIT_UNTIL, timeout=GOTO_TIMEOUT_MS)
    human_pause(2.5)
    _check_risk(page)
    raw = read_state(page, selectors.STATE_PATHS["search"])
    items = parse.parse_list_items(raw)
    if not items and not is_logged_in(page):
        raise XhsError("搜索要登录后才能看，我先放下手机了")
    return items


def _on_explore_page(page: Any) -> bool:
    """"首页"这个列表只有 `/explore` 本身算数——详情页 `/explore/<id>`
    也含 `/explore` 子串，不能用 `in` 判断（S3.1 审查意见 1.5）。"""
    url = (getattr(page, "url", "") or "").split("?", 1)[0]
    return url.rstrip("/") == selectors.EXPLORE_URL.rstrip("/")


def _on_search_results_page(page: Any, query: str) -> bool:
    url = getattr(page, "url", "") or ""
    if "/search_result" not in url:
        return False
    if not query:
        return True
    return urllib.parse.quote(query) in url


def load_more(page: Any, kind: str, current_count: int, query: str = "") -> list[parse.ListItem]:
    """`kind` 是 `"feed"` 或 `"search"`——最近一次列表是哪种就接着翻哪种
    （state 路径不同）。滚动几次触发懒加载，只返回比 `current_count`
    更晚的那些条目、序号接着编。真实"滚几次才够"未登录状态下无法验证，
    见验收记录"待真机"。

    S3.1 审查意见 1.5：`更多` 有可能是在"看"过别的笔记/截图之后才说的，
    这时页面早就不在列表页上了——继续滚只会滚错页面。`search` 还能重新
    导航回搜索结果页（有 `query` 记着）；`feed`（首页推荐）每次刷新都是
    新一批，接不上原来的序号，直接报错让宝宝/小机重新说一次"首页"。"""
    _check_risk(page)
    if kind == "search":
        if not is_logged_in(page):
            raise XhsError("这个要登录后才能看")
        if not _on_search_results_page(page, query):
            page.goto(selectors.search_url(query), wait_until=GOTO_WAIT_UNTIL, timeout=GOTO_TIMEOUT_MS)
            human_pause(2.0)
            _check_risk(page)
    elif kind == "feed":
        if not _on_explore_page(page):
            raise XhsError("首页列表已经翻过去了，再说一次 首页")
    path = selectors.STATE_PATHS.get(kind)
    if path is None:
        raise XhsError("不知道要翻哪个列表，先搜索或者看首页")
    for _ in range(3):
        page.mouse.wheel(0, 1600)
        human_pause(1.8)
        _check_risk(page)  # 每滚一次都check：撞到风控立刻停，不接着硬滚
        raw = read_state(page, path) or []
        if len(raw) > current_count:
            return parse.parse_list_items(raw[current_count:], start_index=current_count + 1)
    return []


# ---------------------------------------------------------------------------
# 详情
# ---------------------------------------------------------------------------


def open_detail(
    page: Any, note_id: str, xsec_token: str, xsec_source: str = "pc_feed"
) -> tuple[parse.Note, parse.CommentPage]:
    page.goto(selectors.detail_url(note_id, xsec_token, xsec_source), wait_until=GOTO_WAIT_UNTIL, timeout=GOTO_TIMEOUT_MS)
    entry = None
    for _ in range(3):
        human_pause(1.6)
        _check_risk(page)
        entry = read_state(page, selectors.note_detail_path(note_id))
        if entry:
            break
    if not entry:
        raise XhsError("这条笔记打不开，可能被删除或者需要登录")
    note = parse.parse_note_from_detail(entry)
    page_data = parse.parse_comments_from_detail(entry)
    return note, page_data


def _ensure_on_note(page: Any, current_note: dict[str, Any]) -> None:
    """截图/翻评论前先确认页面停在当前笔记的详情页（S3.1 审查意见 1.4）：
    `看 <链接>` 走 S1 光路径不开浏览器，之后直接 `截图 评论 1` 会在
    about:blank/首页上找 `.parent-comment`，退化成一张空白截图发给用户
    （S3 验收第 3 条抓到的坑）。`page.url` 不含笔记 id 就当作"还没在这
    篇"，重新导航过去。"""
    note_id = str(current_note.get("id") or "")
    try:
        current_url = page.url or ""
    except Exception:  # noqa: BLE001 — 假 page/真 Playwright 属性访问失败都当"不在"
        current_url = ""
    if note_id and note_id in current_url:
        return
    xsec_token = str(current_note.get("xsec_token") or "")
    xsec_source = str(current_note.get("xsec_source") or "pc_feed")
    open_detail(page, note_id, xsec_token, xsec_source)


def load_comments(page: Any, note_id: str, want: int) -> parse.CommentPage:
    """滚 `.note-scroller` 补评论，直到条数够 `want` 或 `hasMore` 为假，
    最多 `COMMENTS_SCROLL_MAX_ROUNDS` 轮（S3.1 审查意见 1.1）。调用方已经
    确认过 `_ensure_on_note`/`is_logged_in`，这里只管滚——未登录时不该
    走到这里（"不硬滚"）。鼠标不 hover 到 `.note-scroller` 上直接
    `mouse.wheel` 滚的是外层页面，评论区滚不动，所以先 `page.hover`。"""
    entry = read_state(page, selectors.note_detail_path(note_id)) or {}
    page_data = parse.parse_comments_from_detail(entry)
    for _ in range(COMMENTS_SCROLL_MAX_ROUNDS):
        if len(page_data.comments) >= want or not page_data.has_more:
            break
        with contextlib.suppress(Exception):
            page.hover(selectors.DOM["note_scroller"])
        page.mouse.wheel(0, COMMENTS_SCROLL_DY)
        human_pause(1.8)
        _check_risk(page)
        entry = read_state(page, selectors.note_detail_path(note_id)) or {}
        page_data = parse.parse_comments_from_detail(entry)
    return page_data


def _comment_entry_at(entry: dict | None, index: int) -> dict | None:
    comments = ((entry or {}).get("comments") or {}).get("list") or []
    if 0 < index <= len(comments):
        item = comments[index - 1]
        return item if isinstance(item, dict) else None
    return None


def expand_comment(page: Any, note_id: str, index: int) -> parse.Comment:
    """点第 `index` 条评论（跟 state `comments.list` 同序，也是
    `.parent-comment` 第 `index` 个）里的"展开 N 条回复"，重复点到按钮
    消失、楼中楼数量够了、或 `COMMENT_EXPAND_MAX_CLICKS` 次为止，返回
    刷新后的整条评论（含 `sub_preview`/`sub_count`/`sub_count_text`，
    S3.1 审查意见 1.1）。调用方已经确认过 `_ensure_on_note`/`is_logged_in`。
    """
    elements = _safe_query_all(page, selectors.DOM["comment_item"])
    element = elements[index - 1] if elements and 0 < index <= len(elements) else None
    if element is None:
        raise XhsError(f"没有第 {index} 条评论")
    with contextlib.suppress(Exception):
        element.scroll_into_view_if_needed()

    entry = read_state(page, selectors.note_detail_path(note_id))
    target = _comment_entry_at(entry, index)
    wanted_count, _ = parse._parse_count((target or {}).get("subCommentCount"))

    for _ in range(COMMENT_EXPAND_MAX_CLICKS):
        subs = (target or {}).get("subComments") or []
        if len(subs) >= wanted_count:
            break
        show_more = _safe_query(element, selectors.DOM["comment_reply_show_more"])
        if show_more is None:
            break
        with contextlib.suppress(Exception):
            show_more.click()
        human_pause(1.5)
        _check_risk(page)
        entry = read_state(page, selectors.note_detail_path(note_id))
        target = _comment_entry_at(entry, index)

    return parse.comment_from_detail_item(target or {}, index)


# ---------------------------------------------------------------------------
# 截图
# ---------------------------------------------------------------------------


def _screenshot_path(prefix: str) -> Path:
    paths.images_dir().mkdir(parents=True, exist_ok=True)
    return paths.images_dir() / f"shot_{prefix}_{int(time.time() * 1000)}.png"


def screenshot_page(page: Any) -> Path:
    path = _screenshot_path("page")
    page.screenshot(path=str(path))
    return path


def _screenshot_or_fallback(page: Any, path: Path, element: Any, warn_label: str) -> Path:
    """"元素截图；找不到就退化成整页可见区并记警告"——`screenshot_content`
    和 `screenshot_comment` 唯一的差异只是定位元素的方式和警告文案，
    收成一个共用尾巴（2026-09-22 /simplify simplification 审查）。"""
    if element is not None:
        element.screenshot(path=str(path))
        return path
    log_store.write_log("warning", "xhs", f"{warn_label}：定位不到，退化成整页截图")
    page.screenshot(path=str(path))
    return path


def screenshot_content(page: Any) -> Path:
    """正文容器截图；定位失败退化为整页可见区（设计文档 S3）。"""
    path = _screenshot_path("content")
    element = _safe_query(page, selectors.DOM["note_content"])
    return _screenshot_or_fallback(page, path, element, "截图 正文")


def screenshot_comment(page: Any, index: int) -> Path:
    """`.parent-comment` 第 N 个（跟 state `comments.list` 同序），定位
    失败退化为整页可见区（设计文档 S3 现场补充：`get_by_text` 只做兜底，
    本实现先只做"取第 N 个"+ 整页兜底，文本定位兜底留待真机撞见问题时
    再补——没有真实登录态核对过第 N 个跟评论展示顺序是否总是一致）。"""
    path = _screenshot_path(f"comment{index}")
    elements = _safe_query_all(page, selectors.DOM["comment_item"])
    element = elements[index - 1] if elements and 0 < index <= len(elements) else None
    return _screenshot_or_fallback(page, path, element, f"截图 评论 {index}")


# ---------------------------------------------------------------------------
# 登录（维护者专用，不写进小机教法）
# ---------------------------------------------------------------------------

_DATA_URI_RE = re.compile(r"^data:image/\w+;base64,(.+)$")


def login_status(page: Any) -> bool:
    page.goto(selectors.EXPLORE_URL, wait_until=GOTO_WAIT_UNTIL, timeout=GOTO_TIMEOUT_MS)
    human_pause(1.5)
    _check_risk(page)
    return is_logged_in(page)


def _wait_for_qrcode(page: Any) -> None:
    """有 `wait_for_selector` 就等二维码元素最多 `QR_APPEAR_TIMEOUT_MS`；
    假 page 没这个方法、或者真等到超时，都静默返回，交给后面的
    `_read_qrcode_src` 照常判断（维护者 9/22 补丁）。"""
    waiter = getattr(page, "wait_for_selector", None)
    if waiter is None:
        return
    try:
        waiter(selectors.DOM["qrcode_img"], timeout=QR_APPEAR_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — 超时/元素被替换等都按"没等到"处理
        return


def _read_qrcode_src(page: Any) -> str | None:
    element = _safe_query(page, selectors.DOM["qrcode_img"])
    if element is None:
        return None
    try:
        return element.get_attribute("src")
    except Exception:  # noqa: BLE001
        return None


def _save_qrcode(data_uri: str) -> Path:
    match = _DATA_URI_RE.match(data_uri or "")
    if not match:
        raise XhsError("登录二维码读不出来")
    raw = base64.b64decode(match.group(1))
    path = _screenshot_path("login_qr")
    tmp_path = path.with_suffix(".raw.png")
    tmp_path.write_bytes(raw)
    try:
        with Image.open(tmp_path) as image:
            image = image.convert("RGB")
            upscaled = image.resize(
                (image.width * QR_UPSCALE, image.height * QR_UPSCALE), Image.NEAREST
            )
            upscaled.save(path)
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
    return path


def run_login_flow(
    page: Any,
    *,
    share_fn: Callable[[Path], None],
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """`share_fn(png_path)` 由调用方（`xhs.cli`）注入——负责把二维码图片
    经 S2 的分享通道发到聊天，本模块不直接依赖 `xhs.share`（那是 daemon
    HTTP 客户端，跟"登录"这个纯浏览器操作是两件事，注入点分开职责更
    清楚，也方便测试不用真的打桩一整条 share 链路）。每 50 秒重读一次
    二维码，变了才重发；最多等 5 轮；`user.loggedIn` 变真或弹窗消失就算
    成功。`share_fn` 失败（比如 daemon 没跑）只记警告不中止——二维码文件
    已经落在本地了，分享通道通不通不该让整个登录流程直接报错退出（开工
    令 S3 现场补充）；给不了分享时把 PNG 路径写进最终返回的一句话里，
    人工也能去看。这个函数没法在没有真人扫码的情况下真机验证，见验收记录。
    """
    page.goto(selectors.EXPLORE_URL, wait_until=GOTO_WAIT_UNTIL, timeout=GOTO_TIMEOUT_MS)
    human_pause(2.0)
    _check_risk(page)
    if is_logged_in(page):
        return "已经是登录状态了"

    _wait_for_qrcode(page)
    last_src: str | None = None
    last_png_path: Path | None = None
    for _ in range(LOGIN_MAX_ATTEMPTS):
        # 这一轮可能整整睡 50 秒——顺手续一下 keeper 的命，免得看守进程
        # 空闲超时把 Chrome 从正开着的 CDP 会话底下拆掉（2026-09-22
        # code-review 指出：`XHS_KEEPER_IDLE_TIMEOUT` 调低或多等几轮就会
        # 撞上，默认 600s 有 2.3 倍余量、生产今天是安全的，仍然值得顺手
        # 补上这行）。
        paths.touch_keeper()
        src = _read_qrcode_src(page)
        if src is None:
            if is_logged_in(page):
                return "已登录"
            return "没找到登录二维码，页面可能还没加载完，稍后再试一次"
        if src != last_src:
            last_png_path = _save_qrcode(src)
            try:
                share_fn(last_png_path)
            except XhsError as exc:
                log_store.write_log("warning", "xhs", f"登录二维码分享失败（不中止流程）：{exc}")
            last_src = src
        # S3.1 审查意见 2.3：50 秒整段睡完才查一次会让扫完码的人多等最多
        # 50 秒——拆成 `LOGIN_POLL_STEP` 秒一步，扫完码几秒内就能返回；
        # 轮次上限、总时长（LOGIN_MAX_ATTEMPTS × LOGIN_POLL_INTERVAL）
        # 都不变。
        elapsed = 0.0
        logged_in = False
        while elapsed < LOGIN_POLL_INTERVAL:
            step = min(LOGIN_POLL_STEP, LOGIN_POLL_INTERVAL - elapsed)
            sleep_fn(step)
            elapsed += step
            if is_logged_in(page):
                logged_in = True
                break
        if logged_in:
            return "已登录"
    suffix = f"（二维码图片：{last_png_path}）" if last_png_path is not None else ""
    return f"等了几分钟还没扫码，先放下了，想登录再说一次 home 小红书 登录{suffix}"


def submit_code(page: Any, code: str) -> str:
    """先找可见的 `input[placeholder*='验证码']`，找不到再退回现有的
    `.login-container .right form label.auth-code input`——前者更宽松，
    表单结构稍微改版也不容易失效（S3.1 审查意见 2.2）。"""
    input_el = _safe_query(page, selectors.DOM["code_input_placeholder_hint"])
    if input_el is None:
        input_el = _safe_query(page, selectors.DOM["code_input"])
    if input_el is None:
        return "现在没有验证码框"
    input_el.fill(code)
    submit_btn = _safe_query(page, selectors.DOM["submit_btn"])
    if submit_btn is not None:
        submit_btn.click()
    human_pause(1.5)
    if is_logged_in(page):
        return "已登录"
    return "验证码提交了，还没看到登录成功"
