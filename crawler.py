"""
问题检索 + 详情爬取

检索策略：
  1. 百度搜索 site:zhidao.baidu.com + 关键词
  2. zhidao.baidu.com/search?word=xxx 站内搜索
两路并行，去重合并。
"""

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from typing import List
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup


COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


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


async def collect_questions(
    queries: List[str],
    max_per_query: int = 30,
    interval: float = 3.0,
    progress_cb=None,
    log_cb=None,
) -> List[Question]:
    """主入口：给定搜索词列表，跑双路搜索，返回去重的问题列表"""
    all_qs: list[Question] = []
    _log = log_cb or (lambda msg, level="info": None)

    async with httpx.AsyncClient(
        headers=COMMON_HEADERS, follow_redirects=True, timeout=15
    ) as client:
        for i, q in enumerate(queries, 1):
            if progress_cb:
                progress_cb(f"搜索中: {q}")
            _log(f"[{i}/{len(queries)}] 搜索「{q}」", "info")

            t0 = time.time()
            baidu_res, zhidao_res = await asyncio.gather(
                _search_baidu(client, q, max_per_query, _log),
                _search_zhidao(client, q, max_per_query, _log),
            )
            _log(f"  ├ 百度搜索命中 {len(baidu_res)} 条", "debug")
            _log(f"  └ 站内搜索命中 {len(zhidao_res)} 条", "debug")

            before = len(all_qs)
            for q_list in (baidu_res, zhidao_res):
                for item in q_list:
                    item.source_query = q
                all_qs.extend(q_list)
            _log(f"  本词新增 {len(all_qs) - before} 条，累计 {len(all_qs)} 条"
                 f"（耗时 {time.time() - t0:.1f}s）", "debug")

            if not baidu_res and not zhidao_res:
                _log(f"  「{q}」无结果，可能关键词太窄或触发风控", "warn")

            await asyncio.sleep(interval + random.uniform(0, 1.5))

    # 去重（按 URL）
    seen: dict[str, Question] = {}
    for q in all_qs:
        seen.setdefault(q.url, q)
    unique = list(seen.values())
    _log(f"去重完成：原始 {len(all_qs)} 条 → 唯一 {len(unique)} 条"
         f"（剔除重复 {len(all_qs) - len(unique)} 条）", "success")
    return unique


async def fetch_question_detail(
    q: Question,
    client: httpx.AsyncClient | None = None,
    interval: float = 2.0,
    log_cb=None,
) -> Question:
    """爬问题详情页，补 title + snippet"""
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            headers=COMMON_HEADERS, follow_redirects=True, timeout=15
        )
    try:
        resp = await client.get(q.url)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")

        h1 = soup.select_one("h1.title, h1.question-title")
        if h1 and not q.title:
            q.title = h1.get_text(strip=True)

        content_el = soup.select_one(
            "div.question-content, div.wgt-question-detail, "
            "div#detail, div.ask-f-desc"
        )
        if content_el:
            q.snippet = content_el.get_text(" ", strip=True)[:400]
        elif log_cb:
            log_cb(f"详情页未解析到正文 {q.url}", "debug")
    except Exception as e:
        if log_cb:
            log_cb(f"详情抓取失败 {q.url} — {type(e).__name__}: {e}", "warn")
    finally:
        if own_client:
            await client.aclose()

    await asyncio.sleep(interval + random.uniform(0, 1.0))
    return q


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

async def _search_baidu(
    client: httpx.AsyncClient, q: str, rn: int, log_cb=None
) -> List[Question]:
    url = f"https://www.baidu.com/s?wd=site%3Azhidao.baidu.com+{quote(q)}&rn={rn}"
    out: list[Question] = []
    try:
        resp = await client.get(url)
        if "安全验证" in resp.text or "wappass" in str(resp.url):
            if log_cb:
                log_cb("百度搜索触发安全验证，本词结果可能为空", "warn")
            return out
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.select("h3.t a, h3 a"):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if "baidu.com/link?" in href:
                try:
                    real = await client.get(href, follow_redirects=True)
                    href = str(real.url)
                except Exception:
                    continue
            if "zhidao.baidu.com/question/" not in href:
                continue
            out.append(Question(url=href, title=title))
    except Exception as e:
        if log_cb:
            log_cb(f"百度搜索请求失败（{q}）— {type(e).__name__}: {e}", "error")
    return out


async def _search_zhidao(
    client: httpx.AsyncClient, q: str, rn: int, log_cb=None
) -> List[Question]:
    url = f"https://zhidao.baidu.com/search?word={quote(q)}&rn={rn}"
    out: list[Question] = []
    try:
        resp = await client.get(url)
        # 跳登录页说明被风控
        if "passport.baidu.com" in str(resp.url):
            if log_cb:
                log_cb("站内搜索被重定向到登录页（风控），本词结果可能为空", "warn")
            return out
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.select("a[href*='/question/']"):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            out.append(Question(url=href, title=title))
    except Exception as e:
        if log_cb:
            log_cb(f"站内搜索请求失败（{q}）— {type(e).__name__}: {e}", "error")
    # 去重
    seen: set[str] = set()
    deduped = []
    for item in out:
        if item.url not in seen:
            seen.add(item.url)
            deduped.append(item)
    return deduped
