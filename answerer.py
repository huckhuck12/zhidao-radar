"""
Playwright 自动答题器

使用方式：
    ans = Answerer(cookie="xxx", browser_path="playwright-browsers", headless=False)
    ans.start()
    ans.answer("https://zhidao.baidu.com/question/xxx.html", "这是答案文本")
    ans.stop()
"""

import json
import time
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Optional

from playwright.sync_api import sync_playwright, Page, BrowserContext


ANSWERED_LOG_PATH = Path("data/answered.json")


@dataclass
class AnswerLog:
    qid: str
    url: str
    answer_text: str
    success: bool
    timestamp: float = field(default_factory=time.time)

    def to_row(self) -> dict:
        return {
            "qid": self.qid,
            "url": self.url,
            "success": self.success,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)),
            "preview": self.answer_text[:60].replace("\n", " "),
        }


class Answerer:
    def __init__(
        self,
        cookie: str = "",
        browser_path: str = "playwright-browsers",
        headless: bool = True,
        max_per_question: int = 3,
        storage_state_path: str = "data/login_state.json",
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.cookie = cookie
        self.browser_path = browser_path
        self.headless = headless
        self.max_per_question = max_per_question
        self.storage_state_path = Path(storage_state_path)
        self.log_cb = log_callback or (lambda m: print(f"[answerer] {m}"))

        self._answered: dict[str, int] = self._load_answered()
        self._lock = threading.Lock()

        self._pw = None
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ------------------------------------------------------------------
    @staticmethod
    def _load_answered() -> dict:
        if ANSWERED_LOG_PATH.exists():
            try:
                return json.loads(ANSWERED_LOG_PATH.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_answered(self):
        ANSWERED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ANSWERED_LOG_PATH.write_text(
            json.dumps(self._answered, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    def start(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            # 指定本地浏览器路径，避免系统权限问题
            executable_path=self._find_chromium(),
        )

        # 优先用已保存的登录会话（Cookie + localStorage + sessionStorage 全套）
        context_kwargs = {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1280, "height": 800},
        }
        if self.storage_state_path.exists():
            context_kwargs["storage_state"] = str(self.storage_state_path)
            self.log_cb(f"已加载登录会话: {self.storage_state_path.name}")

        self._context = self._browser.new_context(**context_kwargs)

        # 没有会话文件但有 Cookie 字符串 → 用 Cookie 兜底
        if not self.storage_state_path.exists() and self.cookie:
            self._inject_cookie(self.cookie)

        self._page = self._context.new_page()
        self.log_cb("浏览器已启动")

    def _find_chromium(self) -> str | None:
        """在项目内 playwright-browsers 里找 chromium 可执行文件"""
        base = Path(self.browser_path)
        if not base.exists():
            return None
        for p in base.rglob("chrome.exe"):
            return str(p)
        return None

    def _inject_cookie(self, raw: str):
        cookies = []
        for pair in raw.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".baidu.com",
                "path": "/",
            })
        if cookies:
            self._context.add_cookies(cookies)
            self.log_cb(f"已注入 {len(cookies)} 条 Cookie")

    def stop(self):
        if self._page:
            try: self._page.close()
            except Exception: pass
        if self._context:
            try: self._context.close()
            except Exception: pass
        if self._browser:
            try: self._browser.close()
            except Exception: pass
        if self._pw:
            try: self._pw.stop()
            except Exception: pass
        self.log_cb("浏览器已关闭")

    # ------------------------------------------------------------------
    def answer(self, question_url: str, question_qid: str, answer_text: str) -> AnswerLog:
        with self._lock:
            count = self._answered.get(question_qid, 0)
            if count >= self.max_per_question:
                self.log_cb(f"[skip] qid={question_qid} 已答 {count} 次")
                return AnswerLog(question_qid, question_url, answer_text, False)

        if not self._page:
            raise RuntimeError("未启动浏览器，请先调用 start()")

        self.log_cb(f"打开: {question_url}")
        try:
            self._page.goto(question_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

            # 点「我要回答」或类似按钮
            for btn_sel in [
                "text=我要回答",
                "a:has-text('我要回答')",
                "button:has-text('我要回答')",
            ]:
                try:
                    el = self._page.query_selector(btn_sel)
                    if el and el.is_visible():
                        el.click()
                        time.sleep(1)
                        break
                except Exception:
                    continue

            # 找输入框
            textarea = None
            for sel in [
                "div.ask-edit-area textarea",
                "div.editor-content",
                "div[contenteditable='true']",
                "textarea",
            ]:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        textarea = el
                        break
                except Exception:
                    continue

            if textarea is None:
                self.log_cb("[fail] 未找到答题输入框（页面结构可能变了）")
                return AnswerLog(question_qid, question_url, answer_text, False)

            textarea.click()
            time.sleep(0.5)
            textarea.fill(answer_text)
            time.sleep(0.8)

            # 提交
            submitted = False
            for btn_sel in [
                "button.answer-submit-btn",
                "button:has-text('提交回答')",
                "a:has-text('提交回答')",
                "button:has-text('发布')",
            ]:
                try:
                    btn = self._page.query_selector(btn_sel)
                    if btn and btn.is_visible():
                        btn.click()
                        submitted = True
                        break
                except Exception:
                    continue

            if not submitted:
                self.log_cb("[fail] 未找到提交按钮")
                return AnswerLog(question_qid, question_url, answer_text, False)

            time.sleep(3)

            # 保存计数
            with self._lock:
                self._answered[question_qid] = count + 1
                self._save_answered()

            self.log_cb(f"[ok] 回答成功: {question_qid}")
            return AnswerLog(question_qid, question_url, answer_text, True)

        except Exception as e:
            self.log_cb(f"[error] {question_qid}: {e}")
            return AnswerLog(question_qid, question_url, answer_text, False)
