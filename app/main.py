"""Executable assembly for the local school notice assistant."""
from __future__ import annotations

import signal
import threading
import time
import webbrowser

from waitress import create_server

from .application import SyncCoordinator
from .ai_client import AIClient
from .config_store import ConfigStore
from .database import Database
from .enrichment import EnrichmentQueue
from .jwc_auth import JWCAuthManager
from .lifecycle import ControlledTasks, HeartbeatMonitor, LifecycleController, SingleInstanceLock
from .settings import (
    DATA_DIR, DATABASE_PATH, HEARTBEAT_STALE_SECONDS, HOST, LAST_PAGE_GRACE_SECONDS, PORT, SOURCES,
)
from .search import SearchService
from .web import create_app


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://{HOST}:{PORT}/"
    lock = SingleInstanceLock(DATA_DIR / "app.lock")
    if not lock.acquire():
        _open_browser(url)
        return 0

    database = Database(DATABASE_PATH)
    database.initialize()
    config_store = ConfigStore(DATA_DIR / "config.json")
    jwc_auth = JWCAuthManager(DATA_DIR / "jwc-session.bin")
    tasks = ControlledTasks()
    enrichment = EnrichmentQueue(database, config_store, AIClient, tasks=tasks)
    search_service = SearchService(database, config_store, AIClient)
    # Synchronisation only stores school data.  AI work is user-triggered so
    # opening the app or syncing never spends model quota unexpectedly.
    coordinator = SyncCoordinator(database, SOURCES, tasks=tasks, jwc_auth=jwc_auth)
    heartbeat = HeartbeatMonitor(
        grace_seconds=LAST_PAGE_GRACE_SECONDS, stale_seconds=HEARTBEAT_STALE_SECONDS
    )
    server_box = {}
    resources_closed = False

    def close_resources() -> None:
        nonlocal resources_closed
        if resources_closed:
            return
        resources_closed = True
        server = server_box.get("server")
        if server is not None:
            dispatcher = getattr(server, "task_dispatcher", None)
            if dispatcher is not None:
                try:
                    dispatcher.shutdown(cancel_pending=True, timeout=5)
                except TypeError:
                    try:
                        dispatcher.shutdown()
                    except Exception:
                        pass
                except Exception:
                    pass
            try:
                server.close()
            except Exception:
                pass
        try:
            database.close()
        except Exception:
            pass

    lifecycle = LifecycleController(lock, heartbeat, tasks, on_shutdown=close_resources)
    app = create_app(
        database, coordinator, config_store, enrichment=enrichment,
        lifecycle=lifecycle, search_service=search_service, jwc_auth=jwc_auth,
    )

    try:
        server = create_server(app, host=HOST, port=PORT, threads=4)
    except BaseException:
        database.close()
        lock.release()
        raise
    server_box["server"] = server
    server_thread = threading.Thread(target=server.run, name="local-web-server", daemon=True)

    def request_exit(_signum=None, _frame=None) -> None:
        lifecycle.request_exit()

    signal.signal(signal.SIGINT, request_exit)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_exit)

    server_thread.start()
    _open_browser(url)
    try:
        while server_thread.is_alive() and not lifecycle.poll():
            time.sleep(0.25)
    except KeyboardInterrupt:
        lifecycle.request_exit()
    finally:
        lifecycle.shutdown()
        server_thread.join(timeout=5.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
