"""识图 agent 用的截帧工具：`python frame_tool.py 1:30 2:05 …`。

只在 `xhs.video.watch` 起的无头会话里跑，cwd 是 `images/`，从那里的
`JOB_FILE` 读视频地址、时长和帧数上限（agent 看不到也改不了地址），
每截一次把用掉的帧数和截成的帧记回去，超了就拒绝。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 直接按脚本跑时 sys.path[0] 是 xhs/ 自己，里面的 selectors.py 会顶掉
# 标准库 selectors（subprocess 一 import 就炸）——换成仓库根目录。
sys.path[0] = str(Path(__file__).resolve().parent.parent)

from xhs import video  # noqa: E402


def main(argv: list[str]) -> int:
    job_path = Path.cwd() / video.JOB_FILE
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("没有进行中的抽帧任务")
        return 1
    duration = float(job["duration"])
    times: list[float] = []
    for token in argv:
        seconds = video.parse_clock(token)
        if seconds is None:
            print(f"看不懂时间点「{token}」，写成 分:秒，比如 1:30")
            return 1
        if seconds >= duration:
            print(f"{token} 超出了，视频只有 {video.fmt_clock(duration)}")
            return 1
        times.append(seconds)
    if not times:
        print("要写时间点，比如 1:30 2:05")
        return 1
    left = int(job["max_frames"]) - int(job["used"])
    if len(times) > left:
        print(f"这次任务只剩 {left} 帧可截")
        return 1
    frames = video.grab_frames(job["url"], times, Path.cwd())
    job["used"] = int(job["used"]) + len(times)
    # 截成的帧记下来，抽帧结束后存进 current_note，小机 `发帧` 时直接用
    job.setdefault("frames", []).extend(
        {"t": seconds, "path": str(path)} for seconds, path in zip(times, frames) if path
    )
    job_path.write_text(json.dumps(job), encoding="utf-8")
    for seconds, path in zip(times, frames):
        print(f"{video.fmt_clock(seconds)} {path if path else '没截下来'}")
    print(f"还能截 {int(job['max_frames']) - job['used']} 帧")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
