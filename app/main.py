"""Executable assembly for the local school notice assistant."""
from __future__ import annotations

import os
import logging
import signal
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

from waitress import create_server, wasyncore
from waitress.channel import HTTPChannel
from waitress.task import ThreadedTaskDispatcher

from .application import SyncCoordinator
from .ai_client import AIClient
from .config_store import ConfigStore
from .database import Database
from .enrichment import EnrichmentQueue
from .jwc_auth import JWCAuthManager
from .lifecycle import ControlledTasks, HeartbeatMonitor, LifecycleController, SingleInstanceLock
from .settings import (
    DATA_DIR, DATABASE_PATH, HEARTBEAT_ORPHAN_SECONDS, HOST, LAST_PAGE_GRACE_SECONDS, PORT, SOURCES,
    SUSPEND_GAP_SECONDS,
)
from .search import SearchService
from .web import create_app


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _launch_browser(url: str, *, wait: float = 0) -> None:
    # 系统浏览器关联程序可能挂住，不能让它阻塞服务轮询/退出。
    thread = threading.Thread(target=_open_browser, args=(url,), name="open-browser", daemon=True)
    thread.start()
    if wait:
        thread.join(timeout=wait)


_event_logger: logging.Logger | None = None
_event_log_path = None


def _get_event_logger() -> logging.Logger:
    """Build a small rotating file logger for the hidden-window launcher."""
    global _event_logger, _event_log_path
    path = DATA_DIR / "app.log"
    if _event_logger is not None and _event_log_path == path:
        return _event_logger
    if _event_logger is not None:
        for handler in list(_event_logger.handlers):
            handler.close()
            _event_logger.removeHandler(handler)
    logger = logging.getLogger("school_notice_assistant.lifecycle")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(
        path, maxBytes=200_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    _event_logger = logger
    _event_log_path = path
    return logger


def _log_event(message: str, *, exc_info: bool = False) -> None:
    try:
        _get_event_logger().error(message, exc_info=True) if exc_info else _get_event_logger().info(message)
    except OSError:
        pass


def _close_event_logger() -> None:
    global _event_logger, _event_log_path
    if _event_logger is not None:
        for handler in list(_event_logger.handlers):
            _event_logger.removeHandler(handler)
            handler.close()
    _event_logger = None
    _event_log_path = None


def _close_socket_map(socket_map: dict) -> None:
    # 单监听 Waitress server.close() 不会遍历已接受的 HTTPChannel。
    # handle_close 还会释放输出缓冲文件并唤醒等待输出空间的请求线程。
    for channel in list(socket_map.values()):
        try:
            if isinstance(channel, HTTPChannel):
                channel.handle_close()
            else:
                channel.close()
        except Exception:
            _log_event("关闭本地 socket/channel 失败", exc_info=True)


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://{HOST}:{PORT}/"
    lock = SingleInstanceLock(DATA_DIR / "app.lock")
    if not lock.acquire():
        _launch_browser(url, wait=1.0)
        return 0

    database = lifecycle = server = server_thread = dispatcher = jwc_auth = None
    socket_map = {}
    server_stop = threading.Event()
    old_signals = {}
    resources_closed = False
    reason = "启动未完成"
    result = 0

    def close_resources() -> None:
        nonlocal resources_closed
        if resources_closed:
            return
        resources_closed = True
        if jwc_auth is not None:
            try:
                jwc_auth.cancel_login()
            except Exception:
                _log_event("取消教务登录失败", exc_info=True)
        # 先结束 I/O 循环再遍历 map，避免 accept/select 与关闭并发修改 map。
        server_stop.set()
        if server_thread is not None and server_thread.ident is not None:
            server_thread.join(timeout=5.0)
            if server_thread.is_alive():
                _log_event("退出等待超时，本地 web I/O 线程仍未结束")
        _close_socket_map(socket_map)
        if server is not None:
            try:
                server.close()
            except Exception:
                _log_event("关闭本地服务器失败", exc_info=True)
        if dispatcher is not None:
            try:
                dispatcher.shutdown(cancel_pending=True, timeout=5)
                if dispatcher.threads:
                    _log_event(f"退出等待超时，仍有 {len(dispatcher.threads)} 个 HTTP 工作线程")
            except Exception:
                _log_event("关闭 HTTP 工作线程失败", exc_info=True)
        if database is not None:
            try:
                database.close()
            except Exception:
                _log_event("关闭数据库失败", exc_info=True)

    try:
        database = Database(DATABASE_PATH)
        database.initialize()
        tasks = ControlledTasks()
        heartbeat = HeartbeatMonitor(
            grace_seconds=LAST_PAGE_GRACE_SECONDS,
            orphan_seconds=HEARTBEAT_ORPHAN_SECONDS,
            suspend_gap_seconds=SUSPEND_GAP_SECONDS,
        )
        lifecycle = LifecycleController(lock, heartbeat, tasks, on_shutdown=close_resources)
        config_store = ConfigStore(DATA_DIR / "config.json")
        jwc_auth = JWCAuthManager()
        enrichment = EnrichmentQueue(database, config_store, AIClient, tasks=tasks)
        search_service = SearchService(database, config_store, AIClient)
        # 启动只组装服务，不执行学校抓取或 AI 请求。
        coordinator = SyncCoordinator(database, SOURCES, tasks=tasks, jwc_auth=jwc_auth)
        app = create_app(
            database, coordinator, config_store, enrichment=enrichment,
            lifecycle=lifecycle, search_service=search_service,
        )
        # 教务处登录（Playwright）和 AI 请求都在请求线程里阻塞，线程太少时心跳
        # 请求会排队等待，反而被判成"页面没了"。
        # 显式拥有 map 和 dispatcher，bind 或工厂构造中途失败也能清理。
        dispatcher = ThreadedTaskDispatcher()
        dispatcher.set_thread_count(8)
        server = create_server(
            app, host=HOST, port=PORT, threads=8, map=socket_map, _dispatcher=dispatcher,
        )
        server_failure = {}

        def run_server() -> None:
            try:
                # server.run() 只在 map 变空时退出；保留 keep-alive channel 时
                # 会继续运行。由本应用拥有停止条件和有界的 select 等待。
                while not server_stop.is_set() and socket_map:
                    wasyncore.loop(timeout=0.25, map=socket_map, count=1)
            except BaseException as exc:
                server_failure["exception"] = exc
                _log_event(
                    f"本地 web 服务线程异常: {type(exc).__name__}: {str(exc)[:300]}",
                    exc_info=True,
                )

        server_thread = threading.Thread(target=run_server, name="local-web-server", daemon=True)

        def request_exit(_signum=None, _frame=None) -> None:
            lifecycle.request_exit(f"收到信号 {_signum}" if _signum else None)

        for signum in (signal.SIGINT, signal.SIGTERM):
            old_signals[signum] = signal.signal(signum, request_exit)

        server_thread.start()
        _launch_browser(url)
        _log_event(f"启动 (pid {os.getpid()}, {url})")
        while True:
            if lifecycle.poll():
                reason = str(lifecycle.exit_reason or "收到退出请求")
                break
            if not server_thread.is_alive():
                failure = server_failure.get("exception")
                reason = (
                    f"本地 web 服务线程异常: {type(failure).__name__}: {str(failure)[:300]}"
                    if failure is not None else "本地 web 服务线程已停止"
                )
                result = 1
                break
            time.sleep(0.25)
    except KeyboardInterrupt:
        reason = "键盘中断"
    except BaseException as exc:
        reason = f"启动或主循环异常: {type(exc).__name__}: {str(exc)[:300]}"
        _log_event(reason, exc_info=True)
        raise
    finally:
        _log_event(f"退出: {reason}")
        try:
            if lifecycle is not None:
                lifecycle.shutdown()
            else:
                try:
                    close_resources()
                finally:
                    lock.release()
        finally:
            try:
                for signum, handler in old_signals.items():
                    signal.signal(signum, handler)
            finally:
                _close_event_logger()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
