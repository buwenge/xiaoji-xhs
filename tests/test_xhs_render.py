import unittest

from xhs.parse import Comment, CommentPage, ListItem, Note, NoteImage
from xhs.render import (
    render_comment_expand,
    render_comments_block,
    render_comments_from_current,
    render_images_block,
    render_list,
    render_list_item,
    render_note_header,
    render_view,
)
from xhs.vision import ImageDesc


def make_note(**overrides):
    base = dict(
        note_id="n1",
        title="标题",
        desc="正文第一行\n正文第二行",
        author="作者",
        liked_count="10",
        collected_count="5",
        comment_count="2",
        share_count="1",
        time_ms=1758470400000,  # 2025-09-21 (UTC 附近，具体日期只看不炸)
        images=[NoteImage(index=1, url="http://a/1.jpg"), NoteImage(index=2, url="http://a/2.jpg")],
    )
    base.update(overrides)
    return Note(**base)


class RenderNoteHeaderTests(unittest.TestCase):
    def test_includes_title_author_stats(self):
        note = make_note()
        header = render_note_header(note)
        self.assertIn("《标题》", header)
        self.assertIn("作者", header)
        self.assertIn("赞 10", header)
        self.assertIn("藏 5", header)
        self.assertIn("评 2", header)

    def test_zero_time_omits_date(self):
        note = make_note(time_ms=0)
        header = render_note_header(note)
        self.assertNotIn("·  ·", header)


class RenderImagesBlockTests(unittest.TestCase):
    def test_no_images_returns_empty_string(self):
        self.assertEqual(render_images_block([], None), "")

    def test_none_descs_means_count_only(self):
        images = [NoteImage(index=1, url="u1"), NoteImage(index=2, url="u2")]
        self.assertEqual(render_images_block(images, None), "图 2 张")

    def test_with_descs_lists_each_line(self):
        images = [NoteImage(index=1, url="u1"), NoteImage(index=2, url="u2")]
        descs = [
            ImageDesc(index=1, kind="照片", text="第一张", sha256="a"),
            ImageDesc(index=2, kind="文字卡", text="第二张要点", sha256="b"),
        ]
        block = render_images_block(images, descs)
        self.assertIn("图 2 张：", block)
        self.assertIn(" 1 [照片] 第一张", block)
        self.assertIn(" 2 [文字卡] 第二张要点", block)

    def test_series_span_renders_as_one_line(self):
        # 3 张同一个系列（S3.2 系列跳读），只应该出现一行 " 1–3 [系列] …"，
        # 不逐张重复。
        images = [NoteImage(index=i, url=f"u{i}") for i in (1, 2, 3)]
        descs = [
            ImageDesc(index=1, kind="系列", text="教程三步", sha256="a", span=(1, 3)),
            ImageDesc(index=2, kind="系列", text="教程三步", sha256="b", span=(1, 3)),
            ImageDesc(index=3, kind="系列", text="教程三步", sha256="c", span=(1, 3)),
        ]
        block = render_images_block(images, descs)
        self.assertIn(" 1–3 [系列] 教程三步", block)
        self.assertEqual(block.count("[系列]"), 1)

    def test_mixed_single_and_series(self):
        images = [NoteImage(index=i, url=f"u{i}") for i in (1, 2, 3, 4)]
        descs = [
            ImageDesc(index=1, kind="照片", text="封面", sha256="a"),
            ImageDesc(index=2, kind="系列", text="教程", sha256="b", span=(2, 4)),
            ImageDesc(index=3, kind="系列", text="教程", sha256="c", span=(2, 4)),
            ImageDesc(index=4, kind="系列", text="教程", sha256="d", span=(2, 4)),
        ]
        block = render_images_block(images, descs)
        self.assertIn(" 1 [照片] 封面", block)
        self.assertIn(" 2–4 [系列] 教程", block)
        self.assertEqual(block.count("[系列]"), 1)


class RenderCommentsBlockTests(unittest.TestCase):
    def test_shows_total_and_limit(self):
        comments = [
            Comment(index=i, id=str(i), user=f"用户{i}", text="内容" * i, sub_count=i)
            for i in range(1, 8)
        ]
        page = CommentPage(total=46, comments=comments)
        block = render_comments_block(page, limit=5)
        self.assertIn("评论 46 条，显示前 5 条", block)
        self.assertIn("home 小红书 评论 10条", block)
        self.assertIn("[1]", block)

    def test_sub_count_text_shown_when_present(self):
        # 详情页（S3）的楼中楼计数可能是 "10+"/"1.2万" 这种原样文本
        # （2026-09-22 code-review 指出 sub_count_text 一直没被用上）。
        comment = Comment(index=1, id="1", user="u", text="t", sub_count=10, sub_count_text="10+")
        page = CommentPage(total=1, comments=[comment])
        block = render_comments_block(page)
        self.assertIn("楼中楼 10+ 条", block)

    def test_sub_count_falls_back_to_int_when_text_missing(self):
        comment = Comment(index=1, id="1", user="u", text="t", sub_count=3)
        page = CommentPage(total=1, comments=[comment])
        block = render_comments_block(page)
        self.assertIn("楼中楼 3 条", block)
        self.assertNotIn("[6]", block)

    def test_sub_count_zero_has_no_suffix(self):
        page = CommentPage(total=1, comments=[Comment(index=1, id="1", user="u", text="t", sub_count=0)])
        block = render_comments_block(page, limit=5)
        self.assertNotIn("楼中楼", block)

    def test_long_comment_truncated_with_ellipsis(self):
        long_text = "字" * 200
        page = CommentPage(total=1, comments=[Comment(index=1, id="1", user="u", text=long_text, sub_count=0)])
        block = render_comments_block(page, limit=5)
        self.assertIn("…", block)
        self.assertNotIn("字" * 121, block)

    def test_empty_comments_reports_zero(self):
        page = CommentPage(total=0, comments=[])
        self.assertEqual(render_comments_block(page), "评论 0 条")


class RenderViewTests(unittest.TestCase):
    def test_assembles_header_desc_images_comments_in_order(self):
        note = make_note()
        page = CommentPage(total=1, comments=[Comment(index=1, id="1", user="u", text="t", sub_count=0)])
        output = render_view(note, page, None)
        lines = output.split("\n")
        self.assertTrue(lines[0].startswith("《标题》"))
        self.assertIn("正文第一行", output)
        self.assertIn("图 2 张", output)
        self.assertIn("评论 1 条", output)

    def test_no_desc_no_images_still_renders_comments(self):
        note = make_note(desc="", images=[])
        page = CommentPage(total=0, comments=[])
        output = render_view(note, page, None)
        self.assertIn("评论 0 条", output)


class RenderFromCurrentNoteTests(unittest.TestCase):
    def _current(self):
        return {
            "title": "标题",
            "comment_total": 46,
            "comments": [
                {"index": 1, "user": "u1", "text": "t1", "sub_count": 4, "sub_preview": [{"user": "a", "text": "b"}, {"user": "c", "text": "d"}]},
                {"index": 2, "user": "u2", "text": "t2", "sub_count": 0, "sub_preview": []},
            ],
        }

    def test_render_comments_from_current_uses_stored_total(self):
        block = render_comments_from_current(self._current(), limit=5)
        self.assertIn("评论 46 条，显示前 2 条", block)

    def test_render_comments_from_current_notes_when_more_requested_than_available(self):
        # S3.1 审查意见 1.1 + 2026-09-22 code-review 修复：静态"首屏
        # 评论"文案改成按数据说话，但"这次给的没 limit 多"不等于"真的到底
        # 了"——`_current()` 的 comment_total(46) 远大于 available(2)，
        # 该说"还有更多没取到"，不能说"已经到底了"。
        block = render_comments_from_current(self._current(), limit=20)
        self.assertIn("这次只拿到 2 条，还有更多没取到", block)

    def test_render_comments_from_current_reports_bottom_when_total_matches(self):
        current = {
            "title": "标题",
            "comment_total": 2,
            "comments_has_more": False,
            "comments": [
                {"index": 1, "user": "u1", "text": "t1", "sub_count": 0, "sub_preview": []},
                {"index": 2, "user": "u2", "text": "t2", "sub_count": 0, "sub_preview": []},
            ],
        }
        block = render_comments_from_current(current, limit=20)
        self.assertIn("已经到底了，一共 2 条", block)

    def test_render_comment_expand_with_preview_and_remaining(self):
        output = render_comment_expand(self._current(), 1)
        self.assertIn("还有 2 条没展开", output)
        self.assertIn("a：b", output)

    def test_render_comment_expand_no_sub_comments(self):
        output = render_comment_expand(self._current(), 2)
        self.assertIn("没有楼中楼", output)

    def test_render_comments_from_current_shows_sub_count_text(self):
        current = {
            "title": "标题",
            "comment_total": 1,
            "comments": [{"index": 1, "user": "u1", "text": "t1", "sub_count": 10, "sub_count_text": "10+", "sub_preview": []}],
        }
        block = render_comments_from_current(current, limit=1)
        self.assertIn("楼中楼 10+ 条", block)

    def test_render_comment_expand_no_preview_reports_remaining(self):
        # S3.1 审查意见 1.1：preview 为空但 sub_count>0 时按数据说话，不再说
        # "要登录后才能看"；sub_count 是站点权威总数，preview<sub_count
        # 就是确实还有没展开到的，说"还有"不说"已经到底了"。
        current = {
            "title": "标题",
            "comment_total": 1,
            "comments": [{"index": 1, "user": "u1", "text": "t1", "sub_count": 10, "sub_count_text": "10+", "sub_preview": []}],
        }
        output = render_comment_expand(current, 1)
        self.assertIn("还有 10 条没展开", output)

    def test_render_comment_expand_missing_index(self):
        output = render_comment_expand(self._current(), 99)
        self.assertIn("没有第 99 条评论", output)


class RenderListTests(unittest.TestCase):
    def _item(self, **overrides):
        base = dict(index=1, id="n1", xsec_token="t1", title="标题", note_type="normal", author="作者", liked_count="10")
        base.update(overrides)
        return ListItem(**base)

    def test_render_list_item_plain(self):
        line = render_list_item(self._item())
        self.assertEqual(line, "[1] 标题 · 作者 · 赞 10")

    def test_render_list_item_video_has_suffix(self):
        line = render_list_item(self._item(note_type="video"))
        self.assertTrue(line.endswith("（视频）"))

    def test_render_list_joins_items_and_hint(self):
        block = render_list([self._item(index=1), self._item(index=2, title="标题2")])
        lines = block.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("[1]"))
        self.assertTrue(lines[1].startswith("[2]"))
        self.assertIn("home 小红书 看 3 / 更多", lines[2])

    def test_render_list_empty(self):
        self.assertEqual(render_list([]), "没有找到内容")


if __name__ == "__main__":
    unittest.main()
