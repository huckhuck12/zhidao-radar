"""
独立采集进程

Streamlit 在 Windows 上会把事件循环策略切成 Selector，导致 Playwright 无法
创建子进程（NotImplementedError / 直接卡死）。所以采集放到独立进程里跑，
面板只负责启动进程、转发日志、读取结果。

用法（由 app.py 调用）：
    python collect_cli.py <payload.json> <result.json>

payload.json 字段：queries / max_per_query / interval / headless / browser_dir / profile_dir
stdout 每行格式：<level>\\t<message>，level ∈ debug/info/success/warn/error
"""

import asyncio
import json
import sys
from pathlib import Path

from crawler import collect_questions


def main():
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])

    def log(msg, level="info"):
        print(f"{level}\t{msg}", flush=True)

    qs = asyncio.run(collect_questions(
        queries=payload["queries"],
        max_per_query=payload.get("max_per_query", 20),
        interval=payload.get("interval", 3.0),
        log_cb=log,
        headless=payload.get("headless", True),
        browser_dir=payload.get("browser_dir", "playwright-browsers"),
        profile_dir=payload.get("profile_dir", "data/browser_profile"),
        sources=payload.get("sources"),
    ))

    out_path.write_text(
        json.dumps([q.to_dict() for q in qs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"采集进程结束，写入 {len(qs)} 条", "debug")


if __name__ == "__main__":
    main()
