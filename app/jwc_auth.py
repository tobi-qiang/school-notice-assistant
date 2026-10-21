"""Interactive, password-free capture of the JWC CAS browser session.

The captured cookies live in memory for a single sync run and are never
written to disk: every sync that reaches a protected article asks the user
to sign in once in the browser window.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .webplus import _decode_response, _is_authentication_page


def _school_domain(domain: str) -> bool:
    host = domain.lstrip('.').casefold()
    return host == 'suda.edu.cn' or host.endswith('.suda.edu.cn')


def _usable_cookies(cookies) -> list[dict[str, Any]]:
    now = time.time()
    return [item for item in cookies if isinstance(item, dict)
            and item.get('name') and isinstance(item.get('value'), str)
            and _school_domain(str(item.get('domain', '')))
            and (not isinstance(item.get('expires'), (int, float))
                 or item['expires'] <= 0 or item['expires'] > now)]


def _article_readable(url: str, html: str) -> bool:
    if (urlparse(url).hostname or '').casefold() != 'jwc.suda.edu.cn':
        return False
    if _is_authentication_page(url, html):
        return False
    body = BeautifulSoup(html, 'html.parser').select_one('.wp_articlecontent')
    return body is not None and bool(body.get_text(strip=True) or body.find('img'))


class JWCAuthError(RuntimeError):
    pass


class JWCAuthCancelled(JWCAuthError):
    pass


class JWCAuthTimeout(JWCAuthError):
    pass


def apply_cookies(session, cookies) -> int:
    """Copy captured CAS cookies onto a requests session (memory only)."""
    applied = 0
    for item in _usable_cookies(cookies):
        expiry = item.get('expires')
        session.cookies.set(
            str(item['name']), str(item['value']),
            domain=str(item.get('domain', '')),
            path=str(item.get('path') or '/'),
            secure=bool(item.get('secure', False)),
            expires=int(expiry) if isinstance(expiry, (int, float)) and expiry > 0 else None,
        )
        applied += 1
    return applied


class JWCAuthManager:
    """Opens the official CAS page once per sync; keeps cookies in memory."""

    def __init__(
        self,
        *,
        playwright_factory: Callable[[], Any] | None = None,
        session_factory: Callable[[], Any] = requests.Session,
    ):
        self._playwright_factory = playwright_factory
        self._session_factory = session_factory
        self._login_lock = threading.Lock()
        self._cancel_login = threading.Event()

    def cancel_login(self) -> None:
        self._cancel_login.set()

    def login(
        self,
        target_url: str,
        *,
        cancel_event=None,
        on_status: Callable[[str], None] | None = None,
        timeout_seconds: float = 600,
    ) -> list[dict[str, Any]]:
        """Wait for a manual sign-in and return verified cookies for this run."""
        if not self._login_lock.acquire(blocking=False):
            raise JWCAuthError('教务处登录窗口已打开，请先完成或关闭该窗口')
        try:
            return self._login(target_url, cancel_event=cancel_event,
                               on_status=on_status, timeout_seconds=timeout_seconds)
        finally:
            self._login_lock.release()

    def _verify_cookies(self, target_url, cookies) -> list[dict[str, Any]] | None:
        # Verify with the same HTTP stack used by sync; a browser redirect alone
        # does not prove that the session can read protected articles.
        with self._session_factory() as session:
            for item in _usable_cookies(cookies):
                expiry = item.get('expires')
                session.cookies.set(item['name'], item['value'], domain=item['domain'],
                                    path=item.get('path') or '/', secure=item.get('secure', False),
                                    expires=int(expiry) if isinstance(expiry, (int, float)) and expiry > 0 else None)
            try:
                response = session.get(target_url, timeout=(4, 12))
                response.raise_for_status()
            except requests.RequestException as exc:
                raise JWCAuthError('验证教务处登录态时网络失败，请重试') from exc
            if not _article_readable(response.url, _decode_response(response)):
                return None
            metadata = {(c['name'], c['domain'], c.get('path', '/')): c for c in _usable_cookies(cookies)}
            merged = {}
            for c in session.cookies:
                if _school_domain(c.domain):
                    key = (c.name, c.domain, c.path or '/')
                    # Keep browser-only attributes when replacing a cookie.
                    merged[key] = {**metadata.get(key, {}), 'name': c.name, 'value': c.value,
                                   'domain': c.domain, 'path': c.path or '/', 'secure': c.secure,
                                   'expires': c.expires or -1}
            return list(merged.values())

    def _login(self, target_url, *, cancel_event, on_status, timeout_seconds):
        if urlparse(target_url).hostname != 'jwc.suda.edu.cn':
            raise JWCAuthError('只能验证教务处官方公告地址')
        if on_status:
            on_status("请在弹出的苏大统一认证页面完成登录")
        factory = self._playwright_factory
        if factory is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise JWCAuthError("缺少 Playwright，无法打开统一认证窗口") from exc
            factory = sync_playwright

        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        try:
            with factory() as playwright:
                browser = playwright.chromium.launch(channel="chrome", headless=False)
                try:
                    context = browser.new_context(no_viewport=True)
                    page = context.new_page()
                    page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
                    last_verification = -float('inf')
                    while True:
                        if self._cancel_login.is_set() or (cancel_event is not None and cancel_event.is_set()):
                            raise JWCAuthCancelled("用户取消教务处登录")
                        if time.monotonic() >= deadline:
                            raise JWCAuthTimeout("等待统一认证超时")
                        if page.is_closed():
                            raise JWCAuthCancelled("统一认证窗口已关闭")
                        host = (urlparse(page.url).hostname or "").casefold()
                        if (host == "jwc.suda.edu.cn" and time.monotonic() - last_verification >= 2
                                and _article_readable(page.url, page.content())):
                            last_verification = time.monotonic()
                            verified = self._verify_cookies(target_url, context.cookies())
                            if verified:
                                if on_status:
                                    on_status("教务处登录态验证成功，继续同步")
                                return verified
                        page.wait_for_timeout(250)
                finally:
                    browser.close()
        except (JWCAuthCancelled, JWCAuthTimeout, JWCAuthError):
            raise
        except Exception as exc:
            raise JWCAuthError(f"统一认证失败：{type(exc).__name__}") from exc
