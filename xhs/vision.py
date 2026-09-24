"""下载笔记图片（sha256 缓存）+ 打包一次无头 opus 调用描述 + 描述缓存。

图片字节不进小机的上下文：这里下载到 `.xhs/images/`、调用
`headless_claude.run_headless` 时 cwd 就是那个目录（工作目录里没有
`CLAUDE.md`），只有解析出来的文字描述会被调用方拼进 stdout。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests

import headless_claude
import log_store
from xhs import fetch_light, paths, prompts
from xhs.parse import NoteImage

DEFAULT_VISION_MODEL = "sonnet"
DEFAULT_VISION_DETAIL_MODEL = "opus"
VISION_TIMEOUT = 180
DOWNLOAD_TIMEOUT = 15
DOWNLOAD_RETRIES = 1

FALLBACK_KIND = "无法读取"
DOWNLOAD_FAILED_TEXT = "图片下载失败"


@dataclass(frozen=True)
class ImageDesc:
    index: int
    kind: str
    text: str
    sha256: str
    # 系列跳读（S3.2）：这张图属于一个连续区间描述（比如 3–12 张同一份
    # 教程截图只读了首尾两张），`span` 是那个区间的 (首, 尾)，供
    # render.py 把同一 span 的多张图合并渲染成一行。单张描述（绝大多数
    # 情况）没有 span，留 None。
    span: tuple[int, int] | None = None


def vision_model() -> str:
    """惰性读 env（跟仓库其它模块一样：`.env` 由 daemon/home.py 在 import
    之后才加载，模块级常量在 import 那一刻读不到）。默认档：`brief`
    （"看"命令的每张一句话）走这个，是通用额度、不占 Opus/Fable 周额度
    （9/22 用户拍板方案 1：sonnet 主力）。"""
    return os.environ.get("XHS_VISION_MODEL", "").strip() or DEFAULT_VISION_MODEL


def detail_model() -> str:
    """`读图 N 全文`/`读图 N 仔细`（`mode` 为 "full" 或 "detail"）按需
    换成更贵的模型——这两种都是用户明确点名要仔细看的少数几张，不是批量
    描述，成本可控。"""
    return os.environ.get("XHS_VISION_DETAIL_MODEL", "").strip() or DEFAULT_VISION_DETAIL_MODEL


def _guess_ext(url: str, content_type: str | None) -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext.lstrip(".")
    suffix = Path(url.split("?", 1)[0]).suffix.lstrip(".")
    return suffix or "jpg"


def _find_cached_file(sha256: str) -> Path | None:
    matches = sorted(paths.images_dir().glob(f"{sha256}.*"))
    return matches[0] if matches else None


def _download(url: str, known_sha256: str | None = None) -> tuple[str, Path]:
    """下载一张图到 `images/<sha256>.<ext>`；本地已有同名文件直接复用，
    不重复下载。`known_sha256`（调用方从 state 里已经知道的哈希，比如
    `看` 之后紧接着 `读图 N 全文`）命中本地缓存文件时，直接复用、连网络
    请求都不发——不是"文件已存在才不重复写"，是"已经知道哈希就不用再问
    小红书 CDN 要一次字节"。失败（网络/HTTP 错误、写盘失败）原样抛出，
    调用方（`describe_images`）负责按张兜底，不在这里吞。返回
    `(sha256, 本地路径)`。"""
    if known_sha256:
        cached = _find_cached_file(known_sha256)
        if cached is not None:
            return known_sha256, cached
    last_exc: Exception | None = None
    response = None
    for attempt in range(DOWNLOAD_RETRIES + 1):
        try:
            response = requests.get(
                url, headers={"User-Agent": fetch_light.MOBILE_UA}, timeout=DOWNLOAD_TIMEOUT
            )
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_exc = exc
            response = None
    if response is None:
        raise last_exc  # 原样抛出 requests.RequestException，调用方按类型分支处理
    content = response.content
    sha256 = hashlib.sha256(content).hexdigest()
    ext = _guess_ext(url, response.headers.get("Content-Type"))
    path = paths.images_dir() / f"{sha256}.{ext}"
    if not path.exists():
        path.write_bytes(content)
    return sha256, path


def _description_path(sha256: str, mode: str) -> Path:
    return paths.descriptions_dir() / f"{sha256}.{mode}.json"


def _load_cached(sha256: str, mode: str) -> dict | None:
    try:
        text = _description_path(sha256, mode).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _save_cache(sha256: str, mode: str, kind: str, text: str, span: tuple[int, int] | None = None) -> None:
    payload: dict[str, object] = {"kind": kind, "text": text}
    if span is not None:
        payload["span"] = list(span)
    _description_path(sha256, mode).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _span_from_cache(cached: dict) -> tuple[int, int] | None:
    span = cached.get("span")
    if isinstance(span, list) and len(span) == 2:
        try:
            return int(span[0]), int(span[1])
        except (TypeError, ValueError):
            return None
    return None


def _fallback_by_lines(raw_text: str, pending: list[tuple[NoteImage, str, Path]]) -> dict[int, ImageDesc]:
    """opus 输出解析失败时的兜底：原文按行截断分给每张图，好歹有点内容
    而不是空白。"""
    lines = [line.strip() for line in (raw_text or "").splitlines() if line.strip()]
    result: dict[int, ImageDesc] = {}
    for position, (image, sha256, _path) in enumerate(pending):
        snippet = lines[position] if position < len(lines) else raw_text.strip()
        text = (snippet or "读图结果解析失败")[:60]
        result[image.index] = ImageDesc(index=image.index, kind="其他", text=text, sha256=sha256)
    return result


_RANGE_I_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


def _parse_i_field(raw_i: object) -> tuple[list[int], tuple[int, int] | None]:
    """`i` 要么是单张的整数（也可能是模型写成的数字字符串），要么是系列
    合并的区间字符串 `"3-12"`（含首尾）。区间倒着写（`"12-3"`）或非数字
    视为坏区间，返回 `([], None)`——调用方按"这几张模型没给"处理，走现有
    "结果缺描述"兜底与 warning，不单独再开一条日志（S3.2 现场补充：坏
    区间/越界走现有兜底并记 warning）。"""
    if isinstance(raw_i, str):
        match = _RANGE_I_RE.match(raw_i)
        if match:
            a, b = int(match.group(1)), int(match.group(2))
            if a > b:
                return [], None
            return list(range(a, b + 1)), (a, b)
    try:
        return [int(raw_i)], None
    except (TypeError, ValueError):
        return [], None


def _parse_vision_payload(
    payload: dict, pending: list[tuple[NoteImage, str, Path]]
) -> dict[int, ImageDesc]:
    raw_text = payload.get("result") if isinstance(payload, dict) else None
    items: object
    try:
        items = json.loads(raw_text) if isinstance(raw_text, str) else None
        if not isinstance(items, list):
            raise ValueError("不是 JSON 数组")
    except (TypeError, ValueError, json.JSONDecodeError):
        log_store.write_log("warning", "xhs", "读图结果不是合法 JSON 数组，按行兜底")
        return _fallback_by_lines(raw_text or "", pending)

    by_index: dict[int, tuple[str, str, tuple[int, int] | None]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        indexes, span = _parse_i_field(item.get("i"))
        if not indexes:
            continue
        kind = str(item.get("kind") or "其他")
        text = str(item.get("text") or "")
        for idx in indexes:
            by_index[idx] = (kind, text, span)

    result: dict[int, ImageDesc] = {}
    missing = 0
    for image, sha256, _path in pending:
        found = by_index.get(image.index)
        if found is None:
            missing += 1
            result[image.index] = ImageDesc(
                index=image.index, kind=FALLBACK_KIND, text="模型输出里没有这张图的描述", sha256=sha256
            )
        else:
            kind, text, span = found
            result[image.index] = ImageDesc(index=image.index, kind=kind, text=text, sha256=sha256, span=span)
    if missing:
        log_store.write_log("warning", "xhs", f"读图结果缺 {missing} 张的描述，已用兜底文案填上")
    return result


def describe_images(
    *,
    title: str,
    images: list[NoteImage],
    mode: str = "brief",
    indexes: list[int] | None = None,
    sha256_hints: dict[int, str] | None = None,
) -> list[ImageDesc]:
    """给一批图片一句话描述（`mode="brief"`，可能被合并成系列跳读一条）、
    逐字转录（`mode="full"`）或仔细描述（`mode="detail"`），后两者都需要
    配合 `indexes` 指定具体哪几张。已有缓存的图不重新调用模型；没缓存的
    才打包成一次 `headless_claude.run_headless`。`sha256_hints` 是调用方
    已经知道的"笔记内图片序号 → sha256"（比如 `看` 之后紧接着 `读图 N
    全文`），命中时直接认本地缓存文件，不用再找小红书 CDN 重新下载一遍
    已经下过的图。
    """
    targets = images if indexes is None else [img for img in images if img.index in set(indexes)]
    if not targets:
        return []

    paths.images_dir().mkdir(parents=True, exist_ok=True)
    hints = sha256_hints or {}

    results: dict[int, ImageDesc] = {}
    downloaded: list[tuple[NoteImage, str, Path]] = []
    for image in targets:
        try:
            sha256, path = _download(image.url, known_sha256=hints.get(image.index))
        except (requests.RequestException, OSError) as exc:
            # 一张图下载失败不该拖垮整条"看"命令——这张标"无法读取"、不进
            # opus 调用，其余照常；sha256="" 让 _merge_image_sha256 认出
            # 这不是真哈希，不会拿它去覆盖 state 里已有的值。
            log_store.write_log("warning", "xhs", f"第 {image.index} 张图下载失败：{exc}")
            results[image.index] = ImageDesc(
                index=image.index, kind=FALLBACK_KIND, text=DOWNLOAD_FAILED_TEXT, sha256=""
            )
            continue
        downloaded.append((image, sha256, path))

    pending: list[tuple[NoteImage, str, Path]] = []
    for image, sha256, path in downloaded:
        cached = _load_cached(sha256, mode)
        if cached is not None:
            span = _span_from_cache(cached)
            # 缓存按 sha256 存（内容相同即复用，天然跨笔记共享），但
            # `span` 记的是"在写这条缓存的那篇笔记里，第几张到第几张"——
            # 这是笔记内位置信息，不是图片内容的属性。两篇不同笔记碰巧
            # 用了同一张图（同一张水印图/贴纸/转发梗图）时，缓存文本仍然
            # 适用，但那个 span 对这篇笔记的序号毫无意义（2026-09-22
            # code-review 抓到：会渲染出跟这篇笔记序号对不上的"3–12
            # [系列]"）。这张图自己的序号都不落在 span 里，就说明 span
            # 是别的笔记留下的，丢掉、当成普通单张描述用。
            if span is not None and not (span[0] <= image.index <= span[1]):
                span = None
            results[image.index] = ImageDesc(
                index=image.index,
                kind=str(cached.get("kind") or "其他"),
                text=str(cached.get("text") or ""),
                sha256=sha256,
                span=span,
            )
        else:
            pending.append((image, sha256, path))

    if pending:
        # "全文"（逐字转录）和"仔细"（detail 模型仔细描述）都是用户点名
        # 要仔细看的少数几张，共用 detail_model()；brief（"看"命令默认的
        # 一句话批量描述）用便宜的 vision_model()（9/22 用户拍板方案 1）。
        model = detail_model() if mode in ("full", "detail") else vision_model()
        if mode in ("full", "detail"):
            # 逐张单独调用，不跟别的图打包在一起：BASE_TEMPLATE 开头
            # "先看第 1、2 张，判断是不是同一系列，是就只再看最后一张"
            # 这条规则是给 brief 批量描述用的省钱手段；全文/仔细这两种
            # 模式是用户点名要老实转录/仔细看的图，一旦跟别的图打包，
            # 模型可能把它们判成系列只读一张，实际没有按要求转录/仔细
            # 描述（2026-09-22 code-review 抓到：`读图 全文`（不带序号=
            # 全部张）批量请求一整篇教程帖时会撞上这条）。一次一张，
            # `files` 里只有这一张，系列判断天然用不上（没有"第二张"
            # 可比）。
            for image, sha256, path in pending:
                idx = (image.index,)
                batch = _run_vision_batch(
                    title=title,
                    targets=[(image, sha256, path)],
                    mode=mode,
                    model=model,
                    full_indexes=idx if mode == "full" else (),
                    detail_indexes=idx if mode == "detail" else (),
                )
                results.update(batch)
        else:
            results.update(_run_vision_batch(title=title, targets=pending, mode=mode, model=model))

    return [results[image.index] for image in targets]


def _run_vision_batch(
    *,
    title: str,
    targets: list[tuple[NoteImage, str, Path]],
    mode: str,
    model: str,
    full_indexes: tuple[int, ...] = (),
    detail_indexes: tuple[int, ...] = (),
) -> dict[int, ImageDesc]:
    """对 `targets` 打一次 `run_headless`、解析、写缓存，返回
    `{笔记内图片序号: ImageDesc}`。批量与否（brief 一次处理全部 pending、
    full/detail 一次只处理一张）由调用方决定，这里只管"打一次调用"。"""
    prompt = prompts.build_prompt(
        title=title,
        files=[(image.index, path) for image, _sha, path in targets],
        full_indexes=full_indexes,
        detail_indexes=detail_indexes,
    )
    try:
        payload = headless_claude.run_headless(
            prompt,
            model=model,
            tools="Read",
            allowed_tools="Read",
            workdir=paths.images_dir(),
            timeout=VISION_TIMEOUT,
        )
    except headless_claude.HeadlessError as exc:
        log_store.write_log("warning", "xhs", f"读图调用失败：{exc}")
        return {
            image.index: ImageDesc(index=image.index, kind=FALLBACK_KIND, text="读图失败，稍后再试", sha256=sha256)
            for image, sha256, _path in targets
        }
    parsed = _parse_vision_payload(payload, targets)
    result: dict[int, ImageDesc] = {}
    for image, sha256, _path in targets:
        desc = parsed[image.index]
        result[image.index] = desc
        _save_cache(sha256, mode, desc.kind, desc.text, span=desc.span)
    return result
