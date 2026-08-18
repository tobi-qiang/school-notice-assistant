"""WebPlus generic crawler for university notice list/detail pages."""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from html import escape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from app.models import ListEntry, NoticeDocument, SourceConfig

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class CancelledError(Exception):
    """Raised when the cancel event is set between requests."""

class ParseError(Exception):
    """Raised when the HTML structure cannot be parsed."""

class FetchError(Exception):
    """Raised when a network request fails."""

    def __init__(self, message: str, *, status_code: int | None = None, url: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class AuthenticationRequired(FetchError):
    """Raised when a school detail request is redirected to unified login."""


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")

def _parse_date(text: str) -> date | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def build_page_url(source: SourceConfig, page: int) -> str:
    """Build the list-page URL.

    Page 1 uses ``base_url + list_path`` unchanged.
    Subsequent pages replace the final ``list.htm`` with ``listN.htm``.
    """
    base = source.base_url.rstrip("/") + "/" + source.list_path.lstrip("/")
    if page <= 1:
        return base
    # Replace list.htm -> listN.htm (handles both list.htm and list_1.htm variants)
    # We look for the final listX.htm pattern
    m = re.search(r"(list)(?:_(\d+))?(\.htm)$", base)
    if m:
        return base[: m.start()] + f"list{page}.htm"
    # Fallback: insert page number before .htm
    if base.endswith(".htm"):
        return base[:-4] + str(page) + ".htm"
    return base + f"?page={page}"


def extract_article_id(url: str) -> str:
    """Extract article id from common university URL patterns.

    Supported patterns:
      - /xx/list.htm is NOT an article URL (raise ValueError)
      - /page.htm?id=XXX -> XXX
      - /art/YYYY/M/D/art_ID_....htm -> ID
      - /YYYY/MM/DD/art_ID_XXXX.htm -> XXXX
      - /xxxx/xxxx/art_XXXX_YYYY.htm -> XXXX or YYYY (prefer the numeric portion after art_)
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    # Current WebPlus URLs look like /ab/cd/c30836a708694/page.htm.
    # The number after ``a`` is the article id; ``c30836`` is a column id.
    m = re.search(r"/c\d+a(\d+)/page\.htm$", path, re.I)
    if m:
        return m.group(1)

    # /page.htm?id=XXX
    if path.endswith("/page.htm") or path == "page.htm":
        qs = parsed.query
        m = re.search(r"(?:^|&)id=([^&]+)", qs)
        if m:
            return m.group(1)
        raise ValueError(f"page.htm URL missing id parameter: {url}")

    # /art/YYYY/M/D/art_ID_....htm pattern
    m = re.search(r"/art/[^/]+/art_([^_/]+)", path)
    if m:
        return m.group(1)

    # Generic: find art_ID or art_ID_NUM patterns
    m = re.search(r"art_(\d+)(?:_\d+)?", path)
    if m:
        return m.group(1)

    raise ValueError(f"Cannot extract article id from URL: {url}")


# ---------------------------------------------------------------------------
# List parsing
# ---------------------------------------------------------------------------

def parse_list(html: str, source: SourceConfig) -> list[ListEntry]:
    """Parse a list page and return ListEntry objects.

    Tolerates ul/li, table, and other common structures.
    """
    soup = BeautifulSoup(html, "html.parser")
    entries: list[ListEntry] = []
    seen_ids: set[str] = set()
    position = 0

    # Try multiple strategies
    link_date_pairs = _extract_link_date_pairs(soup)

    for link_tag, date_text in link_date_pairs:
        href = link_tag.get("href")
        if not href:
            continue
        url = urljoin(source.base_url, href)
        pub_date = _parse_date(date_text) if date_text else None
        if pub_date is None:
            continue

        title = (link_tag.get("title") or link_tag.get_text(" ", strip=True) or "").strip()
        title = re.sub(r"\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*$", "", title).strip()
        title = re.sub(r"\s+", " ", title)
        if not title:
            # try title attribute
            title = (link_tag.get("title") or "").strip()
        if not title:
            continue

        try:
            aid = extract_article_id(url)
        except ValueError:
            continue

        if aid in seen_ids:
            continue
        seen_ids.add(aid)

        position += 1
        entries.append(ListEntry(
            source_key=source.key,
            article_id=aid,
            title=title,
            url=url,
            published_date=pub_date,
            list_position=position,
        ))

    return entries


def _extract_link_date_pairs(soup: BeautifulSoup) -> list[tuple[Tag, str | None]]:
    """Find (link_tag, date_string) pairs from common list page layouts.

    Strategy 1: li containing <a> and <span class="date">
    Strategy 2: table rows with <a> and a date cell
    Strategy 3: fallback – any <a> near a date pattern
    """
    pairs: list[tuple[Tag, str | None]] = []

    # Strategy 1: ul/li with span.date
    for li in soup.find_all("li"):
        a = li.find("a", href=True)
        if not a:
            continue
        date_span = li.find("span", class_=re.compile(r"date|time", re.I))
        date_text = date_span.get_text(strip=True) if date_span else None
        if date_text and _DATE_RE.search(date_text):
            pairs.append((a, date_text))
            continue
        # date might be inside the <a> itself or sibling text
        date_text = _find_date_near(a)
        if date_text:
            pairs.append((a, date_text))

    # Strategy 2: table rows
    if not pairs:
        for tr in soup.find_all("tr"):
            cells = tr.find_all("td")
            a = tr.find("a", href=True)
            if not a:
                continue
            # look for date in other cells
            date_text = None
            for cell in cells:
                t = cell.get_text(strip=True)
                if _DATE_RE.search(t):
                    date_text = t
                    break
            if date_text:
                pairs.append((a, date_text))
            elif a:
                date_text = _find_date_near(a)
                if date_text:
                    pairs.append((a, date_text))

    # Strategy 3: any link with nearby date text
    if not pairs:
        for a in soup.find_all("a", href=True):
            date_text = _find_date_near(a)
            if date_text:
                pairs.append((a, date_text))

    return pairs


def _find_date_near(tag: Tag) -> str | None:
    """Look for a date in the parent element text around a tag."""
    parent = tag.parent
    if not parent:
        return None
    text = parent.get_text()
    m = _DATE_RE.search(text)
    if m:
        # Try to get just the span or text near the date
        for span in parent.find_all("span", class_=re.compile(r"date|time", re.I)):
            t = span.get_text(strip=True)
            if _DATE_RE.search(t):
                return t
        # fallback to the matched date string from full text
        return m.group(0)
    return None


# ---------------------------------------------------------------------------
# Detail parsing / sanitisation
# ---------------------------------------------------------------------------

# Allowed tags in safe HTML
_ALLOWED_TAGS = frozenset({
    "p", "br", "strong", "em", "b", "i", "u",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td",
    "a", "img",
    "h1", "h2", "h3", "h4", "h5", "h6", "span",
})

_ATTACHMENT_EXT_RE = re.compile(r"\.(pdf|doc|docx|xls|xlsx|zip|rar)$", re.I)


def parse_detail(html: str, source: SourceConfig, entry: ListEntry) -> NoticeDocument:
    """Parse a detail page and return a NoticeDocument.

    Locates common content containers, strips scripts/styles/forms/iframes,
    produces safe HTML and plain text, and extracts attachment names.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Find content container
    body = _find_content_body(soup)
    if body is None:
        raise ParseError("Cannot locate article body in detail page")

    # Remove dangerous elements
    for tag_name in ("script", "style", "iframe", "form", "noscript"):
        for t in body.find_all(tag_name):
            t.decompose()

    # Remove event handler attributes and javascript: URLs
    for t in body.find_all(True):
        attrs_to_remove = [name for name in t.attrs if name.lower().startswith("on")]
        for name in attrs_to_remove:
            del t[name]
        # Handle javascript: URLs
        if t.name == "a" and t.get("href"):
            href = t["href"].strip()
            if href.lower().startswith("javascript:"):
                t.decompose()

    # Title extraction
    title = _extract_title(body) or entry.title

    # Attachment extraction (before sanitising, to see all links)
    attachment_names = _extract_attachments(body, source.base_url)

    # Build safe HTML
    safe_html = _build_safe_html(body, source.base_url)

    # Build plain text
    body_text = body.get_text("\n", strip=True)
    # Collapse excessive blank lines but preserve reasonable structure
    body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
    return NoticeDocument(
        source_key=source.key,
        article_id=entry.article_id,
        official_title=title,
        original_url=entry.url,
        published_date=entry.published_date,
        body_text=body_text,
        body_html=safe_html,
        attachment_names=tuple(attachment_names),
        site_key=source.site_key,
        categories=(source.category,) if source.category else (),
    )


def _find_content_body(soup: BeautifulSoup) -> Tag | None:
    """Find the main content container."""
    selectors = [
        {"class_": re.compile(r"wp_articlecontent", re.I)},
        {"class_": re.compile(r"article[_-]?content", re.I)},
        {"class_": re.compile(r"^content$", re.I)},
        {"id": "content"},
        {"id": re.compile(r"^article")},
        {"class_": re.compile(r"article[_-]?body", re.I)},
        {"class_": re.compile(r"wp[_-]content", re.I)},
        {"class_": re.compile(r"news[_-]?content", re.I)},
    ]
    for sel in selectors:
        el = soup.find(True, **sel)
        if el:
            return el

    # Fallback: look for the largest div or main content area
    divs = soup.find_all(["div", "main", "article"])
    if divs:
        # Pick the one with the most text
        return max(divs, key=lambda d: len(d.get_text()))
    return None


def _extract_title(body: Tag) -> str | None:
    """Extract the article title from the content body."""
    # Look for h1, h2, or h3 at the start
    for tag_name in ("h1", "h2", "h3"):
        el = body.find(tag_name)
        if el:
            t = el.get_text(strip=True)
            if t:
                return t
    return None


def _extract_attachments(body: Tag, base_url: str) -> list[str]:
    """Extract attachment file names from links in the body.

    Recognises common office/document extensions and links near '附件' text.
    Only stores the display name; does not download.
    """
    names: list[str] = []
    seen: set[str] = set()

    for a in body.find_all("a", href=True):
        href = a.get("href", "")
        if not href:
            continue
        full_url = urljoin(base_url, href)
        if _ATTACHMENT_EXT_RE.search(full_url):
            # Use link text as name, fall back to filename
            name = a.get_text(strip=True)
            if not name:
                name = full_url.rsplit("/", 1)[-1]
            if name and name not in seen:
                seen.add(name)
                names.append(name)

    # Also check for "附件" context
    for el in body.find_all(string=True):
        if "附件" in el:
            parent = el.parent
            if parent:
                for a in parent.find_next_siblings("a", href=True)[:5]:
                    href = a.get("href", "")
                    if not href:
                        continue
                    full_url = urljoin(base_url, href)
                    if _ATTACHMENT_EXT_RE.search(full_url):
                        name = a.get_text(strip=True)
                        if not name:
                            name = full_url.rsplit("/", 1)[-1]
                        if name and name not in seen:
                            seen.add(name)
                            names.append(name)

    return names


def _build_safe_html(body: Tag, base_url: str) -> str:
    """Build sanitised HTML allowing only a whitelist of tags.

    Converts all href and src to absolute URLs.
    """
    # Make all links absolute
    for a in body.find_all("a", href=True):
        a["href"] = urljoin(base_url, a["href"])
        if "rel" not in a.attrs:
            a["rel"] = ["noopener"]
        else:
            if "noopener" not in a["rel"]:
                a["rel"].append("noopener")

    for img in body.find_all("img", src=True):
        img["src"] = urljoin(base_url, img["src"])

    # Remove all tags not in whitelist, keeping their text content
    _remove_unsafe_tags(body)

    return str(body)


def _remove_unsafe_tags(root: Tag) -> None:
    """Recursively remove tags not in the whitelist, preserving text."""
    for child in list(root.children):
        if isinstance(child, Tag):
            if child.name not in _ALLOWED_TAGS:
                # Unwrap: move children up
                child.unwrap()
            else:
                _remove_unsafe_tags(child)


def _decode_response(response: requests.Response) -> str:
    """Decode HTML without accepting requests' ISO-8859-1 HTTP default."""
    raw = getattr(response, "content", None)
    if raw is None:
        return str(getattr(response, "text", ""))
    if isinstance(raw, str):
        return raw

    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("Content-Type", ""))
    match = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", content_type, re.I)
    explicit = match.group(1).strip() if match else ""
    if explicit:
        try:
            return raw.decode(explicit)
        except (LookupError, UnicodeDecodeError):
            pass

    # Both configured school sites serve UTF-8 HTML but omit charset in the
    # HTTP header. Requests otherwise assigns ISO-8859-1 per the HTTP default.
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass

    candidates = (
        getattr(response, "apparent_encoding", None),
        getattr(response, "encoding", None),
        "utf-8",
    )
    for encoding in candidates:
        if not encoding or str(encoding).lower() in {"iso-8859-1", "latin-1"}:
            continue
        try:
            return raw.decode(str(encoding))
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _is_authentication_page(final_url: str, text: str) -> bool:
    host = (urlparse(final_url).hostname or "").lower()
    if host == "auth.suda.edu.cn" or host.endswith(".auth.suda.edu.cn"):
        return True
    lowered = text.casefold()
    if "auth.suda.edu.cn/cas/login" in lowered:
        return True
    soup = BeautifulSoup(text, "html.parser")
    app = soup.find(id="app")
    visible = soup.get_text(" ", strip=True)
    return bool(app is not None and not visible and ("cas" in lowered or "login" in lowered))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class WebPlusClient:
    """HTTP client with rate limiting, cancellation, and parsing for WebPlus pages."""

    def __init__(
        self,
        session: requests.Session | None = None,
        connect_timeout: float = 4,
        read_timeout: float = 12,
        interval_seconds: float = 0.8,
        cancel_event: object | None = None,
        sleeper=time.sleep,
    ):
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._interval = interval_seconds
        self._cancel_event = cancel_event
        self._sleeper = sleeper
        self._last_request_time: float = 0.0

    # -- low-level --

    def get_text(self, url: str) -> str:
        """Fetch URL and return text.  Handles encoding detection."""
        if self._cancel_event is not None:
            if callable(self._cancel_event):
                if self._cancel_event():
                    raise CancelledError("cancelled before request")
            elif getattr(self._cancel_event, "is_set", None):
                if self._cancel_event.is_set():
                    raise CancelledError("cancelled before request")

        # Rate limiting
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._interval:
            self._sleeper(self._interval - elapsed)

        try:
            resp = self._session.get(
                url,
                timeout=(self._connect_timeout, self._read_timeout),
            )
            resp.raise_for_status()
        except CancelledError:
            raise
        except Exception as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            final_url = str(getattr(response, "url", "") or url)
            raise FetchError(
                f"GET {url} failed: {exc}", status_code=status_code, url=final_url
            ) from exc

        self._last_request_time = time.monotonic()

        text = _decode_response(resp)
        final_url = str(getattr(resp, "url", "") or url)
        if _is_authentication_page(final_url, text):
            raise AuthenticationRequired(
                "School unified authentication is required",
                status_code=getattr(resp, "status_code", None),
                url=final_url,
            )

        # Check cancel after response
        if self._cancel_event is not None:
            if callable(self._cancel_event):
                if self._cancel_event():
                    raise CancelledError("cancelled after response")
            elif getattr(self._cancel_event, "is_set", None):
                if self._cancel_event.is_set():
                    raise CancelledError("cancelled after response")

        return text

    # -- high-level --

    def fetch_list(self, source: SourceConfig, page: int = 1) -> list[ListEntry]:
        """Fetch and parse a list page."""
        url = build_page_url(source, page)
        html = self.get_text(url)
        return parse_list(html, source)

    def fetch_detail(self, source: SourceConfig, entry: ListEntry) -> NoticeDocument:
        """Fetch and parse a detail page."""
        html = self.get_text(entry.url)
        return parse_detail(html, source, entry)
