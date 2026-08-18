from app import main as main_module


class FakeServer:
    def __init__(self):
        self.closed = False
        self.ran = False
        self.task_dispatcher = FakeDispatcher()
    def run(self): self.ran = True
    def close(self): self.closed = True


class FakeDispatcher:
    def __init__(self): self.shutdown_calls = 0
    def shutdown(self, cancel_pending=True, timeout=5):
        self.shutdown_calls += 1
        assert cancel_pending is True and timeout == 5


def test_main_assembles_local_server_and_releases_resources(tmp_path, monkeypatch):
    server = FakeServer()
    captured = {}
    def create_server(app, host, port, threads):
        captured.update(app=app, host=host, port=port, threads=threads)
        return server
    monkeypatch.setattr(main_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main_module, "DATABASE_PATH", tmp_path / "notices.db")
    monkeypatch.setattr(main_module, "PORT", 0)
    monkeypatch.setattr(main_module, "create_server", create_server)
    monkeypatch.setattr(main_module, "_open_browser", lambda _url: None)
    assert main_module.main() == 0
    assert captured["host"] == "127.0.0.1" and captured["threads"] == 4
    assert server.ran is True and server.closed is True
    assert server.task_dispatcher.shutdown_calls == 1
    lock = main_module.SingleInstanceLock(tmp_path / "app.lock")
    assert lock.acquire() is True
    lock.release()
