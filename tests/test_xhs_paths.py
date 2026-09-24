import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xhs import paths


class DirLayoutTests(unittest.TestCase):
    def test_ensure_dirs_creates_all_three(self):
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir) / "sub" / ".xhs"
            with patch.object(paths, "XHS_DIR", xhs_dir):
                paths.ensure_dirs()
                self.assertTrue(xhs_dir.is_dir())
                self.assertTrue(paths.images_dir().is_dir())
                self.assertTrue(paths.descriptions_dir().is_dir())
                self.assertTrue(paths.profile_dir().is_dir())


class KeeperPathTests(unittest.TestCase):
    def test_keeper_paths_under_xhs_dir(self):
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            with patch.object(paths, "XHS_DIR", xhs_dir):
                self.assertEqual(paths.keeper_json_path(), xhs_dir / "keeper.json")
                self.assertEqual(paths.keeper_touch_path(), xhs_dir / "keeper.touch")
                self.assertEqual(paths.keeper_log_path(), xhs_dir / "keeper.log")
                self.assertEqual(paths.keeper_lock_path(), xhs_dir / "keeper.lock")
                self.assertEqual(paths.profile_dir(), xhs_dir / "profile")

    def test_touch_keeper_creates_and_updates_mtime(self):
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir) / "sub"
            with patch.object(paths, "XHS_DIR", xhs_dir):
                paths.touch_keeper()
                touch_path = paths.keeper_touch_path()
                self.assertTrue(touch_path.exists())
                first_mtime = touch_path.stat().st_mtime
                os.utime(touch_path, (first_mtime - 100, first_mtime - 100))
                paths.touch_keeper()
                self.assertGreater(touch_path.stat().st_mtime, first_mtime - 100)

    def test_state_file_under_xhs_dir(self):
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            with patch.object(paths, "XHS_DIR", xhs_dir):
                self.assertEqual(paths.state_file(), xhs_dir / "state.json")


class FramePathTests(unittest.TestCase):
    """S4：`frame_path()` 特意不挂在 `XHS_DIR` 下（默认 `/dev/shm`），惰性
    读 `XHS_SCREEN_PATH`；`DEFAULT_FRAME_PATH` 是模块属性（不是函数签名默认
    值），跟其它路径常量同一个改道路数。"""

    def test_no_env_follows_module_default_which_is_dev_shm_in_production(self):
        # `tests/conftest.py` 把 `DEFAULT_FRAME_PATH` 整体改道到临时目录
        # 了（全部测试都碰不到真实 /dev/shm），所以这里没法"不打桩直接查
        # 生产常量"——改成把它临时打回生产的真实字面量，一次断言同时锁住
        # "没有 env 覆盖时确实回读 DEFAULT_FRAME_PATH 当前值"这条改道机制、
        # 以及"生产默认值真的是 /dev/shm/xhs_screen.jpg"这条字面量本身
        # （2026-09-22 simplify 审查指出：原来另有一条只测机制、用任意临时
        # 路径的重复测试，合并成这一条）。
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XHS_SCREEN_PATH", None)
            with patch.object(paths, "DEFAULT_FRAME_PATH", Path("/dev/shm/xhs_screen.jpg")):
                self.assertEqual(paths.frame_path(), Path("/dev/shm/xhs_screen.jpg"))

    def test_env_override_wins_over_module_default(self):
        with tempfile.TemporaryDirectory() as tempdir:
            override = Path(tempdir) / "frame.jpg"
            with patch.dict(os.environ, {"XHS_SCREEN_PATH": str(override)}):
                self.assertEqual(paths.frame_path(), override)


class CleanupTests(unittest.TestCase):
    def _touch_with_age(self, path: Path, age_seconds: float, now: float) -> None:
        path.write_bytes(b"x")
        mtime = now - age_seconds
        os.utime(path, (mtime, mtime))

    def test_cleanup_removes_old_images_keeps_fresh(self):
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            with patch.object(paths, "XHS_DIR", xhs_dir):
                paths.ensure_dirs()
                now = time.time()
                old = paths.images_dir() / "old.jpg"
                fresh = paths.images_dir() / "fresh.jpg"
                self._touch_with_age(old, paths.IMAGES_TTL_SECONDS + 3600, now)
                self._touch_with_age(fresh, 60, now)
                paths.cleanup(now=now)
                self.assertFalse(old.exists())
                self.assertTrue(fresh.exists())

    def test_cleanup_descriptions_uses_longer_ttl(self):
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            with patch.object(paths, "XHS_DIR", xhs_dir):
                paths.ensure_dirs()
                now = time.time()
                # 比图片的 24h 长，但还没到描述缓存的 7 天，图片该清、描述不该清
                mid_age = paths.IMAGES_TTL_SECONDS + 3600
                img = paths.images_dir() / "a.jpg"
                desc = paths.descriptions_dir() / "a.brief.json"
                self._touch_with_age(img, mid_age, now)
                self._touch_with_age(desc, mid_age, now)
                paths.cleanup(now=now)
                self.assertFalse(img.exists())
                self.assertTrue(desc.exists())

    def test_cleanup_missing_dir_is_silent(self):
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir) / "does-not-exist"
            with patch.object(paths, "XHS_DIR", xhs_dir):
                paths.cleanup()  # 不抛异常即通过


if __name__ == "__main__":
    unittest.main()
