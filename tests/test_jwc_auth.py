from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import requests

from app.jwc_auth import (
    JWCAuthCancelled, JWCAuthError, JWCAuthManager, JWCAuthTimeout, apply_cookies,
)

TARGET = 'https://jwc.suda.edu.cn/ab/cd/c8620a123/page.htm'
ARTICLE = '<div class="wp_articlecontent"><p>Fixture article</p></div>'


def cookie(name='session', domain='jwc.suda.edu.cn', expires=-1):
    return dict(name=name, value='test-secret', domain=domain, path='/', expires=expires, secure=True)


class _Cookies:
    def __init__(self, owner): self.owner = owner
    def set(self, name, value, **kwargs): self.owner.items.append((name, value, kwargs))


class Session:
    """Minimal stand-in for requests.Session.cookies."""

    def __init__(self):
        self.items = []
        self.cookies = _Cookies(self)


class VerificationSession(requests.Session):
    def __init__(self, readable=True):
        super().__init__()
        self.readable = readable
        self.called = 0

    def get(self, url, **kwargs):
        self.called += 1
        result = requests.Response()
        result.status_code = 200
        result.url = url if self.readable else 'https://auth.suda.edu.cn/cas/login'
        result._content = (ARTICLE if self.readable else '<form>Login</form>').encode()
        return result


class Page:
    def __init__(self, context, *, waiting=False):
        self.context = context
        self.url = TARGET
        self.waits = 0
        self.waiting = waiting

    def goto(self, target, **kwargs):
        self.url = target

    def content(self):
        return '<div>Redirecting</div>' if self.waiting and self.waits == 0 else ARTICLE

    def is_closed(self):
        return self.waits >= 2

    def wait_for_timeout(self, _ms):
        self.waits += 1


class PendingPage(Page):
    """A page that never shows the article body (e.g. a stuck CAS loader)."""

    def content(self):
        return '<div>Redirecting</div>'

    def is_closed(self):
        return False


class Context:
    def __init__(self, *, waiting=False):
        self.page = Page(self, waiting=waiting)

    def new_page(self): return self.page
    def cookies(self): return [cookie()]


class Browser:
    def __init__(self, context): self.context = context; self.closed = False
    def new_context(self, **kwargs): return self.context
    def close(self): self.closed = True


class Playwright:
    def __init__(self, browser):
        self.chromium = SimpleNamespace(launch=lambda **kwargs: browser)
    def __enter__(self): return self
    def __exit__(self, *args): pass


def login_manager(*, readable=True, waiting=False):
    context = Context(waiting=waiting)
    browser = Browser(context)
    session = VerificationSession(readable)
    manager = JWCAuthManager(playwright_factory=lambda: Playwright(browser),
                             session_factory=lambda: session)
    return manager, context, browser, session


def test_login_verifies_http_auth_and_returns_memory_cookies():
    manager, _context, browser, session = login_manager()
    result = manager.login(TARGET)
    assert session.called == 1
    assert [c['name'] for c in result] == ['session']
    assert result[0]['value'] == 'test-secret'
    assert browser.closed


def test_same_host_redirect_page_is_not_treated_as_success():
    manager, context, browser, session = login_manager(waiting=True)
    manager.login(TARGET)
    assert context.page.waits == 1
    assert session.called == 1 and browser.closed


def test_browser_article_without_reusable_http_auth_raises_and_closes():
    manager, _context, browser, session = login_manager(readable=False)
    with pytest.raises(JWCAuthCancelled):
        manager.login(TARGET)
    assert session.called == 1
    assert browser.closed


def test_verify_keeps_cookies_from_both_cas_and_jwc_domains():
    manager, *_ = login_manager()
    cookies = [cookie('CASTGC', 'auth.suda.edu.cn'), cookie('JSESSIONID', 'jwc.suda.edu.cn')]
    verified = manager._verify_cookies(TARGET, cookies)
    assert {(item['name'], item['domain']) for item in verified} == {
        ('CASTGC', 'auth.suda.edu.cn'), ('JSESSIONID', 'jwc.suda.edu.cn'),
    }


def test_login_times_out_when_page_never_becomes_readable():
    context = Context()
    context.page = PendingPage(context)
    browser = Browser(context)
    session = VerificationSession(readable=False)
    manager = JWCAuthManager(playwright_factory=lambda: Playwright(browser),
                             session_factory=lambda: session)
    with pytest.raises(JWCAuthTimeout):
        manager.login(TARGET, timeout_seconds=0.05)
    assert browser.closed and session.called == 0


def test_expired_and_lookalike_domain_cookies_are_not_copied():
    session = Session()
    assert apply_cookies(session, [cookie(expires=time.time() - 10),
                                   cookie(domain='notsuda.edu.cn')]) == 0
    assert not session.items


def test_shutdown_cancels_login_and_closes_browser():
    manager, _context, browser, session = login_manager()
    manager.cancel_login()
    with pytest.raises(JWCAuthCancelled):
        manager.login(TARGET)
    assert browser.closed and session.called == 0


def test_concurrent_login_is_rejected():
    manager, *_ = login_manager()
    with manager._login_lock:
        with pytest.raises(JWCAuthError, match='已打开'):
            manager.login(TARGET)
