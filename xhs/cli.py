"""`xhs.cli.handle(request) -> str`：小机读到的所有小红书内容的唯一出口。

识别子命令 → 执行 → 渲染 → 预算（计数 + 三档提醒 + 5 小时回锅）→ 拼接 →
返回。计数、提醒拼接、输出长度控制只在这一处做（不散落到各个 `_cmd_*`
里）。子命令派发是显式字典，不写 if/elif 长链。
"""

from __future__ import annotations

import dataclasses
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

import cn_numerals
import log_store
import token_estimate

from xhs import XhsError, browser, budget, fetch_light, links, parse, paths, selectors, share
from xhs import state as state_store
from xhs import video, vision
from xhs.render import render_comment_expand, render_comments_from_current, render_list, render_view

# `xhs/` 包不 import `home`：小机实际是 `python home.py …` 跑的，那时
# home.py 是 __main__；如果这里 `from home import HomeError` 再抛
# `HomeError(...)`，抛出来的是"以 home 模块名导入"那份 HomeError 类，跟
# `main()` 里 `except HomeError` 抓的 __main__.HomeError 不是同一个类，
# 会被判定成未捕获异常直接吐 traceback（同一份代码两条导入路径产生两个
# 类对象，是 Python 的已知坑，pytest 里因为整体当模块 import 测不出这个
# 问题）。本包只抛 XhsError；HomeError 的转换交给 home.py 自己那个
# elif 分支去做。


TZ = ZoneInfo("Asia/Shanghai")

OUTPUT_BUDGET_WARN_TOKENS = 3000

HELP_TEXT = """小红书（只读，不点赞不评论不关注）：
  home 小红书 看 <链接>
  home 小红书 看 <链接> 不读图
  home 小红书 评论
  home 小红书 评论 10条
  home 小红书 评论 3 展开
  home 小红书 读图 3 全文
  home 小红书 读图 全文
  home 小红书 读图 3 仔细
  home 小红书 抽帧 <想看什么、怎么抽，随便说>（视频，比如：抽帧 平均看 8 帧 / 抽帧 3 分钟前后她在做什么）
  home 小红书 发帧 1:00 3:00（视频画面发给宝宝；没抽过的时间点也行）
  home 小红书 发帧 全部（上次抽帧截的都发）
  home 小红书 今天
  home 小红书 发原图 1 3
  home 小红书 发原图 全部
  home 小红书 分享链接
  home 小红书 搜索 <关键词>
  home 小红书 首页
  home 小红书 更多
  home 小红书 看 3
  home 小红书 截图
  home 小红书 截图 正文
  home 小红书 截图 评论 3
  home 小红书 放下
  home 小红书 登录（维护者用）
  home 小红书 登录 状态（维护者用）
  home 小红书 登录 验证码 123456（维护者用）"""

SHARE_LINK_SUFFIX = "复制本条信息，打开【小红书】App查看精彩内容！"

NOT_UNDERSTOOD = (
    "没听懂「{text}」。小红书能用的：看 <链接> / 看 N / 搜索 <关键词> / 首页 / 更多 / "
    "评论 / 评论 N 展开 / 读图 N 全文 / 读图 N 仔细 / 抽帧 <想怎么看> / 发帧 1:00 / 截图 / 发原图 N / 分享链接 / "
    "今天 / 放下（完整用法 home 小红书 --help）"
)

_DIGITS_RE = re.compile(r"\d+")


# ---- 语义兜底（2026-09-23）：先把小机的顺口说法改写成标准命令，再交给
# 下面的 `_RESOLVERS`。起因是 9/23 晚小机敲 `home 小红书 搜 短篇恐怖故事`
# 直接"没听懂"——只认"搜索"两个字。只改写句首动词，后面的内容原样保留
# （搜索关键词一个字都不动）；表里同时列出标准词本身，按长度从长到短比，
# 这样"搜索"不会被"搜"截成"搜索 索…"，"看图"/"看评论"也不会被"看"抢走。
_VERB_ALIASES: dict[str, str] = {
    "搜索": "搜索", "搜索一下": "搜索", "搜一下": "搜索", "搜一搜": "搜索",
    "搜搜": "搜索", "搜下": "搜索", "搜": "搜索", "查找": "搜索",
    "找一下": "搜索", "找找": "搜索", "找": "搜索", "search": "搜索",
    "看": "看", "看看": "看", "查看": "看", "打开": "看", "点开": "看",
    "点进": "看", "进入": "看", "open": "看",
    "读图": "读图", "看图": "读图", "看图片": "读图", "读图片": "读图", "识图": "读图",
    "发原图": "发原图", "发图": "发原图", "原图": "发原图",
    "抽帧": "抽帧", "截帧": "抽帧", "看视频": "抽帧", "看画面": "抽帧", "视频抽帧": "抽帧",
    "发帧": "发帧", "发画面": "发帧", "发截帧": "发帧", "发视频截图": "发帧",
    "分享链接": "分享链接", "分享": "分享链接", "发链接": "分享链接", "复制链接": "分享链接",
    "首页": "首页", "推荐": "首页", "主页": "首页", "刷首页": "首页", "刷新": "首页",
    "刷刷": "首页", "刷": "首页", "逛逛": "首页", "逛": "首页",
    "更多": "更多", "下一页": "更多", "翻页": "更多", "往下翻": "更多", "继续翻": "更多",
    "接着翻": "更多", "继续": "更多", "下一批": "更多", "再来点": "更多", "再来一些": "更多",
    "more": "更多",
    "放下": "放下", "关掉": "放下", "关闭": "放下", "关了": "放下", "退出": "放下",
    "不看了": "放下",
    "评论": "评论", "看评论": "评论", "评论区": "评论", "翻评论": "评论", "留言": "评论",
    "截图": "截图", "截屏": "截图", "截个图": "截图", "截张图": "截图", "截一下": "截图",
    "今天": "今天", "今日": "今天", "用量": "今天", "额度": "今天",
}
_VERB_TABLE = sorted(_VERB_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)

# 句首没有认得的动词时，剥掉一个口头前缀再试一次（"帮我搜 …""再看 3"）。
_FILLERS = ("帮我", "给我", "我想", "我要", "那就", "想", "再", "就", "去")

_VERB_TAIL_PUNCT = " \t：:，,"
_ORDINAL_RE = re.compile(rf"第\s*(\d+|{cn_numerals.CN_NUM_PATTERN})\s*[条篇个张号楼页]?")
_CN_NUMBER_RE = re.compile(
    rf"(?<![一-鿿])({cn_numerals.CN_NUM_PATTERN})(?=[条篇个张号楼页]|\s|$)"
)


def _split_verb(text: str) -> tuple[str, str] | None:
    lowered = text.lower()
    for alias, canonical in _VERB_TABLE:
        if lowered.startswith(alias):
            return canonical, text[len(alias) :].lstrip(_VERB_TAIL_PUNCT)
    return None


def _normalize_numbers(text: str) -> str:
    """"第三条""第 3 篇"→"3"，独立的中文数字"三""十条"→"3""10条"。"""

    def ordinal(match: re.Match[str]) -> str:
        value = cn_numerals.to_int(match.group(1))
        return match.group(0) if value is None else f" {value} "

    def standalone(match: re.Match[str]) -> str:
        value = cn_numerals.to_int(match.group(1))
        return match.group(0) if value is None else str(value)

    text = _ORDINAL_RE.sub(ordinal, text)
    text = _CN_NUMBER_RE.sub(standalone, text)
    return re.sub(r"\s+", " ", text).strip()


def _canonicalize(raw_text: str) -> str:
    """把句首同义动词换成标准命令词；非搜索、且不带链接的命令顺带把中文
    数字/序数词归成阿拉伯数字。认不出的原样返回，交给 `_RESOLVERS`。"""
    text = raw_text.strip()
    split = _split_verb(text)
    if split is None:
        for filler in _FILLERS:
            if text.startswith(filler):
                split = _split_verb(text[len(filler) :].lstrip())
                if split is not None:
                    break
    verb, rest = split if split is not None else ("", text)
    # 搜索词、抽帧的原话都要原样交出去，不做数字归一。
    if verb not in ("搜索", "抽帧") and links.find_link(raw_text) is None:
        rest = _normalize_numbers(rest)
    return f"{verb} {rest}".strip()

# "今天"（只读状态，特殊分支处理，从不进这个集合）之外，"登录"（维护者用的
# 管理动作）和"放下"（安全阀——不管读没读够都得能随时把浏览器关掉）也不
# 计入 token 预算、不受 5 小时回锅门槛拦：它们不是"刷小红书"本身。
_NO_BUDGET_COMMANDS = frozenset({"login", "drop"})


def handle(request: Any) -> str:
    """`request` 是 `home.Request`（鸭子类型，只用 `.raw_text`/`.help`），
    不在这里 import 那个类，见上面的模块注释。失败一律抛 `XhsError`，
    调用方（`home.py` 的 xhs 分支）负责转成它自己的 `HomeError`。"""
    paths.ensure_dirs()
    paths.cleanup()
    try:
        return _handle(request)
    except XhsError as exc:
        log_store.write_log("warning", "xhs", str(exc))
        raise


def _handle(request: Any) -> str:
    if request.help or not (request.raw_text or "").strip():
        return HELP_TEXT

    state = state_store.load()
    now = datetime.now(TZ)

    command, ctx = _resolve(request)

    if command == "today":
        return _cmd_today(state, now)

    skip_budget = command in _NO_BUDGET_COMMANDS

    if not skip_budget and budget.should_reboot(state, now):
        state = budget.touch_last_call(state, now)
        state_store.save(state)
        return budget.REBOOT_TEXT

    handler = _DISPATCH[command]
    output, state = handler(request, state, ctx)

    if skip_budget:
        state_store.save(state)
        _log_activity(command, ctx, state, None)
        return output

    estimate = token_estimate.estimate_tokens(output)
    state, notices = budget.apply(state, estimate, now)
    state_store.save(state)
    _log_activity(command, ctx, state, estimate)

    return _assemble(notices, output)


def _note_title(state: dict[str, Any]) -> str:
    title = ((state.get("current_note") or {}).get("title") or "").strip()
    return f"《{title[:30]}》" if title else "一篇笔记"


def _describe_images(ctx: dict[str, Any], state: dict[str, Any]) -> str:
    which = "第 " + "、".join(str(i) for i in ctx["indexes"]) + " 张" if ctx["indexes"] else "全部图"
    mode = "仔细看" if ctx["mode"] == "detail" else "逐字读"
    return f"{mode}{_note_title(state)}的{which}"


def _describe_screenshot(ctx: dict[str, Any], state: dict[str, Any]) -> str:
    target = {"content": "正文", "page": "整页"}.get(ctx["target"]) or f"第 {ctx.get('index')} 条评论"
    return f"截了{_note_title(state)}的{target}"


# 前端日志页"活动"分类里给用户看的一行（9/23 用户要求：之前只看得到
# 小机分享截图，看不到他在刷什么）。发原图/分享链接不记——daemon 的
# /api/xiaoji/share 已经记过"小机分享了…"；今天/登录是查状态和维护者用，
# 也不记。
_ACTIVITY_DESCRIBERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], str]] = {
    "search": lambda ctx, state: f"搜索「{ctx['keyword']}」（{len((state.get('last_list') or {}).get('items') or [])} 条结果）",
    "feed": lambda ctx, state: "刷首页推荐",
    "more": lambda ctx, state: "往下翻了一页",
    "view": lambda ctx, state: f"看{_note_title(state)}",
    "view_list": lambda ctx, state: f"点开第 {ctx['index']} 条：{_note_title(state)}",
    "comments": lambda ctx, state: f"翻{_note_title(state)}的评论",
    "comment_expand": lambda ctx, state: f"展开{_note_title(state)}第 {ctx['index']} 条评论的楼中楼",
    "images": _describe_images,
    "frames": lambda ctx, state: f"抽{_note_title(state)}的视频画面：{ctx['request'][:30] or '大致看看'}",
    "screenshot": _describe_screenshot,
    "drop": lambda ctx, state: "放下小红书",
}


def _log_activity(command: str, ctx: dict[str, Any], state: dict[str, Any], estimate: int | None) -> None:
    describe = _ACTIVITY_DESCRIBERS.get(command)
    if describe is None:
        return
    text = f"小红书：{describe(ctx, state)}"
    if estimate is not None:
        text += f"（约 {estimate} token）"
    try:
        log_store.write_log("info", "activity", text)
    except OSError:
        pass  # 记活动失败不该吞掉小机这次读到的内容


def _assemble(notices: list[str], output: str) -> str:
    pieces = list(notices)
    if output:
        pieces.append(output)
    text = "\n\n".join(pieces)
    estimate = token_estimate.estimate_tokens(text)
    if estimate > OUTPUT_BUDGET_WARN_TOKENS:
        text = f"（本次输出约 {estimate} token）\n\n{text}"
    return text


def _match_view(raw_text: str) -> dict[str, Any] | None:
    url = links.find_link(raw_text)
    if url is None:
        return None
    return {"url": url, "with_images": "不读图" not in raw_text}


def _keyword_matcher(word: str, *, exact: bool = False) -> Callable[[str], dict[str, Any] | None]:
    """"今天"/"分享链接"/"首页"/"更多"/"放下"这五个命令都是"纯关键词、
    没有参数、命中就是空 ctx"，只在"子串命中"（默认）还是"整句去空白后
    精确相等"（`exact=True`）上有差别——用一个工厂代替五份几乎一样的
    函数定义（2026-09-22 /simplify simplification 审查）。"""

    def matcher(raw_text: str) -> dict[str, Any] | None:
        if exact:
            return {} if raw_text.strip() == word else None
        return {} if word in raw_text else None

    return matcher


_match_today = _keyword_matcher("今天")


def _match_comment_expand(raw_text: str) -> dict[str, Any] | None:
    """"评论 3 展开"/"评论 展开 3"/"展开 3"都认：展开只对评论有意义，
    句首就是"展开"时不再要求带"评论"两个字。"""
    if "评论" not in raw_text and not raw_text.strip().startswith("展开"):
        return None
    expand_match = re.search(r"(\d+)\s*[楼条]?\s*展开", raw_text) or re.search(r"展开\s*(\d+)", raw_text)
    if not expand_match:
        return None
    return {"index": int(expand_match.group(1))}


def _match_comments(raw_text: str) -> dict[str, Any] | None:
    if "评论" not in raw_text:
        return None
    count_match = re.search(r"(\d+)\s*条", raw_text)
    limit = int(count_match.group(1)) if count_match else None
    return {"limit": limit}


def _match_images(raw_text: str) -> dict[str, Any] | None:
    """"读图 N 全文"/"读图 全文" 走逐字转录（`mode="full"`）；"读图 N
    仔细" 走 opus 仔细描述（`mode="detail"`）——两个关键词识别方式相同
    （S3.2 现场补充明文要求），谁在句子里就选谁；都不在时仍是"全文"
    （S1 起 `_cmd_images` 一直只服务"读图"这一条命令，没有 brief 分支，
    默认值维持原样，不是新引入的行为）。"""
    if "读图" not in raw_text:
        return None
    indexes = [int(x) for x in _DIGITS_RE.findall(raw_text)]
    mode = "detail" if "仔细" in raw_text else "full"
    return {"indexes": indexes or None, "mode": mode}


def _match_share_images(raw_text: str) -> dict[str, Any] | None:
    if "发原图" not in raw_text:
        return None
    if "全部" in raw_text:
        return {"indexes": None}
    indexes = [int(x) for x in _DIGITS_RE.findall(raw_text)]
    if not indexes:
        raise XhsError("发原图要跟上第几张，比如 home 小红书 发原图 1 3，或者 发原图 全部")
    return {"indexes": indexes}


_match_share_link = _keyword_matcher("分享链接")


def _match_frames(raw_text: str) -> dict[str, Any] | None:
    """"抽帧"后面小机说的话原样交给识图 agent（9/24 用户拍板：跟小院子
    送礼便签一样照转，不套格式），怎么抽、抽几帧由 agent 按他的意思定。"""
    stripped = raw_text.strip()
    if not stripped.startswith("抽帧"):
        return None
    return {"request": stripped[len("抽帧") :].strip()}


def _match_share_frames(raw_text: str) -> dict[str, Any] | None:
    stripped = raw_text.strip()
    if not stripped.startswith("发帧"):
        return None
    rest = stripped[len("发帧") :]
    if "全部" in rest:
        return {"times": None}
    times = []
    for token in re.split(r"[\s,，、]+", rest):
        if not token:
            continue
        seconds = video.parse_clock(token)
        if seconds is None:
            raise XhsError(f"看不懂时间点「{token}」，写成 分:秒，比如 home 小红书 发帧 1:00 3:05，或者 发帧 全部")
        times.append(seconds)
    if not times:
        raise XhsError("发帧要跟上时间点，比如 home 小红书 发帧 1:00 3:05，或者 发帧 全部")
    return {"times": sorted(set(times))}


_SCREENSHOT_COMMENT_RE = re.compile(r"评论\s*(\d+)")
_LOGIN_CODE_RE = re.compile(r"验证码\s*(\d+)")
_VIEW_LIST_RE = re.compile(r"^(?:看\s*)?(\d+)\s*[条篇个号]?(?:\s*不读图)?$")


def _match_login(raw_text: str) -> dict[str, Any] | None:
    """维护者用，不写进小机教法（附录 A）。"登录"最先判——避免"验证码"/
    "状态"这类词以后被别的关键词抢先命中（S3 现场补充明文要求）。"""
    if "登录" not in raw_text:
        return None
    if "状态" in raw_text:
        return {"action": "status"}
    code_match = _LOGIN_CODE_RE.search(raw_text)
    if code_match:
        return {"action": "code", "code": code_match.group(1)}
    return {"action": "start"}


def _match_view_list(raw_text: str) -> dict[str, Any] | None:
    """"看 N"（从最近列表打开第 N 条，走浏览器）——跟"看 <链接>"用同一个
    "看"字但形状完全不同（纯数字，没有链接），用严格锚定的正则避免误吃
    别的文本。"""
    match = _VIEW_LIST_RE.match(raw_text.strip())
    if not match:
        return None
    return {"index": int(match.group(1)), "with_images": "不读图" not in raw_text}


def _match_screenshot(raw_text: str) -> dict[str, Any] | None:
    """必须排在 `comment_expand`/`comments` 前面："截图 评论 3" 得先命中
    "截图"（S3 现场补充明文要求），不然"评论"关键词会抢先把它当成"评论
    展开/评论列表"处理。"""
    if "截图" not in raw_text:
        return None
    if "正文" in raw_text:
        return {"target": "content"}
    comment_match = _SCREENSHOT_COMMENT_RE.search(raw_text)
    if comment_match:
        return {"target": "comment", "index": int(comment_match.group(1))}
    return {"target": "page"}


def _match_search(raw_text: str) -> dict[str, Any] | None:
    """关键词是自由文本，可能字面包含"评论"/"读图"这些别的命令关键词
    （比如"搜索 评论区神评"）——用严格的前缀匹配（不是 `in`），且排在
    `comments`/`images` 等 substring 匹配之前，关键词本身不会被误判成
    别的命令。"""
    stripped = raw_text.strip()
    if not stripped.startswith("搜索"):
        return None
    keyword = stripped[len("搜索") :].strip()
    if not keyword:
        raise XhsError("搜索要跟上关键词，比如 home 小红书 搜索 猫咪零食")
    return {"keyword": keyword}


_match_feed = _keyword_matcher("首页")
_match_more = _keyword_matcher("更多", exact=True)
_match_drop = _keyword_matcher("放下", exact=True)


# 有序 (命令名, 匹配函数) 元组表——不写 if/elif 长链（屎山守则）。顺序即
# 优先级，从上到下第一个命中就用。前 7 条是 S1 原有 if 链原样搬过来的
# 顺序；S3 新命令插进来时特意把"截图"放在"评论"系列前面——理由见各自
# `_match_*` 函数的注释。"搜索"（自由关键词，前缀匹配，S3.1 审查意见 2.1
# 挪到最前）排第一：关键词本身可能含"登录"/"评论"这些别的命令的关键词
# （比如"搜索 登录问题""搜索 评论区神评"），前缀匹配比 in 匹配安全，
# 排最前不会被任何 substring 匹配抢走。"抽帧"（9/24）后面是小机的原话、
# 什么词都可能有，同理前缀匹配、排第二（"发帧"同一批加的，紧随其后）；
# "登录"（维护者用的管理动作）排在它们后面。
_RESOLVERS: tuple[tuple[str, Callable[[str], dict[str, Any] | None]], ...] = (
    ("search", _match_search),
    ("frames", _match_frames),
    ("share_frames", _match_share_frames),
    ("login", _match_login),
    ("view", _match_view),
    ("view_list", _match_view_list),
    ("screenshot", _match_screenshot),
    ("today", _match_today),
    ("feed", _match_feed),
    ("more", _match_more),
    ("drop", _match_drop),
    ("comment_expand", _match_comment_expand),
    ("comments", _match_comments),
    ("images", _match_images),
    ("share_images", _match_share_images),
    ("share_link", _match_share_link),
)


def _resolve(request: Any) -> tuple[str, dict[str, Any]]:
    """先做语义兜底改写，再依次试每个匹配函数，第一个返回非 None 的就是
    命中的命令。"""
    raw_text = request.raw_text or ""
    text = _canonicalize(raw_text)
    for command, matcher in _RESOLVERS:
        ctx = matcher(text)
        if ctx is not None:
            return command, ctx
    raise XhsError(NOT_UNDERSTOOD.format(text=raw_text.strip()))


def _require_current_note(state: dict[str, Any]) -> dict[str, Any]:
    current = state.get("current_note")
    if not current:
        raise XhsError("还没看过笔记，先 home 小红书 看 <链接>")
    return current


def _merge_image_sha256(
    images: list[dict[str, Any]], descs: list[vision.ImageDesc] | None
) -> list[dict[str, Any]]:
    """把这次 vision 结果里的 sha256 合回 state 存的 image dict 列表——
    `看`（首次描述）和`读图 N 全文`（补充描述）都走这一条，不再各写一份
    "按 index 查表"逻辑。"""
    if not descs:
        return images
    desc_by_index = {desc.index: desc for desc in descs}
    merged = []
    for image in images:
        entry = dict(image)
        desc = desc_by_index.get(image.get("index"))
        # desc.sha256 为空串是"这张图下载失败"的兜底描述（vision.py），
        # 不是真哈希——不能拿它去覆盖已有的 sha256（或者把 None 写成
        # "" 这种半吊子值）。
        if desc is not None and desc.sha256:
            entry["sha256"] = desc.sha256
        merged.append(entry)
    return merged


def _cmd_today(state: dict[str, Any], now: datetime) -> str:
    """读专用命令：不计数、不落盘（跨日归零只影响这次显示，不写回
    state.json——留给下一次真正执行的命令去做）。"""
    view = budget.reset_if_new_day(state, now)
    tokens = view.get("tokens_today", 0)
    tier = ""
    # 档位标签从 budget.THRESHOLDS 派生，不再抄一遍数字——万一以后
    # THRESHOLDS 调了，这里不会悄悄显示一个跟实际报警档位对不上的标签。
    for threshold in reversed(budget.THRESHOLDS):
        if tokens >= threshold:
            suffix = "，预制提醒已经报过三档了" if threshold == budget.THRESHOLDS[-1] else ""
            tier = f"（{threshold // 1000}k+ 档{suffix}）"
            break
    return f"今天累计读了约 {tokens} token{tier}"


def _build_current_note(
    note: parse.Note,
    page: parse.CommentPage,
    descs: list[vision.ImageDesc] | None,
    *,
    xsec_token: str,
    url: str,
    xsec_source: str = "pc_feed",
) -> dict[str, Any]:
    """`state["current_note"]` 的组装逻辑——`看 <链接>`（S1）和 `看 N`
    （S3，从最近列表打开）落地的形状必须完全一致（"评论 N""发原图 1 3"
    之类的后续命令都是按这个形状读的），复制第二次就抽共用（屎山守则）。

    `xsec_source`：`看 N` 传列表来源（`pc_feed`/`pc_search`），`看 <链接>`
    光路径默认 `pc_feed`——`评论 N条`/`评论 N 展开` 需要打开浏览器补数据
    时（S3.1 审查意见 1.1）拿它拼详情页 URL。`comments_has_more`：
    `page.has_more`（S3 详情页 state 给的）或者"这一页拿到的条数比总数
    少"（S1 光路径没有 `has_more` 字段，用这个近似），两者用 `or` 合一，
    不必按路径分叉判断——`评论 N条` 靠这个键决定要不要打开浏览器补页。
    """
    images_state = _merge_image_sha256(
        [{"index": image.index, "url": image.url, "sha256": None} for image in note.images], descs
    )
    # dataclasses.asdict 递归展开 sub_preview，字段名跟 parse.Comment/
    # SubComment 天然同步——以后那两个 dataclass 加字段/改名，这里不用跟
    # 着手改一遍，不会出现"忘改这份手抄字典"的漏字段。
    comments_state = [dataclasses.asdict(comment) for comment in page.comments]
    comments_has_more = bool(page.has_more) or len(page.comments) < page.total
    return {
        "id": note.note_id,
        "xsec_token": xsec_token,
        "url": url,
        "xsec_source": xsec_source,
        "title": note.title,
        "author": note.author,
        "comment_total": page.total,
        "comments_has_more": comments_has_more,
        "images": images_state,
        "comments": comments_state,
        "note_type": note.note_type,
        "duration_s": note.duration_s,
        "video_url": note.video_url,
    }


def _cmd_view(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    url = ctx["url"]
    final_url, html = fetch_light.resolve_and_fetch(url)
    initial_state = fetch_light.extract_initial_state(html)
    note = parse.parse_note(initial_state)
    page = parse.parse_comments(initial_state)
    xsec_token = fetch_light.extract_xsec_token(final_url)

    descs = None
    if ctx["with_images"] and note.images:
        descs = vision.describe_images(title=note.title, images=note.images, mode="brief")

    output = render_view(note, page, descs)

    new_state = dict(state)
    new_state["current_note"] = _build_current_note(note, page, descs, xsec_token=xsec_token, url=final_url)
    return output, new_state


def _more_comments_available(current: dict[str, Any]) -> bool:
    """`评论 N条` 要不要打开浏览器补数据（S3.1 审查意见 1.1）：本地缓存条数
    不够 `comment_total`，或者 `_build_current_note` 存的 `comments_has_more`
    是真——两者任一即可。"""
    cached = len(current.get("comments") or [])
    total = current.get("comment_total", cached)
    return cached < total or bool(current.get("comments_has_more"))


def _fetch_more_comments(
    state: dict[str, Any], current: dict[str, Any], want: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """未登录直接 `XhsError`、不滚（红线：不硬闯）；已登录才调
    `browser.load_comments` 滚页面。整体替换 `comments`/`comment_total`/
    `comments_has_more`，不是追加——`browser.load_comments` 已经从头读到
    尾，不存在"旧的 + 新的"拼接问题。"""

    def action(page: Any) -> parse.CommentPage:
        browser._ensure_on_note(page, current)
        if not browser.is_logged_in(page):
            raise XhsError("这个要登录后才能看")
        return browser.load_comments(page, current["id"], want)

    page_data = _with_browser(action)
    new_current = dict(current)
    new_current["comments"] = [dataclasses.asdict(comment) for comment in page_data.comments]
    new_current["comment_total"] = page_data.total
    new_current["comments_has_more"] = page_data.has_more
    new_state = dict(state)
    new_state["current_note"] = new_current
    return new_current, new_state


def _cmd_comments(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    current = _require_current_note(state)
    limit = ctx.get("limit")
    if limit is None:
        limit = 5
    cached = len(current.get("comments") or [])
    if limit > cached and _more_comments_available(current):
        current, state = _fetch_more_comments(state, current, limit)
    return render_comments_from_current(current, limit), state


def _find_comment(current: dict[str, Any], index: int) -> dict[str, Any] | None:
    comments = current.get("comments") or []
    return next((c for c in comments if c.get("index") == index), None)


def _expand_comment_from_browser(
    state: dict[str, Any], current: dict[str, Any], index: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """未登录直接 `XhsError`、不滚；已登录调 `browser.expand_comment`
    展开楼中楼，把刷新后的那一条整体写回 `current_note["comments"]`
    （S3.1 审查意见 1.1）。"""

    def action(page: Any) -> parse.Comment:
        browser._ensure_on_note(page, current)
        if not browser.is_logged_in(page):
            raise XhsError("这个要登录后才能看")
        return browser.expand_comment(page, current["id"], index)

    updated = _with_browser(action)
    new_current = dict(current)
    comments = [dict(c) for c in (new_current.get("comments") or [])]
    for position, comment in enumerate(comments):
        if comment.get("index") == index:
            comments[position] = dataclasses.asdict(updated)
            break
    new_current["comments"] = comments
    new_state = dict(state)
    new_state["current_note"] = new_current
    return new_current, new_state


def _cmd_comment_expand(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    current = _require_current_note(state)
    index = ctx["index"]
    match = _find_comment(current, index)
    if match is not None:
        sub_count = match.get("sub_count") or 0
        sub_preview = match.get("sub_preview") or []
        if sub_count > len(sub_preview):
            current, state = _expand_comment_from_browser(state, current, index)
    return render_comment_expand(current, index), state


def _cmd_images(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    current = _require_current_note(state)
    images = current.get("images") or []
    if not images:
        return "这篇笔记没有图", state
    indexes = ctx.get("indexes")
    mode = ctx.get("mode", "full")
    note_images = [parse.NoteImage(index=img["index"], url=img["url"]) for img in images]
    # 已经在 `看` 那次下载过的图，sha256 早存在 state 里了——带过去让
    # describe_images 优先认本地缓存文件，不用再跟小红书 CDN 要一遍字节。
    sha256_hints = {img["index"]: img["sha256"] for img in images if img.get("sha256")}
    descs = vision.describe_images(
        title=current.get("title", ""),
        images=note_images,
        mode=mode,
        indexes=indexes,
        sha256_hints=sha256_hints,
    )
    if not descs:
        if indexes:
            idx_text = "、".join(str(i) for i in indexes)
            return f"没有第 {idx_text} 张图", state
        return "这篇笔记没有图", state

    new_state = dict(state)
    new_current = dict(current)
    new_current["images"] = _merge_image_sha256(images, descs)
    new_state["current_note"] = new_current

    lines = []
    for desc in descs:
        lines.append(f"{desc.index} [{desc.kind}]\n{desc.text}")
    return "\n\n".join(lines), new_state


def _fresh_video(current: dict[str, Any]) -> tuple[str, int, str]:
    """视频地址带签名会过期：先用光路径重新取一次（顺带拿准确时长和正文
    给识图 agent 当背景），取不到（登录墙、网络）再退回"看"那时存下的。"""
    try:
        _final_url, html = fetch_light.resolve_and_fetch(current.get("url") or "")
        note = parse.parse_note(fetch_light.extract_initial_state(html))
        if note.video_url:
            return note.video_url, note.duration_s or int(current.get("duration_s") or 0), note.desc
    except XhsError as exc:
        log_store.write_log("warning", "xhs", f"抽帧重新取视频地址失败，用看的时候存的：{exc}")
    return str(current.get("video_url") or ""), int(current.get("duration_s") or 0), ""


def _cmd_frames(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    current = _require_current_note(state)
    if current.get("note_type") != "video":
        raise XhsError("这篇不是视频，看图用 home 小红书 读图 N 全文 / 读图 N 仔细")
    url, duration, desc = _fresh_video(current)
    if not url:
        raise XhsError("这条视频的地址没拿到，抽不了帧")
    if not duration:
        raise XhsError("这条视频的时长没拿到，抽不了帧")
    text, frames = video.watch(
        title=current.get("title", ""),
        desc=desc,
        duration=duration,
        url=url,
        request=ctx["request"],
    )
    new_current = dict(current)
    new_current["frames"] = frames
    new_state = dict(state)
    new_state["current_note"] = new_current
    output = f"视频 {video.fmt_clock(duration)}，抽帧看了：\n{text}"
    if frames:
        clocks = " ".join(video.fmt_clock(frame["t"]) for frame in frames)
        output += f"\n（这次截的帧：{clocks}；想发给宝宝：home 小红书 发帧 <时间点> / 发帧 全部）"
    return output, new_state


def _cmd_share_frames(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """抽过的帧直接发；没抽过（或文件已过 24h 清理）的时间点当场用 ffmpeg
    截一张——只截图不过识图模型，不花读图额度。"""
    current = _require_current_note(state)
    if current.get("note_type") != "video":
        raise XhsError("这篇不是视频，发图用 home 小红书 发原图 N")
    saved = {round(float(frame["t"]), 2): Path(frame["path"]) for frame in current.get("frames") or []}
    times = ctx["times"]
    if times is None:
        if not saved:
            raise XhsError("还没抽过帧，先 home 小红书 抽帧，或者直接写时间点：发帧 1:00 3:05")
        times = sorted(saved)
    if len(times) > share.MAX_SHARE_IMAGES:
        raise XhsError(f"一次最多发 {share.MAX_SHARE_IMAGES} 张，分几次发吧")
    missing = [t for t in times if not (saved.get(round(t, 2)) and saved[round(t, 2)].exists())]
    grabbed: dict[float, Path | None] = {}
    if missing:
        url, duration, _desc = _fresh_video(current)
        if not url:
            raise XhsError("这条视频的地址没拿到，截不了帧")
        late = [t for t in missing if duration and t >= duration]
        if late:
            raise XhsError(f"视频只有 {video.fmt_clock(duration)}，{video.fmt_clock(late[0])} 超出了")
        grabbed = dict(zip(missing, video.grab_frames(url, missing, paths.images_dir())))
        failed = [video.fmt_clock(t) for t in missing if grabbed[t] is None]
        if failed:
            raise XhsError(f"{'、'.join(failed)} 这几帧没截下来，发不出去")
    local_paths = [grabbed.get(t) or saved[round(t, 2)] for t in times]
    clocks = "、".join(video.fmt_clock(t) for t in times)
    ids = share.upload_files(local_paths)
    share.share(ids=ids, note=f"小红书：视频画面 {clocks}")
    return f"已把 {len(local_paths)} 张视频画面（{clocks}）发给宝宝", state


def _cmd_share_images(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    current = _require_current_note(state)
    images = current.get("images") or []
    if not images:
        raise XhsError("这篇笔记没有图")

    # 一次性建好"要改的副本 + 按 index 查表"，后面选图/补写 sha256 都在
    # 这一份上做，不用再各自建一份（/simplify simplification 审查
    # 2026-09-22 指出原先建了三份索引表）。
    updated_images = [dict(img) for img in images]
    by_index = {img["index"]: img for img in updated_images}

    indexes = ctx.get("indexes")
    if indexes:
        wanted = sorted(set(indexes))
        missing = [i for i in wanted if i not in by_index]
        if missing:
            idx_text = "、".join(str(i) for i in missing)
            raise XhsError(f"没有第 {idx_text} 张图")
        chosen = [by_index[i] for i in wanted]
    else:
        chosen = updated_images

    if len(chosen) > share.MAX_SHARE_IMAGES:
        raise XhsError(f"一次最多发 {share.MAX_SHARE_IMAGES} 张图，分几次发吧")

    # `sha256` 非空且缓存文件还在就直接用；否则补下载（`不读图` 之后
    # sha256 是 null，或者缓存文件已过 24h 被清理）——只是补文件，不触发
    # opus，跟"读图"命令完全是两条独立路径。要下载的部分并发抓取：这条
    # 命令的耗时基本就是"下载 + 一次 multipart 上传"，不像 vision.py 读图
    # 后面还有一次更慢的 opus 调用可以摊薄下载时间，值得并发（/simplify
    # efficiency 审查 2026-09-22 指出，跟 S1 vision.py 故意不并发的理由
    # 不是同一回事）。
    local_paths_by_index: dict[int, Path] = {}
    need_download = []
    for img in chosen:
        sha256 = img.get("sha256")
        cached = vision._find_cached_file(sha256) if sha256 else None
        if cached is not None:
            local_paths_by_index[img["index"]] = cached
        else:
            need_download.append(img)

    if need_download:
        errors: list[int] = []
        with ThreadPoolExecutor(max_workers=min(4, len(need_download))) as pool:
            future_to_img = {
                pool.submit(vision._download, img["url"], known_sha256=img.get("sha256") or None): img
                for img in need_download
            }
            for future in as_completed(future_to_img):
                img = future_to_img[future]
                try:
                    sha256, path = future.result()
                except (requests.RequestException, OSError):
                    errors.append(img["index"])
                    continue
                local_paths_by_index[img["index"]] = path
                # `img` 就是 `by_index[img["index"]]`（`chosen`/`need_download`
                # 的元素本来就是从 `by_index`/`updated_images` 里挑出来的同一
                # 个 dict 对象，不是拷贝），直接改它自己就行，不用再查一次表
                # （2026-09-22 code-review 指出的多余查表）。
                img["sha256"] = sha256
        if errors:
            idx_text = "、".join(str(i) for i in sorted(errors))
            raise XhsError(f"第 {idx_text} 张图下载失败，发不出去")

    local_paths = [local_paths_by_index[img["index"]] for img in chosen]

    ids = share.upload_files(local_paths)
    note = f"小红书：发原图 {'、'.join(str(i) for i in wanted)}" if indexes else "小红书：发原图 全部"
    share.share(ids=ids, note=note)

    new_state = dict(state)
    new_current = dict(current)
    new_current["images"] = updated_images
    new_state["current_note"] = new_current
    return f"已把 {len(local_paths)} 张原图发给宝宝", new_state


def _cmd_share_link(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    current = _require_current_note(state)
    title = current.get("title") or "（无标题）"
    url = current.get("url") or ""
    text = f"{title}\n{url}\n{SHARE_LINK_SUFFIX}"
    share.share(link={"url": url, "title": title, "text": text}, note="小红书：分享链接")
    return "已把链接发给宝宝", state


def _new_list_result(
    state: dict[str, Any], *, kind: str, query: str, items: list[parse.ListItem]
) -> tuple[str, dict[str, Any]]:
    """`搜索`/`首页`都是"拿一份全新列表、渲染、整段存进 last_list"，唯一
    差异是 kind/query 和拿列表的方式——共用这一段尾巴（2026-09-22
    /simplify simplification 审查）。`更多`语义不同（渲染只给新增那部分、
    保存要拼接"已经是 dict 的旧条目 + 新 dataclass 条目"），勉强凑同一个
    函数反而要加分支，不比现在简单，仍然单独一份。"""
    output = render_list(items)
    new_state = dict(state)
    new_state["last_list"] = {
        "kind": kind,
        "query": query,
        "items": [dataclasses.asdict(item) for item in items],
    }
    return output, new_state


def _with_browser(fn: Callable[[Any], Any]) -> Any:
    """走浏览器的每个命令都经这里（S3.1 审查意见 1.2）：Playwright 层的
    异常（CDP 断连、`page.goto` 超时、target closed 等）在这一处统一收
    口成一句中文，不吐 traceback（全局规则 7）。`XhsError` 原样透传——
    那是 xhs 包自己判断出来的、已经是给小机看的中文提示（比如"这个要
    登录后才能看"），不需要再包一层。"""
    try:
        with browser.session() as page:
            return fn(page)
    except XhsError:
        raise
    except Exception as exc:  # noqa: BLE001 — Playwright 抛的异常类型很多，这里是唯一收口
        detail = f"{type(exc).__name__}：{str(exc)[:60]}"
        log_store.write_log("warning", "xhs", f"浏览器操作失败：{detail}")
        raise XhsError(f"小红书页面没打开：{detail}") from exc


def _cmd_search(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    keyword = ctx["keyword"]
    items = _with_browser(lambda page: browser.search(page, keyword))
    return _new_list_result(state, kind="search", query=keyword, items=items)


def _cmd_feed(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    items = _with_browser(lambda page: browser.open_feed(page))
    return _new_list_result(state, kind="feed", query="", items=items)


def _cmd_more(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    last_list = state.get("last_list")
    if not last_list:
        raise XhsError("还没有列表可以翻页，先 home 小红书 搜索 <关键词> 或者 首页")
    existing_items = last_list.get("items") or []
    query = last_list.get("query", "")
    new_items = _with_browser(
        lambda page: browser.load_more(page, last_list["kind"], len(existing_items), query)
    )
    if not new_items:
        return "没有更多了", state
    output = render_list(new_items)
    new_state = dict(state)
    new_state["last_list"] = {
        **last_list,
        "items": existing_items + [dataclasses.asdict(item) for item in new_items],
    }
    return output, new_state


def _cmd_view_list(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    last_list = state.get("last_list")
    if not last_list:
        raise XhsError("还没有列表，先 home 小红书 搜索 <关键词> 或者 首页")
    index = ctx["index"]
    items = last_list.get("items") or []
    match = next((item for item in items if item.get("index") == index), None)
    if match is None:
        raise XhsError(f"列表里没有第 {index} 条")

    xsec_source = "pc_search" if last_list.get("kind") == "search" else "pc_feed"
    note, page_data = _with_browser(
        lambda page: browser.open_detail(page, match["id"], match["xsec_token"], xsec_source)
    )

    # opus 读图是慢调用，跟浏览器连接没关系——`_with_browser` 里的
    # `with` 块已经退出（CDP 会话已断开，看守进程照常挂着），不占着浏览
    # 器连接空等。
    descs = None
    if ctx["with_images"] and note.images:
        descs = vision.describe_images(title=note.title, images=note.images, mode="brief")

    output = render_view(note, page_data, descs)
    new_state = dict(state)
    new_state["current_note"] = _build_current_note(
        note,
        page_data,
        descs,
        xsec_token=match["xsec_token"],
        url=selectors.detail_url(match["id"], match["xsec_token"], xsec_source),
        xsec_source=xsec_source,
    )
    return output, new_state


def _cmd_screenshot(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """`截图`（page 目标）不要求先"看"过笔记——对着列表页/任意页面都能
    截；`截图 正文`/`截图 评论 N` 需要一个当前笔记（先拿到 id/xsec_token
    才能在必要时 `_ensure_on_note` 导航过去，S3.1 审查意见 1.4）。current
    note 的检查放在打开浏览器之前，省得为了一个必然会失败的命令白开一次
    Chrome。"""
    target = ctx["target"]
    current = _require_current_note(state) if target in ("content", "comment") else None

    def action(page: Any) -> tuple[Path, str]:
        if target == "content":
            browser._ensure_on_note(page, current)
            path = browser.screenshot_content(page)
            note_text = "小红书：截图 正文"
        elif target == "comment":
            browser._ensure_on_note(page, current)
            path = browser.screenshot_comment(page, ctx["index"])
            note_text = f"小红书：截图 评论 {ctx['index']}"
        else:
            path = browser.screenshot_page(page)
            note_text = "小红书：截图"
        return path, note_text

    path, note_text = _with_browser(action)
    ids = share.upload_files([path])
    share.share(ids=ids, note=note_text)
    return "已把截图发给宝宝", state


def _cmd_drop(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return browser.close_keeper(), state


def _cmd_login(request: Any, state: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """维护者专用（`home 小红书 登录` 系列不写进小机教法）。"""
    action = ctx["action"]
    if action == "status":
        logged_in = _with_browser(lambda page: browser.login_status(page))
        return ("已登录" if logged_in else "还没登录"), state
    if action == "code":
        message = _with_browser(lambda page: browser.submit_code(page, ctx["code"]))
        return message, state

    def _share_qrcode(png_path: Path) -> None:
        ids = share.upload_files([png_path])
        share.share(ids=ids, note="小红书：登录二维码")

    message = _with_browser(lambda page: browser.run_login_flow(page, share_fn=_share_qrcode))
    return message, state


_DISPATCH: dict[str, Callable[[Any, dict[str, Any], dict[str, Any]], tuple[str, dict[str, Any]]]] = {
    "view": _cmd_view,
    "view_list": _cmd_view_list,
    "comments": _cmd_comments,
    "comment_expand": _cmd_comment_expand,
    "images": _cmd_images,
    "frames": _cmd_frames,
    "share_frames": _cmd_share_frames,
    "share_images": _cmd_share_images,
    "share_link": _cmd_share_link,
    "search": _cmd_search,
    "feed": _cmd_feed,
    "more": _cmd_more,
    "screenshot": _cmd_screenshot,
    "drop": _cmd_drop,
    "login": _cmd_login,
}
