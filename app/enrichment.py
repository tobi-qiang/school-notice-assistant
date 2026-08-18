"""Recoverable, per-notice AI enrichment processing."""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any

from .ai_client import AIError, InvalidAIResponse
from .models import AIEnrichment, AIStatus

SHARE_MIN_CHARS = 4
SHARE_MAX_CHARS = 40
_SHARE_LABEL_RE = re.compile(r'^\s*(?:\u6458\u8981|\u603b\u7ed3|\u6982\u8981|\u6982\u8ff0|\u7b80\u4ecb)\s*[:\uff1a]\s*')
_SHARE_TEMPORAL_RE = re.compile(
    r'(?:20\d{2}\s*年\s*)?\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号]?)?'
    r'|(?:\d{1,2}\s*[日号]\s*(?:至|到|—|－|-)\s*\d{1,2}\s*[日号]?)'
    r'|(?:\d{1,2}\s*[日号])'
    r'|(?:\d{1,2}\s*[:：]\s*\d{2})'
    r'|(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2})'
    r'|(?:截止(?:时间|日期)?|截至)'
)


class EnrichmentQueue:
    def __init__(self, database, config_store, client_factory, tasks=None):
        self.database = database
        self.config_store = config_store
        self.client_factory = client_factory
        self.tasks = tasks
        self._lock = threading.RLock()
        self._running = False
        self._last_result: dict[str, Any] | None = None
        self._summary_running = False
        self._summary_progress: dict[str, Any] = {
            "status": "idle", "message": "尚未生成摘要", "total": 0,
            "processed": 0, "generated": 0, "reused": 0, "failed": 0,
            "current_title": "", "failure_reasons": {},
        }
        self._notice_locks: dict[int, threading.Lock] = {}

    @staticmethod
    def _text(value: Any) -> str:
        return value if isinstance(value, str) else ""

    @staticmethod
    def _strings(value: Any) -> list[str]:
        return [item for item in value if isinstance(item, str)] if isinstance(value, (list, tuple)) else []

    def _build_enrichment(self, raw: Any, model: str) -> AIEnrichment:
        data = raw if isinstance(raw, dict) else {}
        external = data.get("requires_external_system")
        if external is not None and not isinstance(external, bool):
            external = None
        return AIEnrichment(
            short_title=self._text(data.get("short_title")), summary=self._text(data.get("summary")),
            audience=self._text(data.get("audience")), deadline=self._text(data.get("deadline")),
            action=self._text(data.get("action")), start_time=self._text(data.get("start_time")),
            materials=self._strings(data.get("materials")),
            method_or_location=self._text(data.get("method_or_location")),
            contact=self._text(data.get("contact")), requires_external_system=external,
            uncertainty=self._text(data.get("uncertainty")), tags=self._strings(data.get("tags")),
            status=AIStatus.READY, model=model, generated_at=datetime.now(timezone.utc), error="",
        )

    @staticmethod
    def _safe_error(exc: Exception, notice: dict[str, Any], api_key: str | None) -> str:
        message = f"{type(exc).__name__}: {exc}"
        for sensitive in (api_key, notice.get("body_text"), notice.get("official_title")):
            if sensitive:
                message = message.replace(str(sensitive), "[已隐藏]")
        return message[:300]

    @staticmethod
    def _strip_share_labels(text: str) -> str:
        """Strip common Chinese summary labels such as '摘要：' from input."""
        if not text:
            return ''
        return _SHARE_LABEL_RE.sub('', str(text))

    @classmethod
    def _normalize_share_summary(cls, text: Any) -> str:
        """Single-line, label-stripped, whitespace-collapsed summary."""
        if text is None:
            return ''
        cleaned = cls._strip_share_labels(str(text))
        cleaned = re.sub(r'\s+', '', cleaned)
        return cleaned.strip(' \t\r\n“”‘’\"\'【】')

    @staticmethod
    def _share_summary_in_range(text: str) -> bool:
        return SHARE_MIN_CHARS <= len(text) <= SHARE_MAX_CHARS

    @classmethod
    def _share_summary_acceptable(cls, text: str) -> bool:
        """Accept a concise topic summary only when it contains no time facts."""
        return cls._share_summary_in_range(text) and not _SHARE_TEMPORAL_RE.search(text)

    @classmethod
    def _sanitize_share_summary(cls, text: Any) -> str:
        """Remove time details while keeping the model's topic description."""
        cleaned = cls._normalize_share_summary(text)
        cleaned = _SHARE_TEMPORAL_RE.sub('', cleaned)
        cleaned = re.sub(
            r'(?:具体)?时间(?:为|是|：|:)?|'
            r'截止(?:时间|日期)?(?:为|至|到|：|:)?|截至',
            '', cleaned,
        )
        cleaned = re.sub(r'(?:至|到)(?=$|[，。；、,;])', '', cleaned)
        return cleaned.strip('，。；、,:：;—－- ')

    def process_pending(self, limit: int = 50, cancel_event=None) -> dict[str, Any]:
        event = cancel_event or threading.Event()
        with self._lock:
            if self._running:
                return {"status": "busy", "processed": 0, "failed": 0, "pending": 0}
            self._running = True
        try:
            notices = self.database.list_pending_ai(limit=limit)
            config = self.config_store.load()
            if not (getattr(config, "base_url", "") and getattr(config, "model", "") and getattr(config, "api_key", None)):
                return self._remember({
                    "status": "disabled", "message": "AI 配置未完成",
                    "processed": 0, "failed": 0, "pending": len(notices),
                })
            client = self.client_factory(config)
            processed = failed = embedding_failed = 0
            for index, notice in enumerate(notices):
                if event.is_set():
                    return self._remember({
                        "status": "cancelled", "processed": processed, "failed": failed,
                        "pending": len(notices) - processed,
                    })
                try:
                    raw = client.enrich(
                        notice.get("official_title", ""), notice.get("body_text", ""),
                        notice.get("attachment_names", []),
                    )
                    if event.is_set():
                        return self._remember({
                            "status": "cancelled", "processed": processed, "failed": failed,
                            "pending": len(notices) - processed,
                        })
                    enrichment = self._build_enrichment(raw, config.model)
                    if not self.database.update_ai(notice["id"], enrichment):
                        raise RuntimeError("公告不存在，无法保存 AI 结果")
                    processed += 1
                    if getattr(config, "embedding_enabled", False) and hasattr(self.database, "store_embedding"):
                        try:
                            retrieval_text = "\n".join((
                                notice.get("official_title", ""), enrichment.short_title,
                                " ".join(enrichment.tags), enrichment.summary,
                                str(notice.get("body_text", ""))[:2000],
                            ))
                            vector = client.embedding(retrieval_text)
                            dimensions = int(getattr(config, "embedding_dimensions", len(vector)))
                            self.database.store_embedding(
                                notice["id"], str(config.embedding_model), dimensions, vector
                            )
                        except Exception:
                            # Embedding is experimental and independent: a failure
                            # must not revert a valid chat enrichment.
                            embedding_failed += 1
                except Exception as exc:
                    failed += 1
                    self.database.mark_ai_pending(
                        notice["id"], self._safe_error(exc, notice, getattr(config, "api_key", None))
                    )
            status = "completed" if failed == 0 else "partial"
            return self._remember({
                "status": status, "processed": processed, "failed": failed, "pending": failed,
                "embedding_failed": embedding_failed,
            })
        finally:
            with self._lock:
                self._running = False

    def _remember(self, result: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._last_result = dict(result)
        return result

    def process_notice(self, notice_id: int) -> dict[str, Any]:
        """Synchronously enrich one user-selected notice for a share draft."""
        notice_id = int(notice_id)
        with self._lock:
            notice_lock = self._notice_locks.setdefault(notice_id, threading.Lock())
        with notice_lock:
            notice = self.database.get_notice(notice_id)
            if notice is None:
                return {"status": "missing", "notice": None}
            if str(notice.get("ai_status", "")) == AIStatus.READY.value:
                return {"status": "ready", "notice": notice}
            config = self.config_store.load()
            if not (
                getattr(config, "base_url", "")
                and getattr(config, "model", "")
                and getattr(config, "api_key", None)
            ):
                return {"status": "disabled", "notice": notice, "message": "Chat 配置未完成"}
            try:
                raw = self.client_factory(config).enrich(
                    notice.get("official_title", ""), notice.get("body_text", ""),
                    notice.get("attachment_names", []),
                )
                enrichment = self._build_enrichment(raw, config.model)
                if not self.database.update_ai(notice_id, enrichment):
                    return {"status": "missing", "notice": None}
                return {"status": "ready", "notice": self.database.get_notice(notice_id)}
            except Exception as exc:
                error = self._safe_error(exc, notice, getattr(config, "api_key", None))
                self.database.mark_ai_pending(notice_id, error)
                return {"status": "failed", "notice": notice, "message": error}
            finally:
                with self._lock:
                    self._notice_locks.pop(notice_id, None)

    def summarize_share(self, notice_id: int) -> dict[str, Any]:
        """Return a concise, topic-only single-line share summary for one notice.

        Lookup order:
          1. ``share_summary`` cache already on the row.
          2. ``short_title`` / ``summary`` produced by an earlier
             :meth:`process_notice` call when the normalised length
             satisfies the current topic-summary policy.
          3. :meth:`AIClient.summarize_share` (single Chat call) when
             Chat is configured.

        Failures are not cached; the original notice is returned with
        ``status='failed'`` so the caller can decide on local fallback.
        Disabled Chat returns ``status='disabled'`` with no summary.
        """
        notice_id = int(notice_id)
        with self._lock:
            notice_lock = self._notice_locks.setdefault(notice_id, threading.Lock())
        try:
            with notice_lock:
                notice = self.database.get_notice(notice_id)
                if notice is None:
                    return {
                        'status': 'missing', 'notice': None,
                        'summary': '', 'cached': False,
                    }

                cached = self._normalize_share_summary(notice.get('share_summary') or '')
                if self._share_summary_acceptable(cached):
                    return {
                        'status': 'ready', 'notice': notice,
                        'summary': cached, 'cached': True, 'origin': 'cache',
                    }
                if cached:
                    # Derived caches from an older policy must never remain
                    # visible to bulk sharing when regeneration fails.
                    self.database.update_share_summary(notice_id, '')
                    notice = self.database.get_notice(notice_id) or notice

                for field in ('short_title', 'summary'):
                    existing = self._normalize_share_summary(notice.get(field) or '')
                    if self._share_summary_acceptable(existing):
                        self.database.update_share_summary(notice_id, existing)
                        refreshed = self.database.get_notice(notice_id) or notice
                        return {
                            'status': 'ready', 'notice': refreshed,
                            'summary': existing, 'cached': False, 'origin': 'existing',
                        }

                config = self.config_store.load()
                if not (
                    getattr(config, 'summary_enabled', False)
                    and getattr(config, 'summary_url', '')
                    and getattr(config, 'summary_model', '')
                    and getattr(config, 'summary_api_key', None)
                ):
                    return {
                        'status': 'disabled', 'notice': notice,
                        'summary': '', 'cached': False,
                        'message': '摘要模型未启用或配置未完成',
                    }

                try:
                    raw = self.client_factory(config).summarize_share(
                        notice.get('official_title', ''),
                        notice.get('body_text', ''),
                    )
                except AIError as exc:
                    return {
                        'status': 'failed', 'notice': notice,
                        'summary': '', 'cached': False,
                        'message': self._safe_error(
                            exc, notice, getattr(config, 'summary_api_key', None)
                        ),
                    }
                except Exception as exc:
                    return {
                        'status': 'failed', 'notice': notice,
                        'summary': '', 'cached': False,
                        'message': self._safe_error(
                            exc, notice, getattr(config, 'summary_api_key', None)
                        ),
                    }

                normalized = self._sanitize_share_summary(raw)
                if not self._share_summary_acceptable(normalized):
                    return {
                        'status': 'failed', 'notice': notice,
                        'summary': '', 'cached': False,
                        'message': '摘要过短、过长或包含日期时间',
                    }
                if not self.database.update_share_summary(notice_id, normalized):
                    return {
                        'status': 'missing', 'notice': None,
                        'summary': '', 'cached': False,
                    }
                refreshed = self.database.get_notice(notice_id) or notice
                return {
                    'status': 'ready', 'notice': refreshed,
                    'summary': normalized, 'cached': False, 'origin': 'generated',
                }
        finally:
            with self._lock:
                self._notice_locks.pop(notice_id, None)

    def start(self) -> bool:
        if self.tasks is None:
            raise RuntimeError("受控任务管理器未配置")
        # ControlledTasks passes its cancellation event as the target's first
        # positional argument.  process_pending's first argument is the batch
        # limit, so adapt the callback explicitly instead of accidentally using
        # an Event as the database LIMIT value.
        return bool(self.tasks.start(
            "ai-enrichment",
            lambda cancel_event: self.process_pending(cancel_event=cancel_event),
        ))

    def cancel(self) -> bool:
        return bool(self.tasks and self.tasks.cancel("ai-enrichment"))

    def process_missing_summaries(self, cancel_event=None) -> dict[str, Any]:
        """Generate cached short summaries for every notice that lacks one."""
        event = cancel_event or threading.Event()
        with self._lock:
            if self._summary_running:
                return dict(self._summary_progress)
            self._summary_running = True
        try:
            config = self.config_store.load()
            if not (
                getattr(config, "summary_enabled", False)
                and getattr(config, "summary_url", "")
                and getattr(config, "summary_model", "")
                and getattr(config, "summary_api_key", None)
            ):
                result = {
                    "status": "disabled", "message": "请先在设置中启用并完整配置摘要模型",
                    "total": 0, "processed": 0, "generated": 0, "reused": 0,
                    "failed": 0, "current_title": "", "failure_reasons": {},
                }
                with self._lock:
                    self._summary_progress = result
                return result
            list_candidates = getattr(
                self.database, "list_share_summary_candidates",
                self.database.list_missing_share_summaries,
            )
            notices = [
                notice for notice in list_candidates()
                if not self._share_summary_acceptable(
                    self._normalize_share_summary(notice.get("share_summary") or "")
                )
            ]
            progress = {
                "status": "running", "message": "正在生成摘要", "total": len(notices),
                "processed": 0, "generated": 0, "reused": 0, "failed": 0,
                "current_title": "", "failure_reasons": {},
            }
            with self._lock:
                self._summary_progress = dict(progress)
            for notice in notices:
                if event.is_set():
                    progress.update(status="cancelled", message="摘要生成已停止", current_title="")
                    break
                progress["current_title"] = str(notice.get("official_title") or "")
                with self._lock:
                    self._summary_progress = dict(progress)
                result = self.summarize_share(int(notice["id"]))
                progress["processed"] += 1
                if result.get("status") == "ready":
                    if result.get("origin") == "existing":
                        progress["reused"] += 1
                    else:
                        progress["generated"] += 1
                else:
                    progress["failed"] += 1
                    message = str(result.get("message") or "")
                    if "日期时间" in message or "过短" in message or "过长" in message:
                        reason = "内容格式"
                    elif "限流" in message or "429" in message or "配额" in message:
                        reason = "限流或配额"
                    elif "网络" in message or "timeout" in message.lower():
                        reason = "网络或超时"
                    elif "响应" in message or "InvalidAIResponse" in message:
                        reason = "模型响应"
                    else:
                        reason = "接口错误"
                    reasons = progress["failure_reasons"]
                    reasons[reason] = int(reasons.get(reason, 0)) + 1
                with self._lock:
                    self._summary_progress = dict(progress)
            else:
                progress.update(
                    status="completed" if progress["failed"] == 0 else "partial",
                    message=("摘要生成完成" if progress["failed"] == 0 else "摘要生成完成，部分项目失败"),
                    current_title="",
                )
            with self._lock:
                self._summary_progress = dict(progress)
            return progress
        finally:
            with self._lock:
                self._summary_running = False

    def start_summaries(self) -> bool:
        if self.tasks is None:
            raise RuntimeError("受控任务管理器未配置")
        return bool(self.tasks.start("share-summaries", self.process_missing_summaries))

    def cancel_summaries(self) -> bool:
        return bool(self.tasks and self.tasks.cancel("share-summaries"))

    def summary_status(self) -> dict[str, Any]:
        task = self.tasks.status("share-summaries") if self.tasks is not None else None
        with self._lock:
            result = dict(self._summary_progress)
            result["running"] = self._summary_running or bool(task and task.get("active"))
            if task and task.get("error"):
                result.update(status="failed", message="摘要任务异常结束")
            return result

    def status(self) -> dict[str, Any]:
        task = self.tasks.status("ai-enrichment") if self.tasks is not None else None
        with self._lock:
            return {
                "running": self._running or bool(task and task.get("active")),
                "error": task.get("error") if task else None,
                "last_result": dict(self._last_result) if self._last_result else None,
            }
