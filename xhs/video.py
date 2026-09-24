"""视频笔记抽帧（`抽帧` 命令，9/24）。

小机的原话原样交给识图 agent（无头 claude，只放行 Read 和下面这一条截帧
命令），由它自己决定在哪些时间点截、截几帧。截帧用 ffmpeg 直接对 CDN
地址 `-ss` 定位，靠 HTTP Range 只拉那一小段，不下载整条视频（12 分钟
107MB 的视频实测每帧 1–2 秒）。

agent 的工作目录就是 `images/`（没有 CLAUDE.md，跟读图同一个约定）；
视频地址、时长、帧数上限写在那里的 `JOB_FILE`，截帧工具
（`xhs/frame_tool.py`）从自己的 cwd 读它，agent 命令行里只需要写时间点。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import headless_claude
from xhs import XhsError, fetch_light, paths, prompts, vision

FFMPEG = "ffmpeg"
FRAME_TIMEOUT = 40
FRAME_WORKERS = 3
MAX_FRAMES = 12
AGENT_TIMEOUT = 360
OUTPUT_CHAR_LIMIT = 2400
DESC_CHAR_LIMIT = 300
JOB_FILE = ".frame-job.json"
FRAME_TOOL = Path(__file__).resolve().parent / "frame_tool.py"
# 长边 960：字幕看得清，一帧喂给模型约 1k token 以内。
SCALE_FILTER = "scale='if(gt(iw,ih),960,-2)':'if(gt(iw,ih),-2,960)'"

_CLOCK_RE = re.compile(r"^(\d+):(\d{1,2})(?::(\d{1,2}))?$")


def fmt_clock(seconds: float) -> str:
    """视频时间点：`12:13`、`1:02:03`。"""
    total = max(int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def parse_clock(token: str) -> float | None:
    """`1:30`、`1:02:03`、纯秒数 `90`/`90.5`；认不出返回 None。"""
    token = token.strip()
    match = _CLOCK_RE.match(token)
    if match:
        a, b, c = match.groups()
        return int(a) * 3600 + int(b) * 60 + int(c) if c is not None else int(a) * 60 + int(b)
    try:
        value = float(token)
    except ValueError:
        return None
    return value if value >= 0 else None


def grab_frame(url: str, seconds: float, out_dir: Path) -> Path | None:
    """截一帧存成 `<sha256>.jpg`；失败（超时、地址过期、越界）返回 None。"""
    tmp = out_dir / f".frame-{uuid.uuid4().hex}.jpg"
    # -user_agent 只有网络输入认，本地文件（测试用）带上会直接报错
    ua = ["-user_agent", fetch_light.MOBILE_UA] if url.startswith("http") else []
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", *ua,
        "-ss", f"{seconds:.2f}", "-i", url,
        "-frames:v", "1", "-vf", SCALE_FILTER, "-q:v", "4", "-y", str(tmp),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT, check=True)
        content = tmp.read_bytes()
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        tmp.unlink(missing_ok=True)
    if not content:
        return None
    path = out_dir / f"{hashlib.sha256(content).hexdigest()}.jpg"
    if not path.exists():
        path.write_bytes(content)
    return path


def grab_frames(url: str, seconds: list[float], out_dir: Path) -> list[Path | None]:
    """按传入顺序返回，失败的位置是 None。"""
    with ThreadPoolExecutor(max_workers=min(FRAME_WORKERS, max(len(seconds), 1))) as pool:
        return list(pool.map(lambda t: grab_frame(url, t, out_dir), seconds))


def tool_command() -> str:
    """agent 要敲的命令前缀；`--allowedTools` 按它做前缀放行。"""
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(FRAME_TOOL))}"


def watch(*, title: str, desc: str, duration: int, url: str, request: str) -> tuple[str, list[dict]]:
    """跑一次识图 agent，返回 (它写给小机的文字, 这次截成的帧
    `[{"t": 秒, "path": 路径}]`，按时间排好)。失败抛 `XhsError`。"""
    workdir = paths.images_dir()
    workdir.mkdir(parents=True, exist_ok=True)
    job_path = workdir / JOB_FILE
    job_path.write_text(
        json.dumps({"url": url, "duration": duration, "max_frames": MAX_FRAMES, "used": 0, "frames": []}),
        encoding="utf-8",
    )
    command = tool_command()
    prompt = prompts.build_frames_prompt(
        title=title,
        desc=desc[:DESC_CHAR_LIMIT],
        duration=fmt_clock(duration),
        request=request,
        command=command,
        max_frames=MAX_FRAMES,
    )
    try:
        payload = headless_claude.run_headless(
            prompt,
            model=vision.vision_model(),
            tools="Read,Bash",
            allowed_tools=f"Read,Bash({command}:*)",
            workdir=workdir,
            timeout=AGENT_TIMEOUT,
        )
        frames = json.loads(job_path.read_text(encoding="utf-8")).get("frames") or []
    except headless_claude.HeadlessError as exc:
        raise XhsError(f"抽帧没成功，稍后再试（{str(exc)[:80]}）") from exc
    finally:
        job_path.unlink(missing_ok=True)
    text = str(payload.get("result") or "").strip()
    if not text:
        raise XhsError("抽帧没成功：识图那边什么都没写回来")
    if len(text) > OUTPUT_CHAR_LIMIT:
        text = text[:OUTPUT_CHAR_LIMIT] + "…"
    return text, sorted(frames, key=lambda frame: frame["t"])
