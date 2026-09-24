"""看守进程测试：纯函数部分直接调用；单例/空闲退出/SIGTERM 清理这几条
只有真的起一个子进程才能验证（跟 `test_home_xhs.py::SubprocessRealMainTests`
同一个道理），子进程里把 Xvfb/Chrome 换成假命令（`sleep`），只测"进程
管理"这层逻辑，不碰真浏览器（设计文档 S3："keeper 用假命令 sleep 代替
Xvfb/Chrome 测单例、空闲退出、SIGTERM 清理"）。不连 9223、不起真
Xvfb/Chrome。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xhs import keeper, paths, selectors

REPO_ROOT = Path(__file__).resolve().parent.parent


class FreeDisplayTests(unittest.TestCase):
    def test_returns_start_when_nothing_locked(self):
        with tempfile.TemporaryDirectory():
            # /tmp/.X<n>-lock 是真实路径，测试环境里 :99 几乎必然空闲；
            # 只断言函数至少不抛异常、返回值 >= 99。
            display = keeper._free_display(start=59999)
            self.assertEqual(display, 59999)

    def test_skips_locked_displays(self):
        lock_path = Path("/tmp/.X59998-lock")
        lock_path.write_text("x")
        try:
            self.assertEqual(keeper._free_display(start=59998), 59999)
        finally:
            lock_path.unlink()


class EnvOverrideTests(unittest.TestCase):
    def test_poll_interval_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XHS_KEEPER_POLL_INTERVAL", None)
            self.assertEqual(keeper.poll_interval(), keeper.DEFAULT_POLL_INTERVAL)

    def test_poll_interval_override(self):
        with patch.dict(os.environ, {"XHS_KEEPER_POLL_INTERVAL": "0.5"}):
            self.assertEqual(keeper.poll_interval(), 0.5)

    def test_poll_interval_garbage_falls_back_to_default(self):
        with patch.dict(os.environ, {"XHS_KEEPER_POLL_INTERVAL": "not-a-number"}):
            self.assertEqual(keeper.poll_interval(), keeper.DEFAULT_POLL_INTERVAL)

    def test_idle_timeout_override(self):
        with patch.dict(os.environ, {"XHS_KEEPER_IDLE_TIMEOUT": "1.5"}):
            self.assertEqual(keeper.idle_timeout(), 1.5)

    def test_xvfb_cmd_default_uses_real_binary(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XHS_KEEPER_XVFB_CMD", None)
            cmd = keeper._xvfb_cmd(99)
            self.assertEqual(cmd[0], "Xvfb")
            self.assertIn(":99", cmd)

    def test_xvfb_cmd_override_from_json_env(self):
        with patch.dict(os.environ, {"XHS_KEEPER_XVFB_CMD": json.dumps(["sleep", "100"])}):
            self.assertEqual(keeper._xvfb_cmd(99), ["sleep", "100"])

    def test_chrome_cmd_default_has_root_safety_flags(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XHS_KEEPER_CHROME_CMD", None)
            cmd = keeper._chrome_cmd(99, 9223, Path("/tmp/profile"))
            self.assertIn("--no-sandbox", cmd)
            self.assertIn("--test-type", cmd)
            self.assertIn("--remote-debugging-port=9223", cmd)
            self.assertIn("--user-data-dir=/tmp/profile", cmd)

    def test_chrome_cmd_override_from_json_env(self):
        with patch.dict(os.environ, {"XHS_KEEPER_CHROME_CMD": json.dumps(["sleep", "100"])}):
            self.assertEqual(keeper._chrome_cmd(99, 9223, Path("/tmp/profile")), ["sleep", "100"])

    def test_ffmpeg_cmd_default_follows_actual_display_and_frame_path(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XHS_KEEPER_FFMPEG_CMD", None)
            cmd = keeper._ffmpeg_cmd(101, Path("/dev/shm/xhs_screen.jpg"))
            self.assertEqual(cmd[0], "ffmpeg")
            self.assertIn("-i", cmd)
            self.assertEqual(cmd[cmd.index("-i") + 1], ":101")
            self.assertNotIn(":99", cmd, "不该写死 :99，要跟随传入的实际 display")
            self.assertIn("x11grab", cmd)
            self.assertIn("2", cmd)  # framerate
            self.assertIn("scale=540:-1", cmd)
            self.assertEqual(cmd[-1], "/dev/shm/xhs_screen.jpg")

    def test_ffmpeg_cmd_override_from_json_env(self):
        with patch.dict(os.environ, {"XHS_KEEPER_FFMPEG_CMD": json.dumps(["sleep", "100"])}):
            self.assertEqual(keeper._ffmpeg_cmd(99, Path("/tmp/frame.jpg")), ["sleep", "100"])


class WaitCdpReadyTests(unittest.TestCase):
    def test_skip_flag_returns_true_immediately(self):
        with patch.dict(os.environ, {"XHS_KEEPER_SKIP_CDP_WAIT": "1"}):
            start = time.monotonic()
            self.assertTrue(keeper.wait_cdp_ready(1, timeout=5.0))
            self.assertLess(time.monotonic() - start, 1.0)

    def test_unreachable_port_times_out_false(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XHS_KEEPER_SKIP_CDP_WAIT", None)
            # 59999 端口没有任何东西在监听，应该在 timeout 内返回 False。
            self.assertFalse(keeper.wait_cdp_ready(59999, timeout=0.6))

    def test_should_stop_returns_false_early_without_waiting_full_timeout(self):
        """续工单第 2 项：`should_stop` 一旦为真就该立刻返回 False，不用等
        满 `timeout`——SIGTERM 落在等 CDP 期间不该拖着不退出。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XHS_KEEPER_SKIP_CDP_WAIT", None)
            start = time.monotonic()
            result = keeper.wait_cdp_ready(59999, timeout=10.0, should_stop=lambda: True)
            elapsed = time.monotonic() - start
            self.assertFalse(result)
            self.assertLess(elapsed, 1.0, "should_stop 为真应该立刻返回，不等满 10 秒超时")


class TerminateTests(unittest.TestCase):
    def test_none_proc_is_noop(self):
        keeper._terminate(None)  # 不抛异常即通过

    def test_terminates_sleep_process(self):
        proc = subprocess.Popen(["sleep", "100"])
        try:
            keeper._terminate(proc, grace=3.0)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_already_exited_process_is_noop(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        keeper._terminate(proc)  # 不抛异常即通过


class SleepCheckingStopTests(unittest.TestCase):
    def test_stops_early_when_flag_set(self):
        calls = {"n": 0}

        def should_stop():
            calls["n"] += 1
            return calls["n"] > 1

        start = time.monotonic()
        keeper._sleep_checking_stop(10.0, should_stop)
        self.assertLess(time.monotonic() - start, 2.0)

    def test_sleeps_full_duration_when_never_stopped(self):
        start = time.monotonic()
        keeper._sleep_checking_stop(0.3, lambda: False)
        self.assertGreaterEqual(time.monotonic() - start, 0.25)


class KeeperSubprocessLifecycleTests(unittest.TestCase):
    """真起一个 `python -m xhs.keeper` 子进程，Xvfb/Chrome 都换成
    `sleep 100` 假命令：验证的是单例/空闲退出/SIGTERM 清理这套进程管理
    逻辑本身，全程不连 9223、不起真 Xvfb/Chrome。"""

    def _spawn_keeper(
        self, xhs_dir: Path, *, idle_timeout: str, chrome_cmd: list[str] | None = None
    ) -> subprocess.Popen:
        env = {
            **os.environ,
            "XHS_DIR": str(xhs_dir),
            "XHS_KEEPER_XVFB_CMD": json.dumps(["sleep", "100"]),
            "XHS_KEEPER_CHROME_CMD": json.dumps(chrome_cmd or ["sleep", "100"]),
            "XHS_KEEPER_FFMPEG_CMD": json.dumps(["sleep", "100"]),
            "XHS_SCREEN_PATH": str(xhs_dir / "xhs_screen.jpg"),
            "XHS_KEEPER_SKIP_CDP_WAIT": "1",
            "XHS_KEEPER_POLL_INTERVAL": "0.1",
            "XHS_KEEPER_IDLE_TIMEOUT": idle_timeout,
        }
        return subprocess.Popen(
            [sys.executable, "-m", "xhs.keeper"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _wait_for_keeper_json(self, xhs_dir: Path, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        path = xhs_dir / "keeper.json"
        while time.monotonic() < deadline:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.05)
        self.fail(f"keeper.json 一直没出现：{path}")

    def _child_pids(self, pid: int) -> list[int]:
        result = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
        return [int(x) for x in result.stdout.split()]

    def _children_via_procfs(self, pid: int) -> list[int]:
        """跟 `_child_pids` 读同一件事，但直接读 `/proc` 而不是每次都 spawn
        一个 `pgrep` 子进程——三个孩子从"ffmpeg 起来"到"写状态失败触发
        清理"之间往往只有几毫秒的窗口，`pgrep` 自身的 spawn 开销（几十
        毫秒）会让轮询直接错过这个窗口（曾经真的复现过：轮询全程只看到
        0 个孩子），紧凑读 `/proc` 才追得上。"""
        try:
            with open(f"/proc/{pid}/task/{pid}/children") as handle:
                return [int(x) for x in handle.read().split()]
        except OSError:
            return []

    def _max_children_seen(self, proc: subprocess.Popen, target: int, timeout: float = 5.0) -> list[int]:
        """紧凑轮询到进程退出或凑够 `target` 个孩子为止，返回观察到过的
        "孩子数最多的那一刻"的 pid 列表。"""
        best: list[int] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self._children_via_procfs(proc.pid)
            if len(current) > len(best):
                best = current
            if len(best) >= target or proc.poll() is not None:
                break
        return best

    def test_single_instance_second_exits_without_starting_anything(self):
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            first = self._spawn_keeper(xhs_dir, idle_timeout="1000")
            try:
                info = self._wait_for_keeper_json(xhs_dir)
                self.assertEqual(info["pid"], first.pid)
                children_before = self._child_pids(first.pid)
                self.assertEqual(len(children_before), 3, "应该有 xvfb+chrome+ffmpeg 三个假子进程")

                second = self._spawn_keeper(xhs_dir, idle_timeout="1000")
                second.wait(timeout=5.0)
                self.assertEqual(second.returncode, 0)

                # keeper.json 仍然是第一个进程的（第二个没抢到锁，什么都
                # 没建、也没碰这个文件）。
                info_after = json.loads((xhs_dir / "keeper.json").read_text(encoding="utf-8"))
                self.assertEqual(info_after["pid"], first.pid)
            finally:
                if first.poll() is None:
                    first.terminate()
                    first.wait(timeout=5.0)

    def test_sigterm_cleans_up_children_and_keeper_json(self):
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            proc = self._spawn_keeper(xhs_dir, idle_timeout="1000")
            try:
                self._wait_for_keeper_json(xhs_dir)
                child_pids = self._child_pids(proc.pid)
                self.assertEqual(len(child_pids), 3, "应该有 xvfb+chrome+ffmpeg 三个假子进程")

                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=8.0)
                self.assertEqual(proc.returncode, 0)

                self.assertFalse((xhs_dir / "keeper.json").exists())
                for pid in child_pids:
                    with self.assertRaises(OSError):
                        os.kill(pid, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_idle_timeout_exits_on_its_own(self):
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            proc = self._spawn_keeper(xhs_dir, idle_timeout="0.3")
            try:
                self._wait_for_keeper_json(xhs_dir)
                proc.wait(timeout=8.0)
                self.assertEqual(proc.returncode, 0)
                self.assertFalse((xhs_dir / "keeper.json").exists())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_touching_keeper_file_postpones_idle_exit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            with patch.object(paths, "XHS_DIR", xhs_dir):
                proc = self._spawn_keeper(xhs_dir, idle_timeout="0.6")
                try:
                    self._wait_for_keeper_json(xhs_dir)
                    # 在空闲超时之前反复 touch，看守进程应该续命、不退出。
                    for _ in range(4):
                        time.sleep(0.2)
                        paths.touch_keeper()
                    self.assertIsNone(proc.poll(), "被 touch 续命之后不该已经退出")
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                    proc.wait(timeout=5.0)

    def test_ffmpeg_frame_file_survives_sigterm_cleanup(self):
        """S4："停在最后一帧"——SIGTERM 清理三个孩子之后，ffmpeg 写过的
        帧文件本身不删，只删 `keeper.json`。假 ffmpeg 用 `touch` 真的落一个
        文件在 `XHS_SCREEN_PATH` 指的路径，验证 `_remove_keeper_json()`
        没有连带删掉它。"""
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            frame = xhs_dir / "xhs_screen.jpg"
            env = {
                **os.environ,
                "XHS_DIR": str(xhs_dir),
                "XHS_KEEPER_XVFB_CMD": json.dumps(["sleep", "100"]),
                "XHS_KEEPER_CHROME_CMD": json.dumps(["sleep", "100"]),
                "XHS_KEEPER_FFMPEG_CMD": json.dumps(["sh", "-c", f"touch {frame}; sleep 100"]),
                "XHS_SCREEN_PATH": str(frame),
                "XHS_KEEPER_SKIP_CDP_WAIT": "1",
                "XHS_KEEPER_POLL_INTERVAL": "0.1",
                "XHS_KEEPER_IDLE_TIMEOUT": "1000",
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "xhs.keeper"],
                cwd=REPO_ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                self._wait_for_keeper_json(xhs_dir)
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not frame.exists():
                    time.sleep(0.05)
                self.assertTrue(frame.exists(), "假 ffmpeg 应该已经 touch 出帧文件")

                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=8.0)
                self.assertEqual(proc.returncode, 0)

                self.assertFalse((xhs_dir / "keeper.json").exists())
                self.assertTrue(frame.exists(), "最后一帧不该被清理逻辑删掉")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_chrome_exiting_on_its_own_cleans_up_remaining_children(self):
        """S4："Chrome 退出...时均清理三个孩子"——不只是空闲超时/SIGTERM，
        Chrome 自己崩溃/退出也要触发同一套清理，不能让 ffmpeg/Xvfb 变成
        孤儿继续跑。"""
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            proc = self._spawn_keeper(
                xhs_dir, idle_timeout="1000", chrome_cmd=["sh", "-c", "sleep 0.3"],
            )
            try:
                self._wait_for_keeper_json(xhs_dir)
                child_pids = self._child_pids(proc.pid)
                self.assertEqual(len(child_pids), 3, "应该有 xvfb+chrome+ffmpeg 三个假子进程")

                proc.wait(timeout=8.0)
                self.assertEqual(proc.returncode, 0)
                self.assertFalse((xhs_dir / "keeper.json").exists())
                for pid in child_pids:
                    with self.assertRaises(OSError):
                        os.kill(pid, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_chrome_popen_failure_cleans_up_already_started_xvfb(self):
        """S4 审查意见 1.2：Chrome 是继 Xvfb 之后起的第二个孩子，Popen 本身
        失败（比如可执行文件路径根本不存在）不该让已经起来的 Xvfb 变成
        孤儿——命令换成一个真的不存在的路径，真的触发 `subprocess.Popen`
        抛 `FileNotFoundError`（`OSError` 子类），不是手动摆一个假异常。"""
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            proc = self._spawn_keeper(
                xhs_dir, idle_timeout="1000",
                chrome_cmd=["/nonexistent/xhs-test-binary-does-not-exist"],
            )
            try:
                xvfb_pid = None
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    pids = self._child_pids(proc.pid)
                    if pids:
                        xvfb_pid = pids[0]
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(xvfb_pid, "Xvfb 这个假子进程应该已经起来过")

                proc.wait(timeout=8.0)
                self.assertEqual(proc.returncode, 1, "Chrome 起不来应该走跟 CDP 不就绪一样的 return 1")
                self.assertFalse((xhs_dir / "keeper.json").exists())
                with self.assertRaises(OSError):
                    os.kill(xvfb_pid, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_state_write_failure_after_all_children_started_cleans_up_all_three(self):
        """S4 审查意见 1.2：ffmpeg 起来之后写 keeper.json 失败，不能漏
        Xvfb/Chrome/ffmpeg 三个孩子——把 `keeper.json` 的目标路径预先占成
        一个目录，`state_store.save()` 最后一步 `os.replace()` 必然因为
        目标是目录而失败，验证唯一一个 `finally` 兜得住这种"三个孩子都
        起来了，但收尾这一步本身出错"的情形（原来这段完全没包在
        try/finally 里，异常会直接漏掉三个孩子）。"""
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            (xhs_dir / "keeper.json").mkdir()
            proc = self._spawn_keeper(xhs_dir, idle_timeout="1000")
            try:
                child_pids = self._max_children_seen(proc, target=3)
                self.assertEqual(len(child_pids), 3, "应该看到 xvfb+chrome+ffmpeg 三个假子进程都起来过")

                proc.wait(timeout=8.0)
                self.assertNotEqual(proc.returncode, 0, "写状态失败该带着非零退出码收尾，不是静默吞掉")
                for pid in child_pids:
                    with self.assertRaises(OSError):
                        os.kill(pid, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_sigterm_during_startup_gap_cleans_up_whatever_already_started(self):
        """S4 审查意见 1.2：SIGTERM handler 现在装在起任何子进程之前——就算
        信号正好落在 Xvfb 已经起来、Chrome 还没起来的那 1 秒起步间隙里，
        也不该有任何残留。"""
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            proc = self._spawn_keeper(xhs_dir, idle_timeout="1000")
            try:
                time.sleep(0.3)  # Xvfb 起步间隙内，Chrome 还没起
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=8.0)
                self.assertEqual(proc.returncode, 0)
                self.assertFalse((xhs_dir / "keeper.json").exists())
                time.sleep(0.3)  # 给清理动作留一点余量
                self.assertEqual(self._child_pids(proc.pid), [], "起步间隙里已经起来的孩子不该残留")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_sigterm_during_startup_gap_never_spawns_chrome(self):
        """续工单第 2 项：起步睡眠原来是一次性 `time.sleep(1.0)`，PEP 475
        下 SIGTERM 落在这段间隙里也会把剩余时间睡完，照常继续起 Chrome/
        ffmpeg——不是"最终清理得干净"就够，而是"已经收到停止请求就不该
        再起新孩子"。现在起步睡眠拆成可中断的小步，这里直接断言全程只
        看到过 Xvfb 一个孩子，Chrome 和 ffmpeg 都没机会起来过。"""
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            proc = self._spawn_keeper(xhs_dir, idle_timeout="1000")
            try:
                xvfb_pid = None
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    pids = self._child_pids(proc.pid)
                    if pids:
                        xvfb_pid = pids[0]
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(xvfb_pid, "Xvfb 这个假子进程应该已经起来过")

                proc.send_signal(signal.SIGTERM)

                # target 故意设成不可能达到的数，让轮询一直跑到进程退出
                # 为止，借此拿到"全程见过的最多孩子数"。
                max_children = self._max_children_seen(proc, target=99, timeout=4.0)
                self.assertLessEqual(
                    len(max_children), 1,
                    "SIGTERM 落在 Xvfb 起步间隙里，不该看到 Chrome/ffmpeg 被起来过",
                )

                proc.wait(timeout=8.0)
                self.assertEqual(proc.returncode, 0)
                self.assertFalse((xhs_dir / "keeper.json").exists())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_sigterm_during_cdp_wait_never_spawns_ffmpeg(self):
        """续工单第 2 项：Chrome 已经起来、CDP 还没就绪这段等待期
        （`wait_cdp_ready` 内部轮询，最长 20 秒）收到 SIGTERM，不该继续等
        满超时再判"CDP 没就绪"，也不该继续起 ffmpeg。用一个假 Chrome
        （`sleep`，永远不会真开 CDP 端口）配合真实等待（不设
        `XHS_KEEPER_SKIP_CDP_WAIT`），确认 SIGTERM 落地后很快退出且
        ffmpeg 从未起来过。"""
        with tempfile.TemporaryDirectory() as tempdir:
            xhs_dir = Path(tempdir)
            env = {
                **os.environ,
                "XHS_DIR": str(xhs_dir),
                "XHS_KEEPER_XVFB_CMD": json.dumps(["sleep", "100"]),
                "XHS_KEEPER_CHROME_CMD": json.dumps(["sleep", "100"]),
                "XHS_KEEPER_FFMPEG_CMD": json.dumps(["sleep", "100"]),
                "XHS_SCREEN_PATH": str(xhs_dir / "xhs_screen.jpg"),
                "XHS_CDP_PORT": "59997",  # 没有任何东西监听，CDP 永远不会就绪
                "XHS_KEEPER_POLL_INTERVAL": "0.1",
                "XHS_KEEPER_IDLE_TIMEOUT": "1000",
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "xhs.keeper"],
                cwd=REPO_ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                # 等到看见两个孩子（Xvfb + Chrome），说明已经进入 CDP 等待。
                deadline = time.monotonic() + 5.0
                two_children = False
                while time.monotonic() < deadline:
                    if len(self._child_pids(proc.pid)) >= 2:
                        two_children = True
                        break
                    time.sleep(0.02)
                self.assertTrue(two_children, "Xvfb+Chrome 应该都已经起来、正在等 CDP")

                proc.send_signal(signal.SIGTERM)

                start = time.monotonic()
                proc.wait(timeout=8.0)
                elapsed = time.monotonic() - start
                self.assertEqual(proc.returncode, 0)
                self.assertLess(
                    elapsed, 15.0,
                    "应该在等 CDP 期间就响应停止，不用等满 20 秒超时",
                )
                self.assertFalse((xhs_dir / "keeper.json").exists())
                self.assertEqual(
                    self._child_pids(proc.pid), [],
                    "ffmpeg 不该在收到停止请求之后才被起来",
                )
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    unittest.main()


class CdpPortTests(unittest.TestCase):
    """维护者 S3.1 补丁：`selectors.cdp_port()` 函数内当轮取 `XHS_CDP_PORT`，
    默认 9223；keeper 写进 keeper.json 的端口、browser 兜底读的端口都经它。"""

    def test_default_is_9223(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XHS_CDP_PORT", None)
            self.assertEqual(selectors.cdp_port(), 9223)

    def test_env_override(self):
        with patch.dict(os.environ, {"XHS_CDP_PORT": "9333"}):
            self.assertEqual(selectors.cdp_port(), 9333)

    def test_garbage_env_falls_back(self):
        with patch.dict(os.environ, {"XHS_CDP_PORT": "not-a-port"}):
            self.assertEqual(selectors.cdp_port(), 9223)
