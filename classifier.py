"""
求资源问题分类器 —— 纯关键词规则 + 可扩展黑名单/白名单

命中 RESOURCE_KEYWORDS 视为资源需求问题，
命中 EXCLUDE_KEYWORDS（如"怎么""如何"）则排除（知识类求助，不是要文件）。
"""

import re
from pathlib import Path

# 资源需求关键词（任一命中即算）
RESOURCE_KEYWORDS = [
    # 求助句式
    "求", "跪求", "跪求分享", "求分享", "求发", "求给", "求下载",
    "求链接", "求网盘", "求百度云", "求阿里云", "求夸克",
    "谁有", "有没有", "有吗", "有木有",
    "分享一下", "发我", "发给我", "私信", "邮箱发",
    # 资源类型词
    "下载", "资源", "文件", "网盘", "云盘",
    "电子书", "pdf", "PDF", "epub", "mobi", "doc", "docx",
    "视频", "电影", "电视剧", "动漫", "番剧", "综艺",
    "软件", "安装包", "破解版", "绿色版", "注册机", "序列号",
    "音乐", "歌曲", "无损", "mp3",
    "游戏", "单机", "端游", "手游",
    "模板", "素材", "源码", "代码", "脚本",
    "学习资料", "教程", "讲义", "课件", "PPT", "ppt",
    "课程", "网课", "公开课",
    "图片包", "图集", "壁纸", "表情包",
    # 格式后缀
    ".zip", ".rar", ".7z", ".torrent", "磁力", "种子",
]

# 排除关键词（命中则不视为求资源，只对标题检查）
EXCLUDE_KEYWORDS = [
    "怎么", "如何", "为什么", "原理", "区别", "对比",
    "好不好", "怎么样", "是什么", "能否", "可不可以",
]

# 广告/分享贴信号词（标题命中且不含求助词 → 视为广告贴，只对标题检查）
# 贴吧尤为严重：大量"XX合集百度网盘免费看"引流贴，不是在求资源
AD_SHARE_KEYWORDS = [
    "免费看", "免费观看", "在线看", "在线观看", "资源分享",
    "自取", "已整理", "合集", "推荐", "盘点", "福利",
    "速存", "抓紧", "免费下载", "分享给大家", "共享",
]

# 求助信号词（标题含任一即认为是真实求助，广告规则豁免）
ASK_KEYWORDS = [
    "求", "谁有", "跪求", "有无", "有没有", "有木有",
    "哪位", "大神", "帮忙", "急需", "求发", "找一下",
]


class QuestionClassifier:
    def __init__(self, extra_keywords: list | None = None):
        self.resource_kws = list(RESOURCE_KEYWORDS)
        if extra_keywords:
            self.resource_kws.extend(extra_keywords)
        self._res_pats = [re.compile(re.escape(k), re.IGNORECASE) for k in self.resource_kws]
        self._exc_pats = [re.compile(re.escape(k), re.IGNORECASE) for k in EXCLUDE_KEYWORDS]
        self._ad_pats = [re.compile(re.escape(k), re.IGNORECASE) for k in AD_SHARE_KEYWORDS]
        self._ask_pats = [re.compile(re.escape(k), re.IGNORECASE) for k in ASK_KEYWORDS]

    def is_ad_share(self, title: str) -> bool:
        """标题命中广告/分享词且不含求助词 → 广告引流贴，不是真实求助"""
        title = title or ""
        for p in self._ask_pats:
            if p.search(title):
                return False
        for p in self._ad_pats:
            if p.search(title):
                return True
        return False

    def is_resource_request(self, title: str, content: str = "") -> bool:
        # 标题命中排除词 → 直接排除
        for p in self._exc_pats:
            if p.search(title or ""):
                return False
        # 广告分享贴 → 排除
        if self.is_ad_share(title):
            return False
        text = f"{title} {content}"
        for p in self._res_pats:
            if p.search(text):
                return True
        return False

    def matched_keywords(self, title: str, content: str = "") -> list:
        text = f"{title} {content}"
        hits = []
        for kw, p in zip(self.resource_kws, self._res_pats):
            if p.search(text):
                hits.append(kw)
        return hits
