"""load/save `.xhs/state.json`，原子写（临时文件 + `os.replace`）。

跟仓库其它状态文件（`morning_paper.STATE_PATH` 等）同一个路数：`path`
参数默认 `None`，函数体里才回读 `paths.state_file()` 当前值，不写进签名
默认值——保证测试改道 `xhs.paths.XHS_DIR` 之后，不传 `path=` 的调用点也
真的落到临时目录。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xhs import paths
from file_io import write_json_atomic

DEFAULT_STATE: dict[str, Any] = {
    "day": None,
    "tokens_today": 0,
    "reported": [],
    "last_call_at": None,
    "current_note": None,
    "last_list": None,
}


def load(path: Path | None = None) -> dict[str, Any]:
    """读 state.json；文件不存在/内容损坏/形状不对都退回一份全新默认
    state（不抛异常——坏掉的计数状态不该拦住小机继续刷）。"""
    target = path or paths.state_file()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return dict(DEFAULT_STATE)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return dict(DEFAULT_STATE)
    if not isinstance(data, dict):
        return dict(DEFAULT_STATE)
    merged = dict(DEFAULT_STATE)
    merged.update(data)
    return merged


def save(state: dict[str, Any], path: Path | None = None) -> None:
    target = path or paths.state_file()
    write_json_atomic(target, state)
