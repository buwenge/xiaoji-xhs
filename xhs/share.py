"""把当前笔记的原图/链接卡片发给用户——CLI 侧经 daemon 本地 HTTP：
`upload_files()` 先 multipart 上传到 `/api/upload`（`target=chat`）拿到
附件 id，`share()` 再把 id 列表和/或链接卡片 POST 到 `/api/xiaoji/share`
落一条小机侧附件气泡。

跟 `home.py::phone_api` 共用鉴权/JSON-POST 骨架（`daemon_api.post_json`/
`daemon_api.read_auth_token`/`daemon_api.send_request`），但本模块不
`import home`——`xhs/` 包整体不碰 `home`，见 `xhs/cli.py` 模块顶部注释
同款的类身份坑。上传接口本身是 multipart/form-data，`post_json` 只管
JSON body，所以这里手搓一份最小 multipart body（用 urllib 而不是
requests，跟 `daemon_api`/`home.py::phone_api` 一样打桩
`urllib.request.urlopen` 就能测，不必另外拉一层 mock 风格）；响应解析/
网络错误归类复用 `daemon_api.send_request`，不重抄一遍
`HTTPError`/`URLError`/`JSONDecodeError` 的判断。`upload_files`/`share`
把 daemon_api 三类异常翻成 XhsError 这一段也彼此重复过一次，收进
`_call()` 一处（2026-09-22 `/code-review medium` 指出）。
"""

from __future__ import annotations

import mimetypes
import urllib.request
import uuid
from pathlib import Path

import daemon_api
from xhs import XhsError

UPLOAD_PATH = "/api/upload"
SHARE_PATH = "/api/xiaoji/share"
UPLOAD_TIMEOUT = 55.0
SHARE_TIMEOUT = 20.0

MAX_SHARE_IMAGES = 9
# 跟 chat_upload.MAX_FILES_PER_REQUEST 对齐（daemon 那边真正拒绝请求的
# 上限）。不直接 `from chat_upload import MAX_FILES_PER_REQUEST`——
# chat_upload.py 是 daemon 侧模块，依赖 aiohttp/PIL/sync_from_github 一整条
# 链，`xhs/share.py` 是小机每条命令都要冷启动一次的 CLI 侧模块，为省一个
# 数字的重复把这条依赖链拖进每次 CLI 调用不划算（跟"xhs/ 不 import home"
# 是同一个道理）。接你自己的 daemon 时，这个数跟你上传接口的单次上限保持一致。


def _multipart_body(fields: dict[str, str], files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    """`files` 是 (表单字段名, 文件名, 字节) 列表；返回 (body, content_type)。"""
    boundary = uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    for field_name, filename, data in files:
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        header = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        chunks.append(header + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _call(fn, *args, **kwargs) -> dict:
    """`upload_files`/`share` 共用的 daemon_api 异常翻译层：三类传输层
    异常统一变成 XhsError，一处改文案两处生效。`DaemonResponseError` 带
    `http_status` 时说明是"HTTP 错误状态码但响应体解析不出 JSON"，文案带
    上状态码；否则是"响应 200 但 body 不是合法 JSON"，用通用文案。"""
    try:
        return fn(*args, **kwargs)
    except daemon_api.DaemonAuthError as exc:
        raise XhsError("本机还没登录，发不出去，等会儿再试") from exc
    except daemon_api.DaemonConnectionError as exc:
        raise XhsError("发送失败，daemon 那边连不上") from exc
    except daemon_api.DaemonResponseError as exc:
        if exc.http_status is not None:
            raise XhsError(f"发送失败，daemon 返回 HTTP {exc.http_status}") from exc
        raise XhsError("发送失败，daemon 返回了看不懂的结果") from exc


def upload_files(paths: list[Path]) -> list[str]:
    """把本地文件 multipart 上传到 `/api/upload`（`target=chat`），返回
    附件 id 列表，供 `share()` 引用。不负责决定发哪几张图（那是
    `xhs/cli.py` 的事），只是个 HTTP 客户端。"""
    if not paths:
        return []
    token = _call(daemon_api.read_auth_token)

    files = [("files", p.name, p.read_bytes()) for p in paths]
    body, content_type = _multipart_body({"target": "chat"}, files)
    request = urllib.request.Request(
        f"{daemon_api.DAEMON_API_BASE.rstrip('/')}{UPLOAD_PATH}",
        data=body,
        headers={"Content-Type": content_type, "Cookie": f"xy_auth={token}"},
        method="POST",
    )
    # 响应解析/网络错误归类跟 daemon_api.post_json 共用同一段（本模块只是
    # body 是 multipart 不是 JSON，组装 Request 这步不同，收响应完全一样）。
    data = _call(daemon_api.send_request, request, timeout=UPLOAD_TIMEOUT)

    if not data.get("ok"):
        raise XhsError(str(data.get("error") or "发送失败"))
    return [a["id"] for a in data.get("attachments", []) if a.get("id")]


def share(*, ids: list[str] | None = None, link: dict | None = None, note: str = "") -> None:
    """POST `/api/xiaoji/share`：把已上传的附件 id 和/或链接卡片落进小机
    侧聊天记录。`ids`/`link` 至少给一个。"""
    attachments = list(ids or [])
    if not attachments and not link:
        raise XhsError("没有可以发的内容")
    payload = {"attachments": attachments, "link": link, "source": "xhs", "note": note}
    data = _call(daemon_api.post_json, SHARE_PATH, payload, timeout=SHARE_TIMEOUT)
    if not data.get("ok"):
        raise XhsError(str(data.get("error") or "发送失败"))
