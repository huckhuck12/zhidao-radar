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
) -> List[Question]:
    """主入口：给定搜索词列表，跑双路搜索，返回去重的问题列表"""
    all_qs: list[Question] = []

    async with httpx.AsyncClient(
        headers=COMMON_HEADERS, follow_redirects=True, timeout=15
    ) as client:
        for q in queries:
            if progress_cb:
                progress_cb(f"搜索中: {q}")

            # 并行两种搜索
            results = await asyncio.gather(
                _search_baidu(client, q, max_per_query),
                _search_zhidao(client, q, max_per_query),
            )
            for q_list in results:
                for item in q_list:
                    item.source_query = q
                all_qs.extend(q_list)

            await asyncio.sleep(interval + random.uniform(0, 1.5))

    # 去重（按 URL）
    seen: dict[str, Question] = {}
    for q in all_qs:
        seen.setdefault(q.url, q)
    return list(seen.values())


async def fetch_question_detail(
    q: Question,
    client: httpx.AsyncClient | None = None,
    interval: float = 2.0,
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
    except Exception as e:
        pass
    finally:
        if own_client:
            await client.aclose()

    await asyncio.sleep(interval + random.uniform(0, 1.0))
    return q


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

async def _search_baidu(client: httpx.AsyncClient, q: str, rn: int) -> List[Question]:
    url = f"https://www.baidu.com/s?wd=site%3Azhidao.baidu.com+{quote(q)}&rn={rn}"
    out: list[Question] = []
    try:
        resp = await client.get(url)
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
    except Exception:
        pass
    return out


async def _search_zhidao(client: httpx.AsyncClient, q: str, rn: int) -> List[Question]:
    url = f"https://zhidao.baidu.com/search?word={quote(q)}&rn={rn}"
    out: list[Question] = []
    try:
        resp = await client.get(url)
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.select("a[href*='/question/']"):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            out.append(Question(url=href, title=title))
    except Exception:
        pass
    # 去重
    seen: set[str] = set()
    deduped = []
    for item in out:
        if item.url not in seen:
            seen.add(item.url)
            deduped.append(item)
    return deduped
