"""
问题检索 + 详情爬取（Playwright 版）

背景：百度对无头/无 Cookie 的普通 HTTP 请求基本返回空内容或安全验证页，
站内搜索页也不再生效，因此检索层必须用真实浏览器内核执行。

检索策略：
  1. 打开 https://www.baidu.com/s?wd=site:zhidao.baidu.com+关键词
  2. 从结果卡片抓标题 + 摘要
  3. 结果链接是 baidu.com/link?url=... 跳转，用 APIRequestContext 读 Location 解析成真实问题页
  4. 摘要为空的问题，再开页面补正文

用法：
    async with Crawler(log_cb=print) as c:
        qs = await c.search(["求资源"], max_per_query=20)
        await c.enrich(qs)
"""

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from playwright.async_api import async_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 百度搜索结果页里每一条结果的容器选择器
RESULT_SELECTOR = "#content_left div.result, #content_left div.c-container"

EXTRACT_JS = r"""
(sel) => Array.from(document.querySelectorAll(sel)).map(el => {
  const a = el.querySelector('h3 a') || el.querySelector('a[href]');
  if (!a) return null;
  const title = (a.innerText || '').replace(/\s+/g, ' ').trim();
  const absEl = el.querySelector('.c-abstract, [class*="abstract"], [class*="content-right"]');
  let abstract = absEl ? (absEl.innerText || '') : '';
  if (!abstract.trim()) {
    // 摘要节点缺失时退回整块文本，去掉开头的标题
    abstract = el.innerText || '';
    if (title && abstract.trim().startsWith(title)) {
      abstract = abstract.trim().slice(title.length);
    }
  }
  return {
    href: a.href || '',
    title: title,
    abstract: abstract.replace(/\s+/g, ' ').replace(/\s*-?\s*百度知道\s*$/, '').trim()
  };
}).filter(Boolean)
"""

# 问题详情页：标题 + 正文
DETAIL_JS = r"""
() => {
  const h1 = document.querySelector('h1[class*="title"], h1');
  const body = document.querySelector(
    '.question-content, [class*="question-desc"], .ask-f-desc, .q-content, .rich-content-container'
  );
  const meta = document.querySelector('meta[name="description"]');
  return {
    title: h1 ? (h1.innerText || '').trim() : '',
    body: body ? (body.innerText || '').trim() : '',
    meta: meta ? (meta.content || '').trim() : ''
  };
}
"""


@dataclass
class Question:
    url: str
    qid: str = ""
    title: str = ""
    snippet: str = ""
    source_query: str = ""
    # 分类/匹配后填充
    is_resource: bool = False
    matched_keywords: List[str] = field(default_factory=list)

    def __post_init__(self):
        m = re.search(r"/question/(\d+)", self.url)
        if m and not self.qid:
            self.qid = m.group(1)

    def to_dict(self) -> dict:
        return {
            "qid": self.qid,
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "source_query": self.source_query,
            "is_resource": self.is_resource,
            "matched_keywords": ",".join(self.matched_keywords),
        }


def _find_chromium(browser_dir: str = "playwright-browsers") -> Optional[str]:
    base = Path(browser_dir)
    if not base.exists():
        return None
    for p in base.rglob("chrome.exe"):
        return str(p)
    return None


class Crawler:
    """基于 Playwright 的检索器，使用持久化浏览器配置复用百度 Cookie"""

    def __init__(
        self,
        headless: bool = True,
        browser_dir: str = "playwright-browsers",
        profile_dir: str = "data/browser_profile",
        log_cb=None,
    ):
        self.headless = headless
        self.browser_dir = browser_dir
        self.profile_dir = profile_dir
        self._log = log_cb or (lambda msg, level="info": None)
        self._pw = None
        self._context = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        exe = _find_chromium(self.browser_dir)
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        self._log(f"启动 Chromium（headless={self.headless}，持久化配置）", "debug")
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(Path(self.profile_dir).resolve()),
            headless=self.headless,
            executable_path=exe,
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context.set_default_timeout(25000)
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        await self._prewarm()
        return self

    async def _prewarm(self):
        """先访问百度首页建立正常会话，降低直接搜索被风控的概率"""
        try:
            page = await self._context.new_page()
            await page.goto("https://www.baidu.com/", wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            await page.close()
            self._log("已访问百度首页预热会话", "debug")
        except Exception as e:
            self._log(f"预热失败（继续执行）：{type(e).__name__}", "debug")

    async def __aexit__(self, *exc):
        try:
            if self._context:
                await self._context.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # ------------------------------------------------------------------
    async def _resolve(self, href: str) -> Optional[str]:
        """把 baidu.com/link?url=... 跳转链接解析成真实 URL"""
        if "baidu.com/link?" not in href:
            return href
        try:
            resp = await self._context.request.get(href, max_redirects=0)
            loc = resp.headers.get("location") or resp.headers.get("Location")
            if loc:
                return loc
            # 没拿到 Location 就跟随一次
            resp2 = await self._context.request.get(href)
            return resp2.url
        except Exception as e:
            self._log(f"跳转解析失败 {href[:60]} — {type(e).__name__}", "debug")
            return None

    async def _is_blocked(self, page) -> bool:
        """判断是否被百度风控（安全验证页）"""
        try:
            if "wappass" in page.url or "/verify" in page.url:
                return True
            title = await page.title()
            return "安全验证" in title or "验证码" in title
        except Exception:
            return False

    async def _search_one(
        self, query: str, max_per_query: int, retries: int = 2
    ) -> tuple[str, List[Question]]:
        """搜索单个关键词；被风控时退避重试。返回 (status, questions)"""
        status = "error"
        for attempt in range(1, retries + 1):
            try:
                status, res = await self._search_once(query, max_per_query)
            except Exception as e:
                self._log(f"  搜索异常：{type(e).__name__}: {e}", "error")
                status, res = "error", []

            if status in ("ok", "empty"):
                return status, res
            if status == "blocked" and attempt < retries:
                wait = 15 * attempt
                self._log(
                    f"  触发百度安全验证，{wait}s 后重试（{attempt}/{retries}）", "warn"
                )
                await asyncio.sleep(wait)
                await self._prewarm()
                continue
            if status == "blocked":
                self._log("  多次触发安全验证，本词转入补搜轮", "warn")
            return status, []
        return status, []

    async def _search_once(self, query: str, max_per_query: int):
        """单次搜索，返回 (status, questions)；status ∈ ok/empty/blocked/error"""
        page = await self._context.new_page()
        try:
            rn = min(max_per_query * 2, 50)
            url = (
                "https://www.baidu.com/s?wd="
                f"{quote('site:zhidao.baidu.com ' + query)}&rn={rn}"
            )
            resp = await page.goto(url, wait_until="domcontentloaded")
            if resp is None:
                return "error", []
            await page.wait_for_timeout(600)

            if await self._is_blocked(page):
                return "blocked", []

            try:
                await page.wait_for_selector(RESULT_SELECTOR, timeout=8000)
            except Exception:
                if await self._is_blocked(page):
                    return "blocked", []
                content = await page.content()
                if "没有找到" in content or "未找到" in content:
                    self._log("  百度返回 0 条结果", "debug")
                    return "empty", []
                self._log("  结果容器未出现，可能被风控降级", "warn")
                return "error", []

            raw = await page.evaluate(EXTRACT_JS, RESULT_SELECTOR)
            self._log(f"  解析到 {len(raw)} 条原始结果", "debug")

            # 并发解析跳转链接
            sem = asyncio.Semaphore(5)

            async def one(item):
                async with sem:
                    real = await self._resolve(item["href"])
                    if not real or "zhidao.baidu.com/question/" not in real:
                        return None
                    title = re.sub(r"[-_]\s*百度知道\s*$", "", item["title"]).strip()
                    return Question(
                        url=real,
                        title=title,
                        snippet=item["abstract"][:400],
                    )

            results = await asyncio.gather(*[one(i) for i in raw])
            out = [q for q in results if q]
            dropped = len(raw) - len(out)
            if dropped:
                self._log(f"  过滤非问题页/解析失败 {dropped} 条", "debug")
            return ("ok", out[:max_per_query]) if out else ("empty", [])
        finally:
            await page.close()

    # ------------------------------------------------------------------
    async def search(
        self,
        queries: List[str],
        max_per_query: int = 20,
        interval: float = 3.0,
        progress_cb=None,
    ) -> List[Question]:
        """按关键词列表检索并全局去重；首轮无结果的词会在冷却后再补搜一轮"""
        all_qs: list[Question] = []
        pending = list(queries)
        for pass_no in (1, 2):
            if not pending:
                break
            if pass_no == 2:
                self._log(
                    f"补搜轮：{len(pending)} 个词上一轮无结果，冷却 30s 后重试", "info"
                )
                await asyncio.sleep(30)
            retry: list[str] = []
            for i, q in enumerate(pending, 1):
                if progress_cb:
                    progress_cb(f"搜索中: {q}")
                tag = "补搜" if pass_no == 2 else ""
                self._log(f"{tag}[{i}/{len(pending)}] 搜索「{q}」", "info")
                t0 = time.time()
                try:
                    status, res = await self._search_one(q, max_per_query)
                except Exception as e:
                    self._log(f"  搜索异常：{type(e).__name__}: {e}", "error")
                    status, res = "error", []
                for item in res:
                    item.source_query = q
                all_qs.extend(res)
                self._log(
                    f"  本词命中 {len(res)} 条，累计 {len(all_qs)} 条"
                    f"（耗时 {time.time() - t0:.1f}s）",
                    "info" if res else "warn",
                )
                if not res:
                    retry.append(q)
                await asyncio.sleep(interval + random.uniform(0, 1.5))
                if status == "blocked":
                    # 被风控后立刻搜下一个词会连环触发，先冷却一下
                    self._log("  本词被风控，全局冷却 25s 再继续", "warn")
                    await asyncio.sleep(25)
                    await self._prewarm()
            pending = retry

        if pending:
            self._log(
                f"{len(pending)} 个词最终无结果：{', '.join(pending[:5])}"
                "（建议换更通用的关键词或稍后再试）",
                "warn",
            )

        seen: dict[str, Question] = {}
        for q in all_qs:
            seen.setdefault(q.qid or q.url, q)
        unique = list(seen.values())
        self._log(
            f"去重完成：原始 {len(all_qs)} 条 → 唯一 {len(unique)} 条"
            f"（剔除 {len(all_qs) - len(unique)} 条）",
            "success",
        )
        return unique

    # ------------------------------------------------------------------
    async def enrich(self, qs: List[Question], concurrency: int = 3) -> List[Question]:
        """摘要为空的问题补抓详情页正文"""
        targets = [q for q in qs if not q.snippet or len(q.title) < 4]
        if not targets:
            self._log("所有问题都已有摘要，无需补详情", "debug")
            return qs

        self._log(f"需要补详情 {len(targets)}/{len(qs)} 条", "info")
        sem = asyncio.Semaphore(concurrency)
        done = {"n": 0, "fail": 0}

        async def one(q: Question):
            async with sem:
                page = await self._context.new_page()
                try:
                    await page.goto(q.url, wait_until="domcontentloaded")
                    data = await page.evaluate(DETAIL_JS)
                    t = (data.get("title") or "").strip()
                    if len(t) >= 4:
                        q.title = t
                    body = (data.get("body") or "").strip() or (data.get("meta") or "").strip()
                    if body:
                        q.snippet = " ".join(body.split())[:400]
                except Exception as e:
                    done["fail"] += 1
                    self._log(f"  详情失败 {q.qid or q.url[:40]} — {type(e).__name__}", "warn")
                finally:
                    await page.close()
                    done["n"] += 1
                    if done["n"] % 10 == 0 or done["n"] == len(targets):
                        self._log(f"  详情进度 {done['n']}/{len(targets)}", "debug")

        await asyncio.gather(*[one(q) for q in targets])
        ok = sum(1 for q in targets if q.snippet)
        self._log(f"详情完成：{ok}/{len(targets)} 条拿到正文，失败 {done['fail']} 条", "info")
        return qs


async def collect_questions(
    queries: List[str],
    max_per_query: int = 20,
    interval: float = 3.0,
    progress_cb=None,
    log_cb=None,
    headless: bool = True,
    browser_dir: str = "playwright-browsers",
    profile_dir: str = "data/browser_profile",
) -> List[Question]:
    """一次性采集入口：开浏览器 → 检索 → 补详情 → 关浏览器"""
    async with Crawler(
        headless=headless,
        browser_dir=browser_dir,
        profile_dir=profile_dir,
        log_cb=log_cb,
    ) as c:
        qs = await c.search(queries, max_per_query, interval, progress_cb)
        if qs:
            await c.enrich(qs)
        return qs
