"""摘录：原版 daemon.py 里跟小红书有关的三处接线（不是可直接 import 的文件）。

你的机器人 daemon 只要是 aiohttp + APScheduler（或者任何能挂 HTTP 路由、
能每 5 秒跑一次协程的框架），照下面三段抄进去就行。
"""

import assistant_share                         # integration/assistant_share.py，按你的聊天记录存储改
from xhs import live_api as xhs_live_api

# ── 1. 定时任务：每 5 秒看一眼看守浏览器开没开，翻转时才推 `xhs_live` 给前端 ──
# 注意注册的是 bound `__call__`，不是实例本身：APScheduler 只认
# `asyncio.iscoroutinefunction`，传实例会被丢进线程池同步跑，广播一次都发不出去。
scheduler.add_job(xhs_live_api.LiveStatusJob().__call__, "interval", seconds=5)

# ── 2. HTTP 路由（都放在你的登录 cookie 中间件后面） ──
web_app.router.add_post("/api/upload", chat_upload.upload_api)          # 你已有的聊天上传接口（见下方约定）
web_app.router.add_post("/api/xiaoji/share", assistant_share.share_api)  # 小机把原图/截图/链接卡片发进聊天
web_app.router.add_get("/api/xhs/screen", xhs_live_api.screen_api)       # 偷看：最新一帧 JPEG（no-store）
web_app.router.add_get("/api/xhs/live", xhs_live_api.live_api_handler)   # 偷看：首屏/重连时查浏览器开没开

# ── 3. 本机鉴权令牌 ──
# CLI 侧（home 小红书 发原图/截图/分享链接）以 `Cookie: xy_auth=<令牌>` 调上面两个接口。
# 令牌从 `$XIAOJI_HOME/.web_auth_token` 读（`HOME_WEB_AUTH_TOKEN_FILE` 可改），
# daemon 启动时写一个随机串进去、并让鉴权中间件认它即可。

# ── `/api/upload` 约定（xhs/share.py 依赖的形状） ──
# 请求：multipart/form-data，字段 `target=chat`，文件字段名都叫 `files`（≤9 个）。
# 响应：{"ok": true, "attachments": [{"id": "...", ...}, ...]}；失败 {"ok": false, "error": "..."}。
# 之后 `/api/xiaoji/share` 收 {"attachments": [id...], "link": {title,url,text}?, "note": "..."}，
# 由 assistant_share.handle_share 查回附件、落一条"小机发的、没文字只有附件"的聊天记录并广播
# `assistant_share` 事件。这条消息不进模型上下文，纯粹给人看。
