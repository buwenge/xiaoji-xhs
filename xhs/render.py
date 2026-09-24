"""dataclass → 小机读的文本。全是纯函数：喂 Note/Comment/ImageDesc 或
state 里存的 dict，不碰网络/文件。输出格式样例见设计文档第 5 节。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from xhs.parse import CommentPage, ListItem, Note, NoteImage
from xhs.video import fmt_clock
from xhs.vision import ImageDesc

TZ = ZoneInfo("Asia/Shanghai")

COMMENT_TEXT_LIMIT = 120
DEFAULT_COMMENT_LIMIT = 5
MORE_COMMENTS_HINT = "home 小红书 评论 10条"


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def render_note_header(note: Note) -> str:
    dt = datetime.fromtimestamp(note.time_ms / 1000, tz=TZ) if note.time_ms else None
    date_part = f" · {dt.strftime('%Y-%m-%d')}" if dt else ""
    return (
        f"《{note.title}》 {note.author} · 赞 {note.liked_count} 藏 {note.collected_count} "
        f"评 {note.comment_count}{date_part}"
    )


FRAMES_HINT = "home 小红书 抽帧 <想看什么、怎么抽，随便说>"


def render_video_line(note: Note) -> str:
    """视频笔记读图只看得到封面（9/24 用户追问后补的标注）：不说一声，
    小机会把封面描述当成整篇内容。"""
    if note.note_type != "video":
        return ""
    length = f" {fmt_clock(note.duration_s)}" if note.duration_s else ""
    return f"视频{length}（下面只有封面；想看画面：{FRAMES_HINT}）"


def render_images_block(images: list[NoteImage], descs: list[ImageDesc] | None, *, label: str = "图") -> str:
    """`descs is None` 对应"不读图"：只报张数，不逐张描述。系列跳读
    （S3.2）：同一个 `span`（比如 3–12 张连续的教程截图，模型只打开了
    首尾两张）只渲染一行 `3–12 [系列] …`，不逐张重复。"""
    if not images:
        return ""
    if descs is None:
        return f"{label} {len(images)} 张"
    lines = [f"{label} {len(images)} 张："]
    by_index = {desc.index: desc for desc in descs}
    rendered_spans: set[tuple[int, int]] = set()
    for image in images:
        desc = by_index.get(image.index)
        if desc is None:
            continue
        if desc.span is not None:
            if desc.span in rendered_spans:
                continue
            rendered_spans.add(desc.span)
            start, end = desc.span
            lines.append(f" {start}–{end} [{desc.kind}] {desc.text}")
        else:
            lines.append(f" {image.index} [{desc.kind}] {desc.text}")
    return "\n".join(lines)


def _sub_count_display(sub_count: int, sub_count_text: str) -> str:
    """详情页（S3）的楼中楼计数可能是 `"10+"`/`"1.2万"` 这种原样文本
    （`parse._parse_count` 产出），比整数下界更准确，展示优先用它；S1
    路径/手搭的 dict 没有这个字段时退回整数（2026-09-22 code-review
    指出 `sub_count_text` 一直没被用上）。"""
    return sub_count_text or str(sub_count)


def render_comments_block(page: CommentPage, limit: int = DEFAULT_COMMENT_LIMIT) -> str:
    if not page.comments:
        return f"评论 {page.total} 条" if page.total else "评论 0 条"
    shown = page.comments[: max(limit, 0)]
    lines = [f"评论 {page.total} 条，显示前 {len(shown)} 条（更多：{MORE_COMMENTS_HINT}）"]
    for comment in shown:
        text = _truncate(comment.text, COMMENT_TEXT_LIMIT)
        display = _sub_count_display(comment.sub_count, comment.sub_count_text)
        suffix = f"（楼中楼 {display} 条）" if comment.sub_count else ""
        lines.append(f" [{comment.index}] {comment.user}：{text}{suffix}")
    return "\n".join(lines)


def render_view(
    note: Note,
    page: CommentPage,
    descs: list[ImageDesc] | None,
    *,
    comment_limit: int = DEFAULT_COMMENT_LIMIT,
) -> str:
    parts = [render_note_header(note)]
    video_line = render_video_line(note)
    if video_line:
        parts.append(video_line)
    if note.desc:
        parts.append(note.desc)
    images_block = render_images_block(note.images, descs, label="封面" if video_line else "图")
    if images_block:
        parts.append(images_block)
    parts.append(render_comments_block(page, limit=comment_limit))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 基于 state.json 里 current_note dict 的后续命令（评论 N 条 / 评论 N 展开）
# ---------------------------------------------------------------------------


def render_comments_from_current(current_note: dict[str, Any], limit: int) -> str:
    """`limit > available`（S3.1 审查意见 1.1 之前）曾经是"S1 只预载了首屏
    评论，更多要等浏览器那期上线"——现在 `xhs.cli._cmd_comments` 在渲染
    之前已经尽力打开浏览器补过数据（未登录会直接抛 `XhsError` 说明原因，
    走不到这里）。但"尽力"不等于"一定拿全"：`browser.load_comments` 有
    轮次上限（`COMMENTS_SCROLL_MAX_ROUNDS`），撞到上限时可能已登录、没
    撞风控、也没被拒绝，就是单纯还没滚够——这种情况 `comments_has_more`
    仍是真、或者 `available` 仍小于 `total`，不能说"已经到底了"（monitor
    2026-09-22 code-review 抓到：原来只看"这次给的够不够 limit"就下结论，
    跟"是不是真的没有更多了"是两件事）。"""
    comments = current_note.get("comments") or []
    total = current_note.get("comment_total", len(comments))
    available = len(comments)
    has_more = bool(current_note.get("comments_has_more"))
    shown = comments[: max(min(limit, available), 0)]
    lines = [f"评论 {total} 条，显示前 {len(shown)} 条（更多：{MORE_COMMENTS_HINT}）"]
    for comment in shown:
        text = _truncate(str(comment.get("text") or ""), COMMENT_TEXT_LIMIT)
        sub_count = comment.get("sub_count") or 0
        display = _sub_count_display(sub_count, str(comment.get("sub_count_text") or ""))
        suffix = f"（楼中楼 {display} 条）" if sub_count else ""
        lines.append(f" [{comment.get('index')}] {comment.get('user')}：{text}{suffix}")
    if limit > available:
        if has_more or available < total:
            lines.append(f"这次只拿到 {available} 条，还有更多没取到")
        else:
            lines.append(f"已经到底了，一共 {available} 条")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# S3：搜索/首页列表
# ---------------------------------------------------------------------------

LIST_MORE_HINT = "home 小红书 看 3 / 更多"


def render_list_item(item: ListItem) -> str:
    suffix = "（视频）" if item.note_type == "video" else ""
    return f"[{item.index}] {item.title} · {item.author} · 赞 {item.liked_count}{suffix}"


def render_list(items: list[ListItem]) -> str:
    if not items:
        return "没有找到内容"
    lines = [render_list_item(item) for item in items]
    lines.append(LIST_MORE_HINT)
    return "\n".join(lines)


def render_comment_expand(current_note: dict[str, Any], index: int) -> str:
    """"楼中楼 N 条，要登录后才能看"（S3.1 审查意见 1.1 之前）曾经是静态
    文案——现在 `xhs.cli._cmd_comment_expand` 在渲染之前已经尽力打开
    浏览器展开过（未登录会直接抛 `XhsError` 说明原因，走不到这里）。
    `sub_count` 是站点给的权威总数（`parse._parse_count` 解出来的），
    `len(preview) < sub_count` 就一定意味着确实还有没展开到的（可能是
    `browser.expand_comment` 撞到 `COMMENT_EXPAND_MAX_CLICKS` 点击上限
    提前收手），不能说"已经到底了"——这条跟 `sub_count`/`preview` 都是
    权威数字不一样，不存在"看起来到底但其实没到"的歧义（monitor
    2026-09-22 code-review 指出 `render_comments_from_current` 那条要
    分情况，这条其实没有歧义，直接说"还有"更准确）。"""
    comments = current_note.get("comments") or []
    match = next((c for c in comments if c.get("index") == index), None)
    if match is None:
        return f"没有第 {index} 条评论"
    lines = [f"[{index}] {match.get('user')}：{match.get('text')}"]
    preview = match.get("sub_preview") or []
    sub_count = match.get("sub_count") or 0
    if not sub_count:
        lines.append(" 没有楼中楼")
        return "\n".join(lines)
    for sub in preview:
        lines.append(f"  └ {sub.get('user')}：{sub.get('text')}")
    if len(preview) < sub_count:
        lines.append(f"还有 {sub_count - len(preview)} 条没展开")
    return "\n".join(lines)
