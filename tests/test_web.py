from __future__ import annotations

from app.config_store import AIConfig
from app.web import create_app


class DB:
    def __init__(self):
        self.filters = None
        self.notices = []
        self.completed = None
    def list_notices(self, **kwargs): self.filters = kwargs; return self.notices
    def dashboard_summary(self):
        return {"new_count": 0, "pending_share_count": 0, "last_sync": None, "last_share": None,
                "sync_status": "idle", "sync_message": "尚未同步"}
    def get_notice(self, notice_id):
        return next((item for item in self.notices if item["id"] == notice_id), None)
    def get_share_candidates(self, mode="new", date_start="", date_end=""):
        return {"through_run_id": 4, "items": [], "selection_mode": mode,
                "date_start": date_start, "date_end": date_end}
    def complete_share(self, through_run_id, items, copied_text, **metadata):
        self.completed = (through_run_id, items, copied_text, metadata); return 9
    def share_history(self): return []


class Coordinator:
    def __init__(self): self.start_result = True; self.cancelled = False
    def start_sync(self): return self.start_result
    def cancel(self): self.cancelled = True; return True
    def status(self): return {"running": False, "status": "idle", "message": "尚未同步"}


class Store:
    def __init__(self): self.config = AIConfig(); self.saved = None
    def load(self): return self.config
    def key_hint(self, capability="chat"):
        key = {"chat": self.config.api_key, "embedding": self.config.embedding_api_key,
               "rerank": self.config.rerank_api_key,
               "summary": self.config.summary_api_key}.get(capability)
        return "已设置" if key else "未设置"
    def save(self, config, new_api_key=None, new_embedding_api_key=None, new_rerank_api_key=None,
             new_summary_api_key=None, **_kwargs):
        old = self.config
        self.config = config
        self.config.api_key = new_api_key or old.api_key
        self.saved = new_api_key
        self.config.embedding_api_key = new_embedding_api_key or old.embedding_api_key
        self.config.rerank_api_key = new_rerank_api_key or old.rerank_api_key
        self.config.summary_api_key = new_summary_api_key or old.summary_api_key


class Heartbeat:
    def __init__(self): self.beats = []; self.disconnected = []; self.suspended = []
    def heartbeat(self, identity): self.beats.append(identity)
    def disconnect(self, identity): self.disconnected.append(identity)
    def suspend(self, identity): self.suspended.append(identity)


class Lifecycle:
    def __init__(self): self.heartbeat = Heartbeat(); self.exiting = False
    def request_exit(self): self.exiting = True


def setup(enrichment=None):
    db, coordinator, store, lifecycle = DB(), Coordinator(), Store(), Lifecycle()
    app = create_app(db, coordinator, store, enrichment=enrichment, lifecycle=lifecycle)
    app.config.update(TESTING=True)
    return app.test_client(), db, coordinator, store, lifecycle


def sample_notice():
    return {"id": 1, "source_name": "学院", "categories": ["通知"], "published_date": "2026-08-01",
            "official_title": "<script>原标题</script>", "short_title": "", "original_url": "https://school/1",
            "body_html": "<p>安全正文</p>", "body_text": "安全正文", "attachment_names": ["附件.pdf"],
            "uncertainty": "", "audience": "学生", "deadline": "", "action": "", "start_time": "",
            "method_or_location": "", "contact": "", "snippet": "<img src=x onerror=1>"}


def test_homepage_uses_real_template_and_filters():
    client, db, *_ = setup(); db.notices = [sample_notice()]
    response = client.get("/?q=大创&source=future_notices&year=2026")
    assert response.status_code == 200 and "学校公告助手" in response.get_data(as_text=True)
    assert "<img src=x onerror=1>" not in response.get_data(as_text=True)
    assert db.filters == {"q": "大创", "source_key": "future_notices", "year": 2026, "limit": 15}


def test_detail_and_missing_notice():
    client, db, *_ = setup(); db.notices = [sample_notice()]
    assert client.get("/notice/1").status_code == 200
    assert "安全正文" in client.get("/notice/1").get_data(as_text=True)
    assert client.get("/notice/99").status_code == 404


def test_sync_conflict_stop_and_status():
    client, _db, coordinator, *_ = setup()
    assert client.post("/api/sync").status_code == 202
    coordinator.start_result = False
    assert client.post("/api/sync").status_code == 409
    assert client.post("/api/sync/stop").get_json()["cancelled"] is True
    assert client.get("/api/status").get_json()["status"] == "idle"


def test_fixed_share_snapshot_payload_is_forwarded():
    client, db, *_ = setup()
    assert client.get("/api/share/candidates").get_json()["through_run_id"] == 4
    response = client.post("/api/share/complete", json={
        "through_run_id": 4, "items": [{"id": 1, "selected": False}], "copied_text": "文本"
    })
    assert response.status_code == 200 and response.get_json()["id"] == 9
    assert db.completed == (4, [{"id": 1, "selected": False}], "文本", {
        "selection_mode": "new", "date_start": "", "date_end": "",
    })
    assert client.post("/api/share/complete", data="bad").status_code == 400


def test_range_share_filter_and_metadata_are_forwarded():
    client, db, *_ = setup()
    candidates = client.get(
        "/api/share/candidates?mode=range&start=2026-08-01&end=2026-08-18"
    ).get_json()
    assert candidates["selection_mode"] == "range"
    assert candidates["date_start"] == "2026-08-01"
    response = client.post("/api/share/complete", json={
        "through_run_id": 4, "items": [{"id": 1, "selected": True}],
        "copied_text": "范围文本", "selection_mode": "range",
        "date_start": "2026-08-01", "date_end": "2026-08-18",
    })
    assert response.status_code == 200
    assert db.completed[3] == {
        "selection_mode": "range", "date_start": "2026-08-01", "date_end": "2026-08-18",
    }


def test_settings_never_echo_api_key():
    client, _db, _coordinator, store, _lifecycle = setup()
    response = client.post("/api/settings", json={"base_url": "https://ai", "model": "m", "api_key": "SECRET"})
    assert response.status_code == 200 and "SECRET" not in response.get_data(as_text=True)
    assert store.saved == "SECRET"
    assert "api_key" not in client.get("/api/settings").get_json()


def test_capability_settings_save_only_selected_model():
    client, _db, _coordinator, store, _lifecycle = setup()
    store.config.model = "chat-before"
    response = client.post("/api/settings/embedding", json={
        "embedding_enabled": False, "embedding_url": "https://embed/custom",
        "embedding_model": "my-embed", "embedding_dimensions": 768,
    })
    assert response.status_code == 200
    assert store.config.embedding_model == "my-embed"
    assert store.config.model == "chat-before"


def test_notice_list_api_and_single_share_fallback():
    client, db, *_ = setup(); db.notices = [sample_notice()]
    listing = client.get("/api/notices?page=1&page_size=15").get_json()
    assert listing["page"] == 1 and listing["items"][0]["id"] == 1
    draft = client.post("/api/notices/1/share-draft").get_json()
    assert draft["title"] == "<script>原标题</script>"
    assert draft["text"] == "<script>原标题</script> https://school/1"
    assert draft["warning"]


def test_summary_capability_saves_without_changing_chat():
    client, _db, _coordinator, store, _lifecycle = setup()
    store.config.model = "large-model"
    response = client.post("/api/settings/summary", json={
        "summary_enabled": True, "summary_url": "https://small.example/v1",
        "summary_model": "small-model", "summary_api_key": "SUMMARY-SECRET",
    })
    assert response.status_code == 200
    assert store.config.model == "large-model"
    assert store.config.summary_model == "small-model"
    assert "SUMMARY-SECRET" not in response.get_data(as_text=True)


def test_summary_background_start_stop_and_status():
    class SummaryEnrichment:
        def status(self): return {"running": False}
        def summary_status(self): return {"running": True, "status": "running", "processed": 2}
        def start_summaries(self): return True
        def cancel_summaries(self): return True

    client, _db, _coordinator, store, _lifecycle = setup(enrichment=SummaryEnrichment())
    store.config.summary_enabled = True
    store.config.summary_url = "https://small.example/v1"
    store.config.summary_model = "small-model"
    store.config.summary_api_key = "secret"
    assert client.post("/api/share-summaries/start").status_code == 202
    assert client.post("/api/share-summaries/stop").get_json()["cancelled"] is True
    assert client.get("/api/status").get_json()["summary_job"]["processed"] == 2


def test_single_share_is_exactly_summary_space_url_without_field_labels():
    class ShareEnrichment:
        def summarize_share(self, _notice_id):
            return {"status": "ready", "notice": None,
                    "summary": "本科生创新项目开始申报", "cached": True}
        def status(self): return {"running": False}

    client, db, *_ = setup(enrichment=ShareEnrichment())
    db.notices = [{**sample_notice(), "share_summary": ""}]
    draft = client.post("/api/notices/1/share-draft").get_json()
    assert draft["text"] == "本科生创新项目开始申报 https://school/1"
    assert draft["summary"] == "本科生创新项目开始申报"
    assert draft["cached"] is True
    assert draft["warning"] == ""
    assert all(label not in draft["text"] for label in ("摘要：", "适用对象：", "截止时间：", "需要操作："))


def test_heartbeat_disconnect_and_explicit_exit_are_distinct():
    client, *_rest, lifecycle = setup()
    beat = client.post("/api/heartbeat", headers={"X-Client-ID": "tab-1"})
    assert beat.status_code == 200 and lifecycle.heartbeat.beats == ["tab-1"]
    client.post("/api/disconnect", headers={"X-Client-ID": "tab-1"})
    assert lifecycle.heartbeat.disconnected == ["tab-1"] and lifecycle.exiting is False
    client.post("/api/suspend?client_id=tab-2")
    assert lifecycle.heartbeat.suspended == ["tab-2"]
    response = client.post("/api/exit")
    assert lifecycle.exiting is False
    response.close()
    assert lifecycle.exiting is True


def test_history_routes_and_unknown_api():
    client, *_ = setup()
    assert client.get("/history").status_code == 200
    assert client.get("/api/share-history").get_json() == []
    assert client.get("/api/no-such-route").status_code == 404


def test_health_does_not_register_a_page_or_read_database():
    client, db, _, _, lifecycle = setup()
    response = client.get('/api/health')
    assert response.status_code == 200
    assert response.get_json()['app'] == 'school-notice-assistant'
    assert response.headers['Cache-Control'] == 'no-store'
    assert db.filters is None
    assert lifecycle.heartbeat.beats == []


def test_auth_status_distinguishes_saved_from_verified(tmp_path):
    from app.jwc_auth import JWCAuthManager
    manager = JWCAuthManager(tmp_path / 'session.bin', protector=lambda b: b[::-1],
                             unprotector=lambda b: b[::-1])
    manager._save_state([dict(name='session', value='private', domain='jwc.suda.edu.cn', path='/')])
    app = create_app(DB(), Coordinator(), Store(), jwc_auth=manager)
    client = app.test_client()
    auth = client.get('/api/jwc-auth').get_json()
    assert auth['configured'] and auth['validation'] == 'unverified'
    assert '尚未验证当前是否有效' in client.get('/settings').get_data(as_text=True)
    assert 'private' not in client.get('/api/jwc-auth').get_data(as_text=True)
