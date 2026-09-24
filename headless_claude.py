#!/usr/bin/env python3
"""隔离沙箱跑一次无头 `claude -p`，供任何需要"派一个独立子进程干一件事、
只要结果不要对话"的场景复用（原属 `morning_archive._invoke_haiku`，
2026-09-22 泛化搬出，供 `xhs.vision` 读图复用同一套隔离写法：`timeout` +
`env -i` + 一年期令牌 + `--output-format json` + 检查 `is_error`/
`permission_denials`）。

工作目录/环境隔离/工具白名单由调用方决定（不同场景要读写的文件不一样），
本模块只负责"怎么安全地跑这一次子进程、怎么判断这次调用算不算成功"。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# 无头子进程用的长期令牌文件（`claude setup-token` 生成后自己存成
# `CLAUDE_CODE_OAUTH_TOKEN=...` 一行）；不存在就走子进程默认凭据。
TOKEN_FILE = Path(os.environ.get("CLAUDE_OAUTH_TOKEN_FILE", Path.home() / ".claude-oauth-token.env"))
SUBPROCESS_GRACE = 15  # 给外层 `timeout` 命令先动手的缓冲，python 侧 timeout 略长一点


class HeadlessError(Exception):
    """一次隔离沙箱的无头 `claude -p` 调用失败（进程非零退出/JSON 解析
    失败/`is_error`/权限被拒）。"""


class HeadlessTimeout(HeadlessError):
    pass


def _claude_oauth_token_env_arg() -> list[str]:
    """从一年期令牌文件里取 CLAUDE_CODE_OAUTH_TOKEN，供 env -i 显式传入
    （env -i 清空继承环境，调用方大多也没有登录态）。文件不存在/没有这个
    键就返回空列表，让子进程照旧走默认凭据（会因登录态过期而失败，报错
    信息足够定位）。
    """
    try:
        text = TOKEN_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
            token = line.split("=", 1)[1].strip()
            if token:
                return [f"CLAUDE_CODE_OAUTH_TOKEN={token}"]
    return []


def run_headless(
    prompt: str,
    *,
    model: str,
    tools: str,
    allowed_tools: str,
    workdir: Path,
    timeout: int,
) -> dict:
    """跑一次隔离沙箱的无头 `claude -p` 会话，返回 `--output-format json`
    解析后的 payload（成功时的原始 dict，调用方自己从里面取想要的字段）。
    失败/超时/权限被拒/`is_error` 统一转成 HeadlessError/HeadlessTimeout
    抛出，调用方按自己的场景再包一层业务异常。
    """
    cmd = [
        "timeout",
        str(timeout),
        "env",
        "-i",
        f"HOME={Path.home()}",
        f"PATH={os.environ.get('PATH', '')}",
        "TZ=Asia/Shanghai",
        *_claude_oauth_token_env_arg(),
        "claude",
        "-p",
        "--model",
        model,
        "--tools",
        tools,
        "--allowedTools",
        allowed_tools,
        "--output-format",
        "json",
        prompt,
    ]
    try:
        result = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout + SUBPROCESS_GRACE
        )
    except subprocess.TimeoutExpired as exc:
        raise HeadlessTimeout(f"子进程超时（{timeout}s）") from exc
    if result.returncode != 0:
        raise HeadlessError(f"claude -p 退出码 {result.returncode}：{(result.stderr or '')[-300:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HeadlessError("claude -p 输出不是合法 JSON") from exc
    if payload.get("is_error"):
        raise HeadlessError(f"claude -p 报告失败：{payload.get('result')}")
    denials = payload.get("permission_denials") or []
    if denials:
        raise HeadlessError(f"claude -p 权限被拒：{denials}")
    return payload
