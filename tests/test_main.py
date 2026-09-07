from app import main as main_module
import http.client
import signal
import socket
import threading
import time

import pytest


class FakeServer:
    def __init__(self):
        self.closed = False
        self.ran = False
        self.task_dispatcher = FakeDispatcher()
    def run(self): self.ran = True
    def close(self): self.closed = True


class FakeDispatcher:
    def __init__(self):
        self.shutdown_calls = 0
        self.threads = set()
    def set_thread_count(self, count): pass
    def shutdown(self, cancel_pending=True, timeout=5):
        self.shutdown_calls += 1
        assert cancel_pending is True and timeout == 5


def test_main_assembles_local_server_and_releases_resources(tmp_path, monkeypatch):
    server = FakeServer()
    captured = {}
    def create_server(app, host, port, threads, **kwargs):
        captured.update(app=app, host=host, port=port, threads=threads)
        response = app.test_client().post('/api/exit')
        response.close()
        return server
    monkeypatch.setattr(main_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main_module, "DATABASE_PATH", tmp_path / "notices.db")
    monkeypatch.setattr(main_module, "PORT", 0)
    monkeypatch.setattr(main_module, "create_server", create_server)
    monkeypatch.setattr(main_module, "ThreadedTaskDispatcher", lambda: server.task_dispatcher)
    monkeypatch.setattr(main_module, "_open_browser", lambda _url: None)
    assert main_module.main() == 0
    assert captured["host"] == "127.0.0.1" and captured["threads"] == 8
    assert server.closed is True
    assert server.task_dispatcher.shutdown_calls == 1
    lock = main_module.SingleInstanceLock(tmp_path / "app.lock")
    assert lock.acquire() is True
    lock.release()


@pytest.fixture
def isolated_main(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main_module, "DATABASE_PATH", tmp_path / "notices.db")
    monkeypatch.setattr(main_module, "PORT", 0)
    monkeypatch.setattr(main_module, "_open_browser", lambda _url: None)
    yield tmp_path
    # Also close handles on a failing regression run.
    if main_module._event_logger is not None:
        for handler in list(main_module._event_logger.handlers):
            handler.close()
            main_module._event_logger.removeHandler(handler)
        main_module._event_logger = None


def assert_lock_available(path):
    lock = main_module.SingleInstanceLock(path / "app.lock")
    try:
        assert lock.acquire()
    finally:
        lock.release()


@pytest.mark.parametrize("stage", ["initialize", "config", "app", "signals", "thread"])
def test_startup_failure_releases_database_lock_and_waitress(isolated_main, monkeypatch, stage):
    databases, servers = [], []
    original_db = main_module.Database
    original_factory = main_module.create_server
    original_start = threading.Thread.start
    previous_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    class TrackingDB(original_db):
        def __init__(self, path):
            super().__init__(path)
            databases.append(self)
        def initialize(self):
            super().initialize()
            if stage == "initialize":
                raise RuntimeError("startup regression")

    def factory(*args, **kwargs):
        server = original_factory(*args, **kwargs)
        servers.append(server)
        return server

    def fail(*args, **kwargs):
        raise RuntimeError("startup regression")

    monkeypatch.setattr(main_module, "Database", TrackingDB)
    monkeypatch.setattr(main_module, "create_server", factory)
    if stage == "config":
        monkeypatch.setattr(main_module, "ConfigStore", fail)
    elif stage == "app":
        monkeypatch.setattr(main_module, "create_app", fail)
    elif stage == "signals":
        original_signal = signal.signal
        def install(sig, handler):
            if sig == signal.SIGTERM:
                raise RuntimeError("startup regression")
            return original_signal(sig, handler)
        monkeypatch.setattr(signal, "signal", install)
    elif stage == "thread":
        def start(thread):
            if thread.name == "local-web-server":
                fail()
            return original_start(thread)
        monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(RuntimeError, match="startup regression"):
        main_module.main()
    assert databases[0]._conn is None
    assert_lock_available(isolated_main)
    for server in servers:
        assert not server._map
        assert not server.task_dispatcher.threads
        assert server.socket is None
    for sig, handler in previous_signals.items():
        assert signal.getsignal(sig) == handler
    assert main_module._event_logger is None


def test_bind_failure_closes_partial_waitress_resources(isolated_main, monkeypatch):
    dispatcher = main_module.ThreadedTaskDispatcher()
    socket_map = {}
    factory = main_module.create_server
    monkeypatch.setattr(main_module, "ThreadedTaskDispatcher", lambda: dispatcher)
    def capture(app, **kwargs):
        nonlocal socket_map
        socket_map = kwargs["map"]
        return factory(app, **kwargs)
    monkeypatch.setattr(main_module, "create_server", capture)
    # Actual bind error on an owned, listening loopback socket; no production port.
    with socket.socket() as occupied:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        monkeypatch.setattr(main_module, "PORT", occupied.getsockname()[1])
        with pytest.raises(OSError):
            main_module.main()
    assert not dispatcher.threads
    # Factory must receive the owned map rather than allocate an unreachable one.
    assert not socket_map
    assert_lock_available(isolated_main)


def test_real_waitress_exit_closes_keepalive_channels_and_port(isolated_main, monkeypatch):
    captured, errors, connections = {}, [], []
    factory = main_module.create_server
    original_app = main_module.create_app
    client_finished = threading.Event()
    original_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def make_app(database, *args, **kwargs):
        captured.update(database=database, lifecycle=kwargs["lifecycle"])
        return original_app(database, *args, **kwargs)

    def make_server(*args, **kwargs):
        server = factory(*args, **kwargs)
        captured["server"] = server
        captured["map"] = server._map
        captured["port"] = int(server.effective_port)
        captured["listener"] = server.socket
        return server

    def browser(_url):
        try:
            port = captured["port"]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connections.append(connection)
            connection.request("GET", "/")
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            # Keep an idle HTTP/1.1 connection alive through shutdown.
            assert connection.sock is not None
            connection.request("POST", "/api/exit", body=b"")
            response = connection.getresponse()
            assert response.status == 200
            assert b"shutting_down" in response.read()
            captured["channels"] = list(captured["server"].active_channels.values())
            assert captured["channels"]
            captured["channel_sockets"] = [channel.socket for channel in captured["channels"]]
        except BaseException as exc:
            errors.append(exc)
        finally:
            captured["lifecycle"].request_exit("test cleanup")
            client_finished.set()

    monkeypatch.setattr(main_module, "create_app", make_app)
    monkeypatch.setattr(main_module, "create_server", make_server)
    monkeypatch.setattr(main_module, "_open_browser", browser)
    try:
        assert main_module.main() == 0
        assert client_finished.wait(3)
        assert not errors
        server = captured["server"]
        assert not captured["map"]
        assert not server.active_channels
        assert not server.task_dispatcher.threads
        assert captured["database"]._conn is None
        assert captured["listener"].fileno() == -1
        assert server.trigger._closed
        for channel in captured["channels"]:
            assert channel.socket is None
            assert not channel.connected
        assert all(sock.fileno() == -1 for sock in captured["channel_sockets"])
        assert not any(t.name == "local-web-server" for t in threading.enumerate())
        assert_lock_available(isolated_main)
        # Windows permits reuse of the now released listener even while the
        # client still holds its side of the former keep-alive connection.
        with socket.socket() as replacement:
            replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            replacement.bind(("127.0.0.1", captured["port"]))
            replacement.listen()
        assert main_module._event_logger is None
        assert {sig: signal.getsignal(sig) for sig in original_signals} == original_signals
    finally:
        for connection in connections:
            connection.close()
        # Make the regression itself leak-free when run against the old code.
        if "server" in captured:
            for channel in list(captured["server"].active_channels.values()):
                channel.handle_close()


def test_blocked_browser_does_not_block_exit(isolated_main, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_app = main_module.create_app
    def make_app(*args, **kwargs):
        kwargs["lifecycle"].request_exit("test")
        return original_app(*args, **kwargs)
    def blocked(_url):
        entered.set()
        try:
            release.wait(3)
        finally:
            finished.set()
    monkeypatch.setattr(main_module, "create_app", make_app)
    monkeypatch.setattr(main_module, "_open_browser", blocked)
    try:
        started = time.monotonic()
        assert main_module.main() == 0
        assert entered.is_set()
        assert not finished.is_set()
        assert time.monotonic() - started < 2
        assert_lock_available(isolated_main)
    finally:
        release.set()
        assert finished.wait(3)


def test_server_loop_exception_is_logged_and_returns_failure(isolated_main, monkeypatch):
    def broken_loop(**kwargs):
        raise OSError("synthetic I/O loop failure")
    monkeypatch.setattr(main_module.wasyncore, "loop", broken_loop)
    assert main_module.main() == 1
    assert "synthetic I/O loop failure" in (isolated_main / "app.log").read_text(encoding="utf-8")
    assert_lock_available(isolated_main)


@pytest.mark.parametrize('cancel_fails', [False, True])
def test_login_cancel_precedes_dispatcher_shutdown_even_on_error(isolated_main, monkeypatch, cancel_fails):
    calls = []
    dispatcher = main_module.ThreadedTaskDispatcher()
    original_shutdown = dispatcher.shutdown
    original_app = main_module.create_app
    class Auth:
        def __init__(self, *args): pass
        def cancel_login(self):
            calls.append('cancel-login')
            if cancel_fails:
                raise RuntimeError('cancel regression')
    def shutdown(**kwargs):
        calls.append('dispatcher-shutdown')
        return original_shutdown(**kwargs)
    def make_app(*args, **kwargs):
        kwargs['lifecycle'].request_exit('test')
        return original_app(*args, **kwargs)
    monkeypatch.setattr(main_module, 'JWCAuthManager', Auth)
    monkeypatch.setattr(main_module, 'ThreadedTaskDispatcher', lambda: dispatcher)
    monkeypatch.setattr(dispatcher, 'shutdown', shutdown)
    monkeypatch.setattr(main_module, 'create_app', make_app)
    assert main_module.main() == 0
    assert calls == ['cancel-login', 'dispatcher-shutdown']
    assert not dispatcher.threads
    assert_lock_available(isolated_main)


def test_duplicate_launch_does_not_assemble_a_second_service(isolated_main, monkeypatch):
    owner = main_module.SingleInstanceLock(isolated_main / 'app.lock')
    assert owner.acquire()
    opened = threading.Event()
    def forbidden(*args, **kwargs):
        raise AssertionError('duplicate instance must not assemble resources')
    monkeypatch.setattr(main_module, 'Database', forbidden)
    monkeypatch.setattr(main_module, 'create_server', forbidden)
    monkeypatch.setattr(main_module, '_open_browser', lambda url: opened.set())
    try:
        assert main_module.main() == 0
        assert opened.is_set()
        contender = main_module.SingleInstanceLock(isolated_main / 'app.lock')
        assert not contender.acquire()
        assert owner.acquired
    finally:
        owner.release()
