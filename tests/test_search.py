from types import SimpleNamespace

from app.search import SearchService


class DB:
    def __init__(self, results): self.results = results; self.calls = []
    def list_notices(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.get(kwargs["q"], [])


class Store:
    def __init__(self, complete=True):
        self.config = SimpleNamespace(base_url="url" if complete else "", model="m", api_key="k",
                                      embedding_enabled=False, rerank_enabled=False,
                                      embedding_model="embed")
    def load(self): return self.config


def hit(number, day):
    return {"id": number, "official_title": f"公告{number}", "published_date": f"2026-08-{day:02}",
            "body_text": f"正文{number}", "original_url": f"https://school/{number}"}


def test_aliases_recall_and_dedupe_newest_first():
    db = DB({"大创": [hit(1, 1)], "大学生创新创业训练计划": [hit(2, 3), hit(1, 1)],
             "创新创业训练": [hit(3, 2)]})
    result = SearchService(db).search("大创", use_ai=False)
    assert [item["id"] for item in result["hits"]] == [2, 3, 1]
    assert [call["q"] for call in db.calls] == ["大创", "大学生创新创业训练计划", "创新创业训练"]


def test_source_and_year_are_forwarded_for_every_expansion():
    db = DB({"转专业": [], "专业调整": [], "转入": [], "转出": []})
    SearchService(db).search("转专业", source_key="jwc", year=2026, use_ai=False)
    assert all(call["source_key"] == "jwc" and call["year"] == 2026 for call in db.calls)


def test_incomplete_config_never_creates_client():
    made = []
    service = SearchService(DB({"查询": []}), Store(False), lambda config: made.append(config))
    assert service.search("查询")["ai_used"] is False and made == []


def test_ai_expansion_failure_keeps_local_hits():
    class Client:
        def expand_query(self, _query): raise RuntimeError("down")
    result = SearchService(DB({"查询": [hit(1, 1)]}), Store(), lambda _c: Client()).search("查询")
    assert [item["id"] for item in result["hits"]] == [1]


def test_no_hits_never_calls_ai_or_guesses():
    made = []
    result = SearchService(DB({}), Store(), lambda config: made.append(config)).answer("问题", [])
    assert "没有" in result["answer"] and made == [] and result["ai_used"] is False


def test_answer_context_contains_only_supplied_hits():
    class Client:
        def answer(self, question, documents):
            assert question == "问题"
            joined = "\n".join(documents)
            assert "正文1" in joined and "库外秘密" not in joined
            return "依据公告1回答"
    result = SearchService(DB({}), Store(), lambda _c: Client()).answer("问题", [hit(1, 1)])
    assert result["answer"] == "依据公告1回答" and result["ai_used"] is True
    assert "附件" in result["attachment_note"]


def test_optional_semantic_recall_merges_and_deduplicates():
    class VectorDB(DB):
        def semantic_search(self, vector, model, limit):
            assert vector == [1.0, 0.0] and model == "embed"
            return [hit(2, 2), hit(1, 1)]
    class Client:
        def expand_query(self, query): return [query]
        def embedding(self, query): return [1.0, 0.0]
    store = Store(); store.config.embedding_enabled = True
    result = SearchService(VectorDB({"查询": [hit(1, 1)]}), store, lambda _c: Client()).search("查询")
    assert [item["id"] for item in result["hits"]] == [2, 1]
    assert result["semantic_used"] is True


def test_rerank_failure_preserves_latest_first_order():
    class Client:
        def expand_query(self, query): return [query]
        def rerank(self, *_args): raise RuntimeError("rerank down")
    store = Store(); store.config.rerank_enabled = True
    result = SearchService(
        DB({"查询": [hit(1, 1), hit(2, 2)]}), store, lambda _c: Client()
    ).search("查询")
    assert [item["id"] for item in result["hits"]] == [2, 1]
    assert result["rerank_used"] is False
