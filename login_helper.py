"""
登录助手 —— 打开浏览器让你手动登录百度，保存会话到 data/login_state.json

之后 Answerer 会自动加载这个文件，无需再手动复制 Cookie。

用法:
    venv\\Scripts\\python.exe login_helper.py
"""

from pathlib import Path
from playwright.sync_api import sync_playwright

STATE_PATH = Path("data/login_state.json")


def _find_chromium() -> str | None:
    base = Path("playwright-browsers")
    if not base.exists():
        return None
    for p in base.rglob("chrome.exe"):
        return str(p)
    return None


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
        print("3. 登录成功后，随便点开一个百度知道的问题页确认能答题")
        print("4. 回到终端，按回车键保存会话")
        print("=" * 60 + "\n")

        page.goto("https://www.baidu.com", wait_until="domcontentloaded")

        input("✋ 登录完成后按回车键继续... ")

        # 保存会话
        context.storage_state(path=str(STATE_PATH))
        print(f"\n✅ 登录会话已保存到: {STATE_PATH}")
        print("   下次启动答题时会自动加载，无需再登录\n")

        browser.close()


if __name__ == "__main__":
    main()
