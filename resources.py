"""
资源库 —— 读写 data/resources.json，按关键词匹配问题

匹配逻辑：把问题文本做 lower，资源的 keywords 列表逐一做 in 判断，
得分高者优先返回 top_k。
"""

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List

RESOURCE_DB_PATH = Path("data/resources.json")


@dataclass
class Resource:
    category: str
    keywords: list
    url: str
    password: str
    note: str

    def to_row(self) -> dict:
        return asdict(self)

    def build_answer_text(self, question_title: str = "") -> str:
        lines = []
        if self.note:
            lines.append(self.note)
        lines.append("")
        lines.append(f"链接：{self.url}")
        if self.password:
            lines.append(f"提取码：{self.password}")
        lines.append("")
        lines.append("链接有时效，建议尽快保存~ 如果失效了评论区说一声。")
        return "\n".join(lines)


class ResourceDB:
    def __init__(self, path: Path = RESOURCE_DB_PATH):
        self.path = path
        self.resources: List[Resource] = []
        self.load()

    # ------------------------------------------------------------------
    def load(self):
        self.resources = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for item in data.get("resources", []):
                self.resources.append(Resource(
                    category=item.get("category", ""),
                    keywords=item.get("keywords", []) or [],
                    url=item.get("url", ""),
                    password=item.get("password", ""),
                    note=item.get("note", ""),
                ))
        except Exception:
            pass

    # ------------------------------------------------------------------
    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"resources": [r.to_row() for r in self.resources]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    def match(self, question_text: str, top_k: int = 3) -> List[tuple[Resource, int]]:
        text_lower = question_text.lower()
        scored: list[tuple[int, Resource]] = []
        for r in self.resources:
            score = sum(1 for kw in r.keywords if kw.lower() in text_lower)
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [(r, s) for s, r in scored[:top_k]]

    # ------------------------------------------------------------------
    # CRUD（面板增删改查用）
    # ------------------------------------------------------------------
    def add(self, resource: Resource):
        self.resources.append(resource)
        self.save()

    def delete(self, index: int):
        if 0 <= index < len(self.resources):
            self.resources.pop(index)
            self.save()

    def update(self, index: int, resource: Resource):
        if 0 <= index < len(self.resources):
            self.resources[index] = resource
            self.save()

    def as_dicts(self) -> list[dict]:
        return [r.to_row() for r in self.resources]
