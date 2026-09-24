"""CLI 侧（`home.py`/`xhs/`）调 daemon 本地 HTTP 的通用骨架：读
`.web_auth_token`、拼 `Cookie: xy_auth=...`、发 JSON POST、把网络/解析层的
失败归成三类异常。原本整套写法焊在 `home.py::phone_api` 里，`xhs/share.py`
也要用同一套却不能 `import home`（小机实际是 `python home.py …` 跑的，
`home.py` 当 `__main__` 时和被当模块 `import` 时是两份不同的类身份，见
`xhs/cli.py` 顶部注释同款的坑）——所以抽成这个两边都能 import 的小模块，
`home.py::phone_api` 改为调用它，业务专属的报错文案（"电话后端…"）留在
`phone_api` 自己那层不下沉。

`post_json` 只管"发一次 JSON POST、解析响应"这层，不解释 `{"ok": false}`
这类业务语义——调用方自己读返回的 dict 决定怎么办（跟原来 `phone_api`
最后 `if not data.get("ok", False)` 那步保持一致，只是挪到调用方）。

`send_request` 是再往下一层的公开函数：只管"发一个已经组装好的 urllib
Request、解析响应、把网络/解析层失败归成 `DaemonConnectionError`/
`DaemonResponseError`"，不关心 body 是 JSON 还是 multipart——`post_json`
（JSON body）和 `xhs/share.py::upload_files`（multipart body，body 格式
不同所以自己组装 Request）都调这层，不必各自抄一遍
`HTTPError`/`URLError`/`JSONDecodeError` 的判断（2026-09-22 `/simplify`
四路审查里 reuse/simplification/altitude 三个角度都独立指出这段重复，
一并收进来）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("XIAOJI_HOME", Path(__file__).resolve().parent))
WEB_AUTH_TOKEN_FILE = Path(os.environ.get("HOME_WEB_AUTH_TOKEN_FILE", ROOT / ".web_auth_token"))
DAEMON_API_BASE = os.environ.get(
    "HOME_DAEMON_API_BASE",
    f"http://127.0.0.1:{os.environ.get('WEB_PORT', '3000')}",
)


class DaemonApiError(Exception):
    """`post_json`/`read_auth_token` 抛出的所有错误的基类；调用方按自己
    的场景转成用户看的中文提示，不要把这个类的 `str()` 直接展示出去。"""


class DaemonAuthError(DaemonApiError):
    """本机 `xy_auth` 鉴权令牌文件缺失/为空——daemon 还没起来或还没登录。"""


class DaemonConnectionError(DaemonApiError):
    """连不上本机 daemon HTTP（网络错误/超时）。"""


class DaemonResponseError(DaemonApiError):
    """响应体不是可解析的 JSON（编码问题/daemon 返回了非预期内容）。
    `http_status` 非 None 时表示这次是一个 HTTP 错误状态码、但响应体解析
    不出 JSON（比如 daemon 重启期间反代吐出的 502/504 HTML 错误页）；为
    None 表示响应状态是 200 但 body 本身不是合法 JSON。调用方（`phone_api`/
    `xhs/share.py`）想在报错文案里带上状态码时读这个属性。"""

    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


def read_auth_token(token_file: Path | None = None) -> str:
    """读 `.web_auth_token`；文件缺失/为空都算鉴权未就绪。`token_file`
    覆盖默认的 `WEB_AUTH_TOKEN_FILE`（`phone_api` 传自己的
    `home.WEB_AUTH_TOKEN_FILE`，保留被 `HOME_WEB_AUTH_TOKEN_FILE`/测试
    打桩单独改道的能力，跟 `post_json` 的 `base` 参数一个道理）。
    `xhs/share.py` 的 multipart 上传（不走 `post_json` 的 JSON 请求路径）
    单独需要这个 token 去拼 Cookie 头，所以单独导出。"""
    try:
        token = (token_file or WEB_AUTH_TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DaemonAuthError("本机鉴权令牌尚未就绪") from exc
    if not token:
        raise DaemonAuthError("本机鉴权令牌尚未就绪")
    return token


def post_json(
    path: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 55.0,
    base: str | None = None,
    token_file: Path | None = None,
) -> dict:
    """POST 一段 JSON 到本机 daemon，返回解析后的响应体（不管 `ok` 是
    真是假——那是业务层的事）。`headers` 是调用方要额外附加的头（比如
    `phone_api` 的 `X-Xiaoji-Phone-Token`），跟默认的
    `Content-Type`/`Cookie` 合并，不覆盖后者。`base`/`token_file` 覆盖
    默认的 `DAEMON_API_BASE`/`WEB_AUTH_TOKEN_FILE`（`phone_api` 用它们
    保留可被 `HOME_PHONE_API_BASE`/`home.WEB_AUTH_TOKEN_FILE`/测试打桩
    单独改道的能力）。

    这两个参数存在的唯一原因是 `tests/test_home_phone.py`（锁着不能改）
    直接打桩 `home.WEB_AUTH_TOKEN_FILE`/`home.PHONE_API_BASE` 这两个
    home.py 自己的模块级全局，`phone_api` 必须在调用时把它们读出来传进
    来才能让打桩生效——这是欠账，不是设计初衷（/simplify altitude 审查
    2026-09-22 指出）。哪天那份测试解锁可以改，`home.py` 应该把
    `WEB_AUTH_TOKEN_FILE`/`PHONE_API_BASE` 变成对 `daemon_api` 自己那两个
    模块属性的再导出，到时候这两个参数就能从公开签名里去掉。"""
    token = read_auth_token(token_file)
    all_headers = {"Content-Type": "application/json", "Cookie": f"xy_auth={token}"}
    if headers:
        all_headers.update(headers)

    request = urllib.request.Request(
        f"{(base or DAEMON_API_BASE).rstrip('/')}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=all_headers,
        method="POST",
    )
    return send_request(request, timeout=timeout)


def send_request(request: urllib.request.Request, *, timeout: float) -> dict:
    """发送一个已经组装好的 `urllib.request.Request`（`post_json` 的 JSON
    body 或 `xhs/share.py` 的 multipart body 都行，这层不关心 body 格式），
    解析响应体、把网络/解析层的失败归成 `DaemonConnectionError`/
    `DaemonResponseError`。HTTP 错误状态码若能解析出 JSON body（daemon
    自己返回的 `{"ok": false, "error": ...}`）原样透传（返回 dict，不
    抛异常）——那是业务层的 `{"ok": false}`，调用方自己判断；解析不出 JSON
    的 HTTP 错误（daemon 重启期间反代吐出的 502/504 HTML 错误页之类）则
    抛 `DaemonResponseError(..., http_status=code)`，不再假装是一个正常
    的业务响应字典——2026-09-22 `/code-review medium` 指出旧写法（返回
    `{"error": ...}` 而不抛异常）会让这段生成的通用文案绕过
    `phone_api`/`xhs/share.py` 自己的错误翻译层，直接原样泄漏给用户。"""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            raise DaemonResponseError(f"请求返回 HTTP {exc.code}", http_status=exc.code) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DaemonConnectionError("本机服务暂时连不上，请稍后再试") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DaemonResponseError("本机服务返回了无法识别的结果") from exc
