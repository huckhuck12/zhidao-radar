"""
百度网盘登录助手 —— 打开浏览器让你手动登录网盘，自动导出 Cookie 到 data/pan_cookies.json

自动轮询 BDUSS + STOKEN 两个关键 Cookie，检测到后立即保存并关闭浏览器。

用法:
    venv\\Scripts\\python.exe pan_login_helper.py
"""

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

STATE_PATH = Path("data/pan_cookies.json")
POLL_INTERVAL = 2
REPORT_INTERVAL = 15
TIMEOUT_SECONDS = 600


def _find_chromium() -> str | None:
    base = Path("playwright-browsers")
    if not base.exists():
        return None
    for p in base.rglob("chrome.exe"):
        return str(p)
    return None


def _extract_key_cookies(cookies: list[dict]) -> str:
    """从 context.cookies() 里抽出 BDUSS 和 STOKEN，拼成 Cookie 字符串"""
    needed = {"BDUSS", "STOKEN"}
    found = {}
    for c in cookies:
        name = c.get("name", "")
        if name in needed and c.get("value"):
            found[name] = c["value"]
    if not found:
        return ""
    return "; ".join(f"{k}={v}" for k, v in found.items() if k in needed)


def main():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            executable_path=_find_chromium(),
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        page = context.new_page()

        print("\n" + "=" * 60)
        print("📖 百度网盘登录指南")
        print("=" * 60)
        print("1. 浏览器会打开百度首页")
        print("2. 点击右上角「登录」，扫码或账号密码登录")
        print("3. 登录成功后程序自动检测 BDUSS Cookie 并保存")
        print("=" * 60 + "\n", flush=True)

        page.goto("https://www.baidu.com", wait_until="domcontentloaded")
        print(f"⏳ 等待登录中（最长 {TIMEOUT_SECONDS // 60} 分钟）...", flush=True)

        deadline = time.time() + TIMEOUT_SECONDS
        start = time.time()
        last_report = 0.0

        while time.time() < deadline:
            cookies = context.cookies()
            cookie_str = _extract_key_cookies(cookies)
            if cookie_str:
                STATE_PATH.write_text(
                    json.dumps({"cookie": cookie_str}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"\n✅ 登录成功，Cookie 已保存到: {STATE_PATH}", flush=True)
                print(f"   BDUSS + STOKEN 共 {len(cookie_str)} 字符", flush=True)
                print("   下次启动自动采集/转存会自动加载\n", flush=True)
                browser.close()
                return

            try:
                if page.is_closed():
                    print("\n⚠️ 浏览器被关闭，未检测到登录", flush=True)
                    return
            except Exception:
                print("\n⚠️ 浏览器已断开，未检测到登录", flush=True)
                return

            elapsed = int(time.time() - start)
            if elapsed - last_report >= REPORT_INTERVAL:
                last_report = elapsed
                print(f"   ...已等待 {elapsed}s，尚未检测到 BDUSS", flush=True)

            time.sleep(POLL_INTERVAL)

        print(f"\n❌ 超时（{TIMEOUT_SECONDS}s）未检测到登录，请重试", flush=True)
        try:
            browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
