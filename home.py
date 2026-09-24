#!/usr/bin/env python3
"""小机的"家庭能力"统一入口（公开版只带小红书这一个类别）。

AI 在自己的 shell 工具里跑 `home 小红书 …`，这个脚本把第一个词认成类别，
剩下的原话交给 `xhs.cli.handle()`，结果打到 stdout；出错时只在 stderr
打一句中文、退出码 2，不吐 traceback——AI 读到的永远是一句人话。

原版 home.py 还挂着手环、床头灯、聊天记录、电话、院子、晨报等类别，
都是同一个套路：在 `DOMAIN_ALIASES` 加一行别名、在 `main()` 加一个分支。
想给自己的机器人加别的能力，照着 xhs 分支抄就行。
"""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from dotenv import load_dotenv

# `home` 每次命令都是一个新进程；必须在导入任何读取环境变量的模块之前
# 加载项目 .env。python-dotenv 默认不覆盖调用方已经显式设置的环境变量。
load_dotenv()


DOMAIN_ALIASES = {
    "xhs": ("小红书", "红书", "刷小红书", "xhs"),
}

CATEGORY_HINT = "请先说类别：home 小红书 …"


class HomeError(Exception):
    pass


@dataclass(frozen=True)
class Request:
    domain: str
    text: str
    raw_text: str = ""
    help: bool = False
    dry_run: bool = False


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).strip().lower()
    return re.sub(r"[\s，。！？、；：:]+", "", text)


def best_match(text: str, aliases: dict[str, tuple[str, ...]], threshold: float) -> str | None:
    """无模型的近似匹配：只给命令抬头兜底少量错别字。"""
    text = normalize(text)
    best_domain, best_score = None, 0.0
    for domain, words in aliases.items():
        for word in words:
            score = SequenceMatcher(None, text, normalize(word)).ratio()
            if score > best_score:
                best_domain, best_score = domain, score
    return best_domain if best_score >= threshold else None


def remove_normalized_phrase(raw: str, phrase: str) -> str:
    """从原话里去掉类别词，保留其余部分的原始写法（大小写、空格、标点）。"""
    compact_chars: list[str] = []
    raw_indexes: list[int] = []
    for index, char in enumerate(raw):
        for normalized_char in unicodedata.normalize("NFKC", char).lower():
            if re.match(r"[\s，。！？、；：:]", normalized_char):
                continue
            compact_chars.append(normalized_char)
            raw_indexes.append(index)
    compact = "".join(compact_chars)
    target = normalize(phrase)
    position = compact.find(target)
    if position < 0:
        return raw
    start = raw_indexes[position]
    end = raw_indexes[position + len(target) - 1] + 1
    return (raw[:start] + " " + raw[end:]).strip()


def parse_request(argv: list[str]) -> Request:
    help_requested = any(arg in ("-h", "--help", "帮助", "怎么用") for arg in argv)
    dry_run = "--dry-run" in argv
    raw_tokens = [arg for arg in argv if arg not in ("-h", "--help", "--dry-run")]
    raw = " ".join(raw_tokens).strip()
    if not raw or normalize(raw) in ("帮助", "怎么用"):
        raise HomeError(CATEGORY_HINT)
    tokens = [normalize(arg) for arg in raw_tokens]
    head = tokens[0] if tokens else ""
    matches: list[tuple[int, int, str, str]] = []
    for domain, aliases in DOMAIN_ALIASES.items():
        for alias in aliases:
            pos = head.find(normalize(alias))
            if pos >= 0:
                matches.append((pos, -len(alias), domain, alias))
    if matches:
        _, _, domain, alias = min(matches)
        raw_remainder = remove_normalized_phrase(raw, alias)
        remainder = normalize(raw_remainder)
    else:
        # 没有精确类别名时，只对命令抬头做近似匹配，避免正文抢走类别。
        domain = best_match(head, DOMAIN_ALIASES, threshold=0.49)
        if domain is None:
            raise HomeError(CATEGORY_HINT)
        raw_remainder = " ".join(raw_tokens[1:]).strip()
        remainder = normalize("".join(tokens[1:]))
    return Request(
        domain=domain,
        text=remainder,
        raw_text=raw_remainder,
        help=help_requested,
        dry_run=dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        request = parse_request(argv)
        if request.domain == "xhs":
            # xhs 包只抛 XhsError，在这里转成 HomeError：home.py 被当成
            # __main__ 跑时，别的模块 `from home import HomeError` 拿到的是
            # 另一个类对象，抓不住（见 xhs/cli.py 顶部注释）。
            from xhs import XhsError
            from xhs import cli as xhs_cli

            try:
                output = xhs_cli.handle(request)
            except XhsError as exc:
                raise HomeError(str(exc)) from exc
        else:
            raise HomeError(CATEGORY_HINT)
        print(output)
        return 0
    except HomeError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
