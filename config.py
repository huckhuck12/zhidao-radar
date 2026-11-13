"""
持久化设置 —— Cookie / 搜索词 / 节流参数
全部读写 data/settings.json，这样面板上填的东西不会丢
"""

import json
from pathlib import Path

SETTINGS_PATH = Path("data/settings.json")

DEFAULT_SETTINGS = {
    "baidu_cookie": "",
    "sources": ["zhidao"],                # zhidao / tieba
    "search_queries": [
        "求资源 百度云",
        "求分享 网盘",
        "谁有 下载",
        "跪求 链接",
        "求下载 资源",
    ],
    "max_questions_per_query": 15,       # int
    "request_interval": 8.0,             # float，低于 5s 很容易触发百度安全验证
    "answer_interval": 15.0,             # float
    "max_answers_per_question": 3,       # int
    "playwright_headless": True,
    "playwright_browser_path": "playwright-browsers",
}


def load() -> dict:
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            # 补齐新增字段
            for k, v in DEFAULT_SETTINGS.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    save(DEFAULT_SETTINGS)
    return dict(DEFAULT_SETTINGS)


def save(s: dict):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def reset():
    save(DEFAULT_SETTINGS)
