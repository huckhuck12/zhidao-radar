"""
登录助手 —— 打开浏览器让你手动登录百度，自动检测并保存会话到 data/login_state.json

不再依赖终端回车：每 2 秒轮询 BDUSS Cookie，检测到登录立即保存并关闭浏览器。

用法:
    venv\\Scripts\\python.exe login_helper.py
"""

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

STATE_PATH = Path("data/login_state.json")
POLL_INTERVAL = 2          # 秒
REPORT_INTERVAL = 15       # 每隔多久打印一次等待提示
TIMEOUT_SECONDS = 600      # 最长等待 10 分钟


def _find_chromium() -> str | None:
    base = Path("playwright-browsers")
    if not base.exists():
        return None
    for p in base.rglob("chrome.exe"):
        return str(p)
    return None


def _has_login(context) -> bool:
    """百度登录后会有 BDUSS Cookie"""
    try:
        for c in context.cookies():
            if c.get("name") == "BDUSS" and c.get("value"):
                return True
    except Exception:
        pass
    return False


def main():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,  # 必须可见，让用户手动登录
            executable_path=_find_chromium(),
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        print("\n" + "=" * 60)
        print("📖 登录指南")
        print("=" * 60)
        print("1. 浏览器会打开百度首页")
        print("2. 点击右上角「登录」，选择任意方式登录")
        print("   （扫码 / 账号密码 / 短信验证码 都行）")
        print("3. 登录成功后不用按回车，程序会自动检测并保存")
        print("=" * 60 + "\n", flush=True)

        page.goto("https://www.baidu.com", wait_until="domcontentloaded")
        print(f"⏳ 等待登录中（最长 {TIMEOUT_SECONDS // 60} 分钟）...", flush=True)

        deadline = time.time() + TIMEOUT_SECONDS
        start = time.time()
        last_report = 0.0

        while time.time() < deadline:
            if _has_login(context):
                context.storage_state(path=str(STATE_PATH))
                print(f"\n✅ 登录成功，会话已保存到: {STATE_PATH}", flush=True)
                print("   下次启动答题会自动加载，无需再登录\n", flush=True)
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
                print(f"   ...已等待 {elapsed}s，尚未检测到登录", flush=True)

            time.sleep(POLL_INTERVAL)

        print(f"\n❌ 超时（{TIMEOUT_SECONDS}s）未检测到登录，请重试", flush=True)
        try:
            browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
