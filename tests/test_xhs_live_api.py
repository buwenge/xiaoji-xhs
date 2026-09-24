"""`xhs/live_api.py`（S4：实时观看接口）的回归测试。

HTTP 层用 `aiohttp.test_utils` 起真实 `web.Application`（写法照抄
`tests/test_assistant_share.py`），不启动 daemon、不连 3000/8765 端口。
`browser.keeper_info()`（PID 身份核验）打桩，不碰真实 `/proc`/keeper.json。
`ws_broadcast` 打桩验证广播的事件形状。
"""

from __future__ import annotations

import asyncio
import gc
import os
import tempfile
import unittest
import warnings
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from xhs import live_api, paths


class LiveStatusTests(unittest.TestCase):
    def test_inactive_when_no_keeper(self):
        with mock.patch.object(live_api.browser, "keeper_info", return_value=None):
            self.assertEqual(
                live_api.live_status(),
                {"active": False, "since": None, "last_used_at": None},
            )

    def test_active_reads_started_at_and_last_used_at(self):
        info = {"pid": 1, "started_at": "t0", "last_used_at": "t1", "cdp_port": 9223}
        with mock.patch.object(live_api.browser, "keeper_info", return_value=info):
            self.assertEqual(
                live_api.live_status(),
                {"active": True, "since": "t0", "last_used_at": "t1"},
            )


class ScreenApiHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="xiaoji-xhs-live-")
        self._frame = Path(self._tmp.name) / "frame.jpg"
        self._patch = mock.patch.object(paths, "DEFAULT_FRAME_PATH", self._frame)
        self._patch.start()

        app = web.Application()
        app.router.add_get("/api/xhs/screen", live_api.screen_api)
        self._server = TestServer(app)
        self._client = TestClient(self._server)
        await self._client.start_server()

    async def asyncTearDown(self):
        await self._client.close()
        self._patch.stop()
        self._tmp.cleanup()

    async def test_404_when_no_frame_yet(self):
        resp = await self._client.get("/api/xhs/screen")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.headers["Cache-Control"], "no-store", "审查意见 1.3：404 也要带 no-store，避免负缓存")

    async def test_returns_jpeg_bytes_with_no_store(self):
        self._frame.write_bytes(b"\xff\xd8\xff\xd9fake-jpeg")
        resp = await self._client.get("/api/xhs/screen")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertEqual(resp.headers["Content-Type"], "image/jpeg")
        body = await resp.read()
        self.assertEqual(body, b"\xff\xd8\xff\xd9fake-jpeg")

    async def test_env_override_path_takes_precedence(self):
        # XHS_SCREEN_PATH 优先于 DEFAULT_FRAME_PATH——跟 `paths.frame_path()`
        # 自己的单测覆盖同一条规则，这里额外确认 HTTP 层真的走这条路径。
        override = Path(self._tmp.name) / "override.jpg"
        override.write_bytes(b"override-bytes")
        with mock.patch.dict("os.environ", {"XHS_SCREEN_PATH": str(override)}):
            resp = await self._client.get("/api/xhs/screen")
            self.assertEqual(resp.status, 200)
            self.assertEqual(await resp.read(), b"override-bytes")


class LiveApiHandlerHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        app.router.add_get("/api/xhs/live", live_api.live_api_handler)
        self._server = TestServer(app)
        self._client = TestClient(self._server)
        await self._client.start_server()

    async def asyncTearDown(self):
        await self._client.close()

    async def test_inactive_payload_when_no_keeper(self):
        with mock.patch.object(live_api.browser, "keeper_info", return_value=None):
            resp = await self._client.get("/api/xhs/live")
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Cache-Control"], "no-store", "审查意见 1.3：状态响应也要 no-store")
            body = await resp.json()
            self.assertEqual(body, {"active": False, "since": None, "last_used_at": None})

    async def test_active_payload_when_keeper_alive(self):
        info = {"pid": 1, "started_at": "2026-09-22T20:00:00+08:00", "last_used_at": "2026-09-22T20:05:00+08:00"}
        with mock.patch.object(live_api.browser, "keeper_info", return_value=info):
            resp = await self._client.get("/api/xhs/live")
            body = await resp.json()
            self.assertEqual(body, {
                "active": True,
                "since": "2026-09-22T20:00:00+08:00",
                "last_used_at": "2026-09-22T20:05:00+08:00",
            })


class LiveStatusJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcasts_once_on_transition_to_active(self):
        job = live_api.LiveStatusJob()
        broadcasts = []

        async def fake_broadcast(event):
            broadcasts.append(event)

        with mock.patch.object(live_api, "ws_broadcast", fake_broadcast):
            with mock.patch.object(
                live_api, "live_status",
                return_value={"active": True, "since": "t0", "last_used_at": "t0"},
            ):
                await job()
                await job()  # 第二次状态没变，不该再广播

        self.assertEqual(len(broadcasts), 1)
        self.assertEqual(broadcasts[0], {"type": "xhs_live", "active": True, "since": "t0"})

    async def test_broadcasts_again_on_transition_back_to_inactive(self):
        job = live_api.LiveStatusJob()
        broadcasts = []

        async def fake_broadcast(event):
            broadcasts.append(event)

        with mock.patch.object(live_api, "ws_broadcast", fake_broadcast):
            with mock.patch.object(
                live_api, "live_status",
                return_value={"active": True, "since": "t0", "last_used_at": "t0"},
            ):
                await job()
            with mock.patch.object(
                live_api, "live_status",
                return_value={"active": False, "since": None, "last_used_at": None},
            ):
                await job()

        self.assertEqual(len(broadcasts), 2)
        self.assertEqual(broadcasts[1], {"type": "xhs_live", "active": False, "since": None})

    async def test_no_broadcast_before_first_call(self):
        job = live_api.LiveStatusJob()
        self.assertIsNone(job._last_state)

    async def test_broadcasts_when_since_changes_while_still_active(self):
        """S4 审查意见 1.1："对比 (active, since) 再广播，排除 last_used_at；
        快速重启跨越两次轮询时也更新 since"——看守进程在两次 5 秒轮询之间
        很快重启一次：`active` 全程都是 True，只有 `since`（新的一次
        `started_at`）变了。只比 `active` 会把这次变化吞掉。"""
        job = live_api.LiveStatusJob()
        broadcasts = []

        async def fake_broadcast(event):
            broadcasts.append(event)

        with mock.patch.object(live_api, "ws_broadcast", fake_broadcast):
            with mock.patch.object(
                live_api, "live_status",
                return_value={"active": True, "since": "t0", "last_used_at": "t0"},
            ):
                await job()
            with mock.patch.object(
                live_api, "live_status",
                return_value={"active": True, "since": "t1", "last_used_at": "t1"},
            ):
                await job()

        self.assertEqual(len(broadcasts), 2)
        self.assertEqual(broadcasts[1], {"type": "xhs_live", "active": True, "since": "t1"})

    async def test_no_broadcast_when_only_last_used_at_changes(self):
        """`last_used_at` 每 30 秒都会变，故意排除在比较之外，不该触发
        广播——否则前端会被"内容其实没变"的事件刷屏。"""
        job = live_api.LiveStatusJob()
        broadcasts = []

        async def fake_broadcast(event):
            broadcasts.append(event)

        with mock.patch.object(live_api, "ws_broadcast", fake_broadcast):
            with mock.patch.object(
                live_api, "live_status",
                return_value={"active": True, "since": "t0", "last_used_at": "t0"},
            ):
                await job()
            with mock.patch.object(
                live_api, "live_status",
                return_value={"active": True, "since": "t0", "last_used_at": "t999"},
            ):
                await job()

        self.assertEqual(len(broadcasts), 1)


class LiveStatusJobRealSchedulerTests(unittest.IsolatedAsyncioTestCase):
    """S4 审查意见 1.1（阻断项）：daemon 原来把 `LiveStatusJob()` 实例本身
    传给 `scheduler.add_job`。APScheduler 判断"该不该当协程函数处理"只看
    `asyncio.iscoroutinefunction(obj)`——一个重载了 `__call__` 的实例对这
    个判断永远是 `False`，`AsyncIOScheduler` 于是把它丢进线程池同步跑；
    线程里没有 running event loop，`await ws_broadcast(...)` 这行代码
    产生的协程对象没人 await 就被扔掉，真实广播次数是 0（外加一条
    "coroutine ... was never awaited" 警告）。这两条测试起真实
    `AsyncIOScheduler`，不是手动 `await job()`：一条证明"注册
    `job.__call__` 这个 bound 方法" 是正确修法，另一条反向对照"注册裸
    实例"确实拿不到广播——锁住"必须注册 `.__call__`"这条要求，防止以后
    改回去。"""

    async def test_registering_bound_call_method_actually_gets_awaited(self):
        job = live_api.LiveStatusJob()
        done = asyncio.Event()
        broadcasts = []

        async def fake_broadcast(event):
            broadcasts.append(event)
            done.set()

        info = {"pid": os.getpid(), "started_at": "t0", "last_used_at": "t0"}
        with mock.patch.object(live_api, "ws_broadcast", fake_broadcast), \
                mock.patch.object(live_api.browser, "keeper_info", return_value=info):
            scheduler = AsyncIOScheduler(timezone=timezone.utc)
            scheduler.add_job(job.__call__, "date", run_date=datetime.now(timezone.utc))
            scheduler.start()
            try:
                await asyncio.wait_for(done.wait(), timeout=5.0)
            finally:
                scheduler.shutdown(wait=False)

        self.assertEqual(len(broadcasts), 1)
        self.assertEqual(broadcasts[0], {"type": "xhs_live", "active": True, "since": "t0"})

    async def test_registering_bare_instance_never_gets_awaited_regression_guard(self):
        """注册裸实例是被修掉的 bug 本身：APScheduler 把它丢进线程池同步
        调用，`job()` 建出的协程对象没人 await，垃圾回收时解释器会发一条
        "coroutine ... was never awaited" 的 RuntimeWarning——这条警告是
        这个反向测试预期的直接产物，不是意外噪音。用
        `warnings.catch_warnings(record=True)` 接住并显式断言它，不让它
        流到 pytest 的警告汇总里变成一条"不知道哪来的"未解释警告（续工单
        续修：维护者专项指出"不要让最终验证出现未解释的 RuntimeWarning"）。
        """
        job = live_api.LiveStatusJob()
        broadcasts = []

        async def fake_broadcast(event):
            broadcasts.append(event)

        info = {"pid": os.getpid(), "started_at": "t0", "last_used_at": "t0"}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with mock.patch.object(live_api, "ws_broadcast", fake_broadcast), \
                    mock.patch.object(live_api.browser, "keeper_info", return_value=info):
                scheduler = AsyncIOScheduler(timezone=timezone.utc)
                scheduler.add_job(job, "date", run_date=datetime.now(timezone.utc))
                scheduler.start()
                try:
                    await asyncio.sleep(0.5)
                finally:
                    scheduler.shutdown(wait=False)
            # 触发一次 GC，确保这个线程池扔下的协程对象在离开
            # catch_warnings 窗口之前就被回收、把警告发出来，不会跑到窗口
            # 关闭之后才姗姗来迟、又变回一条未接住的噪音。
            gc.collect()

        self.assertEqual(broadcasts, [], "裸实例注册应该拿不到广播——这就是这条 bug 的真实表现")
        never_awaited = [w for w in caught if issubclass(w.category, RuntimeWarning)
                          and "was never awaited" in str(w.message)]
        self.assertEqual(
            len(never_awaited), 1,
            "应该正好接住一条协程未被 await 的警告——这就是裸实例注册这条 bug 本身的表现，"
            f"实际接住的告警：{[str(w.message) for w in caught]}",
        )


if __name__ == "__main__":
    unittest.main()
