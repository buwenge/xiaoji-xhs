"""daemon 端 `POST /api/xiaoji/share` 处理逻辑（小红书 S2）：把小机已经
上传好的图片附件 id、和/或一张链接卡片，落一条"小机侧"聊天记录（不带
文字、只带附件）+ 广播给前端，同时进"活动"日志。跟小机对话正文完全独立
——不进模型上下文，模型自己不会看到这条消息，纯粹是给用户看的分享。

天然在 `web_auth.auth_middleware` 的 cookie 墙内，不用自己再鉴权。处理
逻辑抽成独立函数（同 `push_notify.handle_fcm_token_report` 的写法）方便
绕开真实 aiohttp request 直接单测；`daemon.py` 只挂一行路由。
"""

from __future__ import annotations

from aiohttp import web

import chat_upload
import history_store
from config import now_local
from push_notify import log_and_broadcast, ws_broadcast


async def share_api(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"ok": False, "error": "请求格式不正确"}, status=400)
    result = await handle_share(payload)
    return web.json_response(result, status=200 if result.get("ok") else 400)


async def handle_share(payload: dict | None) -> dict:
    payload = payload or {}
    # 2026-09-22 code-review 指出：`link` 若是非 dict 的真值（字符串/数字/
    # 列表），原代码会一路走到 `link.get(...)` 抛 AttributeError，变成没
    # 处理过的 500 而不是这个接口其它分支那样规规矩矩的
    # `{"ok": false, "error": ...}`。`attachments` 同理补一道——非列表时
    # `for x in raw_attachments` 对字符串会拆成一个个字符当 id。今天唯一
    # 调用方是 xhs CLI，形状固定不会触发，但这个路由在 cookie 墙内，跟
    # 其它校验过入参形状的 JSON 接口（比如上面 share_api 本身对整段
    # payload 解析失败的处理）站在一起，形状校验不能只做一半。
    raw_attachments = payload.get("attachments")
    if raw_attachments is not None and not isinstance(raw_attachments, list):
        return {"ok": False, "error": "attachments 应为列表"}
    attachment_ids = [str(x) for x in (raw_attachments or []) if x]

    link = payload.get("link") or None
    if link is not None and not isinstance(link, dict):
        return {"ok": False, "error": "link 应为对象"}
    note = str(payload.get("note") or "")

    if not attachment_ids and not link:
        return {"ok": False, "error": "没有附件也没有链接，没什么可发的"}

    atts = chat_upload.history_attachments(attachment_ids) if attachment_ids else []
    if link:
        atts = atts + [{
            "kind": "link",
            "name": str(link.get("title") or ""),
            "size": 0,
            "url": str(link.get("url") or ""),
            "text": str(link.get("text") or ""),
        }]
    if not atts:
        return {"ok": False, "error": "附件 id 都找不到对应文件了"}

    history_store.record_history("assistant", "", "xiaoji", sender="xiaoji", attachments=atts)
    await ws_broadcast({
        "type": "assistant_share",
        "channel": "xiaoji",
        "sender": "xiaoji",
        "timestamp": now_local().isoformat(),
        "attachments": atts,
    })
    await log_and_broadcast("info", "activity", f"小机分享了{len(atts)}张图/一条链接（{note}）")
    return {"ok": True, "count": len(atts)}
