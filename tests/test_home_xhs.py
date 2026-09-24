import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import home
from xhs import cli, paths

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_tcp_port() -> int:
    """向内核要一个当前空闲的 TCP 端口号（绑 0 再读回）。"""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DomainRoutingTests(unittest.TestCase):
    def test_aliases_route_to_xhs_domain(self):
        for word in ("小红书", "红书", "刷小红书", "xhs"):
            request = home.parse_request([word, "今天"])
            self.assertEqual(request.domain, "xhs")

    def test_category_hint_mentions_xhs(self):
        with self.assertRaises(home.HomeError) as ctx:
            home.parse_request([])
        self.assertIn("home 小红书", str(ctx.exception))


class MainDispatchTests(unittest.TestCase):
    def test_help_has_output_via_main(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(paths, "XHS_DIR", Path(tempdir)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = home.main(["小红书", "--help"])
            self.assertEqual(code, 0)
            self.assertIn("小红书", buf.getvalue())

    def test_today_does_not_count_via_main(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(paths, "XHS_DIR", Path(tempdir)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = home.main(["小红书", "今天"])
            self.assertEqual(code, 0)
            self.assertIn("token", buf.getvalue())
            self.assertFalse(paths.state_file().exists())

    def test_unrecognized_subcommand_exits_with_error_code(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(paths, "XHS_DIR", Path(tempdir)):
            buf = io.StringIO()
            with redirect_stdout(io.StringIO()), patch("sys.stderr", buf):
                code = home.main(["小红书", "瞎说八道"])
            self.assertEqual(code, 2)


class SubprocessRealMainTests(unittest.TestCase):
    """`home.py` 实际是 `python home.py …` 跑的：这时 home.py 是
    `__main__`，跟别的代码 `from home import HomeError` 拿到的那份 home
    模块是两个不同的模块对象、两个不同的 HomeError 类——in-process 测试
    （无论 pytest 还是 home.main() 直接调）全部是把 home 当模块 import，
    两边共用同一个类对象，测不出"跨导入路径身份不一致"这种坑，必须真的
    起一个子进程、让 home.py 当 __main__ 跑，才能复现 2026-09-22 维护者
    实测抓到的"XhsError 逃过 except HomeError、直接吐 traceback"那次
    事故。"""

    def test_unrecognized_command_prints_one_clean_line_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tempdir:
            env = {**os.environ, "XHS_DIR": tempdir}
            result = subprocess.run(
                [sys.executable, "home.py", "小红书", "乱七八糟"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 2, msg=f"stdout={result.stdout!r} stderr={result.stderr!r}")
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stderr.strip(), cli.NOT_UNDERSTOOD.format(text="乱七八糟"))

    def test_cannot_connect_cdp_prints_one_clean_line_not_a_traceback(self):
        """S3.1 审查意见 1.2：CDP 连不上（假 Chrome 命令永远不会真的监听
        9223，`XHS_KEEPER_SKIP_CDP_WAIT=1` 只让*看守进程自己*跳过等待、
        提前把 keeper.json 写成"就绪"，`xhs.browser._cdp_alive` 这边的
        真实网络探测仍然会一直失败）时，`ensure()` 最终超时抛
        `XhsError`，`home.py` 必须原样吐一句中文，不能吐 traceback。
        Xvfb 是真的（本机确认装了），Chrome 用 `sleep 100` 假的代替——
        不算"真起 Chrome"/"真连 9223"，这条链路本身连不上东西正是这个
        测试要验证的。跑起来大约 20+ 秒（`ensure()` 默认超时 20s），结束
        后按 keeper.json 记录的 pid 收掉看守进程（连带它启动的 Xvfb/假
        Chrome）。"""
        with tempfile.TemporaryDirectory() as tempdir:
            env = {
                **os.environ,
                "XHS_DIR": tempdir,
                "HOME_DAEMON_API_BASE": "http://127.0.0.1:1",
                "XHS_KEEPER_CHROME_CMD": json.dumps(["sleep", "100"]),
                "XHS_KEEPER_SKIP_CDP_WAIT": "1",
                # 维护者 S3.1 补丁：假看守进程用隔离端口。不然此刻机器上若有
                # 一个真看守进程在监听 9223（小机在刷/维护者在 smoke），
                # `_cdp_alive(9223)` 为真，本测试会顶包连上真 Chrome、真实
                # 访问站点（验收记录待裁决第 6 条那次事故的根因）。
                "XHS_CDP_PORT": str(_free_tcp_port()),
            }
            keeper_pid = None
            try:
                result = subprocess.run(
                    [sys.executable, "home.py", "小红书", "首页"],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                keeper_json = Path(tempdir) / "keeper.json"
                if keeper_json.exists():
                    with contextlib.suppress(OSError, json.JSONDecodeError):
                        keeper_pid = json.loads(keeper_json.read_text(encoding="utf-8")).get("pid")

                self.assertEqual(
                    result.returncode, 2, msg=f"stdout={result.stdout!r} stderr={result.stderr!r}"
                )
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                self.assertTrue(result.stderr.strip())
            finally:
                if keeper_pid:
                    with contextlib.suppress(ProcessLookupError, PermissionError, TypeError, ValueError):
                        os.kill(int(keeper_pid), 15)


class SubprocessLogIsolationTests(unittest.TestCase):
    def test_child_process_log_file_is_not_production(self):
        """9/22–23 上面两条子进程测试往生产 logs.jsonl 漏过 18 条告警。"""
        result = subprocess.run(
            [sys.executable, "-c", "import log_store; print(log_store.LOG_FILE)"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(Path(result.stdout.strip()).resolve(), (Path(REPO_ROOT) / "logs.jsonl").resolve())


if __name__ == "__main__":
    unittest.main()
