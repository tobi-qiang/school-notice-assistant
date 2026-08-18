"""Synchronization coordinator joining WebPlus clients to atomic database batches."""
from __future__ import annotations

import threading
from typing import Any, Callable

from . import settings
from .jwc_auth import JWCAuthError
from .webplus import AuthenticationRequired, FetchError, WebPlusClient


class SyncCoordinator:
    def __init__(
        self, database, sources, client_factory: Callable | None = None,
        tasks=None, known_streak_stop: int = 20, after_commit: Callable[[], Any] | None = None,
        jwc_auth=None,
    ):
        self.database = database
        self.sources = tuple(sources)
        self.client_factory = client_factory or self._default_client
        self.tasks = tasks
        self.known_streak_stop = max(1, int(known_streak_stop))
        self.after_commit = after_commit
        self.jwc_auth = jwc_auth
        self._state_lock = threading.RLock()
        self._active = False
        self._has_started = False
        self._last_result: dict[str, Any] | None = None
        self._event_seq = 0
        self._progress = self._empty_progress()

    def _empty_progress(self) -> dict[str, Any]:
        return {
            "phase": "idle", "source_name": "", "source_index": 0,
            "source_total": len(self.sources), "page": 0, "checked_count": 0,
            "fetched_count": 0, "known_count": 0, "skipped_count": 0, "events": [],
            "message": "", "sources": [
                {"key": source.key, "name": source.name, "index": index, "state": "queued"}
                for index, source in enumerate(self.sources, 1)
            ],
        }

    def _progress_update(self, **values: Any) -> None:
        with self._state_lock:
            self._progress.update(values)

    def _source_state(self, source_key: str, state: str) -> None:
        with self._state_lock:
            for item in self._progress["sources"]:
                if item["key"] == source_key:
                    item["state"] = state
                elif state == "running" and item["state"] == "running":
                    item["state"] = "completed"

    def set_waiting_auth(self, message: str) -> None:
        self._progress_update(phase="waiting_auth", message=message)

    def clear_waiting_auth(self) -> None:
        self._progress_update(phase="fetching", message="")

    def _begin_event(self, source, page: int, title: str, published_date=None) -> int:
        with self._state_lock:
            self._event_seq += 1
            item = {
                "seq": self._event_seq, "title": title, "source_name": source.name,
                "page": page, "state": "fetching", "detail": "正在读取正文",
                "published_date": published_date.isoformat() if published_date else "",
            }
            self._progress["events"].insert(0, item)
            del self._progress["events"][50:]
            return self._event_seq

    def _finish_event(self, seq: int, state: str, detail: str) -> None:
        with self._state_lock:
            for item in self._progress["events"]:
                if item["seq"] == seq:
                    item["state"] = state
                    item["detail"] = detail
                    break

    @staticmethod
    def _default_client(_source, cancel_event):
        return WebPlusClient(
            connect_timeout=settings.REQUEST_CONNECT_TIMEOUT,
            read_timeout=settings.REQUEST_READ_TIMEOUT,
            interval_seconds=settings.REQUEST_INTERVAL_SECONDS,
            cancel_event=cancel_event,
        )

    def _client(self, source, cancel_event):
        try:
            client = self.client_factory(source, cancel_event)
        except TypeError:
            client = self.client_factory()
        if hasattr(client, "_cancel_event"):
            client._cancel_event = cancel_event
        if hasattr(client, "cancel_event"):
            client.cancel_event = cancel_event
        return client

    def run_sync(self, cancel_event=None) -> dict[str, Any]:
        event = cancel_event or threading.Event()
        with self._state_lock:
            if self._active:
                return {"status": "busy", "message": "已有同步任务正在运行"}
            self._active = True
            self._has_started = True
            self._progress = self._empty_progress()
            self._progress["phase"] = "scanning"

        run_id: int | None = None
        position = "sync"
        try:
            run_id = self.database.begin_sync()
            for source_index, source in enumerate(self.sources, 1):
                client = self._client(source, event)
                self._source_state(source.key, "running")
                auth_attempted = False
                if source.key == "jwc_notices" and self.jwc_auth is not None:
                    self.jwc_auth.apply_to(client._session)
                page = 1
                known_streak = 0
                while True:
                    position = f"{source.key}:{page}"
                    self._progress_update(
                        phase="scanning", source_name=source.name,
                        source_index=source_index, page=page,
                    )
                    if event.is_set():
                        return self._fail(run_id, "用户取消同步", True, f"{source.key}:{page}")
                    entries = client.fetch_list(source, page)
                    if event.is_set():
                        return self._fail(run_id, "用户取消同步", True, f"{source.key}:{page}")
                    if not entries:
                        break
                    eligible = [entry for entry in entries if entry.published_date >= source.history_start]
                    if not eligible:
                        break
                    stop_source = False
                    for entry in eligible:
                        with self._state_lock:
                            self._progress["checked_count"] += 1
                        if event.is_set():
                            return self._fail(run_id, "用户取消同步", True, f"{source.key}:{page}")
                        if self.database.is_known(source.site_key, entry.article_id):
                            with self._state_lock:
                                self._progress["known_count"] += 1
                            known_streak += 1
                            if known_streak >= self.known_streak_stop:
                                stop_source = True
                                break
                            continue
                        is_unavailable = getattr(self.database, "is_unavailable", None)
                        if is_unavailable and is_unavailable(source.site_key, entry.article_id):
                            with self._state_lock:
                                self._progress["skipped_count"] += 1
                            continue
                        known_streak = 0
                        event_seq = self._begin_event(source, page, entry.title, entry.published_date)
                        self._progress_update(phase="fetching")
                        try:
                            document = client.fetch_detail(source, entry)
                        except AuthenticationRequired:
                            if source.key != "jwc_notices" or self.jwc_auth is None or auth_attempted:
                                raise
                            auth_attempted = True
                            self.set_waiting_auth("请在弹出的苏大统一认证页面完成登录")
                            try:
                                self.jwc_auth.login(
                                    entry.url, cancel_event=event,
                                    on_status=lambda message: self.set_waiting_auth(message),
                                )
                            except JWCAuthError:
                                raise
                            if not self.jwc_auth.apply_to(client._session):
                                raise RuntimeError("教务处登录态保存失败")
                            self.clear_waiting_auth()
                            document = client.fetch_detail(source, entry)
                        except FetchError as exc:
                            if exc.status_code not in (404, 410):
                                raise
                            self.database.record_unavailable(
                                source.site_key, entry.article_id, source.key, entry.title,
                                exc.url or entry.url, exc.status_code, str(exc),
                            )
                            with self._state_lock:
                                self._progress["skipped_count"] += 1
                            self._finish_event(event_seq, "skipped", f"HTTP {exc.status_code}，已跳过")
                            continue
                        if event.is_set():
                            return self._fail(run_id, "用户取消同步", True, f"{source.key}:{page}")
                        self.database.stage_notice(
                            run_id, document, source.name, source.category, entry.list_position
                        )
                        with self._state_lock:
                            self._progress["fetched_count"] += 1
                        self._finish_event(event_seq, "saved", "正文已暂存")
                    if stop_source:
                        break
                    page += 1
                self._source_state(source.key, "completed")
            self._progress_update(phase="committing")
            result = dict(self.database.commit_sync(run_id))
            result.setdefault("status", "completed")
            result.setdefault("message", "公告同步完成")
            self._progress_update(phase="completed")
            if self.after_commit is not None:
                try:
                    result["enrichment_started"] = bool(self.after_commit())
                except Exception as exc:
                    result["enrichment_started"] = False
                    result["enrichment_error"] = str(exc)[:300]
            return self._remember(result)
        except Exception as exc:
            if run_id is None:
                return self._remember({"status": "failed", "message": str(exc)[:500]})
            if event.is_set():
                return self._fail(run_id, "用户取消同步", True, position)
            return self._fail(run_id, str(exc)[:500], False, position)
        finally:
            with self._state_lock:
                self._active = False

    def _fail(self, run_id: int, error: str, cancelled: bool, position: str) -> dict[str, Any]:
        self.database.fail_sync(
            run_id, error=str(error)[:500], cancelled=cancelled, stop_position=position[:200]
        )
        status = "cancelled" if cancelled else "failed"
        with self._state_lock:
            for item in self._progress["sources"]:
                if item["state"] == "running":
                    item["state"] = status
        self._progress_update(phase=status)
        message = "同步已取消" if cancelled else f"同步失败：{str(error)[:300]}"
        return self._remember({"run_id": run_id, "status": status, "message": message})

    def _remember(self, result: dict[str, Any]) -> dict[str, Any]:
        with self._state_lock:
            self._last_result = dict(result)
        return result

    def start_sync(self) -> bool:
        if self.tasks is None:
            raise RuntimeError("受控任务管理器未配置")
        return bool(self.tasks.start("school-sync", self.run_sync))

    def cancel(self) -> bool:
        return bool(self.tasks and self.tasks.cancel("school-sync"))

    def status(self) -> dict[str, Any]:
        task_status = self.tasks.status("school-sync") if self.tasks is not None else None
        with self._state_lock:
            running = self._active or bool(task_status and task_status.get("active"))
            last = dict(self._last_result) if self._last_result else None
            if self._has_started:
                progress = {
                    **self._progress,
                    "events": [dict(item) for item in self._progress["events"]],
                }
            else:
                # Fresh process that has never run a sync in this lifetime must
                # not pretend that every configured source is still queued. The
                # database may already report completed/idle from a previous
                # run, but this process cannot recover per-source states until
                # it has actually attempted a sync itself.
                progress = {
                    **self._progress,
                    "sources": [],
                    "events": [],
                }
        persisted = None
        if not running and last is None:
            dashboard_summary = getattr(self.database, "dashboard_summary", None)
            if dashboard_summary is not None:
                persisted = dashboard_summary()
        status = (
            "running" if running else
            (last or {}).get("status") or
            (persisted or {}).get("sync_status", "idle")
        )
        message = (
            (progress.get("message") or "正在同步学校公告") if running else
            (last or {}).get("message") or
            (persisted or {}).get("sync_message", "尚未同步")
        )
        return {
            "running": running,
            "status": status,
            "message": message,
            "last_result": last,
            "progress": progress,
        }
