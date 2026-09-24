import contextlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import home
from xhs import XhsError, browser, budget, cli, fetch_light, parse, paths, share, vision
from xhs import state as state_store

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "xhs_note_state.json"
REAL_STATE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
REAL_HTML = f"<html><script>window.__INITIAL_STATE__={FIXTURE_PATH.read_text(encoding='utf-8')}</script></html>"
FINAL_URL = "https://www.xiaohongshu.com/discovery/item/6aae7f7300000000290165a0?xsec_token=REALTOKEN&xsec_source=pc"

TZ = ZoneInfo("Asia/Shanghai")


def req(argv):
    return home.parse_request(["小红书", *argv])


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.xhs_dir = Path(self.tempdir.name)
        self.paths_patcher = patch.object(paths, "XHS_DIR", self.xhs_dir)
        self.paths_patcher.start()
        self.addCleanup(self.paths_patcher.stop)

        # 不读图：view 测试默认打桩 vision，让它什么都不做、也不联网。
        self.vision_patcher = patch.object(
            vision, "describe_images", return_value=[]
        )
        self.mocked_vision = self.vision_patcher.start()
        self.addCleanup(self.vision_patcher.stop)

        self.fetch_patcher = patch.object(
            fetch_light, "resolve_and_fetch", return_value=(FINAL_URL, REAL_HTML)
        )
        self.mocked_fetch = self.fetch_patcher.start()
        self.addCleanup(self.fetch_patcher.stop)


class HelpTests(unittest.TestCase):
    def test_help_has_output_and_does_not_touch_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with patch.object(paths, "XHS_DIR", Path(tempdir)):
                output = cli.handle(req(["--help"]))
                self.assertIn("小红书", output)
                self.assertIn("读图 3 仔细", output)
                self.assertFalse(paths.state_file().exists())


class TodayTests(unittest.TestCase):
    def test_today_does_not_count_or_save(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with patch.object(paths, "XHS_DIR", Path(tempdir)):
                output = cli.handle(req(["今天"]))
                self.assertIn("token", output)
                self.assertFalse(paths.state_file().exists())

    def test_today_reflects_existing_tokens_without_mutating_saved_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with patch.object(paths, "XHS_DIR", Path(tempdir)):
                paths.ensure_dirs()
                # 用 cli 自己的时区算"今天"：写死日期会在跨零点后被 reset_if_new_day 归零（9/23 凌晨假红）
                today = budget.today_str(datetime.now(cli.TZ))
                state_store.save({**state_store.DEFAULT_STATE, "day": today, "tokens_today": 12345})
                output = cli.handle(req(["今天"]))
                self.assertIn("12345", output)


class UnknownCommandTests(unittest.TestCase):
    def test_unrecognized_text_raises_xhs_error(self):
        # cli.handle 只抛 XhsError，不 import home.HomeError——两者转换
        # 交给 home.py 自己的 xhs 分支做（同名类跨模块身份不一致的坑，见
        # xhs/cli.py 模块顶部注释）。
        with tempfile.TemporaryDirectory() as tempdir:
            with patch.object(paths, "XHS_DIR", Path(tempdir)):
                with self.assertRaises(XhsError):
                    cli.handle(req(["瞎说八道"]))


class ViewCommandTests(CliTestCase):
    def test_view_by_link_returns_title_and_sets_current_note(self):
        output = cli.handle(req(["看", "https://xhslink.cn/o/AbCdEf12345"]))
        self.assertIn("把小红书 MCP 搬上云端服务器", output)
        self.assertIn("评论 46 条", output)
        state = state_store.load()
        self.assertEqual(state["current_note"]["id"], "6aae7f7300000000290165a0")
        self.assertEqual(state["current_note"]["xsec_token"], "REALTOKEN")
        self.assertEqual(len(state["current_note"]["comments"]), 5)
        self.mocked_vision.assert_called_once()

    def test_bare_link_without_prefix_also_routes_to_view(self):
        output = cli.handle(req(["https://xhslink.cn/o/AbCdEf12345"]))
        self.assertIn("把小红书 MCP 搬上云端服务器", output)

    def test_no_images_flag_skips_vision_call(self):
        cli.handle(req(["看", "https://xhslink.cn/o/AbCdEf12345", "不读图"]))
        self.mocked_vision.assert_not_called()

    def test_view_counts_tokens(self):
        cli.handle(req(["看", "https://xhslink.cn/o/AbCdEf12345"]))
        state = state_store.load()
        self.assertGreater(state["tokens_today"], 0)
        self.assertEqual(state["day"], datetime.now(TZ).date().isoformat())


class CommentsCommandTests(CliTestCase):
    def _view_first(self):
        cli.handle(req(["看", "https://xhslink.cn/o/AbCdEf12345"]))

    def test_comments_without_current_note_errors(self):
        with self.assertRaises(XhsError):
            cli.handle(req(["评论"]))

    def test_bare_comments_shows_default_five(self):
        self._view_first()
        output = cli.handle(req(["评论"]))
        self.assertIn("评论 46 条，显示前 5 条", output)

    def test_comments_with_count(self):
        self._view_first()
        output = cli.handle(req(["评论", "3条"]))
        self.assertIn("显示前 3 条", output)

    def test_comments_with_explicit_zero_count_shows_zero_not_default(self):
        # ctx["limit"] 是 0（合法输入，只是没人这么用）时不能被当成"没给
        # limit" 而悄悄退回默认的 5 条。
        self._view_first()
        output = cli.handle(req(["评论", "0条"]))
        self.assertIn("显示前 0 条", output)

    def test_comment_expand_shows_sub_preview(self):
        # 第 5 条评论 sub_count==预载 sub_preview 条数（fixture：2/2，已经
        # 全部到手），不该触发 S3.1 的浏览器补页逻辑——这个测试没有 mock
        # browser.session，选一条不会碰浏览器的评论（会触发浏览器的场景
        # 在 CommentExpandBrowserCommandTests 里用 BrowserCliTestCase 单独
        # 测）。
        self._view_first()
        output = cli.handle(req(["评论", "5", "展开"]))
        self.assertIn("路人18", output)


class ImagesCommandTests(CliTestCase):
    def test_images_without_current_note_errors(self):
        with self.assertRaises(XhsError):
            cli.handle(req(["读图", "1", "全文"]))

    def test_images_full_mode_calls_vision_with_full_indexes(self):
        cli.handle(req(["看", "https://xhslink.cn/o/AbCdEf12345", "不读图"]))
        self.mocked_vision.reset_mock()
        self.mocked_vision.return_value = [
            vision.ImageDesc(index=1, kind="照片", text="全文内容", sha256="deadbeef")
        ]
        output = cli.handle(req(["读图", "1", "全文"]))
        self.assertIn("全文内容", output)
        _, kwargs = self.mocked_vision.call_args
        self.assertEqual(kwargs.get("mode"), "full")
        self.assertEqual(kwargs.get("indexes"), [1])

    def test_images_passes_known_sha256_as_hint_to_skip_redownload(self):
        # `看` 默认读图（不带"不读图"），vision 桩回一个带 sha256 的描述；
        # state 里存下这个 sha256 后，`读图 N 全文` 应该把它当 hint 传给
        # vision.describe_images，而不是让 vision 重新下载一遍。
        self.mocked_vision.return_value = [
            vision.ImageDesc(index=1, kind="照片", text="简述", sha256="cafef00d")
        ]
        cli.handle(req(["看", "https://xhslink.cn/o/AbCdEf12345"]))
        state = state_store.load()
        self.assertEqual(state["current_note"]["images"][0]["sha256"], "cafef00d")

        self.mocked_vision.reset_mock()
        self.mocked_vision.return_value = [
            vision.ImageDesc(index=1, kind="照片", text="全文", sha256="cafef00d")
        ]
        cli.handle(req(["读图", "1", "全文"]))
        _, kwargs = self.mocked_vision.call_args
        self.assertEqual(kwargs.get("sha256_hints"), {1: "cafef00d"})

    def test_images_detail_mode_calls_vision_with_detail_mode(self):
        # "读图 N 仔细"（S3.2）跟"读图 N 全文"共用同一条 `_cmd_images`，
        # 只是 mode 换成 "detail"，交给 detail 模型（opus）仔细描述。
        cli.handle(req(["看", "https://xhslink.cn/o/AbCdEf12345", "不读图"]))
        self.mocked_vision.reset_mock()
        self.mocked_vision.return_value = [
            vision.ImageDesc(index=1, kind="照片", text="仔细描述内容", sha256="deadbeef")
        ]
        output = cli.handle(req(["读图", "1", "仔细"]))
        self.assertIn("仔细描述内容", output)
        _, kwargs = self.mocked_vision.call_args
        self.assertEqual(kwargs.get("mode"), "detail")
        self.assertEqual(kwargs.get("indexes"), [1])

    def test_images_default_mode_without_keyword_is_full(self):
        # "读图 1"（不带"全文"/"仔细"）历史行为就是全文模式，S3.2 不改这条。
        cli.handle(req(["看", "https://xhslink.cn/o/AbCdEf12345", "不读图"]))
        self.mocked_vision.reset_mock()
        self.mocked_vision.return_value = [
            vision.ImageDesc(index=1, kind="照片", text="内容", sha256="deadbeef")
        ]
        cli.handle(req(["读图", "1"]))
        _, kwargs = self.mocked_vision.call_args
        self.assertEqual(kwargs.get("mode"), "full")


class ShareImagesCommandTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.download_patcher = patch.object(
            vision, "_download", side_effect=self._fake_download
        )
        self.mocked_download = self.download_patcher.start()
        self.addCleanup(self.download_patcher.stop)

        self.upload_patcher = patch.object(
            share, "upload_files", return_value=["id-a", "id-b"]
        )
        self.mocked_upload = self.upload_patcher.start()
        self.addCleanup(self.upload_patcher.stop)

        self.share_patcher = patch.object(share, "share")
        self.mocked_share = self.share_patcher.start()
        self.addCleanup(self.share_patcher.stop)

    def _fake_download(self, url, known_sha256=None):
        # 真实 sha256 由内容决定，这里只需要"每个 URL 一个稳定、不同的值"
        # 来验证 state 合并逻辑，不必是真的哈希算法。
        return f"sha-{url[-6:]}", Path(f"/tmp/{url[-6:]}.jpg")

    def _view_without_images(self):
        cli.handle(req(["看", "https://xhslink.cn/o/AbCdEf12345", "不读图"]))

    def test_without_current_note_errors(self):
        with self.assertRaises(XhsError):
            cli.handle(req(["发原图", "1"]))

    def test_no_indexes_and_no_all_keyword_errors(self):
        with self.assertRaises(XhsError):
            cli.handle(req(["发原图"]))

    def test_downloads_missing_files_and_shares_selected_indexes(self):
        self._view_without_images()
        state_before = state_store.load()
        self.assertIsNone(state_before["current_note"]["images"][0]["sha256"])

        output = cli.handle(req(["发原图", "1", "3"]))
        self.assertEqual(output, "已把 2 张原图发给宝宝")

        self.assertEqual(self.mocked_download.call_count, 2)
        upload_args, _ = self.mocked_upload.call_args
        self.assertEqual(len(upload_args[0]), 2)

        share_args, share_kwargs = self.mocked_share.call_args
        self.assertEqual(share_kwargs.get("ids"), ["id-a", "id-b"])
        self.assertIn("1", share_kwargs.get("note", ""))
        self.assertIn("3", share_kwargs.get("note", ""))

        state = state_store.load()
        images_by_index = {img["index"]: img for img in state["current_note"]["images"]}
        self.assertTrue(images_by_index[1]["sha256"].startswith("sha-"))
        self.assertTrue(images_by_index[3]["sha256"].startswith("sha-"))
        # 没被选中的图片 sha256 维持原样（None），没有被顺手下载
        self.assertIsNone(images_by_index[2]["sha256"])

    def test_already_cached_sha256_skips_download(self):
        self._view_without_images()
        state = state_store.load()
        state["current_note"]["images"][0]["sha256"] = "already-cached"
        state_store.save(state)

        cached_dir = paths.images_dir()
        cached_dir.mkdir(parents=True, exist_ok=True)
        (cached_dir / "already-cached.jpg").write_bytes(b"x")

        cli.handle(req(["发原图", "1"]))
        self.mocked_download.assert_not_called()
        upload_args, _ = self.mocked_upload.call_args
        self.assertEqual(upload_args[0], [cached_dir / "already-cached.jpg"])

    def test_all_keyword_shares_every_image(self):
        self._view_without_images()
        cli.handle(req(["发原图", "全部"]))
        self.assertEqual(self.mocked_download.call_count, 7)
        upload_args, _ = self.mocked_upload.call_args
        self.assertEqual(len(upload_args[0]), 7)

    def test_missing_index_raises_with_index_named(self):
        self._view_without_images()
        with self.assertRaisesRegex(XhsError, "没有第 99 张图"):
            cli.handle(req(["发原图", "99"]))
        self.mocked_upload.assert_not_called()
        self.mocked_share.assert_not_called()

    def test_too_many_images_rejected_before_any_download(self):
        state_store.save({
            **state_store.DEFAULT_STATE,
            "current_note": {
                "id": "n1", "title": "标题", "url": "https://x", "comment_total": 0,
                "comments": [],
                "images": [{"index": i, "url": f"https://img/{i}", "sha256": None} for i in range(1, 11)],
            },
        })
        with self.assertRaisesRegex(XhsError, "最多发 9 张"):
            cli.handle(req(["发原图", "全部"]))
        self.mocked_download.assert_not_called()

    def test_no_images_in_note_errors(self):
        state_store.save({
            **state_store.DEFAULT_STATE,
            "current_note": {
                "id": "n1", "title": "标题", "url": "https://x", "comment_total": 0,
                "comments": [], "images": [],
            },
        })
        with self.assertRaisesRegex(XhsError, "没有图"):
            cli.handle(req(["发原图", "全部"]))


class ShareLinkCommandTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.share_patcher = patch.object(share, "share")
        self.mocked_share = self.share_patcher.start()
        self.addCleanup(self.share_patcher.stop)

    def test_without_current_note_errors(self):
        with self.assertRaises(XhsError):
            cli.handle(req(["分享链接"]))

    def test_shares_three_line_text_and_returns_confirmation(self):
        cli.handle(req(["看", "https://xhslink.cn/o/AbCdEf12345"]))
        self.mocked_share.reset_mock()
        output = cli.handle(req(["分享链接"]))
        self.assertEqual(output, "已把链接发给宝宝")

        _args, kwargs = self.mocked_share.call_args
        link = kwargs.get("link")
        self.assertIsNotNone(link)
        lines = link["text"].split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], link["title"])
        self.assertEqual(lines[1], link["url"])
        self.assertEqual(lines[2], "复制本条信息，打开【小红书】App查看精彩内容！")
        self.assertEqual(link["url"], FINAL_URL)


class RebootTests(CliTestCase):
    def test_reboot_blocks_command_and_updates_last_call_at(self):
        paths.ensure_dirs()
        now = datetime.now(TZ)
        stale_last_call = (now - timedelta(hours=6)).isoformat()
        state_store.save(
            {
                **state_store.DEFAULT_STATE,
                "day": now.date().isoformat(),
                "tokens_today": 16000,
                "last_call_at": stale_last_call,
            }
        )
        output = cli.handle(req(["评论"]))
        self.assertEqual(output, "确定还要刷吗？今天已经刷过一次了哦。")
        state = state_store.load()
        self.assertNotEqual(state["last_call_at"], stale_last_call)
        self.assertEqual(state["tokens_today"], 16000)  # 命令没真正执行，计数不变

    def test_retry_immediately_after_reboot_goes_through(self):
        paths.ensure_dirs()
        now = datetime.now(TZ)
        stale_last_call = (now - timedelta(hours=6)).isoformat()
        state_store.save(
            {
                **state_store.DEFAULT_STATE,
                "day": now.date().isoformat(),
                "tokens_today": 16000,
                "last_call_at": stale_last_call,
                "current_note": {
                    "id": "n1",
                    "title": "标题",
                    "comment_total": 1,
                    "comments": [{"index": 1, "user": "u", "text": "t", "sub_count": 0, "sub_preview": []}],
                    "images": [],
                },
            }
        )
        first = cli.handle(req(["评论"]))
        self.assertEqual(first, "确定还要刷吗？今天已经刷过一次了哦。")
        second = cli.handle(req(["评论"]))
        self.assertIn("评论 1 条", second)


class NoticeAssemblyTests(CliTestCase):
    def test_notice_prefixed_when_crossing_threshold(self):
        paths.ensure_dirs()
        now = datetime.now(TZ)
        state_store.save(
            {
                **state_store.DEFAULT_STATE,
                "day": now.date().isoformat(),
                "tokens_today": 9990,
                "last_call_at": now.isoformat(),
            }
        )
        output = cli.handle(req(["看", "https://xhslink.cn/o/AbCdEf12345"]))
        self.assertTrue(output.startswith("预制提醒："))


# ---------------------------------------------------------------------------
# S3：搜索/首页/更多/看 N/截图/放下/登录——全部经 browser.session() 打桩，
# 不连真浏览器/9223（每个测试断言"UI 动作序列 + 从 state 取数"这一层的
# 调用参数，browser.py 内部逻辑已经在 test_xhs_browser.py 单独测过）。
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _dummy_session():
    yield "PAGE"


class BrowserCliTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.xhs_dir = Path(self.tempdir.name)
        self.paths_patcher = patch.object(paths, "XHS_DIR", self.xhs_dir)
        self.paths_patcher.start()
        self.addCleanup(self.paths_patcher.stop)
        self.session_patcher = patch.object(browser, "session", _dummy_session)
        self.session_patcher.start()
        self.addCleanup(self.session_patcher.stop)

    def _saved_state(self):
        return state_store.load(paths.state_file())


def _item(index, **overrides):
    base = dict(index=index, id=f"n{index}", xsec_token=f"t{index}", title=f"标题{index}", note_type="normal", author=f"u{index}", liked_count="10")
    base.update(overrides)
    return parse.ListItem(**base)


class SearchCommandTests(BrowserCliTestCase):
    def test_renders_list_and_saves_last_list(self):
        items = [_item(1)]
        with patch.object(browser, "search", return_value=items) as search_mock:
            output = cli.handle(req(["搜索", "猫咪零食"]))
        search_mock.assert_called_once_with("PAGE", "猫咪零食")
        self.assertIn("[1] 标题1", output)
        state = self._saved_state()
        self.assertEqual(state["last_list"]["kind"], "search")
        self.assertEqual(state["last_list"]["query"], "猫咪零食")
        self.assertEqual(len(state["last_list"]["items"]), 1)

    def test_missing_keyword_raises_before_touching_browser(self):
        with patch.object(browser, "search") as search_mock:
            with self.assertRaises(XhsError):
                cli.handle(req(["搜索"]))
        search_mock.assert_not_called()

    def test_keyword_containing_reserved_words_is_not_hijacked(self):
        # "搜索 评论区神评" 里含"评论"，不能被 _match_comments 抢先命中。
        items = [_item(1)]
        with patch.object(browser, "search", return_value=items) as search_mock:
            cli.handle(req(["搜索", "评论区神评"]))
        search_mock.assert_called_once_with("PAGE", "评论区神评")


class FeedCommandTests(BrowserCliTestCase):
    def test_renders_list_and_saves_last_list(self):
        items = [_item(1), _item(2, note_type="video")]
        with patch.object(browser, "open_feed", return_value=items) as feed_mock:
            output = cli.handle(req(["首页"]))
        feed_mock.assert_called_once_with("PAGE")
        self.assertIn("（视频）", output)
        state = self._saved_state()
        self.assertEqual(state["last_list"]["kind"], "feed")


class MoreCommandTests(BrowserCliTestCase):
    def test_without_last_list_errors(self):
        with self.assertRaises(XhsError):
            cli.handle(req(["更多"]))

    def test_appends_new_items_and_renumbers(self):
        paths.ensure_dirs()
        state_store.save(
            {
                **state_store.DEFAULT_STATE,
                "last_list": {"kind": "feed", "query": "", "items": [{"index": 1, "id": "n1", "xsec_token": "t1", "title": "标题1", "note_type": "normal", "author": "u1", "liked_count": "1"}]},
            }
        )
        new_items = [_item(2)]
        with patch.object(browser, "load_more", return_value=new_items) as more_mock:
            output = cli.handle(req(["更多"]))
        # S3.1 审查意见 1.5：`load_more` 多了 `query` 参数（"更多"若不在列表
        # 页要用它重新导航），feed 场景 query 恒为空字符串。
        more_mock.assert_called_once_with("PAGE", "feed", 1, "")
        self.assertIn("[2]", output)
        state = self._saved_state()
        self.assertEqual(len(state["last_list"]["items"]), 2)

    def test_no_new_items_reports_no_more(self):
        paths.ensure_dirs()
        state_store.save(
            {
                **state_store.DEFAULT_STATE,
                "last_list": {"kind": "feed", "query": "", "items": []},
            }
        )
        with patch.object(browser, "load_more", return_value=[]):
            output = cli.handle(req(["更多"]))
        self.assertEqual(output, "没有更多了")


DETAIL_NOTE = parse.Note(
    note_id="n1", title="标题", desc="正文", author="作者", liked_count="1",
    collected_count="2", comment_count="3", share_count="4", time_ms=0, images=[],
)
DETAIL_PAGE = parse.CommentPage(total=0, comments=[])


class ViewListCommandTests(BrowserCliTestCase):
    def _seed_last_list(self, kind="feed"):
        paths.ensure_dirs()
        state_store.save(
            {
                **state_store.DEFAULT_STATE,
                "last_list": {
                    "kind": kind,
                    "query": "" if kind == "feed" else "关键词",
                    "items": [{"index": 3, "id": "n3", "xsec_token": "TOKEN3", "title": "标题3", "note_type": "normal", "author": "u3", "liked_count": "9"}],
                },
            }
        )

    def test_without_last_list_errors(self):
        with self.assertRaises(XhsError):
            cli.handle(req(["看", "3"]))

    def test_index_not_in_list_errors(self):
        self._seed_last_list()
        with self.assertRaises(XhsError):
            cli.handle(req(["看", "9"]))

    def test_opens_detail_with_pc_feed_source(self):
        self._seed_last_list(kind="feed")
        with patch.object(
            browser, "open_detail", return_value=(DETAIL_NOTE, DETAIL_PAGE)
        ) as open_mock, patch.object(vision, "describe_images", return_value=[]):
            output = cli.handle(req(["看", "3"]))
        open_mock.assert_called_once_with("PAGE", "n3", "TOKEN3", "pc_feed")
        self.assertIn("标题", output)
        state = self._saved_state()
        self.assertEqual(state["current_note"]["id"], "n1")
        self.assertEqual(state["current_note"]["xsec_token"], "TOKEN3")

    def test_opens_detail_with_pc_search_source(self):
        self._seed_last_list(kind="search")
        with patch.object(
            browser, "open_detail", return_value=(DETAIL_NOTE, DETAIL_PAGE)
        ) as open_mock, patch.object(vision, "describe_images", return_value=[]):
            cli.handle(req(["看", "3"]))
        open_mock.assert_called_once_with("PAGE", "n3", "TOKEN3", "pc_search")

    def test_bu_du_tu_skips_vision(self):
        self._seed_last_list()
        note_with_image = parse.Note(
            note_id="n1", title="标题", desc="正文", author="作者", liked_count="1",
            collected_count="2", comment_count="3", share_count="4", time_ms=0,
            images=[parse.NoteImage(index=1, url="http://x/1.jpg")],
        )
        with patch.object(
            browser, "open_detail", return_value=(note_with_image, DETAIL_PAGE)
        ), patch.object(vision, "describe_images") as vision_mock:
            cli.handle(req(["看", "3", "不读图"]))
        vision_mock.assert_not_called()


class ScreenshotCommandTests(BrowserCliTestCase):
    def _fake_path(self):
        path = self.xhs_dir / "images" / "shot.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def _seed_current_note(self):
        paths.ensure_dirs()
        state_store.save(
            {
                **state_store.DEFAULT_STATE,
                "current_note": {
                    "id": "n1",
                    "xsec_token": "TOKEN",
                    "xsec_source": "pc_feed",
                    "url": "https://www.xiaohongshu.com/explore/n1?xsec_token=TOKEN&xsec_source=pc_feed",
                    "title": "标题",
                    "author": "作者",
                    "comment_total": 0,
                    "comments_has_more": False,
                    "images": [],
                    "comments": [],
                },
            }
        )

    def test_plain_screenshot(self):
        path = self._fake_path()
        with patch.object(browser, "screenshot_page", return_value=path) as shot_mock, patch.object(
            share, "upload_files", return_value=["id1"]
        ) as upload_mock, patch.object(share, "share") as share_mock:
            output = cli.handle(req(["截图"]))
        shot_mock.assert_called_once_with("PAGE")
        upload_mock.assert_called_once_with([path])
        share_mock.assert_called_once_with(ids=["id1"], note="小红书：截图")
        self.assertEqual(output, "已把截图发给宝宝")

    def test_content_screenshot(self):
        # S3.1 审查意见 1.4：content/comment 目标先 `_ensure_on_note`，需要
        # 先有一个 current_note。
        self._seed_current_note()
        path = self._fake_path()
        with patch.object(browser, "_ensure_on_note") as ensure_mock, patch.object(
            browser, "screenshot_content", return_value=path
        ) as shot_mock, patch.object(share, "upload_files", return_value=["id1"]), patch.object(
            share, "share"
        ) as share_mock:
            cli.handle(req(["截图", "正文"]))
        ensure_mock.assert_called_once()
        self.assertEqual(ensure_mock.call_args[0][0], "PAGE")
        shot_mock.assert_called_once_with("PAGE")
        share_mock.assert_called_once_with(ids=["id1"], note="小红书：截图 正文")

    def test_content_screenshot_without_current_note_errors(self):
        with self.assertRaises(XhsError):
            cli.handle(req(["截图", "正文"]))

    def test_comment_screenshot(self):
        self._seed_current_note()
        path = self._fake_path()
        with patch.object(browser, "_ensure_on_note") as ensure_mock, patch.object(
            browser, "screenshot_comment", return_value=path
        ) as shot_mock, patch.object(share, "upload_files", return_value=["id1"]), patch.object(
            share, "share"
        ) as share_mock:
            cli.handle(req(["截图", "评论", "3"]))
        ensure_mock.assert_called_once()
        shot_mock.assert_called_once_with("PAGE", 3)
        share_mock.assert_called_once_with(ids=["id1"], note="小红书：截图 评论 3")

    def test_comment_screenshot_without_current_note_errors(self):
        with self.assertRaises(XhsError):
            cli.handle(req(["截图", "评论", "3"]))

    def test_screenshot_does_not_require_current_note(self):
        # "截图"可以对着列表页/任意页面截，不该被要求先"看"过一篇笔记。
        path = self._fake_path()
        with patch.object(browser, "screenshot_page", return_value=path), patch.object(
            share, "upload_files", return_value=["id1"]
        ), patch.object(share, "share"):
            output = cli.handle(req(["截图"]))
        self.assertEqual(output, "已把截图发给宝宝")


# ---------------------------------------------------------------------------
# S3.1 审查意见 1.1：评论 N条/评论 N 展开缓存不够时打开浏览器补数据。
# ---------------------------------------------------------------------------


class CommentsBrowserCommandTests(BrowserCliTestCase):
    def _seed_current_note(self, comments, comment_total, comments_has_more):
        paths.ensure_dirs()
        state_store.save(
            {
                **state_store.DEFAULT_STATE,
                "current_note": {
                    "id": "n1",
                    "xsec_token": "TOKEN",
                    "xsec_source": "pc_feed",
                    "url": "https://www.xiaohongshu.com/explore/n1?xsec_token=TOKEN&xsec_source=pc_feed",
                    "title": "标题",
                    "author": "作者",
                    "comment_total": comment_total,
                    "comments_has_more": comments_has_more,
                    "images": [],
                    "comments": comments,
                },
            }
        )

    def _one_comment(self, index=1):
        return {
            "index": index,
            "id": f"c{index}",
            "user": f"u{index}",
            "text": f"t{index}",
            "sub_count": 0,
            "sub_preview": [],
            "sub_count_text": "0",
        }

    def test_fetches_more_when_cache_insufficient_and_more_available(self):
        self._seed_current_note(comments=[self._one_comment(1)], comment_total=10, comments_has_more=True)
        new_page = parse.CommentPage(
            total=10,
            comments=[
                parse.Comment(index=1, id="c1", user="u1", text="t1", sub_count=0, sub_count_text="0"),
                parse.Comment(index=2, id="c2", user="u2", text="t2", sub_count=0, sub_count_text="0"),
            ],
            has_more=False,
        )
        with patch.object(browser, "_ensure_on_note") as ensure_mock, patch.object(
            browser, "is_logged_in", return_value=True
        ), patch.object(browser, "load_comments", return_value=new_page) as load_mock:
            output = cli.handle(req(["评论", "2条"]))
        ensure_mock.assert_called_once()
        load_mock.assert_called_once_with("PAGE", "n1", 2)
        self.assertIn("显示前 2 条", output)
        state = self._saved_state()
        self.assertEqual(len(state["current_note"]["comments"]), 2)
        self.assertEqual(state["current_note"]["comment_total"], 10)
        self.assertFalse(state["current_note"]["comments_has_more"])

    def test_not_logged_in_raises_and_does_not_scroll(self):
        self._seed_current_note(comments=[self._one_comment(1)], comment_total=10, comments_has_more=True)
        with patch.object(browser, "_ensure_on_note"), patch.object(
            browser, "is_logged_in", return_value=False
        ), patch.object(browser, "load_comments") as load_mock:
            with self.assertRaises(XhsError):
                cli.handle(req(["评论", "2条"]))
        load_mock.assert_not_called()

    def test_cached_enough_skips_browser(self):
        self._seed_current_note(
            comments=[self._one_comment(1), self._one_comment(2)], comment_total=2, comments_has_more=False
        )
        with patch.object(browser, "session", side_effect=AssertionError("不该碰浏览器")):
            output = cli.handle(req(["评论", "1条"]))
        self.assertIn("显示前 1 条", output)


class CommentExpandBrowserCommandTests(BrowserCliTestCase):
    def _seed_current_note(self, sub_count, sub_preview):
        paths.ensure_dirs()
        state_store.save(
            {
                **state_store.DEFAULT_STATE,
                "current_note": {
                    "id": "n1",
                    "xsec_token": "TOKEN",
                    "xsec_source": "pc_feed",
                    "url": "https://www.xiaohongshu.com/explore/n1?xsec_token=TOKEN&xsec_source=pc_feed",
                    "title": "标题",
                    "author": "作者",
                    "comment_total": 1,
                    "comments_has_more": False,
                    "images": [],
                    "comments": [
                        {
                            "index": 1,
                            "id": "c1",
                            "user": "u1",
                            "text": "t1",
                            "sub_count": sub_count,
                            "sub_preview": sub_preview,
                            "sub_count_text": str(sub_count),
                        }
                    ],
                },
            }
        )

    def test_expand_fetches_when_sub_count_greater_than_preview(self):
        self._seed_current_note(sub_count=5, sub_preview=[{"user": "a", "text": "b"}])
        updated = parse.Comment(
            index=1,
            id="c1",
            user="u1",
            text="t1",
            sub_count=5,
            sub_preview=[parse.SubComment(user="a", text="b"), parse.SubComment(user="c", text="d")],
            sub_count_text="5",
        )
        with patch.object(browser, "_ensure_on_note") as ensure_mock, patch.object(
            browser, "is_logged_in", return_value=True
        ), patch.object(browser, "expand_comment", return_value=updated) as expand_mock:
            output = cli.handle(req(["评论", "1", "展开"]))
        ensure_mock.assert_called_once()
        expand_mock.assert_called_once_with("PAGE", "n1", 1)
        self.assertIn("c：d", output)
        state = self._saved_state()
        self.assertEqual(len(state["current_note"]["comments"][0]["sub_preview"]), 2)

    def test_expand_not_logged_in_raises(self):
        self._seed_current_note(sub_count=5, sub_preview=[])
        with patch.object(browser, "_ensure_on_note"), patch.object(
            browser, "is_logged_in", return_value=False
        ), patch.object(browser, "expand_comment") as expand_mock:
            with self.assertRaises(XhsError):
                cli.handle(req(["评论", "1", "展开"]))
        expand_mock.assert_not_called()

    def test_expand_skips_browser_when_preview_already_complete(self):
        self._seed_current_note(sub_count=1, sub_preview=[{"user": "a", "text": "b"}])
        with patch.object(browser, "session", side_effect=AssertionError("不该碰浏览器")):
            output = cli.handle(req(["评论", "1", "展开"]))
        self.assertIn("a：b", output)


class BuildCurrentNoteTests(unittest.TestCase):
    """S3.1 审查意见 1.1：`_build_current_note` 新增 `xsec_source`/
    `comments_has_more` 两个键，S1/S3 两条路径都要带。"""

    def _note(self):
        return parse.Note(
            note_id="n1", title="标题", desc="正文", author="作者", liked_count="1",
            collected_count="2", comment_count="3", share_count="4", time_ms=0, images=[],
        )

    def test_s1_path_defaults_pc_feed_and_computes_has_more_from_counts(self):
        page = parse.CommentPage(total=46, comments=[parse.Comment(index=1, id="c1", user="u", text="t", sub_count=0)])
        current = cli._build_current_note(self._note(), page, None, xsec_token="TOKEN", url="https://x")
        self.assertEqual(current["xsec_source"], "pc_feed")
        self.assertTrue(current["comments_has_more"])  # 1 条 < 总数 46

    def test_s3_path_uses_explicit_source_and_has_more_flag(self):
        page = parse.CommentPage(
            total=1,
            comments=[parse.Comment(index=1, id="c1", user="u", text="t", sub_count=0)],
            has_more=True,
        )
        current = cli._build_current_note(
            self._note(), page, None, xsec_token="TOKEN", url="https://x", xsec_source="pc_search"
        )
        self.assertEqual(current["xsec_source"], "pc_search")
        self.assertTrue(current["comments_has_more"])  # page.has_more 为真，即使条数已经够

    def test_no_more_when_all_fetched_and_has_more_false(self):
        page = parse.CommentPage(
            total=1,
            comments=[parse.Comment(index=1, id="c1", user="u", text="t", sub_count=0)],
            has_more=False,
        )
        current = cli._build_current_note(self._note(), page, None, xsec_token="TOKEN", url="https://x")
        self.assertFalse(current["comments_has_more"])


class DropCommandTests(BrowserCliTestCase):
    def test_calls_close_keeper_and_returns_message(self):
        with patch.object(browser, "close_keeper", return_value="已经放下了") as close_mock:
            output = cli.handle(req(["放下"]))
        close_mock.assert_called_once_with()
        self.assertEqual(output, "已经放下了")

    def test_does_not_count_tokens(self):
        with patch.object(browser, "close_keeper", return_value="已经放下了"):
            cli.handle(req(["放下"]))
        state = self._saved_state()
        self.assertEqual(state["tokens_today"], 0)

    def test_bypasses_reboot_gate(self):
        paths.ensure_dirs()
        now = datetime.now(TZ)
        state_store.save(
            {
                **state_store.DEFAULT_STATE,
                "day": now.date().isoformat(),
                "tokens_today": 16000,
                "last_call_at": (now - timedelta(hours=6)).isoformat(),
            }
        )
        with patch.object(browser, "close_keeper", return_value="已经放下了"):
            output = cli.handle(req(["放下"]))
        self.assertEqual(output, "已经放下了")


class LoginCommandTests(BrowserCliTestCase):
    def test_status_action(self):
        with patch.object(browser, "login_status", return_value=True) as status_mock:
            output = cli.handle(req(["登录", "状态"]))
        status_mock.assert_called_once_with("PAGE")
        self.assertEqual(output, "已登录")

    def test_status_action_false(self):
        with patch.object(browser, "login_status", return_value=False):
            output = cli.handle(req(["登录", "状态"]))
        self.assertEqual(output, "还没登录")

    def test_code_action(self):
        with patch.object(browser, "submit_code", return_value="已登录") as code_mock:
            output = cli.handle(req(["登录", "验证码", "123456"]))
        code_mock.assert_called_once_with("PAGE", "123456")
        self.assertEqual(output, "已登录")

    def test_start_action_shares_qrcode_via_injected_fn(self):
        captured = {}

        def fake_run_login_flow(page, *, share_fn):
            captured["page"] = page
            share_fn(Path("/tmp/fake_qr.png"))
            return "已登录"

        with patch.object(browser, "run_login_flow", side_effect=fake_run_login_flow), patch.object(
            share, "upload_files", return_value=["id1"]
        ) as upload_mock, patch.object(share, "share") as share_mock:
            output = cli.handle(req(["登录"]))
        self.assertEqual(captured["page"], "PAGE")
        upload_mock.assert_called_once_with([Path("/tmp/fake_qr.png")])
        share_mock.assert_called_once_with(ids=["id1"], note="小红书：登录二维码")
        self.assertEqual(output, "已登录")

    def test_login_does_not_count_tokens_or_hit_reboot_gate(self):
        paths.ensure_dirs()
        now = datetime.now(TZ)
        state_store.save(
            {
                **state_store.DEFAULT_STATE,
                "day": now.date().isoformat(),
                "tokens_today": 16000,
                "last_call_at": (now - timedelta(hours=6)).isoformat(),
            }
        )
        with patch.object(browser, "login_status", return_value=True):
            output = cli.handle(req(["登录", "状态"]))
        self.assertEqual(output, "已登录")
        state = self._saved_state()
        self.assertEqual(state["tokens_today"], 16000)  # 没被 apply() 加过


class ResolverOrderingTests(unittest.TestCase):
    def test_login_wins_over_everything(self):
        command, ctx = cli._resolve(req(["登录", "状态"]))
        self.assertEqual(command, "login")
        self.assertEqual(ctx, {"action": "status"})

    def test_screenshot_wins_over_comments_when_both_keywords_present(self):
        command, ctx = cli._resolve(req(["截图", "评论", "3"]))
        self.assertEqual(command, "screenshot")
        self.assertEqual(ctx, {"target": "comment", "index": 3})

    def test_view_link_still_wins_over_view_list(self):
        command, _ = cli._resolve(req(["看", "https://xhslink.cn/o/AbCdEf12345"]))
        self.assertEqual(command, "view")

    def test_view_list_matches_bare_digit(self):
        command, ctx = cli._resolve(req(["看", "3"]))
        self.assertEqual(command, "view_list")
        self.assertEqual(ctx, {"index": 3, "with_images": True})

    def test_search_wins_over_comments_keyword_collision(self):
        command, ctx = cli._resolve(req(["搜索", "评论区神评"]))
        self.assertEqual(command, "search")
        self.assertEqual(ctx, {"keyword": "评论区神评"})

    def test_search_wins_over_login_keyword_collision(self):
        # S3.1 审查意见 2.1："搜索 登录问题" 不该被 "登录" 关键词抢走。
        command, ctx = cli._resolve(req(["搜索", "登录问题"]))
        self.assertEqual(command, "search")
        self.assertEqual(ctx, {"keyword": "登录问题"})

    def test_more_only_matches_bare_word(self):
        self.assertIsNone(cli._match_more("更多的内容"))
        command, _ = cli._resolve(req(["更多"]))
        self.assertEqual(command, "more")

    def test_drop_only_matches_bare_word(self):
        self.assertIsNone(cli._match_drop("放下手机"))


class SemanticFallbackTests(unittest.TestCase):
    """2026-09-23：小机敲 `home 小红书 搜 短篇恐怖故事` 被"没听懂"。"""

    def resolve(self, *argv):
        return cli._resolve(req(list(argv)))

    def test_search_synonyms_keep_keyword_verbatim(self):
        for argv in (["搜", "短篇恐怖故事"], ["搜一下", "短篇恐怖故事"], ["找", "短篇恐怖故事"],
                     ["搜索：短篇恐怖故事"], ["帮我搜", "短篇恐怖故事"], ["搜短篇恐怖故事"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.resolve(*argv), ("search", {"keyword": "短篇恐怖故事"}))

    def test_search_keyword_numbers_and_command_words_untouched(self):
        self.assertEqual(self.resolve("搜", "三体", "第一部", "评论"),
                         ("search", {"keyword": "三体 第一部 评论"}))

    def test_real_search_still_not_cut_by_short_alias(self):
        self.assertEqual(self.resolve("搜索", "索尼相机"), ("search", {"keyword": "索尼相机"}))

    def test_view_list_synonyms_and_ordinals(self):
        for argv in (["看", "第3条"], ["看第三篇"], ["打开", "3"], ["点开", "第三个"], ["查看", "3"],
                     ["看看", "3"], ["想看", "3"], ["3"], ["看", "3号"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.resolve(*argv), ("view_list", {"index": 3, "with_images": True}))
        self.assertEqual(self.resolve("打开", "第十二条", "不读图"),
                         ("view_list", {"index": 12, "with_images": False}))

    def test_open_link_synonym(self):
        command, ctx = self.resolve("打开", "https://xhslink.cn/o/AbCdEf12345")
        self.assertEqual(command, "view")
        self.assertEqual(ctx["url"], "https://xhslink.cn/o/AbCdEf12345")

    def test_images_synonyms(self):
        self.assertEqual(self.resolve("看图", "2"), ("images", {"indexes": [2], "mode": "full"}))
        self.assertEqual(self.resolve("读图", "第二张", "仔细"), ("images", {"indexes": [2], "mode": "detail"}))
        self.assertEqual(self.resolve("识图", "一", "三"), ("images", {"indexes": [1, 3], "mode": "full"}))

    def test_comment_synonyms(self):
        self.assertEqual(self.resolve("看评论"), ("comments", {"limit": None}))
        self.assertEqual(self.resolve("评论区", "十条"), ("comments", {"limit": 10}))
        self.assertEqual(self.resolve("展开", "3"), ("comment_expand", {"index": 3}))
        self.assertEqual(self.resolve("评论", "展开", "3"), ("comment_expand", {"index": 3}))
        self.assertEqual(self.resolve("展开第二条评论"), ("comment_expand", {"index": 2}))
        self.assertEqual(self.resolve("评论", "第3楼", "展开"), ("comment_expand", {"index": 3}))

    def test_keyword_command_synonyms(self):
        cases = {
            ("下一页",): "more", ("继续翻",): "more", ("再来点",): "more", ("more",): "more",
            ("关掉",): "drop", ("不看了",): "drop",
            ("推荐",): "feed", ("刷刷",): "feed", ("去首页",): "feed",
            ("截屏",): "screenshot", ("分享",): "share_link", ("发链接",): "share_link",
            ("今日",): "today", ("用量",): "today",
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(self.resolve(*argv)[0], expected)

    def test_share_images_synonym(self):
        self.assertEqual(self.resolve("发图", "1", "3"), ("share_images", {"indexes": [1, 3]}))
        self.assertEqual(self.resolve("原图", "全部"), ("share_images", {"indexes": None}))

    def test_screenshot_comment_with_chinese_number(self):
        self.assertEqual(self.resolve("截个图", "评论", "第五条"), ("screenshot", {"target": "comment", "index": 5}))

    def test_unknown_still_raises_and_echoes_text(self):
        with self.assertRaises(XhsError) as caught:
            self.resolve("跳个舞")
        self.assertIn("跳个舞", str(caught.exception))
        self.assertIn("搜索 <关键词>", str(caught.exception))

    def test_bare_category_returns_help_without_touching_state(self):
        with patch.object(state_store, "load", side_effect=AssertionError):
            self.assertEqual(cli._handle(home.parse_request(["小红书"])), cli.HELP_TEXT)


class ActivityLogTests(unittest.TestCase):
    """9/23 用户：前端日志"活动"里要看得到小机在刷什么。"""

    def log(self, command, ctx, state, estimate=120):
        with patch.object(cli.log_store, "write_log") as write:
            cli._log_activity(command, ctx, state, estimate)
        return write

    def test_search_logs_keyword_count_and_tokens(self):
        write = self.log("search", {"keyword": "短篇恐怖故事"}, {"last_list": {"items": [{}, {}, {}]}})
        write.assert_called_once_with("info", "activity", "小红书：搜索「短篇恐怖故事」（3 条结果）（约 120 token）")

    def test_view_list_uses_note_title(self):
        write = self.log("view_list", {"index": 2, "with_images": True}, {"current_note": {"title": "雨夜的电梯"}})
        write.assert_called_once_with("info", "activity", "小红书：点开第 2 条：《雨夜的电梯》（约 120 token）")

    def test_images_and_screenshot_descriptions(self):
        note = {"current_note": {"title": "猫"}}
        self.assertEqual(self.log("images", {"indexes": [1, 3], "mode": "detail"}, note).call_args.args[2],
                         "小红书：仔细看《猫》的第 1、3 张（约 120 token）")
        self.assertEqual(self.log("screenshot", {"target": "comment", "index": 5}, note).call_args.args[2],
                         "小红书：截了《猫》的第 5 条评论（约 120 token）")

    def test_drop_has_no_token_suffix(self):
        write = self.log("drop", {}, {}, estimate=None)
        write.assert_called_once_with("info", "activity", "小红书：放下小红书")

    def test_share_today_login_not_logged(self):
        for command in ("share_images", "share_link", "today", "login"):
            with self.subTest(command=command):
                self.log(command, {}, {}).assert_not_called()

    def test_every_logged_command_is_a_real_command(self):
        self.assertLessEqual(set(cli._ACTIVITY_DESCRIBERS), set(cli._DISPATCH) | {"today"})


if __name__ == "__main__":
    unittest.main()
