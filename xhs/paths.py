"""`.xhs/` 目录下各文件路径 + 目录准备 + 过期清理。

跟 `morning_feedback.FEEDBACK_PATH`/`garden.GARDEN_FILE` 一个路数：模块级
路径常量只在函数体里"当轮取"（`xxx_dir()` 每次调用都读一次 `XHS_DIR`
当前值），不焊进函数签名默认值，测试靠 `patch.object(xhs.paths,
"XHS_DIR", tmp)` 就能真正改道（`tests/conftest.py` 收集阶段已经把它指到
临时目录，任何测试都碰不到真实的 `~/.xhs/`）。

默认目录特意选在机器人仓库**外**（`~/.xhs`，不是
`<仓库>/.xhs`）：`vision.py` 读图时会把 cwd 切到 `images_dir()`
下跑 `claude -p`，而 `claude -p` 会往上找父目录链上的 `CLAUDE.md` 一路
喂给模型——cwd 在机器人仓库底下就会把小机的人格文件整份
搭车塞进每次读图请求，白吃约 8k token 还可能带偏模型的 JSON 输出。
部署时确认一下家目录和 `~/.claude/` 下没有 `CLAUDE.md`，否则用 `XHS_DIR` 换个地方。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

XHS_DIR = Path(os.environ.get("XHS_DIR", Path.home() / ".xhs"))

IMAGES_DIRNAME = "images"
DESCRIPTIONS_DIRNAME = "descriptions"
PROFILE_DIRNAME = "profile"
STATE_FILENAME = "state.json"
KEEPER_JSON_FILENAME = "keeper.json"
KEEPER_TOUCH_FILENAME = "keeper.touch"
KEEPER_LOG_FILENAME = "keeper.log"
KEEPER_LOCK_FILENAME = "keeper.lock"

IMAGES_TTL_SECONDS = 24 * 3600
DESCRIPTIONS_TTL_SECONDS = 7 * 24 * 3600

# S4：keeper 里的 ffmpeg 覆写的最新一帧，daemon 的 `/api/xhs/screen` 读出去。
# 特意默认在 `XHS_DIR` 之外的 `/dev/shm`（设计文档第 4 节：只留内存最新一张，
# 不落盘、不跟 images/descriptions 一起走 24h/7 天清理）。`DEFAULT_FRAME_PATH`
# 是模块属性（不是写进函数默认参数），`tests/conftest.py` 收集阶段整体改道到
# 临时文件，任何测试都碰不到真实共享内存路径；单个测试还能用
# `XHS_SCREEN_PATH` 再单独指定一次。
DEFAULT_FRAME_PATH = Path("/dev/shm/xhs_screen.jpg")


def frame_path() -> Path:
    raw = os.environ.get("XHS_SCREEN_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_FRAME_PATH


def state_file() -> Path:
    return XHS_DIR / STATE_FILENAME


def images_dir() -> Path:
    return XHS_DIR / IMAGES_DIRNAME


def descriptions_dir() -> Path:
    return XHS_DIR / DESCRIPTIONS_DIRNAME


def profile_dir() -> Path:
    """Chrome `--user-data-dir`：登录态在这里，永久保留（不随 images/
    descriptions 一起清理）。"""
    return XHS_DIR / PROFILE_DIRNAME


def keeper_json_path() -> Path:
    """看守进程状态：`{pid, display, cdp_port, started_at, last_used_at,
    stage}`。只由 `xhs.keeper` 自己写；CLI 侧只读，要"续命"就 touch
    `keeper_touch_path()`，不直接改这个文件（避免两个写者，见设计文档 S3
    现场补充）。"""
    return XHS_DIR / KEEPER_JSON_FILENAME


def keeper_touch_path() -> Path:
    """CLI 每次用到浏览器就 touch 这个文件的 mtime；看守进程每轮轮询读它
    当 `last_used_at` 回写进 `keeper_json_path()`。"""
    return XHS_DIR / KEEPER_TOUCH_FILENAME


def keeper_log_path() -> Path:
    return XHS_DIR / KEEPER_LOG_FILENAME


def keeper_lock_path() -> Path:
    """看守进程单例锁（`flock`），防止两个 CLI 进程同时
    各起一个看守进程。"""
    return XHS_DIR / KEEPER_LOCK_FILENAME


def touch_keeper() -> None:
    """更新 `keeper_touch_path()` 的 mtime——`xhs.browser.ensure()`
    每次用到浏览器前后都调一次（一次浏览器命令会调好几次）。只保证
    `XHS_DIR` 本身在，不叫完整的 `ensure_dirs()`（那会顺带 mkdir
    images/descriptions/profile 三个跟"记一下时间戳"毫不相干的目录，
    2026-09-22 /simplify efficiency 审查指出：`cli._handle` 开头已经
    调过一次完整的 `ensure_dirs()`，这里只需保底目录还在）。"""
    XHS_DIR.mkdir(parents=True, exist_ok=True)
    keeper_touch_path().touch(exist_ok=True)


def ensure_dirs() -> None:
    """每次 `xhs.cli.handle` 开头跑：保证 `.xhs/`、`images/`、
    `descriptions/`、`profile/` 都在。"""
    for directory in (XHS_DIR, images_dir(), descriptions_dir(), profile_dir()):
        directory.mkdir(parents=True, exist_ok=True)


def cleanup(now: float | None = None) -> None:
    """删 24h 前的 `images/*`、7 天前的 `descriptions/*`；单个文件删失败
    静默跳过，不让清理阻断正常命令。"""
    now = time.time() if now is None else now
    _cleanup_dir(images_dir(), IMAGES_TTL_SECONDS, now)
    _cleanup_dir(descriptions_dir(), DESCRIPTIONS_TTL_SECONDS, now)


def _cleanup_dir(directory: Path, ttl_seconds: float, now: float) -> None:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if now - entry.stat().st_mtime > ttl_seconds:
                entry.unlink()
        except OSError:
            continue
