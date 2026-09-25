"""小文件读写的共用小工具：独占文件锁、容错读 JSON、原子写 JSON。**标准库 only**。

新代码要锁/原子写 JSON 先用这里，别再写一份。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterator


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """护住 `path` 的完整读改写（多个进程同时读改写时排队）。
    锁文件是同目录下的 `.<文件名>.lock`，只做 flock 用，内容为空。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(f".{path.name}.lock").open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_json(path: Path, default):
    """读不到（不存在/没权限）或不是合法 JSON 都回 `default`。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    """临时文件 + fsync + `os.replace` + 目录 fsync；写失败不留孤儿临时文件。

    已有文件保持原权限（临时文件默认 600，不跟过来的话 644 的 state.json 之类
    会被悄悄改成 600）；新文件就是 600。目录 fsync 让改名这一步也扛得住断电。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
