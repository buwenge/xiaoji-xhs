import json
import os
import pathlib
from datetime import datetime

import pytz

# XIAOJI_LOG_FILE 只给测试用：tests/conftest.py 设了它，测试里起的子进程
# （真跑 home.py 那几条）也跟着写临时文件，不再漏进生产 logs.jsonl（9/23）。
LOG_FILE = pathlib.Path(os.environ.get("XIAOJI_LOG_FILE") or pathlib.Path(__file__).parent / "logs.jsonl")
TZ = pytz.timezone("Asia/Shanghai")

# 排查用的内部诊断日志（如思维链截断排查），只写文件供事后翻查，
# 不该跟着"全部"混进前端日志面板刷屏；显式按分类筛选时仍能取到。
INTERNAL_ONLY_CATEGORIES = {"thinking_debug"}


def write_log(
    level: str,
    category: str,
    message: str,
    detail: dict = None,
    *,
    log_file: pathlib.Path = None,
) -> dict:
    entry = {
        "timestamp": datetime.now(TZ).isoformat(),
        "level": level,
        "category": category,
        "message": message,
    }
    if detail:
        entry["detail"] = detail
    with open(log_file or LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_logs(filter_category: str = "all", limit: int = 50) -> list:
    if not LOG_FILE.exists():
        return []
    logs = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if filter_category == "all":
                if entry.get("category") in INTERNAL_ONLY_CATEGORIES:
                    continue
            elif entry.get("category") != filter_category:
                continue
            logs.append(entry)
    return logs[-limit:]
