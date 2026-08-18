"""Reliable local-first search with optional AI query expansion and grounded answers."""
from __future__ import annotations

from typing import Any


ALIASES = {
    "大创": ("大学生创新创业训练计划", "创新创业训练"),
    "转专业": ("专业调整", "转入", "转出"),
}


class SearchService:
    def __init__(self, database, config_store=None, client_factory=None):
        self.database = database
        self.config_store = config_store
        self.client_factory = client_factory

    def _client(self):
        if self.config_store is None or self.client_factory is None:
            return None
        config = self.config_store.load()
        if not (getattr(config, "base_url", "") and getattr(config, "model", "") and getattr(config, "api_key", None)):
            return None
        return self.client_factory(config)

    def search(
        self, query: str, source_key: str = "", year: int | None = None,
        limit: int = 50, use_ai: bool = True,
    ) -> dict[str, Any]:
        query = query.strip()
        queries = [query]
        for alias, expansions in ALIASES.items():
            if alias in query:
                queries.extend(expansions)
        ai_used = False
        client = None
        config = None
        if use_ai:
            try:
                config = self.config_store.load() if self.config_store is not None else None
                client = self._client()
                if client is not None:
                    expanded = client.expand_query(query)
                    values = [expanded] if isinstance(expanded, str) else expanded
                    if isinstance(values, (list, tuple)):
                        queries.extend(item.strip() for item in values if isinstance(item, str) and item.strip())
                        ai_used = True
            except Exception:
                pass
        unique_queries = list(dict.fromkeys(queries))
        hits_by_id: dict[int, dict[str, Any]] = {}
        for item_query in unique_queries:
            for notice in self.database.list_notices(
                q=item_query, source_key=source_key, year=year, limit=max(limit, 50)
            ):
                hits_by_id.setdefault(int(notice["id"]), notice)
        semantic_used = False
        if (client is not None and config is not None
                and getattr(config, "embedding_enabled", False)
                and hasattr(self.database, "semantic_search")):
            try:
                query_vector = client.embedding(query)
                semantic_hits = self.database.semantic_search(
                    query_vector, str(config.embedding_model), limit=max(limit, 50)
                )
                for notice in semantic_hits:
                    hits_by_id.setdefault(int(notice["id"]), notice)
                semantic_used = True
            except Exception:
                pass
        hits = sorted(
            hits_by_id.values(), key=lambda item: (str(item.get("published_date", "")), int(item["id"])),
            reverse=True,
        )[:max(1, int(limit))]
        rerank_used = False
        if (client is not None and config is not None and hits
                and getattr(config, "rerank_enabled", False)):
            try:
                documents = [
                    f"{item.get('official_title', '')}\n{str(item.get('body_text', ''))[:1200]}" for item in hits
                ]
                order = client.rerank(query, documents)
                valid = []
                for index in order:
                    if isinstance(index, int) and 0 <= index < len(hits) and index not in valid:
                        valid.append(index)
                valid.extend(index for index in range(len(hits)) if index not in valid)
                hits = [hits[index] for index in valid]
                rerank_used = True
            except Exception:
                pass
        return {"query": query, "queries": unique_queries, "hits": hits, "ai_used": ai_used,
                "semantic_used": semantic_used, "rerank_used": rerank_used}

    def answer(self, question: str, hits: list[dict[str, Any]], use_ai: bool = True) -> dict[str, Any]:
        attachment_note = "附件内容未抓取；涉及附件细节时请查看学校原文。"
        if not hits:
            return {"answer": "没有在本地公告正文中找到足够依据，无法作答。", "hits": [],
                    "attachment_note": attachment_note, "ai_used": False}
        documents = [
            "\n".join((
                f"公告ID：{item.get('id')}", f"标题：{item.get('official_title', '')}",
                f"发布日期：{item.get('published_date', '')}",
                f"正文摘录：{str(item.get('body_text', ''))[:3000]}",
                f"原文：{item.get('original_url', '')}",
            ))
            for item in hits
        ]
        if use_ai:
            try:
                client = self._client()
                if client is not None:
                    answer = client.answer(question, documents)
                    if isinstance(answer, str) and answer.strip():
                        return {"answer": answer.strip(), "hits": hits,
                                "attachment_note": attachment_note, "ai_used": True}
            except Exception:
                pass
        return {
            "answer": "已找到相关公告，但当前无法生成联网回答；请查看下列本地搜索结果。",
            "hits": hits, "attachment_note": attachment_note, "ai_used": False,
        }
