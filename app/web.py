"""Flask application factory for the local-only user interface."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from uuid import uuid4

from flask import Flask, jsonify, make_response, render_template, request

from .ai_client import AIClient
from .config_store import AIConfig, ConfigSecurityError, normalize_chat_url
from .jwc_auth import JWCAuthError
from .settings import SOURCES
from .webplus import WebPlusClient


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def create_app(
    database, coordinator, config_store, enrichment=None, lifecycle=None,
    search_service=None, jwc_auth=None,
) -> Flask:
    app = Flask(__name__)
    heartbeat = getattr(lifecycle, "heartbeat", lifecycle)

    def client_id() -> str:
        return request.headers.get("X-Client-ID") or request.cookies.get("client_id") or uuid4().hex

    @app.get("/")
    def index():
        query = request.args.get("q", "").strip()
        source = request.args.get("source", "").strip()
        year_text = request.args.get("year", "").strip()
        try:
            year = int(year_text) if year_text else None
        except ValueError:
            year = None
        list_page = getattr(database, "list_notice_page", None)
        if list_page is not None:
            listing = list_page(q=query, source_key=source, year=year, page=1, page_size=15)
            notices = listing["items"]
        else:
            notices = database.list_notices(q=query, source_key=source, year=year, limit=15)
            listing = {"items": notices, "page": 1, "has_more": len(notices) == 15,
                       "total": len(notices)}
        response = make_response(render_template(
            "index.html", notices=notices, summary=database.dashboard_summary(), sources=SOURCES,
            filters={"q": query, "source": source, "year": year_text},
            listing=listing,
        ))
        response.set_cookie("client_id", client_id(), samesite="Strict", httponly=True)
        return response

    @app.get("/api/notices")
    def notice_page_api():
        query = request.args.get("q", "").strip()
        source = request.args.get("source", "").strip()
        year_text = request.args.get("year", "").strip()
        try:
            year = int(year_text) if year_text else None
        except ValueError:
            return jsonify(error="年份格式错误"), 400
        page = _bounded_int(request.args.get("page"), 1, 1, 10_000)
        default_size = 15 if page == 1 else 20
        page_size = _bounded_int(request.args.get("page_size"), default_size, 1, 50)
        list_page = getattr(database, "list_notice_page", None)
        if list_page is not None:
            result = list_page(
                q=query, source_key=source, year=year, page=page, page_size=page_size
            )
        else:
            values = database.list_notices(
                q=query, source_key=source, year=year, limit=page * page_size
            )
            start = (page - 1) * page_size
            result = {
                "items": values[start:start + page_size], "page": page,
                "has_more": len(values) > start + page_size, "total": len(values),
            }
        allowed = {
            "id", "official_title", "short_title", "title", "source_name",
            "published_date", "snippet", "summary", "preview", "preview_fragments",
            "original_url", "ai_status",
        }
        items = [{key: value for key, value in item.items() if key in allowed}
                 for item in result.get("items", [])]
        return jsonify(
            items=items, page=int(result.get("page", page)),
            has_more=bool(result.get("has_more", False)),
            total=int(result.get("total", len(items))),
        )

    @app.get("/notice/<int:notice_id>")
    def notice_detail(notice_id: int):
        notice = database.get_notice(notice_id)
        if notice is None:
            return render_template("detail.html", notice=None), 404
        notice = dict(notice)
        notice["attachments"] = notice.get("attachment_names", [])
        notice["ai_uncertainty"] = notice.get("uncertainty", "")
        notice["date_conflict"] = False
        notice["info_card"] = [
            ("适用对象", notice.get("audience")), ("截止时间", notice.get("deadline")),
            ("需要操作", notice.get("action")), ("开始时间", notice.get("start_time")),
            ("方式或地点", notice.get("method_or_location")), ("联系方式", notice.get("contact")),
        ]
        return render_template("detail.html", notice=notice)

    def share_draft_payload(
        notice: dict, ai_status: str, warning: str = "",
        *, summary: str = "", cached: bool = False,
    ) -> dict:
        title = str(notice.get("short_title") or notice.get("official_title") or "\u672a\u547d\u540d\u516c\u544a").strip()
        text_summary = str(summary or "").strip()
        if not text_summary:
            text_summary = title
        url = str(notice.get("original_url") or "")
        text = " ".join(part for part in (text_summary, url) if part)
        return {
            "id": int(notice["id"]), "title": title,
            "summary": text_summary, "url": url, "text": text,
            "cached": bool(cached),
            "ai_status": ai_status, "warning": warning,
        }

    @app.post("/api/notices/<int:notice_id>/share-draft")
    def notice_share_draft(notice_id: int):
        notice = database.get_notice(notice_id)
        if notice is None:
            return jsonify(error="\u516c\u544a\u4e0d\u5b58\u5728"), 404
        status = str(notice.get("ai_status") or "pending")
        warning = ""
        summary = str(notice.get("share_summary") or "").strip()
        cached = bool(summary)
        if enrichment is not None:
            result = enrichment.summarize_share(notice_id)
            refreshed = result.get("notice")
            if refreshed is not None:
                notice = refreshed
            new_status = str(result.get("status") or status)
            if new_status in {"ready", "disabled", "failed", "missing"}:
                status = new_status
            summary = str(result.get("summary") or "").strip()
            cached = bool(result.get("cached", False) and summary)
            origin = str(result.get("origin") or "")
            if status == "ready":
                status = "cached" if cached or origin == "existing" else "generated"
            if status in {"disabled", "failed", "missing"}:
                warning = "摘要模型暂不可用，已使用原公告标题；你可以直接修改后复制。"
                status = "fallback" if status == "disabled" else "unavailable"
        elif not cached:
            warning = "未配置摘要模型，已使用原公告标题；你可以直接修改后复制。"
            status = "fallback"
        elif cached:
            status = "cached"
        return jsonify(share_draft_payload(
            dict(notice), status, warning, summary=summary, cached=cached,
        ))

    def config_view():
        config = config_store.load()
        result = asdict(config)
        result.pop("api_key", None)
        result.pop("embedding_api_key", None)
        result.pop("rerank_api_key", None)
        result.pop("summary_api_key", None)
        result["key_hint"] = config_store.key_hint("chat")
        result["embedding_key_hint"] = config_store.key_hint("embedding")
        result["rerank_key_hint"] = config_store.key_hint("rerank")
        result["summary_key_hint"] = config_store.key_hint("summary")
        result["chat_url"] = normalize_chat_url(config.base_url)
        return result

    def merged_config(payload: dict, current: AIConfig, capability: str | None = None) -> AIConfig:
        def chosen(name: str, old):
            return payload.get(name, old)
        use_chat = capability in (None, "chat")
        use_embedding = capability in (None, "embedding")
        use_rerank = capability in (None, "rerank")
        use_summary = capability in (None, "summary")
        config = AIConfig(
            base_url=str(chosen("base_url", current.base_url) if use_chat else current.base_url).strip(),
            model=str(chosen("model", current.model) if use_chat else current.model).strip(),
            embedding_enabled=(
                bool(chosen("embedding_enabled", current.embedding_enabled))
                if use_embedding else current.embedding_enabled
            ),
            embedding_url=str(
                chosen("embedding_url", current.embedding_url) if use_embedding else current.embedding_url
            ).strip(),
            embedding_model=str(
                chosen("embedding_model", current.embedding_model)
                if use_embedding else current.embedding_model
            ).strip(),
            embedding_dimensions=int(
                chosen("embedding_dimensions", current.embedding_dimensions)
                if use_embedding else current.embedding_dimensions
            ),
            rerank_enabled=(
                bool(chosen("rerank_enabled", current.rerank_enabled))
                if use_rerank else current.rerank_enabled
            ),
            rerank_url=str(
                chosen("rerank_url", current.rerank_url) if use_rerank else current.rerank_url
            ).strip(),
            rerank_model=str(
                chosen("rerank_model", current.rerank_model) if use_rerank else current.rerank_model
            ).strip(),
            summary_enabled=(
                bool(chosen("summary_enabled", current.summary_enabled))
                if use_summary else current.summary_enabled
            ),
            summary_url=str(
                chosen("summary_url", current.summary_url) if use_summary else current.summary_url
            ).strip(),
            summary_model=str(
                chosen("summary_model", current.summary_model) if use_summary else current.summary_model
            ).strip(),
            api_key=(str(payload.get("api_key")).strip() if use_chat and payload.get("api_key") else current.api_key),
            embedding_api_key=(
                str(payload.get("embedding_api_key")).strip()
                if use_embedding and payload.get("embedding_api_key") else current.embedding_api_key
            ),
            rerank_api_key=(
                str(payload.get("rerank_api_key")).strip()
                if use_rerank and payload.get("rerank_api_key") else current.rerank_api_key
            ),
            summary_api_key=(
                str(payload.get("summary_api_key")).strip()
                if use_summary and payload.get("summary_api_key") else current.summary_api_key
            ),
        )
        return config

    def _validate_capability(config: AIConfig, capability: str) -> None:
        if capability == "chat" and not (config.base_url and config.model):
            raise ValueError("Chat URL 和模型不能为空")
        if capability == "embedding" and config.embedding_enabled and not (
            config.embedding_url and config.embedding_model and config.embedding_api_key
        ):
            raise ValueError("Embedding 启用后必须独立填写 URL、Key 和模型")
        if capability == "rerank" and config.rerank_enabled and not (
            config.rerank_url and config.rerank_model and config.rerank_api_key
        ):
            raise ValueError("Rerank 启用后必须独立填写 URL、Key 和模型")
        if capability == "summary" and config.summary_enabled and not (
            config.summary_url and config.summary_model and config.summary_api_key
        ):
            raise ValueError("摘要模型启用后必须独立填写 URL、Key 和模型")

    @app.get("/settings")
    def settings_page():
        return render_template(
            "settings.html", config=config_view(),
            jwc_auth=(jwc_auth.status() if jwc_auth is not None else {"configured": False}),
        )

    @app.get("/history")
    def share_history():
        batches = []
        for item in database.share_history():
            batch = dict(item)
            batch["completed_at"] = batch.get("created_at")
            batches.append(batch)
        return render_template("history.html", batches=batches)

    @app.post("/api/sync")
    def start_sync():
        if not coordinator.start_sync():
            return jsonify(error="同步任务已在运行"), 409
        return jsonify(status="started"), 202

    @app.post("/api/sync/stop")
    def stop_sync():
        return jsonify(cancelled=bool(coordinator.cancel()))

    @app.get("/api/status")
    def status():
        result = coordinator.status()
        if enrichment is not None:
            result["enrichment"] = enrichment.status()
            summary_status = getattr(enrichment, "summary_status", None)
            if summary_status is not None:
                result["summary_job"] = summary_status()
        if jwc_auth is not None:
            result["jwc_auth"] = jwc_auth.status()
        return jsonify(result)

    @app.get("/api/jwc-auth")
    def jwc_auth_status():
        return jsonify(jwc_auth.status() if jwc_auth is not None else {"configured": False})

    @app.post("/api/jwc-auth/login")
    def jwc_auth_login():
        if jwc_auth is None:
            return jsonify(error="教务处登录管理未配置"), 503
        source = next((item for item in SOURCES if item.key == "jwc_notices"), None)
        try:
            entries = WebPlusClient(interval_seconds=0).fetch_list(source, 1) if source else []
            if not entries:
                return jsonify(error="无法读取教务处公告列表"), 502
            result = jwc_auth.login(entries[0].url)
        except JWCAuthError as exc:
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            return jsonify(error=f"教务处登录失败：{type(exc).__name__}"), 502
        return jsonify(status="ok", auth=result)

    @app.post("/api/jwc-auth/clear")
    def jwc_auth_clear():
        if jwc_auth is not None:
            jwc_auth.clear()
        return jsonify(status="cleared")

    @app.post("/api/ai/retry")
    def retry_ai():
        if enrichment is None:
            return jsonify(error="AI 富化未配置"), 503
        if not enrichment.start():
            return jsonify(error="AI 富化任务已在运行"), 409
        return jsonify(status="started"), 202

    @app.post("/api/share-summaries/start")
    def start_share_summaries():
        if enrichment is None or not hasattr(enrichment, "start_summaries"):
            return jsonify(error="摘要生成任务未配置"), 503
        config = config_store.load()
        if not (
            config.summary_enabled and config.summary_url
            and config.summary_model and config.summary_api_key
        ):
            return jsonify(error="请先在设置中启用并完整配置摘要模型"), 400
        if not enrichment.start_summaries():
            return jsonify(error="摘要生成任务已在运行"), 409
        return jsonify(status="started"), 202

    @app.post("/api/share-summaries/stop")
    def stop_share_summaries():
        if enrichment is None or not hasattr(enrichment, "cancel_summaries"):
            return jsonify(cancelled=False)
        return jsonify(cancelled=bool(enrichment.cancel_summaries()))

    @app.get("/api/share/candidates")
    def share_candidates():
        mode = request.args.get("mode", "new").strip() or "new"
        try:
            return jsonify(database.get_share_candidates(
                mode, request.args.get("start", "").strip(), request.args.get("end", "").strip()
            ))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.post("/api/share/complete")
    def complete_share():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return jsonify(error="分享数据格式错误"), 400
        try:
            batch_id = database.complete_share(
                int(payload.get("through_run_id", 0)), payload["items"],
                str(payload.get("copied_text", "")),
                selection_mode=str(payload.get("selection_mode", "new")),
                date_start=str(payload.get("date_start", "")),
                date_end=str(payload.get("date_end", "")),
            )
        except (TypeError, ValueError, KeyError) as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(id=batch_id, status="completed")

    @app.get("/api/share-history")
    def share_history_api():
        return jsonify(database.share_history())

    @app.post("/api/search/answer")
    def search_answer():
        if search_service is None:
            return jsonify(error="搜索服务未配置"), 503
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not str(payload.get("question", "")).strip():
            return jsonify(error="问题不能为空"), 400
        question = str(payload["question"]).strip()
        year = payload.get("year")
        try:
            year = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            return jsonify(error="年份格式错误"), 400
        search_result = search_service.search(
            str(payload.get("query") or question), source_key=str(payload.get("source_key", "")),
            year=year, limit=20, use_ai=bool(payload.get("use_ai", True)),
        )
        return jsonify(search_service.answer(
            question, search_result["hits"], use_ai=bool(payload.get("use_ai", True))
        ))

    @app.get("/api/settings")
    def get_settings():
        return jsonify(config_view())

    @app.post("/api/settings")
    def save_settings():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="设置数据格式错误"), 400
        current = config_store.load()
        try:
            config = merged_config(payload, current)
            supplied_key = payload.get("api_key")
            supplied_embedding_key = payload.get("embedding_api_key")
            supplied_rerank_key = payload.get("rerank_api_key")
            supplied_summary_key = payload.get("summary_api_key")
            if config.embedding_enabled and not (
                config.embedding_url and config.embedding_model
                and (supplied_embedding_key or current.embedding_api_key)
            ):
                raise ValueError("Embedding 启用后必须独立填写 URL、Key 和模型")
            if config.rerank_enabled and not (
                config.rerank_url and config.rerank_model
                and (supplied_rerank_key or current.rerank_api_key)
            ):
                raise ValueError("Rerank 启用后必须独立填写 URL、Key 和模型")
            if config.summary_enabled and not (
                config.summary_url and config.summary_model
                and (supplied_summary_key or current.summary_api_key)
            ):
                raise ValueError("摘要模型启用后必须独立填写 URL、Key 和模型")
            config_store.save(
                config,
                new_api_key=str(supplied_key) if supplied_key else None,
                new_embedding_api_key=(
                    str(supplied_embedding_key) if supplied_embedding_key else None
                ),
                new_rerank_api_key=str(supplied_rerank_key) if supplied_rerank_key else None,
                new_summary_api_key=str(supplied_summary_key) if supplied_summary_key else None,
            )
        except (TypeError, ValueError, ConfigSecurityError) as exc:
            return jsonify(error=f"设置无效：{exc}"), 400
        return jsonify(status="saved", config=config_view())

    @app.post("/api/settings/<capability>")
    def save_capability(capability: str):
        if capability not in {"chat", "embedding", "rerank", "summary"}:
            return jsonify(error="未知能力"), 404
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="设置数据格式错误"), 400
        current = config_store.load()
        try:
            config = merged_config(payload, current, capability)
            _validate_capability(config, capability)
            config_store.save(
                config,
                new_api_key=(str(payload["api_key"]) if capability == "chat" and payload.get("api_key") else None),
                new_embedding_api_key=(
                    str(payload["embedding_api_key"])
                    if capability == "embedding" and payload.get("embedding_api_key") else None
                ),
                new_rerank_api_key=(
                    str(payload["rerank_api_key"])
                    if capability == "rerank" and payload.get("rerank_api_key") else None
                ),
                new_summary_api_key=(
                    str(payload["summary_api_key"])
                    if capability == "summary" and payload.get("summary_api_key") else None
                ),
            )
        except (TypeError, ValueError, ConfigSecurityError) as exc:
            return jsonify(error=f"设置无效：{exc}"), 400
        return jsonify(status="saved", capability=capability, config=config_view())

    @app.post("/api/settings/test/<capability>")
    def test_ai(capability: str):
        if capability not in {"chat", "embedding", "rerank", "summary"}:
            return jsonify(error="未知能力"), 404
        try:
            payload = request.get_json(silent=True)
            current = config_store.load()
            temporary = merged_config(payload if isinstance(payload, dict) else {}, current, capability)
            message = AIClient(temporary).test_connection(capability)
        except Exception as exc:
            return jsonify(error=str(exc)[:300]), 400
        return jsonify(status="ok", message=message)

    @app.post("/api/heartbeat")
    def receive_heartbeat():
        identity = client_id()
        if heartbeat is not None:
            heartbeat.heartbeat(identity)
        response = jsonify(status="ok", client_id=identity)
        response.set_cookie("client_id", identity, samesite="Strict", httponly=True)
        return response

    @app.post("/api/disconnect")
    def disconnect():
        if heartbeat is not None:
            heartbeat.disconnect(client_id())
        return jsonify(status="disconnected")

    @app.post("/api/exit")
    def exit_program():
        response = jsonify(status="shutting_down")
        if lifecycle is not None:
            # WSGI invokes this only after the response body has been handed
            # off, avoiding a race where the server closes before fetch reads it.
            response.call_on_close(lifecycle.request_exit)
        return response

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify(error="未找到"), 404
        return "未找到", 404

    @app.errorhandler(500)
    def internal_error(_error):
        if request.path.startswith("/api/"):
            return jsonify(error="内部错误"), 500
        return "内部错误", 500

    return app
