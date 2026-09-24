"""daemon 端小红书"实时观看"接口（S4）：`GET /api/xhs/screen` 吐看守进程
里 ffmpeg 覆写的最新一帧 JPEG（`no-store`，没有帧文件就 404）；
`GET /api/xhs/live` 读 keeper.json 回 `{active, since, last_used_at}`，前端
首屏/断线重连用它初始化；`LiveStatusJob` 是一个 5 秒轮询一次的 scheduler
job，`active` 翻转时才广播 `xhs_live` 事件（不是每 5 秒都推）。

PID 身份核验复用 `xhs.browser.keeper_info()`（S3.1 审查意见 1.3 那套
`/proc/<pid>/cmdline` 核验），不在这里重新手写一遍：坏 JSON、死 PID、
PID 被复用，`keeper_info()` 内部已经统一处理成"不算活着"。

状态记忆（"跟上次比有没有变化"）放在 `LiveStatusJob` 实例的属性上，不是
模块级可变全局——`daemon.py` 只实例化一次、把这一个实例注册成 scheduler
job（CC.md 屎山守则：不新增模块级可变状态）。
"""

from __future__ import annotations

from aiohttp import web

from push_notify import ws_broadcast
from xhs import browser, paths


async def screen_api(request: web.Request) -> web.Response:
    """`web.FileResponse` 跟 `chat_upload.serve_upload`/`daemon.serve_voice_note`
    同一个路数（2026-09-22 simplify reuse 审查指出：手写 `read_bytes()` 是
    在重新发明它们已经在用的东西），且不会把整张图先读进内存再堵在
    `await`上——这是个每 500ms 被轮询一次的接口，值得跟别处一样走这条
    现成路径。缺帧的 404 也要带 `no-store`（审查意见 1.3）：Cloudflare 只认
    这个头不改写，没了它一次偶发的"还没来得及生成帧"会被当负缓存钉住。"""
    path = paths.frame_path()
    if not path.is_file():
        resp = web.Response(status=404)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    resp = web.FileResponse(path)
    resp.headers["Cache-Control"] = "no-store"
    resp.content_type = "image/jpeg"
    return resp


def live_status() -> dict:
    info = browser.keeper_info()
    if info is None:
        return {"active": False, "since": None, "last_used_at": None}
    return {
        "active": True,
        "since": info.get("started_at"),
        "last_used_at": info.get("last_used_at"),
    }


async def live_api_handler(request: web.Request) -> web.Response:
    resp = web.json_response(live_status())
    resp.headers["Cache-Control"] = "no-store"
    return resp


class LiveStatusJob:
    """`scheduler.add_job(job.__call__, "interval", seconds=5)`——注意注册的
    是 bound `__call__` 方法而不是实例本身（审查意见 1.1 阻断项）：
    APScheduler 判断"是不是协程函数"只看 `asyncio.iscoroutinefunction`，
    一个重载了 `__call__` 的实例本身永远判 False，于是 `AsyncIOScheduler`
    会把它丢进线程池同步跑——线程里没有 running event loop，`await
    ws_broadcast(...)` 这次调用产生的协程对象没人 await 就被扔掉，实际
    广播次数是 0（审查者 用真实 AsyncIOScheduler + date job 复现过）。
    `job.__call__` 这个 bound 方法本身，`iscoroutinefunction` 判断才是
    True。

    比较 `(active, since)` 整个元组再决定要不要广播，不是只比 `active`：
    看守进程如果在两次 5 秒轮询之间很快重启一次（active 全程都是 True，
    但 `since` 变了——新的一次 `started_at`），只比 `active` 会把这次变化
    吞掉，前端拿到的"从什么时候开始"就是旧的。`last_used_at` 故意不参与
    比较——它每 30 秒都会变，不该触发广播。"""

    def __init__(self) -> None:
        self._last_state: tuple[bool, str | None] | None = None

    async def __call__(self) -> None:
        status = live_status()
        state = (status["active"], status["since"])
        if state == self._last_state:
            return
        self._last_state = state
        await ws_broadcast({
            "type": "xhs_live",
            "active": state[0],
            "since": state[1],
        })
