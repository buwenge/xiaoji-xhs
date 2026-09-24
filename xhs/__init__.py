"""xhs 包：小机逛小红书的所有逻辑。唯一对外入口是 `xhs.cli.handle(request)`，
`home.py` 只加一个域名别名 + 一行调用（见 home.py 的 DOMAIN_ALIASES["xhs"]
与 main() 里 `elif request.domain == "xhs":`）。

只读：不做点赞/评论/收藏/关注/发布/删帖，不留这些接口/参数/TODO。
图片字节永远不进小机的上下文：小机只读脚本 stdout 的纯文字。
"""

from __future__ import annotations


class XhsError(Exception):
    """xhs 包内部统一的用户可见错误：一句中文，不带 traceback。

    子模块（fetch_light/vision/...）遇到失败就近抛这个；`xhs.cli.handle`
    是唯一的翻译点，负责转成 `home.HomeError` 再往上抛给 `home.main()`
    的统一兜底（原版 home.py 里别的能力
    也是各自的异常被 handle_* 转译成 HomeError，同一个套路），同时留一条
    `log_store` 警告日志。
    """
