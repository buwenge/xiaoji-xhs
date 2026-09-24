import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import headless_claude
from xhs import paths, vision
from xhs.parse import NoteImage


class FakeImageResponse:
    def __init__(self, content: bytes, content_type: str = "image/jpeg"):
        self.content = content
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        return None


def _success_payload(items):
    return {"is_error": False, "permission_denials": [], "result": json.dumps(items, ensure_ascii=False)}


class VisionTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.xhs_dir = Path(self.tempdir.name)
        self.patcher = patch.object(paths, "XHS_DIR", self.xhs_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        paths.ensure_dirs()

    def _images(self, n=2):
        return [NoteImage(index=i, url=f"http://example.com/{i}.jpg") for i in range(1, n + 1)]


class DescribeImagesBatchingTests(VisionTestCase):
    def test_all_images_packed_into_a_single_run_headless_call(self):
        images = self._images(3)
        content_by_url = {img.url: f"bytes-{img.index}".encode() for img in images}
        call_count = {"n": 0}

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(content_by_url[url])

        def fake_run_headless(prompt, **kwargs):
            call_count["n"] += 1
            items = [{"i": i, "kind": "照片", "text": f"图{i}"} for i in range(1, 4)]
            return _success_payload(items)

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ):
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        self.assertEqual(call_count["n"], 1)
        self.assertEqual([d.index for d in descs], [1, 2, 3])
        self.assertEqual(descs[0].text, "图1")

    def test_cached_description_skips_run_headless(self):
        images = self._images(1)
        content = b"same-bytes"
        sha256 = hashlib.sha256(content).hexdigest()
        cache_path = paths.descriptions_dir() / f"{sha256}.brief.json"
        cache_path.write_text(json.dumps({"kind": "照片", "text": "缓存的描述"}), encoding="utf-8")

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(content)

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless"
        ) as mocked_run:
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        mocked_run.assert_not_called()
        self.assertEqual(descs[0].text, "缓存的描述")

    def test_mixed_cache_hit_and_miss_only_calls_for_missing(self):
        images = self._images(2)
        content_by_url = {img.url: f"bytes-{img.index}".encode() for img in images}
        # 预先给第一张图种下缓存
        sha1 = hashlib.sha256(content_by_url[images[0].url]).hexdigest()
        (paths.descriptions_dir() / f"{sha1}.brief.json").write_text(
            json.dumps({"kind": "照片", "text": "已缓存"}), encoding="utf-8"
        )

        captured_prompts = []

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(content_by_url[url])

        def fake_run_headless(prompt, **kwargs):
            captured_prompts.append(prompt)
            return _success_payload([{"i": 2, "kind": "文字卡", "text": "新描述"}])

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ):
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        self.assertEqual(len(captured_prompts), 1)
        self.assertIn("1 张图片文件", captured_prompts[0])  # 只打包了缺缓存的那 1 张
        self.assertEqual(descs[0].text, "已缓存")
        self.assertEqual(descs[1].text, "新描述")


class DownloadFailureResilienceTests(VisionTestCase):
    def test_one_image_503_does_not_crash_the_rest(self):
        # 3 张图，第 2 张 503；不该把整条"看"命令炸掉——那张标"无法读取"，
        # 其余两张照常走 opus 描述，而且 run_headless 只该收到正常下载的
        # 那两张（第 2 张连 opus 调用都不该进）。
        images = self._images(3)

        def fake_get(url, headers=None, timeout=None):
            if url == images[1].url:
                raise requests.HTTPError("503 Server Error")
            return FakeImageResponse(f"bytes-{url}".encode())

        captured_prompts = []

        def fake_run_headless(prompt, **kwargs):
            captured_prompts.append(prompt)
            return _success_payload([{"i": 1, "kind": "照片", "text": "第一张"}, {"i": 3, "kind": "照片", "text": "第三张"}])

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ), patch.object(vision.log_store, "write_log") as mocked_log:
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        self.assertEqual(descs[0].text, "第一张")
        self.assertEqual(descs[1].kind, "无法读取")
        self.assertEqual(descs[1].text, "图片下载失败")
        self.assertEqual(descs[1].sha256, "")
        self.assertEqual(descs[2].text, "第三张")
        # run_headless 只打包了下载成功的两张，没有把失败那张的路径也塞进去
        self.assertEqual(len(captured_prompts), 1)
        self.assertIn("2 张图片文件", captured_prompts[0])
        mocked_log.assert_called()
        self.assertEqual(mocked_log.call_args_list[0].args[0], "warning")
        self.assertEqual(mocked_log.call_args_list[0].args[1], "xhs")

    def test_failed_image_sha256_does_not_overwrite_state_via_merge(self):
        # 跟 xhs/cli.py::_merge_image_sha256 的约定对上：sha256="" 不能
        # 被当成"新哈希"合回 state。这里只测 vision 这一侧产出的形状，
        # cli 侧的合并逻辑由 test_xhs_cli.py 自己测。
        images = self._images(1)

        def fake_get(url, headers=None, timeout=None):
            raise requests.ConnectionError("connection reset")

        with patch.object(vision.requests, "get", side_effect=fake_get):
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        self.assertEqual(descs[0].sha256, "")


class DescribeImagesFallbackTests(VisionTestCase):
    def test_malformed_json_falls_back_by_lines_and_logs_warning(self):
        images = self._images(2)

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(b"bytes")

        def fake_run_headless(prompt, **kwargs):
            return {"is_error": False, "permission_denials": [], "result": "第一行描述\n第二行描述"}

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ), patch.object(vision.log_store, "write_log") as mocked_log:
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        self.assertEqual(descs[0].text, "第一行描述")
        self.assertEqual(descs[1].text, "第二行描述")
        mocked_log.assert_called()
        self.assertEqual(mocked_log.call_args[0][0], "warning")
        self.assertEqual(mocked_log.call_args[0][1], "xhs")

    def test_run_headless_error_falls_back_to_unreadable(self):
        images = self._images(1)

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(b"bytes")

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=headless_claude.HeadlessError("挂了")
        ):
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        self.assertEqual(descs[0].kind, "无法读取")

    def test_missing_index_in_model_output_gets_fallback_entry(self):
        images = self._images(2)

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(b"bytes")

        def fake_run_headless(prompt, **kwargs):
            # 模型只回了第 1 张的描述，第 2 张漏了
            return _success_payload([{"i": 1, "kind": "照片", "text": "只有第一张"}])

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ):
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        self.assertEqual(descs[0].text, "只有第一张")
        self.assertEqual(descs[1].kind, "无法读取")


class Sha256HintSkipsDownloadTests(VisionTestCase):
    def test_known_sha256_with_cached_file_skips_network_download(self):
        images = self._images(1)
        content = b"already-downloaded-bytes"
        sha256 = hashlib.sha256(content).hexdigest()
        # 模拟"看"已经下过这张图，本地缓存文件已经在
        cached_file = paths.images_dir() / f"{sha256}.jpg"
        cached_file.write_bytes(content)

        def fake_run_headless(prompt, **kwargs):
            return _success_payload([{"i": 1, "kind": "照片", "text": "全文描述"}])

        with patch.object(vision.requests, "get") as mocked_get, patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ):
            descs = vision.describe_images(
                title="标题", images=images, mode="full", sha256_hints={1: sha256}
            )

        mocked_get.assert_not_called()
        self.assertEqual(descs[0].sha256, sha256)
        self.assertEqual(descs[0].text, "全文描述")

    def test_known_sha256_without_local_file_falls_back_to_download(self):
        images = self._images(1)
        content = b"fresh-bytes"

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(content)

        def fake_run_headless(prompt, **kwargs):
            return _success_payload([{"i": 1, "kind": "照片", "text": "新下载"}])

        with patch.object(vision.requests, "get", side_effect=fake_get) as mocked_get, patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ):
            descs = vision.describe_images(
                title="标题",
                images=images,
                mode="full",
                # 给了个 hint，但本地没有这个哈希对应的文件——该下载还是要下载
                sha256_hints={1: "0" * 64},
            )

        mocked_get.assert_called_once()
        self.assertEqual(descs[0].text, "新下载")


class DescribeImagesIndexesAndModeTests(VisionTestCase):
    def test_indexes_filters_which_images_are_processed(self):
        images = self._images(3)

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(f"bytes-{url}".encode())

        called = {}

        def fake_run_headless(prompt, **kwargs):
            called["prompt"] = prompt
            return _success_payload([{"i": 2, "kind": "照片", "text": "第二张"}])

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ):
            descs = vision.describe_images(title="标题", images=images, mode="full", indexes=[2])

        self.assertEqual([d.index for d in descs], [2])
        self.assertIn("对第 2 张", called["prompt"])

    def test_empty_images_returns_empty_without_calling_network(self):
        with patch.object(vision.requests, "get") as mocked_get:
            descs = vision.describe_images(title="标题", images=[], mode="brief")
        mocked_get.assert_not_called()
        self.assertEqual(descs, [])

    def test_vision_model_env_override(self):
        with patch.dict("os.environ", {"XHS_VISION_MODEL": "opus"}):
            self.assertEqual(vision.vision_model(), "opus")

    def test_vision_model_defaults_to_sonnet(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("XHS_VISION_MODEL", None)
            self.assertEqual(vision.vision_model(), "sonnet")

    def test_detail_model_env_override(self):
        with patch.dict("os.environ", {"XHS_VISION_DETAIL_MODEL": "sonnet"}):
            self.assertEqual(vision.detail_model(), "sonnet")

    def test_detail_model_defaults_to_opus(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("XHS_VISION_DETAIL_MODEL", None)
            self.assertEqual(vision.detail_model(), "opus")


class DescribeImagesModelSelectionTests(VisionTestCase):
    """S3.2 方案 1：brief 走便宜的 sonnet（`vision_model()`），"全文"/
    "仔细"两种按需模式都换成更贵的 `detail_model()`（默认 opus）。"""

    def _run_and_capture_model(self, mode, indexes=None):
        images = self._images(1)
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(b"bytes")

        def fake_run_headless(prompt, **kwargs):
            captured["model"] = kwargs.get("model")
            return _success_payload([{"i": 1, "kind": "照片", "text": "描述"}])

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ):
            vision.describe_images(title="标题", images=images, mode=mode, indexes=indexes)
        return captured["model"]

    def test_brief_mode_uses_vision_model(self):
        with patch.dict("os.environ", {"XHS_VISION_MODEL": "sonnet", "XHS_VISION_DETAIL_MODEL": "opus"}):
            self.assertEqual(self._run_and_capture_model("brief"), "sonnet")

    def test_full_mode_uses_detail_model(self):
        with patch.dict("os.environ", {"XHS_VISION_MODEL": "sonnet", "XHS_VISION_DETAIL_MODEL": "opus"}):
            self.assertEqual(self._run_and_capture_model("full", indexes=[1]), "opus")

    def test_detail_mode_uses_detail_model(self):
        with patch.dict("os.environ", {"XHS_VISION_MODEL": "sonnet", "XHS_VISION_DETAIL_MODEL": "opus"}):
            self.assertEqual(self._run_and_capture_model("detail", indexes=[1]), "opus")


class SeriesSpanTests(VisionTestCase):
    """S3.2 系列跳读：模型把一段连续文字卡合并成一条 `{"i": "a-b", ...}`，
    区间里每张图各自的描述缓存都要写同一条并带上 span，好让 render.py
    合并渲染成一行；缓存命中时 span 也要能原样读回来。"""

    def test_range_response_fills_every_image_in_span_with_same_text(self):
        images = self._images(4)

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(f"bytes-{url}".encode())

        def fake_run_headless(prompt, **kwargs):
            return _success_payload(
                [
                    {"i": 1, "kind": "照片", "text": "封面"},
                    {"i": "2-4", "kind": "系列", "text": "教程三步"},
                ]
            )

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ):
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        self.assertIsNone(descs[0].span)
        for desc in descs[1:]:
            self.assertEqual(desc.kind, "系列")
            self.assertEqual(desc.text, "教程三步")
            self.assertEqual(desc.span, (2, 4))

    def test_span_cached_per_image_and_survives_cache_hit(self):
        images = self._images(2)
        content_by_url = {img.url: f"bytes-{img.index}".encode() for img in images}

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(content_by_url[url])

        def fake_run_headless(prompt, **kwargs):
            return _success_payload([{"i": "1-2", "kind": "系列", "text": "系列描述"}])

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ) as mocked_run:
            first = vision.describe_images(title="标题", images=images, mode="brief")
            second = vision.describe_images(title="标题", images=images, mode="brief")

        mocked_run.assert_called_once()  # 第二次全部命中缓存
        for desc in second:
            self.assertEqual(desc.span, (1, 2))
        self.assertEqual([d.span for d in first], [d.span for d in second])

    def test_reversed_range_is_malformed_and_falls_back(self):
        images = self._images(2)

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(b"bytes")

        def fake_run_headless(prompt, **kwargs):
            return _success_payload([{"i": "2-1", "kind": "系列", "text": "坏区间"}])

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ), patch.object(vision.log_store, "write_log") as mocked_log:
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        for desc in descs:
            self.assertEqual(desc.kind, "无法读取")
        mocked_log.assert_called()

    def test_non_numeric_i_is_malformed_and_falls_back(self):
        images = self._images(1)

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(b"bytes")

        def fake_run_headless(prompt, **kwargs):
            return _success_payload([{"i": "abc", "kind": "照片", "text": "坏 i"}])

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ):
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        self.assertEqual(descs[0].kind, "无法读取")

    def test_cross_note_span_from_reused_image_bytes_is_discarded(self):
        # 2026-09-22 code-review：缓存按 sha256（图片内容）存，天然会被
        # 两篇不同笔记里同一张图（同一张水印图/贴纸）复用；`span` 记的是
        # "在写这条缓存的那篇笔记里第几张到第几张"，是笔记内位置信息，
        # 跟这张图自己在*当前*这篇笔记里的序号是两回事。当前序号如果压根
        # 不落在缓存里的 span 区间内，说明这个 span 是别的笔记留下的，
        # 必须丢掉，不能原样渲染成这篇笔记也有的"系列"。
        images = self._images(1)  # 这篇笔记只有 1 张图，序号是 1
        content = b"shared-bytes-across-two-notes"
        sha256 = hashlib.sha256(content).hexdigest()
        (paths.descriptions_dir() / f"{sha256}.brief.json").write_text(
            json.dumps({"kind": "系列", "text": "另一篇笔记的系列描述", "span": [3, 12]}),
            encoding="utf-8",
        )

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(content)

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless"
        ) as mocked_run:
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        mocked_run.assert_not_called()  # 仍然命中缓存，不重新调用模型
        self.assertIsNone(descs[0].span)  # 但 span 对不上当前笔记，丢弃
        self.assertEqual(descs[0].text, "另一篇笔记的系列描述")  # 文字内容还能用

    def test_span_within_bounds_survives_cache_hit(self):
        # 对照组：span 跟当前序号对得上时正常保留（不是把 span 一律清空）。
        images = self._images(1)
        content = b"same-note-span-bytes"
        sha256 = hashlib.sha256(content).hexdigest()
        (paths.descriptions_dir() / f"{sha256}.brief.json").write_text(
            json.dumps({"kind": "系列", "text": "同一篇笔记的系列描述", "span": [1, 3]}),
            encoding="utf-8",
        )

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(content)

        with patch.object(vision.requests, "get", side_effect=fake_get):
            descs = vision.describe_images(title="标题", images=images, mode="brief")

        self.assertEqual(descs[0].span, (1, 3))


class FullDetailModeDoesNotBatchAcrossImagesTests(VisionTestCase):
    """2026-09-22 code-review：BASE_TEMPLATE 开头"先看第 1、2 张判断是否
    系列，只再打开最后一张"是给 brief 批量描述省钱用的规则；"读图 全文"
    （不带序号＝全部张）这种一次点名好几张要老实转录的请求，如果照样
    打包成一次调用，模型可能把中间几张判成系列只读一张、没有真的按
    "全文"/"仔细"的要求逐张处理。全文/仔细模式必须一次一张单独调用，
    `files` 里只有这一张，系列判断天然用不上。"""

    def test_full_mode_with_multiple_images_calls_run_headless_once_per_image(self):
        images = [NoteImage(index=i, url=f"http://example.com/{i}.jpg") for i in range(1, 4)]

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(f"bytes-{url}".encode())

        responses = iter(
            [
                _success_payload([{"i": 1, "kind": "文字卡", "text": "第1张全文"}]),
                _success_payload([{"i": 2, "kind": "文字卡", "text": "第2张全文"}]),
                _success_payload([{"i": 3, "kind": "文字卡", "text": "第3张全文"}]),
            ]
        )
        call_prompts = []

        def fake_run_headless(prompt, **kwargs):
            call_prompts.append(prompt)
            return next(responses)

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ):
            descs = vision.describe_images(title="标题", images=images, mode="full")

        self.assertEqual(len(call_prompts), 3)  # 逐张单独调用，不是打包成一次
        self.assertEqual([d.text for d in descs], ["第1张全文", "第2张全文", "第3张全文"])
        self.assertTrue(all("1 张图片文件" in p for p in call_prompts))
        self.assertIn("对第 1 张", call_prompts[0])
        self.assertIn("对第 2 张", call_prompts[1])
        self.assertIn("对第 3 张", call_prompts[2])

    def test_detail_mode_with_multiple_images_calls_run_headless_once_per_image(self):
        images = [NoteImage(index=i, url=f"http://example.com/{i}.jpg") for i in range(1, 3)]

        def fake_get(url, headers=None, timeout=None):
            return FakeImageResponse(f"bytes-{url}".encode())

        responses = iter(
            [
                _success_payload([{"i": 1, "kind": "照片", "text": "第1张仔细"}]),
                _success_payload([{"i": 2, "kind": "照片", "text": "第2张仔细"}]),
            ]
        )
        call_prompts = []

        def fake_run_headless(prompt, **kwargs):
            call_prompts.append(prompt)
            return next(responses)

        with patch.object(vision.requests, "get", side_effect=fake_get), patch.object(
            headless_claude, "run_headless", side_effect=fake_run_headless
        ):
            descs = vision.describe_images(title="标题", images=images, mode="detail")

        self.assertEqual(len(call_prompts), 2)
        self.assertEqual([d.text for d in descs], ["第1张仔细", "第2张仔细"])
        self.assertIn("仔细描述", call_prompts[0])


if __name__ == "__main__":
    unittest.main()
