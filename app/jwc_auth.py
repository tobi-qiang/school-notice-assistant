"""Interactive, password-free capture of the JWC CAS browser session."""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
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


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise JWCAuthError("教务处登录态只能在 Windows 上安全保存")
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "school-notice-jwc", None, None, None, 0, ctypes.byref(output)
    ):
        raise JWCAuthError("无法使用 Windows DPAPI 加密教务处登录态")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def _unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise JWCAuthError("教务处登录态只能在 Windows 上解密")
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise JWCAuthError("已保存的教务处登录态无法解密")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


class JWCAuthManager:
    """Stores only encrypted Suda cookies captured from an official CAS page."""

    def __init__(
        self,
        state_path: str | Path,
        *,
        protector: Callable[[bytes], bytes] = _protect,
        unprotector: Callable[[bytes], bytes] = _unprotect,
        playwright_factory: Callable[[], Any] | None = None,
        session_factory: Callable[[], Any] = requests.Session,
    ):
        self.state_path = Path(state_path)
        self._protector = protector
        self._unprotector = unprotector
        self._playwright_factory = playwright_factory
        self._session_factory = session_factory
        self._login_lock = threading.Lock()
        self._cancel_login = threading.Event()
        self._validation = 'unverified'

    def has_state(self) -> bool:
        return self.state_path.is_file() and self.state_path.stat().st_size > 0

    def clear(self) -> None:
        if self.state_path.exists():
            self.state_path.unlink()

    def status(self) -> dict[str, Any]:
        state = self._load_state()
        usable = _usable_cookies((state or {}).get('cookies', []))
        validation = self._validation if usable else ('expired' if state else 'missing')
        messages = {
            'missing': '尚未保存可用登录态',
            'expired': '已保存的 Cookie 已过期，需要重新登录',
            'unverified': '已保存登录态，尚未验证当前是否有效',
            'valid': '已保存登录态，本次访问验证成功',
            'rejected': '学校已要求重新认证，将先尝试恢复已有登录态',
        }
        return {
            "configured": state is not None,
            "saved_at": str((state or {}).get("saved_at", "")),
            "validation": validation,
            "message": messages[validation],
        }

    def mark_rejected(self) -> None:
        self._validation = 'rejected'

    def cancel_login(self) -> None:
        self._cancel_login.set()

    def _load_state(self) -> dict[str, Any] | None:
        if not self.has_state():
            return None
        try:
            decoded = self._unprotector(self.state_path.read_bytes())
            state = json.loads(decoded.decode("utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
                return None
            return state
        except Exception:
            return None

    def _save_state(self, cookies: list[dict[str, Any]]) -> None:
        allowed = _usable_cookies(cookies)
        if not allowed:
            raise JWCAuthError("登录成功后未取得苏州大学会话 Cookie")
        payload = json.dumps(
            {"saved_at": datetime.now().astimezone().isoformat(), "cookies": allowed},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        encrypted = self._protector(payload)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_bytes(encrypted)
        os.replace(temporary, self.state_path)

    def apply_to(self, session) -> bool:
        state = self._load_state()
        if state is None:
            return False
        try:
            cookies = _usable_cookies(state["cookies"])
            for item in cookies:
                domain = str(item.get("domain", ""))
                expires = item.get("expires")
                if not isinstance(expires, (int, float)) or expires <= 0:
                    expires = None
                session.cookies.set(
                    str(item["name"]), str(item["value"]),
                    domain=domain, path=str(item.get("path") or "/"),
                    secure=bool(item.get("secure", False)),
                    expires=expires,
                )
            return bool(cookies)
        except Exception:
            return False

    def remember_session(self, session) -> None:
        """Persist rotated cookies only after the caller has read a real article."""
        previous = (self._load_state() or {}).get('cookies', [])
        metadata = {(c['name'], c['domain'], c.get('path', '/')): c
                    for c in _usable_cookies(previous)}
        merged = {}
        for cookie in session.cookies:
            if not _school_domain(cookie.domain):
                continue
            key = (cookie.name, cookie.domain, cookie.path or '/')
            merged[key] = {**metadata.get(key, {}), 'name': cookie.name, 'value': cookie.value,
                           'domain': cookie.domain, 'path': cookie.path or '/',
                           'secure': cookie.secure, 'expires': cookie.expires or -1}
        self._save_state(list(merged.values()))
        self._validation = 'valid'

    def _verify_cookies(self, target_url, cookies) -> list[dict[str, Any]] | None:
        # Verify with the same HTTP stack used by sync; a browser redirect alone
        # does not prove that the saved cookies can read protected articles.
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
                raise JWCAuthError('验证教务处登录态时网络失败，请重试；原登录态已保留') from exc
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

    def login(
        self,
        target_url: str,
        *,
        cancel_event=None,
        on_status: Callable[[str], None] | None = None,
        timeout_seconds: float = 600,
    ) -> dict[str, Any]:
        """Restore existing CAS cookies, then save only a verified article session."""
        if not self._login_lock.acquire(blocking=False):
            raise JWCAuthError('教务处登录窗口已打开，请先完成或关闭该窗口')
        try:
            return self._login(target_url, cancel_event=cancel_event,
                               on_status=on_status, timeout_seconds=timeout_seconds)
        finally:
            self._login_lock.release()

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
                    state = self._load_state()
                    cookies = _usable_cookies((state or {}).get('cookies', []))
                    if cookies:
                        context.add_cookies(cookies)
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
                            cookies = context.cookies()
                            verified = self._verify_cookies(target_url, cookies)
                            if verified:
                                self._save_state(verified)
                                self._validation = 'valid'
                                if on_status:
                                    on_status("教务处登录态验证成功，继续同步")
                                return self.status()
                        page.wait_for_timeout(250)
                finally:
                    browser.close()
        except (JWCAuthCancelled, JWCAuthTimeout, JWCAuthError):
            raise
        except Exception as exc:
            raise JWCAuthError(f"统一认证失败：{type(exc).__name__}") from exc
