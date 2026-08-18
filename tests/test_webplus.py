"""Tests for app.webplus module."""

from __future__ import annotations

import os
import time
import unittest
from datetime import date
from unittest.mock import MagicMock, patch
from urllib.parse import urljoin

import requests

from app.models import ListEntry, NoticeDocument, SourceConfig
from app.webplus import (
    CancelledError,
    AuthenticationRequired,
    FetchError,
    ParseError,
    WebPlusClient,
    build_page_url,
    extract_article_id,
    parse_detail,
    parse_list,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _sample_source() -> SourceConfig:
    return SourceConfig(
        key="test_src",
        name="Test School",
        base_url="http://example.edu",
        list_path="xwb/list.htm",
        category="notices",
        history_start=date(2024, 6, 1),
        site_key="",
    )


def _sample_entry() -> ListEntry:
    return ListEntry(
        source_key="test_src",
        article_id="9001",
        title="Test Notice",
        url="http://example.edu/2025/03/15/art_1001_9001.htm",
        published_date=date(2025, 3, 15),
        list_position=1,
    )


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

class TestBuildPageUrl(unittest.TestCase):

    def test_page_1_returns_base(self):
        src = _sample_source()
        url = build_page_url(src, 1)
        self.assertEqual(url, "http://example.edu/xwb/list.htm")

    def test_page_2_replaces_list_htm(self):
        src = _sample_source()
        url = build_page_url(src, 2)
        self.assertEqual(url, "http://example.edu/xwb/list2.htm")

    def test_page_3(self):
        src = _sample_source()
        url = build_page_url(src, 3)
        self.assertEqual(url, "http://example.edu/xwb/list3.htm")


class TestExtractArticleId(unittest.TestCase):

    def test_current_webplus_column_article_pattern(self):
        self.assertEqual(
            extract_article_id("https://future.suda.edu.cn/d0/56/c30836a708694/page.htm"),
            "708694",
        )

    def test_art_id_pattern(self):
        self.assertEqual(
            extract_article_id("/2025/03/15/art_1001_9001.htm"), "1001"
        )

    def test_art_id_art_slash_pattern(self):
        self.assertEqual(
            extract_article_id("/art/2025/3/15/art_1001_detail.htm"), "1001"
        )

    def test_page_htm_with_id(self):
        self.assertEqual(
            extract_article_id("/page.htm?id=9999"), "9999"
        )

    def test_full_url(self):
        self.assertEqual(
            extract_article_id("http://example.edu/2024/10/01/art_1006_9006.htm"),
            "1006",
        )

    def test_list_htm_raises(self):
        with self.assertRaises(ValueError):
            extract_article_id("/xwb/list.htm")

    def test_unknown_pattern_raises(self):
        with self.assertRaises(ValueError):
            extract_article_id("/random/notice.html")


# ---------------------------------------------------------------------------
# List parsing
# ---------------------------------------------------------------------------

class TestParseList(unittest.TestCase):

    def test_parse_real_fixture(self):
        src = _sample_source()
        path = os.path.join(FIXTURES, "list.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        entries = parse_list(html, src)
        self.assertEqual(len(entries), 6)
        # Check first entry
        self.assertEqual(entries[0].title, "关于2025年春季开学安排的通知")
        self.assertEqual(entries[0].article_id, "1001")
        self.assertEqual(entries[0].published_date, date(2025, 3, 15))
        self.assertEqual(entries[0].list_position, 1)

    def test_absolute_url_preserved(self):
        src = _sample_source()
        path = os.path.join(FIXTURES, "list.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        entries = parse_list(html, src)
        # The last entry with absolute URL should keep it
        abs_entry = next(e for e in entries if e.article_id == "1005")
        self.assertTrue(abs_entry.url.startswith("http://"))

    def test_slash_date_format(self):
        src = _sample_source()
        path = os.path.join(FIXTURES, "list.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        entries = parse_list(html, src)
        # Entry with YYYY/MM/DD format
        entry = next(e for e in entries if e.article_id == "1004")
        self.assertEqual(entry.published_date, date(2024, 12, 20))

    def test_page_htm_entry(self):
        src = _sample_source()
        path = os.path.join(FIXTURES, "list.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        entries = parse_list(html, src)
        entry = next(e for e in entries if e.article_id == "1003")
        self.assertEqual(entry.title, "期末考试安排调整通知")

    def test_duplicate_article_id_skipped(self):
        src = _sample_source()
        html = '''<html><body>
        <ul>
        <li><span class="date">2025-01-01</span><a href="/art_001_100.htm">First</a></li>
        <li><span class="date">2025-01-02</span><a href="/art_001_100.htm">Duplicate</a></li>
        <li><span class="date">2025-01-03</span><a href="/art_002_200.htm">Second</a></li>
        </ul>
        </body></html>'''
        entries = parse_list(html, src)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].article_id, "001")
        self.assertEqual(entries[1].article_id, "002")
        self.assertEqual(entries[0].title, "First")

    def test_empty_list(self):
        src = _sample_source()
        html = "<html><body><p>No notices</p></body></html>"
        entries = parse_list(html, src)
        self.assertEqual(entries, [])


# ---------------------------------------------------------------------------
# Detail parsing
# ---------------------------------------------------------------------------

class TestParseDetail(unittest.TestCase):

    def test_parse_real_fixture(self):
        src = _sample_source()
        entry = _sample_entry()
        path = os.path.join(FIXTURES, "detail.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        doc = parse_detail(html, src, entry)

        self.assertIsInstance(doc, NoticeDocument)
        self.assertEqual(doc.source_key, "test_src")
        self.assertEqual(doc.article_id, "9001")
        self.assertTrue("关于2025年春季开学安排" in doc.official_title)
        self.assertEqual(doc.published_date, date(2025, 3, 15))

    def test_sanitisation_removes_script(self):
        src = _sample_source()
        entry = _sample_entry()
        html = '''<html><body>
        <div class="wp_articlecontent">
        <p>Hello</p>
        <script>alert(1)</script>
        <p>World</p>
        </div>
        </body></html>'''
        doc = parse_detail(html, src, entry)
        self.assertNotIn("<script", doc.body_html)

    def test_sanitisation_removes_iframe(self):
        src = _sample_source()
        entry = _sample_entry()
        html = '''<html><body>
        <div class="wp_articlecontent">
        <p>Content</p>
        <iframe src="http://evil.com"></iframe>
        </div>
        </body></html>'''
        doc = parse_detail(html, src, entry)
        self.assertNotIn("<iframe", doc.body_html)

    def test_sanitisation_removes_form(self):
        src = _sample_source()
        entry = _sample_entry()
        html = '''<html><body>
        <div class="wp_articlecontent">
        <form action="http://evil.com"><input name="x"></form>
        <p>Safe</p>
        </div>
        </body></html>'''
        doc = parse_detail(html, src, entry)
        self.assertNotIn("<form", doc.body_html)

    def test_sanitisation_removes_onclick(self):
        src = _sample_source()
        entry = _sample_entry()
        html = '''<html><body>
        <div class="wp_articlecontent">
        <a href="http://example.com" onclick="steal()">Click</a>
        </div>
        </body></html>'''
        doc = parse_detail(html, src, entry)
        self.assertNotIn("onclick", doc.body_html)

    def test_javascript_url_removed(self):
        src = _sample_source()
        entry = _sample_entry()
        html = '''<html><body>
        <div class="wp_articlecontent">
        <a href="javascript:alert(1)">Bad</a>
        <a href="http://ok.com">Good</a>
        </div>
        </body></html>'''
        doc = parse_detail(html, src, entry)
        self.assertNotIn("javascript:", doc.body_html)
        self.assertIn("http://ok.com", doc.body_html)

    def test_rel_noopener_on_links(self):
        src = _sample_source()
        entry = _sample_entry()
        html = '''<html><body>
        <div class="wp_articlecontent">
        <a href="http://example.com/page">Link</a>
        </div>
        </body></html>'''
        doc = parse_detail(html, src, entry)
        self.assertIn("noopener", doc.body_html)

    def test_image_src_absolutized(self):
        src = _sample_source()
        entry = _sample_entry()
        html = '''<html><body>
        <div class="wp_articlecontent">
        <img src="/images/logo.png">
        </div>
        </body></html>'''
        doc = parse_detail(html, src, entry)
        self.assertIn("http://example.edu/images/logo.png", doc.body_html)

    def test_allowed_tags_preserved(self):
        src = _sample_source()
        entry = _sample_entry()
        html = '''<html><body>
        <div class="wp_articlecontent">
        <h1>Title</h1>
        <p>Text with <strong>bold</strong> and <em>italic</em></p>
        <ul><li>Item</li></ul>
        <table><tr><td>Cell</td></tr></table>
        </div>
        </body></html>'''
        doc = parse_detail(html, src, entry)
        self.assertIn("<h1>", doc.body_html)
        self.assertIn("<strong>", doc.body_html)
        self.assertIn("<table>", doc.body_html)

    def test_disallowed_tags_removed(self):
        src = _sample_source()
        entry = _sample_entry()
        html = '''<html><body>
        <div class="wp_articlecontent">
        <p>Hello</p>
        <div class="ad">Ad content</div>
        <font color="red">Red</font>
        </div>
        </body></html>'''
        doc = parse_detail(html, src, entry)
        self.assertNotIn("<font", doc.body_html)

    def test_attachment_extraction(self):
        src = _sample_source()
        entry = _sample_entry()
        path = os.path.join(FIXTURES, "detail.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        doc = parse_detail(html, src, entry)
        self.assertIn("2025年春季校历.pdf", doc.attachment_names)
        self.assertIn("新生指南.docx", doc.attachment_names)

    def test_attachment_names_only_no_download(self):
        """Attachment names are stored but no actual download occurs."""
        src = _sample_source()
        entry = _sample_entry()
        html = '''<html><body>
        <div class="wp_articlecontent">
        <p><a href="/files/report.xlsx">Report</a></p>
        <p><a href="/files/data.zip">Data</a></p>
        </div>
        </body></html>'''
        doc = parse_detail(html, src, entry)
        self.assertEqual(doc.attachment_names, ("Report", "Data"))

    def test_no_content_raises_parse_error(self):
        src = _sample_source()
        entry = _sample_entry()
        # Truly empty body should raise ParseError
        html = "<html><body></body></html>"
        with self.assertRaises(ParseError):
            parse_detail(html, src, entry)

    def test_body_text_reasonable_newlines(self):
        src = _sample_source()
        entry = _sample_entry()
        html = '''<html><body>
        <div class="wp_articlecontent">
        <p>Paragraph one</p>
        <p>Paragraph two</p>
        </div>
        </body></html>'''
        doc = parse_detail(html, src, entry)
        self.assertIn("Paragraph one", doc.body_text)
        self.assertIn("Paragraph two", doc.body_text)

    def test_published_date_not_overridden(self):
        """Detail page date conflict should not change entry published_date."""
        src = _sample_source()
        entry = _sample_entry()
        html = '''<html><body>
        <div class="wp_articlecontent">
        <p>Published: 2024-01-01</p>
        <p>Content</p>
        </div>
        </body></html>'''
        doc = parse_detail(html, src, entry)
        self.assertEqual(doc.published_date, date(2025, 3, 15))


# ---------------------------------------------------------------------------
# Fake session / transport tests
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, text="", status_code=200, encoding="utf-8", *, content=None, headers=None, url=None):
        self.text = text
        self.content = content if content is not None else text.encode(encoding or "utf-8")
        self.headers = headers or {}
        self.status_code = status_code
        self.encoding = encoding
        self.apparent_encoding = "gbk"
        self.url = url or "http://example.edu/final"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class TestWebPlusClientFakeTransport(unittest.TestCase):

    def _make_client(self, session=None, **kwargs):
        return WebPlusClient(session=session, **kwargs)

    def test_fetch_list_with_fake_session(self):
        mock_session = MagicMock()
        mock_session.get.return_value = FakeResponse(
            text=open(os.path.join(FIXTURES, "list.html"), encoding="utf-8").read(),
        )
        mock_session.headers = {}

        client = self._make_client(session=mock_session)
        src = _sample_source()
        entries = client.fetch_list(src, 1)
        self.assertEqual(len(entries), 6)

    def test_fetch_detail_with_fake_session(self):
        mock_session = MagicMock()
        mock_session.get.return_value = FakeResponse(
            text=open(os.path.join(FIXTURES, "detail.html"), encoding="utf-8").read(),
        )
        mock_session.headers = {}

        client = self._make_client(session=mock_session)
        src = _sample_source()
        entry = _sample_entry()
        doc = client.fetch_detail(src, entry)
        self.assertIsInstance(doc, NoticeDocument)
        self.assertTrue(len(doc.attachment_names) > 0)

    def test_network_error_wrapped(self):
        mock_session = MagicMock()
        mock_session.get.side_effect = requests.ConnectionError("timeout")
        mock_session.headers = {}

        client = self._make_client(session=mock_session)
        src = _sample_source()
        with self.assertRaises(FetchError) as cm:
            client.fetch_list(src, 1)
        self.assertIn("timeout", str(cm.exception))
        self.assertIsNone(cm.exception.status_code)

    def test_http_error_wrapped(self):
        mock_session = MagicMock()
        mock_session.get.return_value = FakeResponse(status_code=404)
        mock_session.headers = {}

        client = self._make_client(session=mock_session)
        src = _sample_source()
        with self.assertRaises(FetchError) as cm:
            client.fetch_list(src, 1)
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(cm.exception.url, "http://example.edu/final")

    def test_cancel_before_request(self):
        cancel_event = MagicMock()
        cancel_event.is_set.return_value = True

        client = self._make_client(cancel_event=cancel_event)
        with self.assertRaises(CancelledError):
            client.get_text("http://example.edu/test.htm")

    def test_callable_cancel_event(self):
        cancelled = [False]

        def check():
            return cancelled[0]

        mock_session = MagicMock()
        mock_session.headers = {}

        client = self._make_client(
            session=mock_session, cancel_event=check
        )
        cancelled[0] = True
        with self.assertRaises(CancelledError):
            client.get_text("http://example.edu/test.htm")

    def test_custom_sleeper_and_timeout_params(self):
        """Test that timeout parameters and custom sleeper are used."""
        mock_session = MagicMock()
        mock_session.get.return_value = FakeResponse(text="<html><body>OK</body></html>")
        mock_session.headers = {}
        sleep_calls = []

        def fake_sleeper(seconds):
            sleep_calls.append(seconds)

        client = WebPlusClient(
            session=mock_session,
            connect_timeout=2,
            read_timeout=5,
            interval_seconds=0.1,
            sleeper=fake_sleeper,
        )
        # First call should not sleep
        client.get_text("http://example.edu/test.htm")
        self.assertEqual(len(sleep_calls), 0)
        # Second call should sleep due to interval
        client.get_text("http://example.edu/test2.htm")
        self.assertTrue(len(sleep_calls) > 0)
        self.assertTrue(sleep_calls[0] <= 0.1)

        # Verify timeout tuple passed to requests
        call_kwargs = mock_session.get.call_args_list[0][1]
        self.assertEqual(call_kwargs["timeout"], (2, 5))

    def test_user_agent_set(self):
        mock_session = MagicMock()
        mock_session.get.return_value = FakeResponse(text="OK")
        mock_session.headers = {}

        client = self._make_client(session=mock_session)
        self.assertIn("User-Agent", mock_session.headers)
        self.assertIn("Mozilla", mock_session.headers["User-Agent"])

    def test_encoding_fallback(self):
        mock_session = MagicMock()
        resp = FakeResponse(text="Hello", encoding=None)
        resp.apparent_encoding = "utf-8"
        mock_session.get.return_value = resp
        mock_session.headers = {}

        client = self._make_client(session=mock_session)
        text = client.get_text("http://example.edu/test.htm")
        self.assertEqual(text, "OK" if resp.text == "OK" else resp.text)

    def test_missing_charset_prefers_strict_utf8_over_http_latin1_default(self):
        mock_session = MagicMock()
        raw = "未来学院通知".encode("utf-8")
        mock_session.get.return_value = FakeResponse(
            encoding="ISO-8859-1", content=raw, headers={"Content-Type": "text/html"}
        )
        mock_session.headers = {}
        client = self._make_client(session=mock_session)
        self.assertEqual(client.get_text("http://example.edu/test.htm"), "未来学院通知")

    def test_explicit_charset_is_honoured(self):
        mock_session = MagicMock()
        raw = "中文公告".encode("gb18030")
        mock_session.get.return_value = FakeResponse(
            content=raw, headers={"Content-Type": "text/html; charset=gb18030"}
        )
        mock_session.headers = {}
        client = self._make_client(session=mock_session)
        self.assertEqual(client.get_text("http://example.edu/test.htm"), "中文公告")

    def test_cas_redirect_raises_authentication_required(self):
        mock_session = MagicMock()
        mock_session.get.return_value = FakeResponse(
            text='<html><body><div id="app"></div></body></html>',
            url="https://auth.suda.edu.cn/cas/login?service=jwc",
        )
        mock_session.headers = {}
        client = self._make_client(session=mock_session)
        with self.assertRaises(AuthenticationRequired):
            client.get_text("https://jwc.suda.edu.cn/example/page.htm")


class TestRealWebPlusRegressions(unittest.TestCase):
    def test_list_title_does_not_include_nested_date(self):
        source = _sample_source()
        html = '<li><a href="/art_123_456.htm">真正标题 <span class="date">2025-07-02</span></a></li>'
        entries = parse_list(html, source)
        self.assertEqual(entries[0].title, "真正标题")

    def test_empty_content_container_is_preserved_as_metadata_only(self):
        document = parse_detail(
            '<html><body><div class="wp_articlecontent"><p>&nbsp;</p></div></body></html>',
            _sample_source(), _sample_entry(),
        )
        self.assertEqual(document.body_text, "")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()




