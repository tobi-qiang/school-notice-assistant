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
