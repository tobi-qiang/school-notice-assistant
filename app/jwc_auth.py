"""Interactive, password-free capture of the JWC CAS browser session."""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


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
    ):
        self.state_path = Path(state_path)
        self._protector = protector
        self._unprotector = unprotector
        self._playwright_factory = playwright_factory

    def has_state(self) -> bool:
        return self.state_path.is_file() and self.state_path.stat().st_size > 0

    def clear(self) -> None:
        if self.state_path.exists():
            self.state_path.unlink()

    def status(self) -> dict[str, Any]:
        state = self._load_state()
        return {
            "configured": state is not None,
            "saved_at": str((state or {}).get("saved_at", "")),
        }

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
        allowed = [
            item for item in cookies
            if str(item.get("domain", "")).lstrip(".").casefold().endswith("suda.edu.cn")
        ]
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
            for item in state["cookies"]:
                domain = str(item.get("domain", ""))
                if not domain.lstrip(".").casefold().endswith("suda.edu.cn"):
                    continue
                expires = item.get("expires")
                if not isinstance(expires, (int, float)) or expires <= 0:
                    expires = None
                session.cookies.set(
                    str(item["name"]), str(item["value"]),
                    domain=domain, path=str(item.get("path") or "/"),
                    secure=bool(item.get("secure", False)),
                    expires=expires,
                )
            return True
        except Exception:
            return False

    def login(
        self,
        target_url: str,
        *,
        cancel_event=None,
        on_status: Callable[[str], None] | None = None,
        timeout_seconds: float = 600,
    ) -> dict[str, Any]:
        """Open official CAS in system Chrome and persist the resulting cookies."""
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
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise JWCAuthCancelled("用户取消教务处登录")
                        if time.monotonic() >= deadline:
                            raise JWCAuthTimeout("等待统一认证超时")
                        if page.is_closed():
                            raise JWCAuthCancelled("统一认证窗口已关闭")
                        host = (urlparse(page.url).hostname or "").casefold()
                        if host == "jwc.suda.edu.cn":
                            cookies = context.cookies()
                            self._save_state(cookies)
                            if on_status:
                                on_status("教务处登录成功，继续同步")
                            return self.status()
                        page.wait_for_timeout(250)
                finally:
                    browser.close()
        except (JWCAuthCancelled, JWCAuthTimeout, JWCAuthError):
            raise
        except Exception as exc:
            raise JWCAuthError(f"统一认证失败：{type(exc).__name__}") from exc
