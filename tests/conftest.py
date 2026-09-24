"""测试全局改道：任何测试都碰不到真实的 `~/.xhs/`、共享内存帧和活动日志。

`xhs.paths.XHS_DIR` / `DEFAULT_FRAME_PATH` 都是模块属性、调用时当轮取，
所以在收集阶段改一次就对所有没显式传路径的调用点生效；测试里真起
`home.py` 子进程的那几条靠环境变量继承同一改道。
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import log_store  # noqa: E402

log_store.LOG_FILE = Path(tempfile.gettempdir()) / "xiaoji-xhs-test-logs.jsonl"
os.environ["XIAOJI_LOG_FILE"] = str(log_store.LOG_FILE)

import xhs.paths as xhs_paths  # noqa: E402

_XHS_TEST_DIR = tempfile.TemporaryDirectory(prefix="xiaoji-test-xhs-")
xhs_paths.XHS_DIR = Path(_XHS_TEST_DIR.name)
xhs_paths.DEFAULT_FRAME_PATH = Path(_XHS_TEST_DIR.name) / "xhs_screen.jpg"
