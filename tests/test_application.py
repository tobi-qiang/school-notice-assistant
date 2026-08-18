from __future__ import annotations

import threading
from datetime import date

from app.application import SyncCoordinator
from app.models import ListEntry, NoticeDocument, SourceConfig
from app.webplus import AuthenticationRequired, FetchError


def source(key="one", *, site="school", start=date(2024, 6, 1)):
    return SourceConfig(key, key, "https://example.test", "/list.htm", key, start, site)


def entry(src, article_id, published=date(2025, 1, 1), position=1):
    return ListEntry(src.key, article_id, article_id, f"https://example.test/{article_id}", published, position)


def document(src, item):
    return NoticeDocument(
        src.key, item.article_id, item.title, item.url, item.published_date,
        "正文", "<p>正文</p>", site_key=src.site_key, categories=(src.category,),
    )


class FakeDatabase:
    def __init__(self, known=()):
        self.known = set(known)
        self.unavailable = set()
        self.unavailable_records = []
        self.started = 0
        self.staged = []
        self.committed = []
        self.failed = []

    def begin_sync(self):
        self.started += 1
        return self.started

    def is_known(self, site_key, article_id):
        return (site_key, article_id) in self.known

    def is_unavailable(self, site_key, article_id):
        return (site_key, article_id) in self.unavailable

    def record_unavailable(self, site_key, article_id, source_key, title, url, http_status, error=""):
        self.unavailable.add((site_key, article_id))
        self.unavailable_records.append((site_key, article_id, http_status))

    def stage_notice(self, *args):
        self.staged.append(args)

    def commit_sync(self, run_id):
        self.committed.append(run_id)
        return {"run_id": run_id, "status": "completed", "new_count": len(self.staged)}

    def fail_sync(self, run_id, **kwargs):
        self.failed.append((run_id, kwargs))


class FakeClient:
    def __init__(self, pages, details=None, fail_detail=False, after_list=None):
        self.pages = pages
        self.details = details or {}
        self.fail_detail = fail_detail
        self.after_list = after_list
        self.calls = []
        self.detail_calls = []
        self.cancel_event = None

    def fetch_list(self, src, page):
        self.calls.append((src.key, page))
        if self.after_list:
            self.after_list()
        return self.pages.get((src.key, page), [])

    def fetch_detail(self, src, item):
        self.detail_calls.append(item.article_id)
        if isinstance(self.fail_detail, Exception):
            raise self.fail_detail
        if self.fail_detail:
            raise RuntimeError("detail unavailable")
        return self.details.get(item.article_id) or document(src, item)


def coordinator(db, sources, client, **kwargs):
    return SyncCoordinator(db, sources, client_factory=lambda _source, event: _assign(client, event), **kwargs)


def _assign(client, event):
    client.cancel_event = event
    return client


def test_syncs_all_three_sources_and_commits_once():
    sources = [source("a"), source("b"), source("c")]
    pages = {(src.key, 1): [entry(src, src.key)] for src in sources}
    db, client = FakeDatabase(), FakeClient(pages)
    result = coordinator(db, sources, client).run_sync()
    assert result["status"] == "completed"
    assert len(db.staged) == 3
    assert db.committed == [1]
    assert not db.failed


def test_history_cutoff_stops_without_fetching_details():
    src = source(start=date(2024, 6, 1))
    client = FakeClient({(src.key, 1): [entry(src, "old", date(2024, 5, 31))]})
    db = FakeDatabase()
    coordinator(db, [src], client).run_sync()
    assert db.staged == []
    assert client.calls == [(src.key, 1)]


def test_consecutive_known_stops_source():
    src = source()
    known = {(src.site_key, name) for name in ("1", "2")}
    client = FakeClient({(src.key, 1): [entry(src, "1"), entry(src, "2"), entry(src, "new")]})
    db = FakeDatabase(known)
    coordinator(db, [src], client, known_streak_stop=2).run_sync()
    assert db.staged == []


def test_detail_failure_fails_whole_batch():
    src = source()
    db = FakeDatabase()
    client = FakeClient({(src.key, 1): [entry(src, "x")]}, fail_detail=True)
    result = coordinator(db, [src], client).run_sync()
    assert result["status"] == "failed"
    assert db.committed == []
    assert db.failed[0][1]["stop_position"] == "one:1"


def test_cancel_after_request_fails_as_cancelled_and_shares_event():
    src = source()
    event = threading.Event()
    client = FakeClient({(src.key, 1): [entry(src, "x")]}, after_list=event.set)
    db = FakeDatabase()
    result = coordinator(db, [src], client).run_sync(event)
    assert result["status"] == "cancelled"
    assert db.failed[0][1]["cancelled"] is True
    assert client.cancel_event is event


def test_busy_does_not_begin_second_batch():
    db = FakeDatabase()
    service = SyncCoordinator(db, [])
    service._active = True
    assert service.run_sync()["status"] == "busy"
    assert db.started == 0


class FakeTasks:
    def __init__(self):
        self.target = None
        self.cancelled = False

    def start(self, name, target):
        assert name == "school-sync"
        self.target = target
        return True

    def cancel(self, name):
        self.cancelled = name == "school-sync"
        return self.cancelled

    def status(self, _name):
        return {"active": False, "error": None}


def test_start_and_cancel_only_use_controlled_tasks():
    tasks = FakeTasks()
    service = SyncCoordinator(FakeDatabase(), [], tasks=tasks)
    assert service.start_sync() is True
    assert tasks.target == service.run_sync
    assert service.cancel() is True


def test_start_without_task_registry_is_rejected():
    service = SyncCoordinator(FakeDatabase(), [])
    try:
        service.start_sync()
    except RuntimeError:
        pass
    else:
        raise AssertionError("start_sync must not create an unmanaged thread")


def test_post_commit_hook_failure_does_not_roll_back_sync():
    db = FakeDatabase()
    service = SyncCoordinator(db, [], after_commit=lambda: (_ for _ in ()).throw(RuntimeError("AI down")))
    result = service.run_sync()
    assert result["status"] == "completed" and result["enrichment_started"] is False
    assert db.committed == [1] and db.failed == []


def test_gone_detail_is_recorded_skipped_and_batch_continues():
    src = source()
    gone, good = entry(src, "gone"), entry(src, "good", position=2)
    client = FakeClient({(src.key, 1): [gone, good]})
    original = client.fetch_detail

    def fetch_detail(current_source, item):
        if item.article_id == "gone":
            client.detail_calls.append(item.article_id)
            raise FetchError("gone", status_code=410, url=item.url)
        return original(current_source, item)

    client.fetch_detail = fetch_detail
    db = FakeDatabase()
    service = coordinator(db, [src], client)
    assert service.run_sync()["status"] == "completed"
    assert db.unavailable_records == [(src.site_key, "gone", 410)]
    assert len(db.staged) == 1
    progress = service.status()["progress"]
    assert progress["phase"] == "completed" and progress["skipped_count"] == 1
    assert [item["state"] for item in progress["events"]] == ["saved", "skipped"]


def test_progress_events_are_newest_first_and_unavailable_is_not_requested():
    src = source()
    first, second = entry(src, "first"), entry(src, "second", position=2)
    db = FakeDatabase()
    db.unavailable.add((src.site_key, "first"))
    client = FakeClient({(src.key, 1): [first, second]})
    service = coordinator(db, [src], client)
    assert service.run_sync()["status"] == "completed"
    progress = service.status()["progress"]
    assert client.detail_calls == ["second"]
    assert progress["skipped_count"] == 1
    assert progress["events"][0]["title"] == "second"
    assert progress["events"][0]["state"] == "saved"


def test_non_permanent_fetch_error_still_fails_batch():
    src = source()
    client = FakeClient(
        {(src.key, 1): [entry(src, "broken")]},
        fail_detail=FetchError("server down", status_code=500, url="https://example.test/broken"),
    )
    db = FakeDatabase()
    assert coordinator(db, [src], client).run_sync()["status"] == "failed"
    assert db.committed == [] and db.failed


def test_status_uses_persisted_database_result_after_restart():
    db = FakeDatabase()
    db.dashboard_summary = lambda: {
        "sync_status": "completed", "sync_message": "公告同步完成"
    }
    status = coordinator(db, [], FakeClient({})).status()
    assert status["status"] == "completed"
    assert status["message"] == "公告同步完成"


def test_source_states_remain_visible_after_fast_completion():
    sources = [source("a"), source("b")]
    service = coordinator(FakeDatabase(), sources, FakeClient({}))
    assert service.run_sync()["status"] == "completed"
    assert [item["state"] for item in service.status()["progress"]["sources"]] == [
        "completed", "completed"
    ]


def test_jwc_authentication_is_requested_once_then_detail_retried():
    src = source("jwc_notices", site="jwc_suda")
    item = entry(src, "one")
    client = FakeClient({(src.key, 1): [item]})
    calls = {"details": 0}

    def fetch_detail(current_source, current_item):
        calls["details"] += 1
        if calls["details"] == 1:
            raise AuthenticationRequired("login", url="https://auth.suda.edu.cn/cas/login")
        return document(current_source, current_item)

    client.fetch_detail = fetch_detail
    client._session = object()

    class Auth:
        def __init__(self): self.login_calls = 0; self.apply_calls = 0
        def apply_to(self, _session): self.apply_calls += 1; return self.login_calls > 0
        def login(self, _url, **kwargs): self.login_calls += 1; return {"configured": True}

    auth = Auth()
    service = SyncCoordinator(
        FakeDatabase(), [src], client_factory=lambda *_: client, jwc_auth=auth
    )
    assert service.run_sync()["status"] == "completed"
    assert calls["details"] == 2 and auth.login_calls == 1 and auth.apply_calls == 2



def test_event_published_date_is_iso_from_list_entry():
    src = source()
    item = entry(src, "x", published=date(2025, 3, 14))
    db = FakeDatabase()
    client = FakeClient({(src.key, 1): [item]})
    service = coordinator(db, [src], client)
    assert service.run_sync()["status"] == "completed"
    events = service.status()["progress"]["events"]
    assert events[0]["title"] == item.title
    assert events[0]["published_date"] == "2025-03-14"
    assert events[0]["state"] == "saved"


def test_status_after_restart_with_completed_db_does_not_show_queued_sources():
    src = source()
    db = FakeDatabase()
    db.dashboard_summary = lambda: {
        "sync_status": "completed", "sync_message": "公告同步完成"
    }
    service = coordinator(db, [src], FakeClient({}))
    status = service.status()
    assert status["status"] == "completed"
    assert status["message"] == "公告同步完成"
    assert status["progress"]["sources"] == []
    assert status["progress"]["events"] == []


def test_status_after_restart_with_idle_db_does_not_show_queued_sources():
    src = source()
    db = FakeDatabase()
    db.dashboard_summary = lambda: {
        "sync_status": "idle", "sync_message": "尚未同步"
    }
    service = coordinator(db, [src], FakeClient({}))
    status = service.status()
    assert status["status"] == "idle"
    assert status["progress"]["sources"] == []
    assert status["progress"]["events"] == []


def test_status_preserves_real_source_states_when_sync_has_run_in_this_process():
    sources = [source("a"), source("b")]
    db = FakeDatabase()
    db.dashboard_summary = lambda: {
        "sync_status": "failed", "sync_message": "同步失败"
    }
    service = coordinator(db, sources, FakeClient({}))
    assert service.run_sync()["status"] == "completed"
    # After a real sync in this process, real source states must remain visible
    # even though the persisted dashboard still reports a stale "failed" entry.
    states = [item["state"] for item in service.status()["progress"]["sources"]]
    assert states == ["completed", "completed"]


def test_status_after_failed_sync_in_same_process_keeps_failed_source_state():
    src = source()
    client = FakeClient({(src.key, 1): [entry(src, "x")]}, fail_detail=True)
    db = FakeDatabase()
    db.dashboard_summary = lambda: {
        "sync_status": "failed", "sync_message": "同步失败"
    }
    service = coordinator(db, [src], client)
    assert service.run_sync()["status"] == "failed"
    progress = service.status()["progress"]
    assert [item["state"] for item in progress["sources"]] == ["failed"]
    assert progress["phase"] == "failed"
