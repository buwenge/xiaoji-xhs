"""看守进程：拉起 Xvfb + 系统 Chrome，维持登录态，空闲/收到 SIGTERM 时
自己清理退出。`python -m xhs.keeper` 跑，由 `xhs.browser.ensure()` 用
`Popen(start_new_session=True)` 拉起（CLI 那次调用不等它跑完，看守进程
是独立进程组，跟 CLI 进程本身的生死无关）。

单例：`flock(paths.keeper_lock_path())` 非阻塞抢锁，抢不到说明已经有一个
看守进程在跑（或正在起），本进程什么都没做直接退出。

生命周期：起 Xvfb → 等一下 → 起 Chrome（`--remote-debugging-port` 等参数
见设计文档 S3）→ 等 CDP 就绪 → 起 ffmpeg（S4：`x11grab` 2 fps 覆写
`paths.frame_path()`，跟随实际 display，不是写死 `:99`）→ 写
`keeper.json` → 每 `XHS_KEEPER_POLL_INTERVAL`（默认 30）秒读一次
`paths.keeper_touch_path()` 的 mtime 当 `last_used_at` 回写进
`keeper.json`（`keeper.json` 只由本模块写，CLI 侧只 touch `keeper.touch`，
避免两个写者——设计文档 S3 现场补充），同时看一眼 Chrome 是否还活着；
Chrome 自己退出、空闲超过 `XHS_KEEPER_IDLE_TIMEOUT`（默认 600）秒、或收到
SIGTERM，三种情况都依次 SIGTERM ffmpeg/Chrome/Xvfb，等 5 秒还没退再
SIGKILL，删 `keeper.json`，退出——最后一帧留在 `paths.frame_path()` 不删
（"停在最后一帧"，设计文档 S4）。杀进程只用记下来的 PID，不用 `pkill -f`
（红线）。ffmpeg 只在 CDP 已就绪（确定 Chrome 真的起来了）之后才起，起不
来就写日志跳过、不让实时画面这个附加功能拖垮浏览器本体；CDP 一直不就绪
的失败路径里 ffmpeg 还没起过，不需要额外清理，不会泄漏进程。

测试注入点（惰性读 env，跟仓库其它模块同路数）：`XHS_KEEPER_XVFB_CMD`/
`XHS_KEEPER_CHROME_CMD`/`XHS_KEEPER_FFMPEG_CMD`（JSON 数组，覆盖真实命令
行，测试用 `["sleep", "100"]` 这类假命令站着不动代替真浏览器/ffmpeg，验证
的是"拉起/单例/空闲退出/SIGTERM 清理"这套进程管理逻辑，不是
Xvfb/Chrome/ffmpeg 本身）、`XHS_KEEPER_SKIP_CDP_WAIT=1`（假命令不会真开
CDP 端口，跳过等待直接判"就绪"）、`XHS_KEEPER_POLL_INTERVAL`/
`XHS_KEEPER_IDLE_TIMEOUT`（秒，测试用很小的值让空闲退出几百毫秒内就能观
察到，不用真等 10 分钟）、`XHS_SCREEN_PATH`（`xhs.paths.frame_path()` 的
测试覆盖，见该函数注释）。生产环境这几个 env 都不设，走真实值。
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from xhs import paths, selectors
from xhs import state as state_store

TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_POLL_INTERVAL = 30.0
DEFAULT_IDLE_TIMEOUT = 600.0
CDP_READY_TIMEOUT = 20.0
TERMINATE_GRACE = 5.0
DISPLAY_SCREEN = "1000x1400x24"
WINDOW_SIZE = "1000,1400"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def poll_interval() -> float:
    return _env_float("XHS_KEEPER_POLL_INTERVAL", DEFAULT_POLL_INTERVAL)


def idle_timeout() -> float:
    return _env_float("XHS_KEEPER_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT)


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


def _free_display(start: int = 99) -> int:
    display = start
    while Path(f"/tmp/.X{display}-lock").exists():
        display += 1
    return display


def _xvfb_cmd(display: int) -> list[str]:
    override = os.environ.get("XHS_KEEPER_XVFB_CMD", "").strip()
    if override:
        return json.loads(override)
    return ["Xvfb", f":{display}", "-screen", "0", DISPLAY_SCREEN]


def _chrome_cmd(display: int, port: int, profile: Path) -> list[str]:
    override = os.environ.get("XHS_KEEPER_CHROME_CMD", "").strip()
    if override:
        return json.loads(override)
    return [
        "google-chrome-stable",
        "--no-sandbox",
        "--test-type",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--lang=zh-CN",
        f"--window-size={WINDOW_SIZE}",
        "--window-position=0,0",
        "--disable-blink-features=AutomationControlled",
    ]


def _ffmpeg_cmd(display: int, frame: Path) -> list[str]:
    """S4：`x11grab` 2 fps 抓当前 display，缩到 540 宽，原子覆写单张 JPEG
    （设计文档 S4 逐字命令）。`-i` 跟随传入的实际 display，不写死 `:99`。"""
    override = os.environ.get("XHS_KEEPER_FFMPEG_CMD", "").strip()
    if override:
        return json.loads(override)
    return [
        "ffmpeg",
        "-loglevel", "error",
        "-f", "x11grab",
        "-framerate", "2",
        "-video_size", "1000x1400",
        "-i", f":{display}",
        "-vf", "scale=540:-1",
        "-q:v", "6",
        "-update", "1",
        "-atomic_writing", "1",
        "-y", str(frame),
    ]


def _skip_cdp_wait() -> bool:
    return os.environ.get("XHS_KEEPER_SKIP_CDP_WAIT", "").strip() == "1"


def wait_cdp_ready(
    port: int, timeout: float = CDP_READY_TIMEOUT, should_stop=lambda: False
) -> bool:
    """轮询 `http://127.0.0.1:<port>/json/version`；测试用假命令
    （`sleep`）不会真开端口，`XHS_KEEPER_SKIP_CDP_WAIT=1` 让它直接判
    "就绪"，不用真的开一个假 HTTP 服务器陪跑。`should_stop` 每轮检查一次
    ——SIGTERM 落在等待期间不用等满 `timeout`（默认 20 秒）才能退出：调用
    方看到返回 False 时应先看 `should_stop()`，为真就走"收到停止请求"分支
    而不是"CDP 一直没就绪"分支（续工单第 2 项）。"""
    if _skip_cdp_wait():
        return True
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    while time.monotonic() < deadline:
        if should_stop():
            return False
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def _terminate(proc: subprocess.Popen | None, *, grace: float = TERMINATE_GRACE) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def _write_keeper_json(data: dict) -> None:
    """`xhs.state.save` 本来就是"原子写一个 dict 到指定路径"，不用在这里
    再手搓一遍 tempfile+os.replace（2026-09-22 /simplify reuse 审查指出，
    顺带拿到它的异常安全清理——写失败时不会在 `.xhs/` 里留一个孤儿
    `.tmp-<pid>` 文件）。keeper.json 每 30 秒被读一次（比如 S4 的 live
    接口），半吊子的写一半文件不能被读到。"""
    state_store.save(data, paths.keeper_json_path())


def _remove_keeper_json() -> None:
    try:
        paths.keeper_json_path().unlink()
    except OSError:
        pass


def _sleep_checking_stop(total: float, should_stop) -> None:
    """跟一次性 `time.sleep(total)` 不一样：SIGTERM 的默认处理是"重试被
    打断的系统调用直到整段时间睡完"（PEP 475），一次性长睡眠会让"放下"
    命令看起来卡住不退出。拆成小步睡、每步之间检查一次退出标志，SIGTERM
    落地后最多再等一小步就能真正开始清理。"""
    step = 0.5
    elapsed = 0.0
    while elapsed < total:
        if should_stop():
            return
        this_step = min(step, total - elapsed)
        time.sleep(this_step)
        elapsed += this_step


def run() -> int:
    paths.ensure_dirs()
    lock_path = paths.keeper_lock_path()
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # 已经有一个看守进程在跑（或正在起）——本进程什么都没建、也没碰过
        # keeper.json（那是别人的），直接退出。
        return 0

    display = _free_display()
    log_handle = open(paths.keeper_log_path(), "a", encoding="utf-8")
    log_handle.write(f"[{_now_iso()}] 起看守进程 pid={os.getpid()} display={display}\n")
    log_handle.flush()

    # 先把三个孩子的变量初始化成 None，再装 SIGTERM handler，最后才开始
    # 逐个起子进程：不管 SIGTERM 落在哪个时间点、不管哪一步半路抛异常
    # （Chrome Popen 失败、写 keeper.json 失败……），下面唯一一个 finally
    # 都能安全地对着"当时已经起了几个"收尾，不会因为某个变量还没赋值就
    # 漏清理（审查意见 1.2）。
    xvfb_proc: subprocess.Popen | None = None
    chrome_proc: subprocess.Popen | None = None
    ffmpeg_proc: subprocess.Popen | None = None

    stop_requested = {"flag": False}

    def _on_sigterm(signum, frame):
        stop_requested["flag"] = True

    def _stopped() -> bool:
        return stop_requested["flag"]

    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        xvfb_proc = subprocess.Popen(_xvfb_cmd(display), stdout=log_handle, stderr=log_handle)
        # 给 Xvfb 一点起步时间，避免 Chrome 立刻连一个还没起来的 display；
        # 拆成可中断的小步睡而不是一次性 `time.sleep(1.0)`——SIGTERM 落在
        # 这段起步间隙里也要能及时响应，不能等一次性睡眠自己睡完（续工单
        # 第 2 项："收到停止仍继续 spawn Chrome"）。
        _sleep_checking_stop(1.0, _stopped)
        if _stopped():
            log_handle.write(f"[{_now_iso()}] 起步间隙收到停止请求，不再起 Chrome\n")
            return 0

        profile = paths.profile_dir()
        profile.mkdir(parents=True, exist_ok=True)
        chrome_env = dict(os.environ)
        chrome_env["DISPLAY"] = f":{display}"
        chrome_env["TZ"] = "Asia/Shanghai"
        chrome_env["LANG"] = "zh_CN.UTF-8"
        try:
            chrome_proc = subprocess.Popen(
                _chrome_cmd(display, selectors.cdp_port(), profile),
                stdout=log_handle,
                stderr=log_handle,
                env=chrome_env,
            )
        except OSError as exc:
            log_handle.write(f"[{_now_iso()}] Chrome 起不来，清理：{exc}\n")
            return 1

        ready = wait_cdp_ready(selectors.cdp_port(), should_stop=_stopped)
        if _stopped():
            log_handle.write(f"[{_now_iso()}] 等 CDP 期间收到停止请求，不再起 ffmpeg\n")
            return 0
        if not ready:
            log_handle.write(f"[{_now_iso()}] CDP 一直没就绪，放弃并清理\n")
            return 1

        # S4：Chrome 确定起来了才起 ffmpeg——CDP 没就绪那条失败路径永远
        # 轮不到这一行。ffmpeg 起不来（比如二进制缺失）只记日志、不影响
        # 浏览器本体："看不了实时画面"不该连"能刷小红书"都搭进去。
        try:
            ffmpeg_proc = subprocess.Popen(
                _ffmpeg_cmd(display, paths.frame_path()),
                stdout=log_handle,
                stderr=log_handle,
            )
        except OSError as exc:
            log_handle.write(f"[{_now_iso()}] ffmpeg 起不来，先不管实时画面：{exc}\n")
            ffmpeg_proc = None

        if _stopped():
            log_handle.write(f"[{_now_iso()}] 三个孩子起完时收到停止请求，不再写 ready 状态\n")
            return 0

        started_at = _now_iso()
        paths.touch_keeper()
        state = {
            "pid": os.getpid(),
            "display": display,
            "cdp_port": selectors.cdp_port(),
            "started_at": started_at,
            "last_used_at": started_at,
            "stage": "ready",
        }
        _write_keeper_json(state)
        log_handle.write(f"[{_now_iso()}] 就绪：{state}\n")
        log_handle.flush()

        interval = poll_interval()
        idle_limit = idle_timeout()

        last_written_ts: float | None = None
        while not stop_requested["flag"]:
            _sleep_checking_stop(interval, lambda: stop_requested["flag"])
            if stop_requested["flag"]:
                break
            if chrome_proc.poll() is not None:
                # Chrome 自己退出了（崩溃/被外部杀掉），不是空闲超时也不是
                # SIGTERM，但同样要清理剩下两个孩子（设计文档 S4："Chrome
                # 退出、空闲或 SIGTERM 时均清理三个孩子"）——不然 ffmpeg
                # 会对着一个死掉的 Chrome 留下的最后一帧空转，Xvfb 也会
                # 孤零零地一直挂着。
                log_handle.write(f"[{_now_iso()}] Chrome 自己退出了，清理\n")
                break
            try:
                last_used_ts = paths.keeper_touch_path().stat().st_mtime
            except OSError:
                last_used_ts = time.time()
            # touch 没变就不用重写 keeper.json——空闲挂着的这几十分钟，每
            # 30 秒写一份内容完全相同的文件纯属浪费磁盘 I/O（2026-09-22
            # /simplify efficiency 审查指出）；空闲超时判断本身仍然每轮
            # 都算。
            if last_written_ts != last_used_ts:
                state["last_used_at"] = datetime.fromtimestamp(last_used_ts, tz=TZ).isoformat()
                _write_keeper_json(state)
                last_written_ts = last_used_ts
            if time.time() - last_used_ts > idle_limit:
                log_handle.write(f"[{_now_iso()}] 空闲超过 {idle_limit}s，退出\n")
                break

        return 0
    finally:
        # 不管上面是正常走到这、提前 return，还是任何一步抛了异常（Chrome
        # Popen 失败已经被上面 catch 成 return 1，但 touch_keeper()/
        # _write_keeper_json() 半路失败这种没被专门 catch 的异常也会经过
        # 这里）——已经起来的孩子（不管几个）都会被清理，未起来的
        # `_terminate(None)` 是 no-op。
        log_handle.write(f"[{_now_iso()}] 清理退出\n")
        _terminate(ffmpeg_proc)
        _terminate(chrome_proc)
        _terminate(xvfb_proc)
        _remove_keeper_json()  # 只删状态文件；最后一帧留在 paths.frame_path()（"停在最后一帧"）
        log_handle.flush()
        log_handle.close()


if __name__ == "__main__":
    sys.exit(run())
