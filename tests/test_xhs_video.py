"""视频笔记：解析时长/地址、"视频"标注、抽帧命令（原话照转识图 agent）、截帧工具。"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import headless_claude
import home
from xhs import XhsError, cli, fetch_light, frame_tool, parse, paths, share, video
from xhs import state as state_store
from xhs.render import render_view

FIXTURE = json.loads((Path(__file__).resolve().parent / "fixtures" / "xhs_video_note.json").read_text(encoding="utf-8"))
VIDEO_STATE = {"noteData": {"data": {"noteData": FIXTURE, "commentData": {"comments": []}}}}
VIDEO_HTML = f"<html><script>window.__INITIAL_STATE__={json.dumps(VIDEO_STATE, ensure_ascii=False)}</script></html>"
FINAL_URL = "https://www.xiaohongshu.com/discovery/item/6a92d86500000000210306ab?xsec_token=TOKEN&xsec_source=pc"


def req(argv):
    return home.parse_request(["小红书", *argv])


class ParseVideoTests(unittest.TestCase):
    def test_real_video_note_has_type_duration_and_h264_url(self):
        note = parse.parse_note(VIDEO_STATE)
        self.assertEqual(note.note_type, "video")
        self.assertEqual(note.duration_s, 732)
        self.assertEqual(note.video_url, FIXTURE["video"]["media"]["stream"]["h264"][0]["masterUrl"])
        # 读图只会碰到这一张封面
        self.assertEqual(len(note.images), 1)

    def test_detail_path_same_shape(self):
        note = parse.parse_note_from_detail({"note": FIXTURE})
        self.assertEqual((note.note_type, note.duration_s), ("video", 732))

    def test_backup_url_and_stream_duration_fallbacks(self):
        data = json.loads(json.dumps(FIXTURE))
        data["video"]["capa"] = {}
        for entry in data["video"]["media"]["stream"]["h264"]:
            entry["masterUrl"] = ""
        note = parse.parse_note_from_detail({"note": data})
        self.assertEqual(note.video_url, FIXTURE["video"]["media"]["stream"]["h264"][0]["backupUrls"][0])
        self.assertEqual(note.duration_s, 732)

    def test_image_note_defaults(self):
        data = {k: v for k, v in FIXTURE.items() if k not in ("type", "video")}
        note = parse.parse_note_from_detail({"note": data})
        self.assertEqual((note.note_type, note.duration_s, note.video_url), ("normal", 0, ""))


class RenderVideoTests(unittest.TestCase):
    def test_video_note_says_video_and_cover(self):
        note = parse.parse_note(VIDEO_STATE)
        text = render_view(note, parse.CommentPage(comments=[], total=0), None)
        self.assertIn("视频 12:12（下面只有封面；想看画面：home 小红书 抽帧", text)
        self.assertIn("封面 1 张", text)
        self.assertNotIn("图 1 张", text)

    def test_image_note_unchanged(self):
        data = {k: v for k, v in FIXTURE.items() if k not in ("type", "video")}
        note = parse.parse_note_from_detail({"note": data})
        text = render_view(note, parse.CommentPage(comments=[], total=0), None)
        self.assertNotIn("视频", text)
        self.assertIn("图 1 张", text)


class ResolveFramesTests(unittest.TestCase):
    def test_request_passed_verbatim_even_with_other_command_words(self):
        # 原话里带"评论""今天""截图""登录"也不能被别的命令抢走，数字不归一
        for words, expected in [
            (["抽帧", "看看评论区说的第三段", "今天"], "看看评论区说的第三段 今天"),
            (["看视频", "截图那种画面", "三分钟前后"], "截图那种画面 三分钟前后"),
            (["抽帧", "登录页面那段"], "登录页面那段"),
            (["抽帧"], ""),
        ]:
            command, ctx = cli._resolve(req(words))
            self.assertEqual(command, "frames", words)
            self.assertEqual(ctx, {"request": expected})

    def test_search_still_first(self):
        command, _ = cli._resolve(req(["搜索", "抽帧教程"]))
        self.assertEqual(command, "search")


class FramesCommandTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        for patcher in (
            patch.object(paths, "XHS_DIR", Path(tempdir.name)),
            patch.object(fetch_light, "resolve_and_fetch", return_value=(FINAL_URL, VIDEO_HTML)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _save_current(self, **overrides):
        current = {"id": "6a92", "title": "恐怖小故事", "url": FINAL_URL, "note_type": "video",
                   "duration_s": 732, "video_url": "http://stored/v.mp4", "images": [], "comments": []}
        current.update(overrides)
        paths.ensure_dirs()
        state_store.save({"current_note": current})

    def test_view_stores_video_fields(self):
        cli.handle(req(["看", FINAL_URL, "不读图"]))
        current = state_store.load()["current_note"]
        self.assertEqual((current["note_type"], current["duration_s"]), ("video", 732))
        self.assertTrue(current["video_url"].startswith("http://sns-video"))

    def test_frames_relays_request_and_fresh_url(self):
        self._save_current()
        frames = [{"t": 60, "path": "/x/a.jpg"}, {"t": 185.5, "path": "/x/b.jpg"}]
        with patch.object(video, "watch", return_value=("1:00 她在吃饭", frames)) as watch:
            out = cli.handle(req(["抽帧", "她", "中间吃了什么？"]))
        kwargs = watch.call_args.kwargs
        self.assertEqual(kwargs["request"], "她 中间吃了什么？")
        self.assertEqual(kwargs["url"], FIXTURE["video"]["media"]["stream"]["h264"][0]["masterUrl"])
        self.assertEqual(kwargs["duration"], 732)
        self.assertIn("视频 12:12，抽帧看了：\n1:00 她在吃饭", out)
        self.assertIn("这次截的帧：1:00 3:05", out)
        self.assertEqual(state_store.load()["current_note"]["frames"], frames)

    def test_falls_back_to_stored_url_when_refetch_fails(self):
        self._save_current()
        with patch.object(fetch_light, "resolve_and_fetch", side_effect=XhsError("登录墙")), \
                patch.object(video, "watch", return_value=("ok", [])) as watch:
            cli.handle(req(["抽帧"]))
        self.assertEqual(watch.call_args.kwargs["url"], "http://stored/v.mp4")

    def test_image_note_refused_without_calling_agent(self):
        self._save_current(note_type="normal")
        with patch.object(video, "watch", side_effect=AssertionError("不该调")):
            with self.assertRaises(XhsError) as caught:
                cli.handle(req(["抽帧", "6 帧"]))
        self.assertIn("不是视频", str(caught.exception))


class ShareFramesTests(FramesCommandTests):
    def _frame_file(self, name):
        path = paths.images_dir() / name
        path.write_bytes(b"jpg")
        return path

    def test_resolve_forms(self):
        self.assertEqual(cli._resolve(req(["发帧", "1:00", "0:30", "1:00"])), ("share_frames", {"times": [30, 60]}))
        self.assertEqual(cli._resolve(req(["发帧", "全部"])), ("share_frames", {"times": None}))
        self.assertEqual(cli._resolve(req(["发视频截图", "2:00"])), ("share_frames", {"times": [120]}))
        with self.assertRaises(XhsError):
            cli._resolve(req(["发帧"]))

    def test_all_sends_saved_frames_without_grabbing(self):
        self._save_current()
        a, b = self._frame_file("a.jpg"), self._frame_file("b.jpg")
        state = state_store.load()
        state["current_note"]["frames"] = [{"t": 60, "path": str(a)}, {"t": 185.5, "path": str(b)}]
        state_store.save(state)
        with patch.object(video, "grab_frames", side_effect=AssertionError("不该截")), \
                patch.object(share, "upload_files", return_value=["i1", "i2"]) as upload, \
                patch.object(share, "share") as shared:
            out = cli.handle(req(["发帧", "全部"]))
        self.assertEqual(upload.call_args.args[0], [a, b])
        self.assertEqual(shared.call_args.kwargs, {"ids": ["i1", "i2"], "note": "小红书：视频画面 1:00、3:05"})
        self.assertIn("已把 2 张视频画面（1:00、3:05）发给宝宝", out)

    def test_unsaved_time_grabbed_fresh_saved_reused(self):
        self._save_current()
        a = self._frame_file("a.jpg")
        state = state_store.load()
        state["current_note"]["frames"] = [{"t": 60, "path": str(a)}]
        state_store.save(state)
        fresh = self._frame_file("c.jpg")
        with patch.object(video, "grab_frames", return_value=[fresh]) as grab, \
                patch.object(share, "upload_files", return_value=["i"]) as upload, \
                patch.object(share, "share"):
            cli.handle(req(["发帧", "1:00", "6:06"]))
        self.assertEqual(grab.call_args.args[1], [366])
        self.assertEqual(upload.call_args.args[0], [a, fresh])

    def test_out_of_range_and_image_note(self):
        self._save_current()
        with patch.object(share, "upload_files", side_effect=AssertionError("不该发")):
            with self.assertRaises(XhsError) as caught:
                cli.handle(req(["发帧", "20:00"]))
            self.assertIn("超出了", str(caught.exception))
            with self.assertRaises(XhsError):
                cli.handle(req(["发帧", "全部"]))  # 还没抽过
        self._save_current(note_type="normal")
        with self.assertRaises(XhsError):
            cli.handle(req(["发帧", "1:00"]))


class WatchTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        patcher = patch.object(paths, "XHS_DIR", Path(tempdir.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prompt_permissions_and_job_file(self):
        seen = {}

        def fake_run(prompt, **kwargs):
            seen.update(kwargs, prompt=prompt)
            job_path = kwargs["workdir"] / video.JOB_FILE
            seen["job"] = json.loads(job_path.read_text())
            # 模拟截帧工具记下两帧（乱序），watch 要按时间排好返回
            job_path.write_text(json.dumps({**seen["job"], "frames": [{"t": 90, "path": "b"}, {"t": 30, "path": "a"}]}))
            return {"result": "1:00 画面"}

        with patch.object(headless_claude, "run_headless", side_effect=fake_run):
            text = video.watch(title="T", desc="正文", duration=732, url="http://v/1.mp4",
                               request="红袖标是哪一段？看仔细点")
        self.assertEqual(text, ("1:00 画面", [{"t": 30, "path": "a"}, {"t": 90, "path": "b"}]))
        self.assertIn("「红袖标是哪一段？看仔细点」", seen["prompt"])
        self.assertIn(video.tool_command(), seen["prompt"])
        self.assertEqual(seen["tools"], "Read,Bash")
        self.assertEqual(seen["allowed_tools"], f"Read,Bash({video.tool_command()}:*)")
        self.assertEqual(seen["job"], {"url": "http://v/1.mp4", "duration": 732, "max_frames": video.MAX_FRAMES,
                                       "used": 0, "frames": []})
        self.assertNotIn("http://v/1.mp4", seen["prompt"])  # 地址不经 agent 的手
        self.assertFalse((paths.images_dir() / video.JOB_FILE).exists())

    def test_headless_failure_becomes_xhs_error_and_cleans_job(self):
        with patch.object(headless_claude, "run_headless", side_effect=headless_claude.HeadlessError("权限被拒")):
            with self.assertRaises(XhsError):
                video.watch(title="T", desc="", duration=60, url="u", request="")
        self.assertFalse((paths.images_dir() / video.JOB_FILE).exists())


class ClockTests(unittest.TestCase):
    def test_parse_and_format(self):
        self.assertEqual(video.parse_clock("1:30"), 90)
        self.assertEqual(video.parse_clock("1:02:03"), 3723)
        self.assertEqual(video.parse_clock("45.5"), 45.5)
        self.assertIsNone(video.parse_clock("一分钟"))
        self.assertEqual(video.fmt_clock(732), "12:12")
        self.assertEqual(video.fmt_clock(3723), "1:02:03")


class FrameToolTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.dir = Path(tempdir.name)
        (self.dir / video.JOB_FILE).write_text(json.dumps({"url": "u", "duration": 100, "max_frames": 3, "used": 0}))
        old = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, old)

    def _run(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = frame_tool.main(list(argv))
        return code, buf.getvalue()

    def test_cap_range_and_bad_token(self):
        with patch.object(video, "grab_frames", return_value=[self.dir / "a.jpg", None]) as grab:
            code, out = self._run("0:10", "50")
        self.assertEqual(code, 0)
        self.assertEqual(grab.call_args.args[1], [10, 50.0])
        self.assertIn("0:50 没截下来", out)
        self.assertIn("还能截 1 帧", out)
        job = json.loads((self.dir / video.JOB_FILE).read_text())
        self.assertEqual(job["frames"], [{"t": 10, "path": str(self.dir / "a.jpg")}])
        with patch.object(video, "grab_frames", side_effect=AssertionError("超额不该截")):
            self.assertEqual(self._run("0:20", "0:30")[0], 1)
            self.assertIn("超出了", self._run("1:40")[1])
            self.assertIn("看不懂", self._run("一分钟")[1])

    def test_script_imports_cleanly_as_agent_runs_it(self):
        # 按 agent 的方式直接跑脚本：xhs/selectors.py 不能顶掉标准库 selectors
        result = subprocess.run([sys.executable, str(video.FRAME_TOOL)], cwd=self.dir,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("要写时间点", result.stdout)


@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
class GrabFrameTests(unittest.TestCase):
    def test_grabs_real_frame_from_local_video(self):
        with tempfile.TemporaryDirectory() as tempdir:
            src = Path(tempdir) / "v.mp4"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
                            "-t", "3", "-y", str(src)], check=True, timeout=60)
            out = Path(tempdir) / "out"
            out.mkdir()
            frames = video.grab_frames(str(src), [1.0, 99.0], out)
            self.assertIsNotNone(frames[0])
            self.assertTrue(frames[0].name.endswith(".jpg") and frames[0].stat().st_size > 0)
            self.assertIsNone(frames[1])  # 越界不产出文件
            self.assertEqual([p.name for p in out.iterdir() if p.name.startswith(".")], [])


if __name__ == "__main__":
    unittest.main()
