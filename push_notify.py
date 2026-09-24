"""占位：`xhs/live_api.py` 用它把"浏览器开了/关了"推给前端。

原版是机器人 daemon 自己的 WebSocket 广播函数（给所有在线网页端发一条
JSON）。接进你自己的机器人时，把这个文件换成你的实现，或者改
`xhs/live_api.py` 顶部的 import 指向你已有的广播函数——签名只要是
`async def ws_broadcast(message: dict) -> None` 就行。
"""

from __future__ import annotations


async def ws_broadcast(message: dict) -> None:
    raise NotImplementedError("把 push_notify.ws_broadcast 换成你自己 daemon 的 WebSocket 广播")
