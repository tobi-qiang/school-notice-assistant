from __future__ import annotations

import json

from app.jwc_auth import JWCAuthManager


class Cookies:
    def __init__(self): self.items = []
    def set(self, name, value, **kwargs): self.items.append((name, value, kwargs))


class Session:
    def __init__(self): self.cookies = Cookies()


def reversible(data: bytes) -> bytes:
    return data[::-1]


def test_encrypted_cookie_state_is_filtered_and_applied(tmp_path):
    manager = JWCAuthManager(tmp_path / "state.bin", protector=reversible, unprotector=reversible)
    manager._save_state([
        {"name": "CASTGC", "value": "secret", "domain": ".suda.edu.cn", "path": "/", "secure": True},
        {"name": "foreign", "value": "no", "domain": ".example.com", "path": "/"},
    ])
    raw = (tmp_path / "state.bin").read_bytes()
    assert b"secret" not in raw
    session = Session()
    assert manager.apply_to(session) is True
    assert [(name, value) for name, value, _ in session.cookies.items] == [("CASTGC", "secret")]


def test_clear_and_invalid_state(tmp_path):
    path = tmp_path / "state.bin"
    path.write_bytes(b"broken")
    manager = JWCAuthManager(path, protector=reversible, unprotector=reversible)
    assert manager.apply_to(Session()) is False
    manager.clear()
    assert manager.has_state() is False

import time
import threading
from types import SimpleNamespace

import pytest
import requests

from app.jwc_auth import JWCAuthCancelled, JWCAuthError

TARGET = 'https://jwc.suda.edu.cn/ab/cd/c8620a123/page.htm'
ARTICLE = '<div class="wp_articlecontent"><p>Fixture article</p></div>'


def cookie(name='session', domain='jwc.suda.edu.cn', expires=-1):
    return dict(name=name, value='test-secret', domain=domain, path='/', expires=expires, secure=True)


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
        self.context.restored_before_navigation = bool(self.context.added)
        self.url = target

    def content(self):
        return '<div>Redirecting</div>' if self.waiting and self.waits == 0 else ARTICLE

    def is_closed(self):
        return self.waits >= 2

    def wait_for_timeout(self, _ms):
        self.waits += 1


class Context:
    def __init__(self, *, waiting=False):
        self.added = []
        self.page = Page(self, waiting=waiting)
        self.restored_before_navigation = False

    def add_cookies(self, items): self.added = items
    def new_page(self): return self.page
    def cookies(self): return self.added or [cookie()]


class Browser:
    def __init__(self, context): self.context = context; self.closed = False
    def new_context(self, **kwargs): return self.context
    def close(self): self.closed = True


class Playwright:
    def __init__(self, browser):
        self.chromium = SimpleNamespace(launch=lambda **kwargs: browser)
    def __enter__(self): return self
    def __exit__(self, *args): pass


def login_manager(tmp_path, *, readable=True, waiting=False):
    context = Context(waiting=waiting)
    browser = Browser(context)
    session = VerificationSession(readable)
    manager = JWCAuthManager(tmp_path / 'state.bin', protector=reversible,
                             unprotector=reversible,
                             playwright_factory=lambda: Playwright(browser),
                             session_factory=lambda: session)
    return manager, context, browser, session


def test_login_restores_saved_cookies_before_navigation_and_verifies_http(tmp_path):
    manager, context, browser, session = login_manager(tmp_path)
    expiry = time.time() + 3600
    manager._save_state([cookie('CASTGC', 'auth.suda.edu.cn', expiry)])
    assert manager.status()['validation'] == 'unverified'
    result = manager.login(TARGET)
    assert context.restored_before_navigation
    assert context.added[0]['name'] == 'CASTGC'
    assert session.called == 1
    assert result['validation'] == 'valid'
    assert browser.closed
    assert manager._load_state()['cookies'][0]['expires'] > time.time()


def test_same_host_redirect_page_is_not_saved_as_success(tmp_path):
    manager, context, browser, session = login_manager(tmp_path, waiting=True)
    manager.login(TARGET)
    assert context.page.waits == 1
    assert session.called == 1 and browser.closed


def test_browser_article_without_reusable_http_auth_does_not_overwrite_state(tmp_path):
    manager, context, browser, session = login_manager(tmp_path, readable=False)
    manager._save_state([cookie()])
    original = manager.state_path.read_bytes()
    with pytest.raises(JWCAuthCancelled):
        manager.login(TARGET)
    assert session.called == 1
    assert manager.state_path.read_bytes() == original
    assert browser.closed
    assert manager.status()['validation'] != 'valid'


def test_expired_and_lookalike_domain_cookies_are_not_applied(tmp_path):
    manager, *_ = login_manager(tmp_path)
    payload = {'saved_at': '', 'cookies': [cookie(expires=time.time() - 10),
                                         cookie(domain='notsuda.edu.cn')]}
    manager.state_path.write_bytes(reversible(json.dumps(payload).encode()))
    session = Session()
    assert manager.apply_to(session) is False
    assert not session.cookies.items
    assert manager.status()['validation'] == 'expired'


def test_school_subdomain_filter_excludes_suffix_lookalikes(tmp_path):
    manager, *_ = login_manager(tmp_path)
    manager._save_state([cookie(), cookie(domain='notsuda.edu.cn')])
    assert len(manager._load_state()['cookies']) == 1


def test_process_restart_reports_saved_not_guaranteed_authenticated(tmp_path):
    manager, *_ = login_manager(tmp_path)
    manager.login(TARGET)
    restarted = JWCAuthManager(manager.state_path, protector=reversible, unprotector=reversible)
    assert restarted.status()['configured'] is True
    assert restarted.status()['validation'] == 'unverified'
    assert '尚未验证' in restarted.status()['message']


def test_successful_sync_persists_rotated_cookies(tmp_path):
    manager, *_ = login_manager(tmp_path)
    manager._save_state([cookie('CASTGC', 'auth.suda.edu.cn')])
    session = requests.Session()
    manager.apply_to(session)
    session.cookies.set('session', 'rotated', domain='jwc.suda.edu.cn', path='/')
    manager.remember_session(session)
    restarted = JWCAuthManager(manager.state_path, protector=reversible, unprotector=reversible)
    restored = requests.Session()
    assert restarted.apply_to(restored)
    assert restored.cookies.get('session', domain='jwc.suda.edu.cn') == 'rotated'
    assert restored.cookies.get('CASTGC', domain='auth.suda.edu.cn') == 'test-secret'
    assert manager.status()['validation'] == 'valid'


def test_shutdown_cancels_login_and_closes_browser(tmp_path):
    manager, _, browser, session = login_manager(tmp_path)
    manager.cancel_login()
    with pytest.raises(JWCAuthCancelled):
        manager.login(TARGET)
    assert browser.closed and session.called == 0


def test_concurrent_login_is_rejected(tmp_path):
    manager, *_ = login_manager(tmp_path)
    with manager._login_lock:
        with pytest.raises(JWCAuthError, match='已打开'):
            manager.login(TARGET)


def test_cookie_deleted_by_school_is_not_restored_from_old_file(tmp_path):
    manager, *_ = login_manager(tmp_path)
    manager._save_state([cookie('session'), cookie('old')])
    session = requests.Session()
    manager.apply_to(session)
    session.cookies.clear(domain='jwc.suda.edu.cn', path='/', name='old')
    manager.remember_session(session)
    assert [c['name'] for c in manager._load_state()['cookies']] == ['session']
