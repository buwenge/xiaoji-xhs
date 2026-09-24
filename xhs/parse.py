"""state JSON → dataclass，纯函数（喂 dict，不碰网络/文件）。字段路径以
`tests/fixtures/xhs_note_state.json`（S0 真实抓取，README 有来源说明）为
准，不是凭空猜的形状。

一个真实存在的坑：笔记作者的字段是 `noteData.data.noteData.user.nickName`
（大写 N），评论楼层的字段是 `comments[i].user.nickname`（小写 n）——两处
不是同一个 key，写测试锁住，免得以后有人"顺手"改成一个函数复用两处。

S3（浏览器详情页 `note.noteDetailMap[id]`）跟 S1（`fetch_light` 抽出来的
`noteData.data.noteData`）字段基本一致，只有作者字段大小写不同
（`user.nickname` vs `user.nickName`）——`parse_note`/`parse_note_from_detail`
共用同一个 `_build_note()`，靠一处 `or` 兼容两种大小写，不复制第二份。
评论字段两边差异更大（见 `parse_comments_from_detail` 的注释），各自一份
解析函数，产出同一套 `Comment`/`CommentPage`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class NoteImage:
    index: int
    url: str


@dataclass(frozen=True)
class Note:
    note_id: str
    title: str
    desc: str
    author: str
    liked_count: str
    collected_count: str
    comment_count: str
    share_count: str
    time_ms: int
    images: list[NoteImage] = field(default_factory=list)
    # 视频笔记（`type == "video"`）：`imageList` 只有一张封面，视频本体在
    # `video.media.stream` 里——读图从来只碰封面；`抽帧` 命令才用 video_url
    # 按时间点截几帧（9/24）。普通图文笔记这三项保持默认。
    note_type: str = "normal"
    duration_s: int = 0
    video_url: str = ""


@dataclass(frozen=True)
class SubComment:
    user: str
    text: str


@dataclass(frozen=True)
class Comment:
    index: int
    id: str
    user: str
    text: str
    sub_count: int
    sub_preview: list[SubComment] = field(default_factory=list)
    # 详情页（S3）的 likeCount/subCommentCount 是字符串且可能带 "+"/"万"
    # （比如 "10+"、"1.2万"）——sub_count 保留整数下界供排序/判断"有没有"，
    # sub_count_text 是原样展示文本；render.py 显示用 text，判断"有没有
    # 楼中楼"用 sub_count（int）。S1 路径两者相同（都是 str(sub_count)）。
    sub_count_text: str = ""


@dataclass(frozen=True)
class CommentPage:
    total: int
    comments: list[Comment] = field(default_factory=list)
    has_more: bool = False


@dataclass(frozen=True)
class ListItem:
    """搜索结果 / 首页推荐的一条：两者的 state 形状都是同一个 `noteCard`
    （见 `xhs_browser_state.README.md`），共用同一个解析函数，不写两份。
    """

    index: int
    id: str
    xsec_token: str
    title: str
    note_type: str
    author: str
    liked_count: str


def _note_data(state: dict) -> dict:
    try:
        data = state["noteData"]["data"]["noteData"]
    except (KeyError, TypeError) as exc:
        raise ValueError("笔记数据缺 noteData.data.noteData") from exc
    if not isinstance(data, dict):
        raise ValueError("noteData.data.noteData 形状不对")
    return data


def _comment_data(state: dict) -> dict:
    try:
        data = state["noteData"]["data"]["commentData"]
    except (KeyError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _image_url(image: dict) -> str:
    """S1（`fetch_light` 抽的手机页 HTML）的 `imageList[i].url` 本身就有
    值；S3（PC 详情页 `note.noteDetailMap`）同一个字段是空字符串，真正的
    图片地址在 `urlDefault` 或 `infoList[].url`（优先 `WB_DFT`，找不到就
    用第一条）——不是同一个站点两次实现，是同一个字段在移动端/PC 端接口
    里给的值不一样，两边都要认。"""
    url = str(image.get("url") or "")
    if url:
        return url
    url = str(image.get("urlDefault") or "")
    if url:
        return url
    info_list = image.get("infoList") or []
    fallback = ""
    for info in info_list:
        if not isinstance(info, dict):
            continue
        candidate = str(info.get("url") or "")
        if not candidate:
            continue
        if not fallback:
            fallback = candidate
        if info.get("imageScene") == "WB_DFT":
            return candidate
    return fallback


def _video_url(video: dict) -> str:
    """优先 h264（ffmpeg 解码最稳），没有再用 h265；每路先 `masterUrl`，
    空了用第一条 `backupUrls`。地址带签名、会过期，`抽帧` 时会重新取一次。"""
    stream = ((video.get("media") or {}).get("stream")) or {}
    for codec in ("h264", "h265", "av1"):
        for entry in stream.get(codec) or []:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("masterUrl") or "")
            if not url:
                backups = entry.get("backupUrls") or []
                url = str(backups[0]) if backups else ""
            if url:
                return url
    return ""


def _video_duration(video: dict) -> int:
    duration = _as_int((video.get("capa") or {}).get("duration"))
    if duration:
        return duration
    stream = ((video.get("media") or {}).get("stream")) or {}
    for entries in stream.values():
        for entry in entries or []:
            if isinstance(entry, dict) and _as_int(entry.get("duration")):
                return _as_int(entry.get("duration")) // 1000
    return 0


def _build_note(note_data: dict) -> Note:
    """S1（`noteData.data.noteData`）和 S3 详情页（`noteDetailMap[id].note`）
    字段基本一致，唯一差异是作者字段大小写（`nickName` / `nickname`）——
    两边都认，不复制第二份 `parse_note_from_detail` 专属逻辑。"""
    interact = note_data.get("interactInfo") or {}
    user = note_data.get("user") or {}
    video = note_data.get("video") or {}
    images: list[NoteImage] = []
    for position, image in enumerate(note_data.get("imageList") or [], start=1):
        if not isinstance(image, dict):
            continue
        images.append(NoteImage(index=position, url=_image_url(image)))
    return Note(
        note_id=str(note_data.get("noteId") or ""),
        title=str(note_data.get("title") or ""),
        desc=str(note_data.get("desc") or ""),
        author=str(user.get("nickName") or user.get("nickname") or ""),
        liked_count=str(interact.get("likedCount") or "0"),
        collected_count=str(interact.get("collectedCount") or "0"),
        comment_count=str(interact.get("commentCount") or "0"),
        share_count=str(interact.get("shareCount") or "0"),
        time_ms=_as_int(note_data.get("time")),
        images=images,
        note_type=str(note_data.get("type") or "normal"),
        duration_s=_video_duration(video),
        video_url=_video_url(video),
    )


def parse_note(state: dict) -> Note:
    return _build_note(_note_data(state))


def parse_note_from_detail(entry: dict) -> Note:
    """`entry` 是 `note.noteDetailMap[note_id]`（`browser.read_state` 已经
    解过 ref），形状 `{currentTime, note, comments}`。"""
    note_data = entry.get("note") if isinstance(entry, dict) else None
    if not isinstance(note_data, dict):
        raise ValueError("详情数据缺 note 字段")
    return _build_note(note_data)


_COUNT_UNIT_RE = re.compile(r"^([\d.]+)\s*万\+?$")


def _parse_count(value: object) -> tuple[int, str]:
    """小红书的计数展示格式：普通整数、`"10+"`、`"1.2万"`、`"1.2万+"`。
    返回 `(下界整数, 原样展示文本)`——`int("10+")` 会直接抛异常，
    `_as_int("10+")` 会静默变成 0（详情页 fixture 里大量出现这种值，
    S3 现场补充明确要求处理，不能吞成 0）。"""
    if value is None:
        return 0, "0"
    text = str(value).strip()
    if not text:
        return 0, "0"
    unit_match = _COUNT_UNIT_RE.match(text)
    if unit_match:
        core, multiplier = unit_match.group(1), 10000
    else:
        core, multiplier = (text[:-1] if text.endswith("+") else text), 1
    try:
        return int(float(core) * multiplier), text
    except ValueError:
        return 0, text


def _parse_sub_comments(raw: object) -> list[SubComment]:
    result: list[SubComment] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        user = (item.get("user") or {}).get("nickname") or ""
        result.append(SubComment(user=str(user), text=str(item.get("content") or "")))
    return result


def parse_comments(state: dict) -> CommentPage:
    comment_data = _comment_data(state)
    raw_comments = comment_data.get("comments") or []
    comments: list[Comment] = []
    for position, item in enumerate(raw_comments, start=1):
        if not isinstance(item, dict):
            continue
        user = (item.get("user") or {}).get("nickname") or ""
        sub_count = _as_int(item.get("subCommentCount"))
        comments.append(
            Comment(
                index=position,
                id=str(item.get("id") or ""),
                user=str(user),
                text=str(item.get("content") or ""),
                sub_count=sub_count,
                sub_preview=_parse_sub_comments(item.get("subComments")),
                sub_count_text=str(sub_count),
            )
        )
    total = _as_int(comment_data.get("commentCount"))
    return CommentPage(total=total, comments=comments)


def _parse_sub_comments_detail(raw: object) -> list[SubComment]:
    """详情页楼中楼预载：`{content, userInfo:{nickname}, ...}`，字段名跟
    S1 的 `{content, user:{nickname}}` 不同（`user` vs `userInfo`），但产出
    同一个 `SubComment`。"""
    result: list[SubComment] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        user = (item.get("userInfo") or {}).get("nickname") or ""
        result.append(SubComment(user=str(user), text=str(item.get("content") or "")))
    return result


def comment_from_detail_item(item: dict, index: int) -> Comment:
    """详情页 `comments.list[i]`（或展开楼中楼后重读的同一条）→ `Comment`。
    `parse_comments_from_detail` 的循环体和 `browser.expand_comment` 的
    收尾原来各抄一份（维护者 S3.1 审查：复制第二次就抽共用）。"""
    user = (item.get("userInfo") or {}).get("nickname") or ""
    sub_count, sub_count_text = _parse_count(item.get("subCommentCount"))
    return Comment(
        index=index,
        id=str(item.get("id") or ""),
        user=str(user),
        text=str(item.get("content") or ""),
        sub_count=sub_count,
        sub_preview=_parse_sub_comments_detail(item.get("subComments")),
        sub_count_text=sub_count_text,
    )


def parse_comments_from_detail(entry: dict) -> CommentPage:
    """`entry` 是 `note.noteDetailMap[note_id]`。`entry["comments"]` 形状
    `{list, cursor, hasMore, loading, firstRequestFinish}`，每条字段名跟
    S1 的 `commentData.comments` 不同：`userInfo.nickname`（S1 `user.nickname`）、
    `createTime`（S1 `time`，本函数暂不产出，`Comment` 没有时间字段）、
    `likeCount`/`subCommentCount` 是字符串且可能带 `+`（S1 是 int）、
    `subComments` 预载 1 条（S1 预载 2–3 条）。

    详情页 state 本身不单独给评论总数，真正的总数在
    `entry["note"]["interactInfo"]["commentCount"]`（`_build_note` 取的
    是同一个字段）——`entry` 里 "note" 跟 "comments" 本来就是同一份数据
    的两半，两边形状在这一个函数里对齐，不指望调用方事后拿 `note.
    comment_count` 覆盖（2026-09-22 /simplify altitude 审查指出：原来
    只在注释里写"由 browser.py 覆盖"，但 browser.py 从没真的做过这一
    步，导致 `看 N` 落地的 `comment_total` 一直是"这一页拿到几条"而不是
    真实总数）。"""
    comments_data = entry.get("comments") if isinstance(entry, dict) else None
    note_data = entry.get("note") if isinstance(entry, dict) else None
    # `commentCount` 跟 `subCommentCount` 是同一种"可能带 +/万"的展示格式
    # （`xhs_feed_feeds.json` 里同一个 interactInfo 也有 "1.3万" 这种
    # likedCount），用 `_as_int` 会跟本函数原本想修的那个坑一模一样
    # 静默变 0——必须用 `_parse_count`（2026-09-22 code-review 抓到：
    # 上一次修复只堵了"字面量 3277"这一种形状，没堵住"1.2万"这种）。
    total, _ = _parse_count(((note_data or {}).get("interactInfo") or {}).get("commentCount"))
    if not isinstance(comments_data, dict):
        return CommentPage(total=total, comments=[])
    raw_comments = comments_data.get("list") or []
    comments: list[Comment] = []
    for position, item in enumerate(raw_comments, start=1):
        if not isinstance(item, dict):
            continue
        comments.append(comment_from_detail_item(item, position))
    has_more = bool(comments_data.get("hasMore"))
    return CommentPage(total=total, comments=comments, has_more=has_more)


def parse_list_items(raw_items: object, start_index: int = 1) -> list[ListItem]:
    """搜索结果（`search.feeds`）和首页推荐（`feed.feeds`）共用同一个
    `noteCard` 形状（`xhs_browser_state.README.md` 已确认），不写两份。
    `start_index`：`更多` 翻页时序号接着已有列表往后编，不是每页都从
    1 开始。

    登录态真机核对（S3.2 现场补充）发现搜索结果里混着非笔记卡片（热搜词/
    广告位：无标题、无作者、赞 0），字段是 `modelType` 不是 `"note"`、
    `id`/`xsecToken` 常常也是空的——这种直接跳过，不占序号（过滤后序号
    连续，不留空位）。`displayTitle` 为空但确实是笔记（多为无标题视频）
    的保留，标题显示"（无标题）"，跟"这条根本不是笔记"分开处理。"""
    items: list[ListItem] = []
    position = start_index
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        if item.get("modelType") != "note":
            continue
        item_id = str(item.get("id") or "")
        xsec_token = str(item.get("xsecToken") or "")
        if not item_id or not xsec_token:
            continue
        card = item.get("noteCard") or {}
        user = card.get("user") or {}
        interact = card.get("interactInfo") or {}
        items.append(
            ListItem(
                index=position,
                id=item_id,
                xsec_token=xsec_token,
                title=str(card.get("displayTitle") or "") or "（无标题）",
                note_type=str(card.get("type") or "normal"),
                author=str(user.get("nickname") or user.get("nickName") or ""),
                liked_count=str(interact.get("likedCount") or "0"),
            )
        )
        position += 1
    return items
