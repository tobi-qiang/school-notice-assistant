# -*- coding: utf-8 -*-
"""Frontend asset safety & interaction tests. 只检查模板和静态文件，不跑业务逻辑。"""

from pathlib import Path
import unittest

ROOT = Path(__file__).parent.parent / "app"
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"


def read_text(p):
    return p.read_text(encoding="utf-8")


class TestHtmlXss(unittest.TestCase):
    """HTML 模板中动态文本默认转义，正文 body_html 唯一允许 safe。"""

    def test_index_uses_escape(self):
        html = read_text(TEMPLATES / "index.html")
        self.assertIn('value="{{ filters.q|e }}"', html, "搜索框应使用 |e 转义")
        self.assertIn('value="{{ filters.year|e }}"', html, "年份输入应使用 |e 转义")
        self.assertIn('{{ (notice.short_title or notice.official_title)|e }}', html)
        self.assertIn("{{ notice.preview|e }}", html)
        self.assertIn("{{ fragment.text|e }}", html)
        self.assertIn('{% elif notice.snippet %}<p class="notice-preview">{{ notice.snippet|e }}</p>{% endif %}', html)
        self.assertNotIn("snippet|safe", html)
        self.assertNotIn("preview|safe", html)
        self.assertNotIn("official_title|safe", html)
        self.assertNotIn("short_title|safe", html)

    def test_detail_only_body_html_safe(self):
        html = read_text(TEMPLATES / "detail.html")
        self.assertIn("{{ notice.body_html|safe }}", html)
        lines = html.splitlines()
        for line in lines:
            if "|safe" in line and "body_html" not in line:
                if "{#" not in line:
                    self.fail(f"非 body_html 使用了 safe: {line.strip()}")

    def test_no_unescaped_dynamic_in_settings(self):
        html = read_text(TEMPLATES / "settings.html")
        self.assertIn('value="{{ config.base_url }}"', html)

    def test_source_custom_select_uses_short_labels_and_escaped_keys(self):
        html = read_text(TEMPLATES / "index.html")
        self.assertIn('id="sourceSelect"', html)
        self.assertIn('role="listbox"', html)
        self.assertIn('{% for src in sources %}', html)
        self.assertIn('data-source="{{ src.key|e }}"', html)
        self.assertIn('data-source=""', html, "全部 按钮应保留空 source")
        self.assertIn("未来学院·通知", html)
        self.assertIn("未来学院·奖助", html)
        self.assertIn("教务处·通知", html)
        self.assertNotIn('<select id="sourceFilter"', html, "不应退回原生下拉")

    def test_initial_batch_is_15_cards(self):
        html = read_text(TEMPLATES / "index.html")
        self.assertIn("notices[:15]", html, "首批应通过 Jinja 切片限制为 15 条")

    def test_sentinel_and_load_more_present(self):
        html = read_text(TEMPLATES / "index.html")
        self.assertIn('id="noticeSentinel"', html)
        self.assertIn('id="noticeLoadMore"', html)
        self.assertIn('id="noticeEmpty"', html)
        self.assertIn('id="noticeEnd"', html)

    def test_eyebrow_and_metrics_removed_from_index(self):
        html = read_text(TEMPLATES / "index.html")
        self.assertNotIn("仅保存在本机", html)
        self.assertNotIn('class="metrics"', html)
        self.assertNotIn("class=\"chips\"", html)


class TestJsXss(unittest.TestCase):
    """JS 必须用 DOM API/textContent 构造卡片与 mark，禁 innerHTML 注入动态数据。"""

    def test_uses_dom_api_for_dynamic_candidates(self):
        js = read_text(STATIC / "app.js")
        self.assertIn("document.createElement('label')", js)
        self.assertIn("content.textContent", js)
        self.assertIn("title.value", js)

    def test_share_candidates_avoid_innerhtml_xss(self):
        js = read_text(STATIC / "app.js")
        self.assertNotIn("candidates.innerHTML", js)
        self.assertIn("candidates.replaceChildren()", js)

    def test_no_direct_title_url_in_template_strings_to_dom(self):
        js = read_text(STATIC / "app.js")
        self.assertNotIn("escapeHtml", js)
        self.assertIn("textContent", js)

    def test_card_builder_uses_dom_api(self):
        js = read_text(STATIC / "app.js")
        self.assertIn("buildCard =", js)
        self.assertIn("createElement('mark')", js)
        self.assertIn("document.createTextNode", js)
        self.assertNotIn("innerHTML = `${", js, "card 不应使用模板字符串写 innerHTML")

    def test_notice_card_innerhtml_not_used(self):
        js = read_text(STATIC / "app.js")
        # 行内 innerHTML = 仅允许出现 sync-events 行（无动态数据）以外的检查：
        self.assertNotIn("card.innerHTML", js)
        self.assertNotIn("preview.innerHTML", js)

    def test_no_innerhtml_with_dynamic_value(self):
        js = read_text(STATIC / "app.js")
        # 不应存在 `${...}` 形式拼接动态数据写入 innerHTML 的模式
        for line in js.splitlines():
            if "innerHTML" in line and ("${" in line or "+" in line):
                self.fail(f"动态数据写入 innerHTML：{line.strip()}")


class TestIncrementalList(unittest.TestCase):
    """增量加载、AbortController、IntersectionObserver、history 同步。"""

    def test_calls_notices_endpoint(self):
        js = read_text(STATIC / "app.js")
        self.assertIn("/api/notices?", js)
        self.assertIn("INITIAL_PAGE_SIZE = 15", js)
        self.assertIn("NEXT_PAGE_SIZE = 20", js)

    def test_uses_abort_controller(self):
        js = read_text(STATIC / "app.js")
        self.assertIn("AbortController", js)
        self.assertIn("controller.abort()", js)
        self.assertIn("AbortError", js)
        self.assertIn("inflightSeq", js)

    def test_history_push_and_replace(self):
        js = read_text(STATIC / "app.js")
        self.assertIn("history.pushState", js)
        self.assertIn("history.replaceState", js)
        self.assertIn("popstate", js)

    def test_intersection_observer(self):
        js = read_text(STATIC / "app.js")
        self.assertIn("IntersectionObserver", js)
        self.assertIn("rootMargin", js)
        self.assertIn("600px", js)

    def test_idle_prefetch_without_dom_insert(self):
        js = read_text(STATIC / "app.js")
        self.assertIn("requestIdleCallback", js)
        self.assertIn("prefetch: true", js)
        self.assertIn("prefetched", js)

    def test_load_more_button_visible_on_error(self):
        js = read_text(STATIC / "app.js")
        self.assertIn("showLoadMore", js)
        self.assertIn("继续加载", js)
        self.assertIn("hideLoadMore", js)


class TestInteractionFeatures(unittest.TestCase):
    """复制、同步、停止、分享快照、设置保存、AI 三类测试、heartbeat 和 pagehide disconnect 可用。"""

    def test_copy_button_works(self):
        detail_html = read_text(TEMPLATES / "detail.html")
        self.assertIn("data-copy", detail_html)
        js = read_text(STATIC / "app.js")
        self.assertIn("navigator.clipboard.writeText", js)

    def test_sync_stop_buttons(self):
        js = read_text(STATIC / "app.js")
        self.assertIn("/api/sync", js)
        self.assertIn("/api/sync/stop", js)

    def test_live_status_poll_and_three_event_window(self):
        js = read_text(STATIC / "app.js")
        html = read_text(TEMPLATES / "index.html")
        self.assertIn("pollStatus()", js)
        self.assertIn("setTimeout(pollStatus", js)
        self.assertNotIn("s.finished", js)
        self.assertIn("events.slice(0, 3)", js)
        self.assertIn("本地服务已断开", js)
        self.assertIn('id="syncEvents"', html)

    def test_share_dialog_workflow(self):
        js = read_text(STATIC / "app.js")
        self.assertIn("/api/share/candidates", js)
        self.assertIn("/api/share/complete", js)

    def test_settings_save_and_tests(self):
        js = read_text(STATIC / "app.js")
        settings_html = read_text(TEMPLATES / "settings.html")
        has_settings = "/api/settings" in js
        self.assertTrue(has_settings, "应有/api/settings 接口调用")
        self.assertIn("data-test-ai", settings_html)
        self.assertIn("/api/settings/test/", js)

    def test_heartbeat_and_disconnect(self):
        js = read_text(STATIC / "app.js")
        self.assertIn("/api/heartbeat", js)
        self.assertIn("pagehide", js)
        self.assertIn("sendBeacon", js)

    def test_custom_source_select_accessibility_and_immediate_filter(self):
        js = read_text(STATIC / "app.js")
        css = read_text(STATIC / "style.css")
        self.assertIn("setSourceMenuOpen", js)
        self.assertIn("selectSource", js)
        self.assertIn("ArrowDown", js)
        self.assertIn("Escape", js)
        self.assertIn("pointerdown", js)
        self.assertIn("applyFilters", js)
        self.assertIn(".notice-search-panel", css)
        self.assertIn("z-index: 20", css)
        self.assertIn("overflow: visible", css)

    def test_single_share_opens_before_request_and_shows_progress(self):
        js = read_text(STATIC / "app.js")
        detail = read_text(TEMPLATES / "detail.html")
        self.assertIn("正在生成简短事项摘要", js)
        self.assertLess(js.index("singleShareDialog.showModal()"), js.index("/share-draft"))
        self.assertIn('id="singleShareStatus"', detail)
        self.assertIn('id="copySingleShare" class="primary" disabled', detail)

    def test_optional_summary_model_and_background_progress_controls(self):
        js = read_text(STATIC / "app.js")
        settings = read_text(TEMPLATES / "settings.html")
        index = read_text(TEMPLATES / "index.html")
        self.assertIn('data-capability="summary"', settings)
        self.assertIn('name="summary_api_key"', settings)
        self.assertIn('id="summaryButton"', index)
        self.assertIn('id="summaryPanel"', index)
        self.assertIn("/api/share-summaries/start", js)
        self.assertIn("status.summary_job?.running", js)
        self.assertIn("item.share_summary || item.short_title || item.official_title", js)
        self.assertNotIn("`【${", js)

    def test_exit_stops_polling_and_has_browser_close_fallback(self):
        js = read_text(STATIC / "app.js")
        html = read_text(TEMPLATES / "base.html")
        self.assertIn("requestProgramExit", js)
        self.assertIn("navigator.sendBeacon('/api/exit'", js)
        self.assertIn("clearTimeout(syncPollTimer)", js)
        self.assertIn("clearInterval(heartbeatTimer)", js)
        self.assertIn("window.close()", js)
        self.assertNotIn("toast(error.message));\n      window.close()", js)
        self.assertIn('id="exitScreen"', html)
        self.assertIn('id="closePageAgain"', html)


class TestSyncPanelStates(unittest.TestCase):
    """同步面板：非 running 时隐藏来源/事件列表；事件行第二行来源左、时间右。"""

    def test_hide_sources_and_events_when_not_running(self):
        css = read_text(STATIC / "sync-status.css")
        self.assertIn('.status-panel:not([data-state="running"]) .sync-sources', css)
        self.assertIn('.status-panel:not([data-state="running"]) .sync-events', css)
        js = read_text(STATIC / "app.js")
        self.assertIn("setSyncRunning", js)
        self.assertIn("syncPanel?.dataset.state !== 'running'", js)

    def test_event_second_row_layout_source_left_time_right(self):
        css = read_text(STATIC / "sync-status.css")
        self.assertIn(".event-source", css)
        self.assertIn("justify-content: space-between", css)
        self.assertIn(".event-source-name", css)
        self.assertIn(".event-source-time", css)
        js = read_text(STATIC / "app.js")
        self.assertIn("event-source-name", js)
        self.assertIn("event-source-time", js)
        self.assertIn("formatClock", js)


class TestMobileUsability(unittest.TestCase):
    """移动端 CSS 布局可用性（简单检查 viewport 和响应式）。"""

    def test_viewport_meta(self):
        html = read_text(TEMPLATES / "base.html")
        self.assertIn('name="viewport"', html)

    def test_mobile_media_query(self):
        css = read_text(STATIC / "style.css")
        self.assertIn("@media (max-width: 760px)", css)

    def test_prefers_reduced_motion(self):
        css = read_text(STATIC / "style.css")
        self.assertIn("prefers-reduced-motion: reduce", css)
        css2 = read_text(STATIC / "sync-status.css")
        self.assertIn("prefers-reduced-motion: reduce", css2)


class TestButtonErrorRecovery(unittest.TestCase):
    """按钮错误恢复：toast 提示在失败时显示。"""

    def test_error_toast(self):
        js = read_text(STATIC / "app.js")
        self.assertIn('el.textContent =', js)
        self.assertIn("toast(error.message)", js)


if __name__ == "__main__":
    unittest.main()
