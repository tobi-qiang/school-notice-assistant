from types import SimpleNamespace
import threading

from app.ai_client import AIError
from app.enrichment import EnrichmentQueue
from app.models import AIStatus


class DB:
    def __init__(self, notices):
        self.notices = notices
        self.saved = []
        self.pending = []
    def list_pending_ai(self, limit=50): return self.notices[:limit]
    def update_ai(self, notice_id, value): self.saved.append((notice_id, value)); return True
    def mark_ai_pending(self, notice_id, error=""): self.pending.append((notice_id, error)); return True
    def get_notice(self, notice_id):
        return next((item for item in self.notices if item["id"] == notice_id), None)
    def update_share_summary(self, notice_id, summary):
        for index, item in enumerate(self.notices):
            if item["id"] == notice_id:
                self.notices[index] = {**item, "share_summary": summary}
                return True
        return False
    def list_missing_share_summaries(self, limit=5000):
        return [item for item in self.notices if not item.get("share_summary")][:limit]
    def list_share_summary_candidates(self, limit=5000):
        return self.notices[:limit]


class Store:
    def __init__(self, **values): self.config = SimpleNamespace(**values)
    def load(self): return self.config


def notice(number=1, body="正文秘密"):
    return {"id": number, "official_title": "标题", "body_text": body, "attachment_names": ["附件.pdf"]}


def test_missing_configuration_never_creates_client():
    called = []
    queue = EnrichmentQueue(DB([notice()]), Store(base_url="", model="", api_key=None), lambda c: called.append(c))
    result = queue.process_pending()
    assert result["status"] == "disabled" and result["pending"] == 1
    assert called == []


def test_result_is_strictly_cleaned():
    class Client:
        def enrich(self, *_args):
            return {"short_title": "短标题", "summary": 3, "materials": ["表格", 9],
                    "tags": ["申报", None], "requires_external_system": "yes"}
    db = DB([notice()])
    queue = EnrichmentQueue(db, Store(base_url="x", model="m", api_key="k"), lambda _c: Client())
    assert queue.process_pending()["status"] == "completed"
    value = db.saved[0][1]
    assert value.status is AIStatus.READY and value.summary == ""
    assert value.materials == ["表格"] and value.tags == ["申报"]
    assert value.requires_external_system is None and value.model == "m"


def test_one_failure_does_not_stop_later_notices_and_hides_secrets():
    class Client:
        calls = 0
        def enrich(self, _title, body, _attachments):
            self.calls += 1
            if self.calls == 1: raise RuntimeError(f"bad KEY123 {body}")
            return {"short_title": "ok"}
    db = DB([notice(1), notice(2, "另一正文")])
    result = EnrichmentQueue(
        db, Store(base_url="x", model="m", api_key="KEY123"), lambda _c: Client()
    ).process_pending()
    assert result == {"status": "partial", "processed": 1, "failed": 1, "pending": 1,
                      "embedding_failed": 0}
    assert len(db.saved) == 1
    assert "KEY123" not in db.pending[0][1] and "正文秘密" not in db.pending[0][1]


def test_cancel_before_first_item_keeps_all_pending():
    event = threading.Event(); event.set()
    db = DB([notice(1), notice(2)])
    queue = EnrichmentQueue(db, Store(base_url="x", model="m", api_key="k"), lambda _c: object())
    result = queue.process_pending(cancel_event=event)
    assert result["status"] == "cancelled" and result["pending"] == 2
    assert db.saved == []


def test_reentrant_call_is_busy():
    queue = EnrichmentQueue(DB([]), Store(base_url="x", model="m", api_key="k"), lambda _c: object())
    queue._running = True
    assert queue.process_pending()["status"] == "busy"


def test_optional_embedding_is_saved_without_affecting_chat_status():
    class Client:
        def enrich(self, *_args): return {"short_title": "短标题", "tags": ["标签"], "summary": "摘要"}
        def embedding(self, text): assert "短标题" in text; return [1.0, 0.0]
    class VectorDB(DB):
        def __init__(self, notices): super().__init__(notices); self.vector = None
        def store_embedding(self, *args): self.vector = args; return True
    db = VectorDB([notice()])
    config = Store(base_url="x", model="m", api_key="k", embedding_enabled=True,
                   embedding_dimensions=2, embedding_model="embed")
    result = EnrichmentQueue(db, config, lambda _c: Client()).process_pending()
    assert result["status"] == "completed" and result["embedding_failed"] == 0
    assert db.vector[1:] == ("embed", 2, [1.0, 0.0])


def test_start_passes_task_event_as_cancel_event_not_limit():
    class Tasks:
        def __init__(self): self.target = None
        def start(self, name, target):
            assert name == "ai-enrichment"
            self.target = target
            return True
        def status(self, _name): return {"active": False, "error": None}

    tasks = Tasks()
    queue = EnrichmentQueue(
        DB([]), Store(base_url="x", model="m", api_key="k"), lambda _c: object(), tasks=tasks
    )
    assert queue.start() is True
    event = threading.Event()
    assert tasks.target(event)["status"] == "completed"


def test_status_exposes_background_task_error():
    class Tasks:
        def status(self, _name): return {"active": False, "error": "boom"}

    status = EnrichmentQueue(
        DB([]), Store(base_url="", model="", api_key=None), lambda _c: object(), tasks=Tasks()
    ).status()
    assert status == {"running": False, "error": "boom", "last_result": None}


def test_process_one_notice_saves_and_returns_fresh_value():
    class OneDB(DB):
        def update_ai(self, notice_id, value):
            super().update_ai(notice_id, value)
            self.notices[0] = {**self.notices[0], "ai_status": "ready", "summary": value.summary}
            return True
    class Client:
        def enrich(self, *_args): return {"summary": "可分享摘要", "short_title": "短标题"}

    db = OneDB([notice()])
    result = EnrichmentQueue(
        db, Store(base_url="x", model="m", api_key="k"), lambda _c: Client()
    ).process_notice(1)
    assert result["status"] == "ready"
    assert result["notice"]["summary"] == "可分享摘要"


def test_process_one_notice_falls_back_when_chat_is_disabled():
    db = DB([notice()])
    result = EnrichmentQueue(
        db, Store(base_url="", model="", api_key=None), lambda _c: object()
    ).process_notice(1)
    assert result["status"] == "disabled" and result["notice"]["id"] == 1


def test_share_summary_cache_skips_client():
    item = {**notice(), "share_summary": "本科生创新项目开始申报"}
    called = []
    result = EnrichmentQueue(
        DB([item]), Store(base_url="x", model="m", api_key="k"), lambda _c: called.append(True)
    ).summarize_share(1)
    assert result["cached"] is True
    assert result["summary"] == "本科生创新项目开始申报"
    assert called == []


def test_share_summary_reuses_existing_ai_short_title_and_caches_it():
    item = {**notice(), "share_summary": "", "short_title": "本科生创新项目申报通知", "summary": ""}
    db = DB([item])
    result = EnrichmentQueue(
        db, Store(base_url="x", model="m", api_key="k"), lambda _c: object()
    ).summarize_share(1)
    assert result["status"] == "ready" and result["cached"] is False
    assert db.get_notice(1)["share_summary"] == "本科生创新项目申报通知"


def test_share_summary_calls_dedicated_ai_once_without_hard_truncation():
    class Client:
        calls = 0
        def summarize_share(self, *_args):
            self.calls += 1
            return "摘要：本科生创新项目现已开始申报，请及时提交材料"
    client = Client()
    item = {**notice(), "share_summary": "", "short_title": "", "summary": ""}
    db = DB([item])
    result = EnrichmentQueue(
        db, Store(summary_enabled=True, summary_url="x", summary_model="m", summary_api_key="k"), lambda _c: client
    ).summarize_share(1)
    assert result["status"] == "ready" and client.calls == 1
    assert result["summary"] == "本科生创新项目现已开始申报，请及时提交材料"
    assert not result["summary"].startswith("摘要")
    assert db.get_notice(1)["share_summary"] == result["summary"]


def test_share_summary_failure_is_not_cached():
    class Client:
        def summarize_share(self, *_args): raise AIError("temporary")
    item = {**notice(), "share_summary": "", "short_title": "", "summary": ""}
    db = DB([item])
    result = EnrichmentQueue(
        db, Store(summary_enabled=True, summary_url="x", summary_model="m", summary_api_key="k"), lambda _c: Client()
    ).summarize_share(1)
    assert result["status"] == "failed"
    assert db.get_notice(1).get("share_summary", "") == ""


def test_share_summary_rejects_too_short_ai_output():
    class Client:
        def summarize_share(self, *_args): return "通知"
    item = {**notice(), "share_summary": "", "short_title": "", "summary": ""}
    db = DB([item])
    result = EnrichmentQueue(
        db, Store(summary_enabled=True, summary_url="x", summary_model="m", summary_api_key="k"), lambda _c: Client()
    ).summarize_share(1)
    assert result["status"] == "failed"
    assert db.get_notice(1).get("share_summary", "") == ""


def test_share_summary_removes_dates_from_cached_or_generated_text():
    class Client:
        def summarize_share(self, *_args): return "本科生选课时间为8月25日"
    item = {
        **notice(), "share_summary": "本科生选课时间为8月2", "short_title": "", "summary": "",
    }
    db = DB([item])
    result = EnrichmentQueue(
        db, Store(summary_enabled=True, summary_url="x", summary_model="m", summary_api_key="k"),
        lambda _config: Client(),
    ).summarize_share(1)
    assert result["status"] == "ready"
    assert result["summary"] == "本科生选课"


def test_background_summary_generation_processes_only_missing_and_reports_progress():
    class Client:
        def summarize_share(self, title, _body):
            return f"{title}生成的小摘要内容"
    existing = {**notice(1), "official_title": "已有摘要公告", "share_summary": "已经缓存的摘要内容"}
    missing = {**notice(2), "official_title": "待处理公告", "share_summary": "", "short_title": "", "summary": ""}
    queue = EnrichmentQueue(
        DB([existing, missing]),
        Store(summary_enabled=True, summary_url="x", summary_model="small", summary_api_key="k"),
        lambda _config: Client(),
    )
    result = queue.process_missing_summaries()
    assert result["status"] == "completed"
    assert result["total"] == 1 and result["processed"] == 1
    assert result["generated"] == 1 and result["failed"] == 0
    assert queue.summary_status()["running"] is False


def test_background_summary_generation_is_disabled_without_its_own_model():
    queue = EnrichmentQueue(DB([]), Store(summary_enabled=False), lambda _config: object())
    result = queue.process_missing_summaries()
    assert result["status"] == "disabled"


def test_background_summary_generation_revalidates_old_temporal_cache():
    class Client:
        def summarize_share(self, *_args): return "本科生第一学期选课安排"
    stale = {
        **notice(), "share_summary": "本科生选课时间为8月2", "short_title": "", "summary": "",
    }
    db = DB([stale])
    queue = EnrichmentQueue(
        db, Store(summary_enabled=True, summary_url="x", summary_model="small", summary_api_key="k"),
        lambda _config: Client(),
    )
    result = queue.process_missing_summaries()
    assert result["generated"] == 1
    assert db.get_notice(1)["share_summary"] == "本科生第一学期选课安排"
