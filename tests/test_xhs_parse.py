import json
import unittest
from pathlib import Path

from xhs import parse

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "xhs_note_state.json"
REAL_STATE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
# README 记的真实数字：笔记 7 张图、正文 908 字、评论总数 46、首屏顶层
# 5 条、第一条楼中楼 4 条（预载 3 条）。


class ParseNoteTests(unittest.TestCase):
    def test_title_and_id(self):
        note = parse.parse_note(REAL_STATE)
        self.assertEqual(note.title, "把小红书 MCP 搬上云端服务器")
        self.assertEqual(note.note_id, "6aae7f7300000000290165a0")

    def test_author_uses_capital_nick_name_field(self):
        # 笔记作者字段是 user.nickName（大写 N），跟评论楼层的
        # user.nickname（小写 n）不是同一个 key。
        note = parse.parse_note(REAL_STATE)
        self.assertEqual(note.author, "路人17")

    def test_image_count_and_order(self):
        note = parse.parse_note(REAL_STATE)
        self.assertEqual(len(note.images), 7)
        self.assertEqual([img.index for img in note.images], [1, 2, 3, 4, 5, 6, 7])
        self.assertTrue(all(img.url.startswith("http") for img in note.images))

    def test_interact_counts_are_strings(self):
        note = parse.parse_note(REAL_STATE)
        self.assertEqual(note.liked_count, "266")
        self.assertEqual(note.collected_count, "220")
        self.assertEqual(note.comment_count, "46")

    def test_missing_note_data_raises(self):
        with self.assertRaises(ValueError):
            parse.parse_note({"noteData": {"data": {}}})


class ParseCommentsTests(unittest.TestCase):
    def test_total_and_top_level_count(self):
        page = parse.parse_comments(REAL_STATE)
        self.assertEqual(page.total, 46)
        self.assertEqual(len(page.comments), 5)

    def test_comment_user_uses_lowercase_nickname_field(self):
        page = parse.parse_comments(REAL_STATE)
        self.assertEqual(page.comments[0].user, "路人17")

    def test_first_comment_sub_count_and_preview(self):
        page = parse.parse_comments(REAL_STATE)
        first = page.comments[0]
        self.assertEqual(first.sub_count, 4)
        self.assertEqual(len(first.sub_preview), 3)
        self.assertTrue(first.sub_preview[0].text.startswith("无法登录跳验证码的一个笨办法"))

    def test_comment_indexes_are_1_based_in_order(self):
        page = parse.parse_comments(REAL_STATE)
        self.assertEqual([c.index for c in page.comments], [1, 2, 3, 4, 5])

    def test_missing_comment_data_returns_empty_page(self):
        page = parse.parse_comments({"noteData": {"data": {}}})
        self.assertEqual(page.total, 0)
        self.assertEqual(page.comments, [])

    def test_sub_count_text_mirrors_int_on_s1_path(self):
        # S1 光路径两者相同（设计文档 S3 现场补充原话）。
        page = parse.parse_comments(REAL_STATE)
        first = page.comments[0]
        self.assertEqual(first.sub_count_text, str(first.sub_count))


# ---------------------------------------------------------------------------
# S3：浏览器路径 fixture（真实站点片段，来源见 xhs_browser_state.README.md）
# ---------------------------------------------------------------------------

FEED_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "xhs_feed_feeds.json"
DETAIL_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "xhs_note_detail.json"
FEED_FEEDS = json.loads(FEED_FIXTURE_PATH.read_text(encoding="utf-8"))
NOTE_DETAIL = json.loads(DETAIL_FIXTURE_PATH.read_text(encoding="utf-8"))
DETAIL_NOTE_ID = "6a6ebc3f00000000050284d7"
DETAIL_ENTRY = NOTE_DETAIL["noteDetailMap"][DETAIL_NOTE_ID]


class ParseCountTests(unittest.TestCase):
    def test_plain_int(self):
        self.assertEqual(parse._parse_count(7), (7, "7"))

    def test_plus_suffix(self):
        self.assertEqual(parse._parse_count("10+"), (10, "10+"))

    def test_wan_unit(self):
        self.assertEqual(parse._parse_count("1.2万"), (12000, "1.2万"))

    def test_wan_unit_with_plus(self):
        self.assertEqual(parse._parse_count("1.2万+"), (12000, "1.2万+"))

    def test_none_defaults_to_zero(self):
        self.assertEqual(parse._parse_count(None), (0, "0"))

    def test_garbage_defaults_to_zero_but_keeps_text(self):
        self.assertEqual(parse._parse_count("未知"), (0, "未知"))


class ParseListItemsTests(unittest.TestCase):
    def test_shared_shape_for_feed_and_search(self):
        # 搜索项与 feed 项是同一个 noteCard 形状（README 已确认），共用
        # 同一个 parse_list_items，不写两份。
        items = parse.parse_list_items(FEED_FEEDS)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].index, 1)
        self.assertEqual(items[0].id, "6a6ebc3f00000000050284d7")
        self.assertEqual(items[0].title, "假如你手握200万存款，你会怎么办？")
        self.assertEqual(items[0].author, "路人13")
        self.assertEqual(items[0].liked_count, "517")
        self.assertEqual(items[0].note_type, "normal")

    def test_video_type_item(self):
        items = parse.parse_list_items(FEED_FEEDS)
        video_items = [item for item in items if item.note_type == "video"]
        self.assertEqual(len(video_items), 1)

    def test_empty_or_missing_feeds(self):
        self.assertEqual(parse.parse_list_items([]), [])
        self.assertEqual(parse.parse_list_items(None), [])

    def test_start_index_continues_numbering_for_more_pagination(self):
        items = parse.parse_list_items(FEED_FEEDS[1:], start_index=5)
        self.assertEqual([item.index for item in items], [5, 6])


# 仿真非真实抓取：登录态真机核对（S3.2 现场补充）发现搜索结果里混着热搜词/
# 广告位这类非笔记卡片（`modelType` 不是 `"note"`，标题/作者/id/xsecToken
# 常常也是空的）——手工造一条最小形状即可验证过滤，不改动
# `xhs_feed_feeds.json`（那是有 README 记录来源的真实抓取，保持纯净）。
NON_NOTE_CARD = {
    "id": "",
    "modelType": "rec_query",
    "trackId": "fake-rec-query",
    "index": 99,
}

# 同样是仿真：无标题视频（`displayTitle` 空但确实是笔记，id/xsecToken 都在）。
BLANK_TITLE_NOTE_CARD = {
    "id": "fake0000000000000000001",
    "modelType": "note",
    "xsecToken": "fake-token-abc",
    "noteCard": {
        "displayTitle": "",
        "type": "video",
        "user": {"nickname": "无标题作者"},
        "interactInfo": {"likedCount": "3"},
    },
}


class ParseListItemsFiltersNonNoteCardsTests(unittest.TestCase):
    def test_non_note_card_is_skipped_and_does_not_consume_a_slot(self):
        items = parse.parse_list_items(FEED_FEEDS + [NON_NOTE_CARD])
        self.assertEqual(len(items), 3)  # 第 4 条（非笔记）被过滤，序号不留空位
        self.assertEqual([item.index for item in items], [1, 2, 3])

    def test_blank_title_note_shown_as_placeholder(self):
        items = parse.parse_list_items([BLANK_TITLE_NOTE_CARD])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "（无标题）")

    def test_missing_id_or_xsec_token_is_skipped_even_if_modeltype_note(self):
        broken = {"id": "", "modelType": "note", "xsecToken": "", "noteCard": {}}
        self.assertEqual(parse.parse_list_items([broken]), [])


class ParseNoteFromDetailTests(unittest.TestCase):
    def test_matches_s1_shape_except_author_case(self):
        note = parse.parse_note_from_detail(DETAIL_ENTRY)
        self.assertEqual(note.note_id, DETAIL_NOTE_ID)
        self.assertEqual(note.title, "假如你手握200万存款，你会怎么办？")
        self.assertEqual(note.author, "路人13")  # 详情页是小写 nickname
        self.assertEqual(note.comment_count, "3277")

    def test_image_url_falls_back_to_info_list_when_url_field_empty(self):
        # PC 详情页 imageList[i].url 是空字符串，真实地址在 infoList
        # （优先 WB_DFT）——跟 S1 手机页 HTML 直接有 url 不是同一个坑，
        # 但两边都要能拿到能下载的地址。
        note = parse.parse_note_from_detail(DETAIL_ENTRY)
        self.assertEqual(len(note.images), 1)
        self.assertTrue(note.images[0].url.startswith("http"))
        self.assertIn("nd_dft_wlteh_webp", note.images[0].url)

    def test_missing_note_raises(self):
        with self.assertRaises(ValueError):
            parse.parse_note_from_detail({"comments": {}})


class ParseCommentsFromDetailTests(unittest.TestCase):
    def test_field_name_differences_from_s1(self):
        page = parse.parse_comments_from_detail(DETAIL_ENTRY)
        self.assertEqual(len(page.comments), 3)
        self.assertEqual(page.comments[0].user, "路人16")  # userInfo.nickname
        self.assertTrue(page.has_more)

    def test_total_uses_note_comment_count_not_page_length(self):
        # 详情页这一页只预载 3 条，但真实总数（note.interactInfo.
        # commentCount）是 3277——total 不能是 len(comments)（2026-09-22
        # /simplify altitude 审查抓到的坑：原来注释说"由 browser.py 覆盖"
        # 但从没真的覆盖过）。
        page = parse.parse_comments_from_detail(DETAIL_ENTRY)
        self.assertEqual(page.total, 3277)
        self.assertNotEqual(page.total, len(page.comments))

    def test_total_handles_wan_unit_without_zeroing(self):
        # commentCount 也可能是 "1.2万" 这种格式（同一个 interactInfo 形状
        # 下 likedCount 已经真实出现过"1.3万"，见 xhs_feed_feeds.json）；
        # 用 _as_int 会跟 subCommentCount 那个坑一样静默变 0
        # （2026-09-22 code-review 抓到：上一次修复只堵了纯数字这一种
        # 形状）。
        entry = {
            "note": {"interactInfo": {"commentCount": "1.2万"}},
            "comments": {"list": [], "hasMore": False},
        }
        page = parse.parse_comments_from_detail(entry)
        self.assertEqual(page.total, 12000)

    def test_sub_count_with_plus_suffix_is_not_zero(self):
        # _as_int("10+") 会静默变成 0——这是 S3 现场补充明确要求避免的坑。
        page = parse.parse_comments_from_detail(DETAIL_ENTRY)
        first = page.comments[0]
        self.assertEqual(first.sub_count, 10)
        self.assertEqual(first.sub_count_text, "10+")

    def test_second_comment_sub_count_without_plus(self):
        page = parse.parse_comments_from_detail(DETAIL_ENTRY)
        second = page.comments[1]
        self.assertEqual(second.sub_count, 7)
        self.assertEqual(second.sub_count_text, "7")

    def test_third_comment_no_more_sub_comments(self):
        page = parse.parse_comments_from_detail(DETAIL_ENTRY)
        third = page.comments[2]
        self.assertEqual(third.sub_count, 1)
        self.assertEqual(third.sub_count_text, "1")

    def test_sub_preview_uses_user_info_nickname(self):
        page = parse.parse_comments_from_detail(DETAIL_ENTRY)
        first = page.comments[0]
        self.assertEqual(len(first.sub_preview), 1)
        self.assertEqual(first.sub_preview[0].user, "路人8")

    def test_missing_comments_returns_empty_page(self):
        page = parse.parse_comments_from_detail({"note": {}})
        self.assertEqual(page.total, 0)
        self.assertEqual(page.comments, [])
        self.assertFalse(page.has_more)


if __name__ == "__main__":
    unittest.main()
