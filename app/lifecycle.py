import threading
import time
import logging
import errno

# Platform-specific imports
try:
    import msvcrt
    PLATFORM_WINDOWS = True
except ImportError:
    import fcntl
    PLATFORM_WINDOWS = False


class SingleInstanceLock:
    '''确保程序单实例运行的锁'''
    
    def __init__(self, path):
        self.path = path
        self.fp = None
        self.acquired = False
        
    def acquire(self):
        '''尝试获取锁'''
        if self.acquired:
            return True
            
        # 文件路径/权限错误不代表已有实例，必须交给调用方报告。
        # 保留同一个文件：删除/重建会让并发启动者锁住不同 inode。
        self.fp = open(self.path, 'a+b')
        try:
            self.fp.seek(0)
            
            if PLATFORM_WINDOWS:
                # Windows 使用 msvcrt.locking 锁定第一个字节
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                # Unix/Linux 使用 fcntl
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                
            self.acquired = True
            return True
        except OSError as exc:
            # 获取锁失败，关闭文件
            if self.fp:
                self.fp.close()
                self.fp = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
            
    def release(self):
        '''释放锁'''
        if not self.acquired:
            return
            
        try:
            self.fp.seek(0)
            if PLATFORM_WINDOWS:
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
                
        except (IOError, OSError):
            pass  # close 仍会释放操作系统持有的锁。
        finally:
            try:
                self.fp.close()
            finally:
                self.fp = None
                self.acquired = False
            
    def __enter__(self):
        if self.acquire():
            return self
        else:
            raise RuntimeError('Failed to acquire lock')
            
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class HeartbeatMonitor:
    '''心跳监控器'''

    def __init__(self, grace_seconds=10, orphan_seconds=86400, suspend_gap_seconds=30,
                 clock=time.monotonic):
        self.grace_seconds = grace_seconds
        # 心跳缺失不能证明标签页已经关闭：浏览器可能把后台页冻结数小时。
        # 这里只清理从未报告过“暂停”的超长期孤儿；正常关闭依赖 disconnect。
        self.orphan_seconds = orphan_seconds
        # 主循环每 0.25 秒轮询一次。若两次轮询相隔过久，说明整机睡眠/休眠过，
        # 这段时间浏览器根本没机会发心跳，不能算作"页面已经没了"。
        self.suspend_gap_seconds = suspend_gap_seconds
        self.clock = clock

        self.clients = {}  # client_id -> last_heartbeat_time
        self.suspended_clients = set()
        self._lock = threading.RLock()
        self._exit_requested = False
        self._had_clients = False  # 是否曾经有过客户端
        self._grace_start = None   # 最近一次"无客户端"的起点
        self._grace_reason = None  # 进入宽限期的原因，用于日志
        self._last_check = None    # 上一次 should_exit 的时刻
        self.exit_reason = None

    def heartbeat(self, client_id):
        '''接收客户端心跳'''
        with self._lock:
            self.clients[client_id] = self.clock()
            self.suspended_clients.discard(client_id)
            self._had_clients = True
            self._grace_start = None
            self._grace_reason = None

    def suspend(self, client_id):
        '''登记仍存在但被浏览器冻结/bfcache 暂停的客户端'''
        with self._lock:
            self.clients[client_id] = self.clock()
            self.suspended_clients.add(client_id)
            self._had_clients = True
            self._grace_start = None
            self._grace_reason = None

    def disconnect(self, client_id):
        '''客户端断开连接'''
        with self._lock:
            self.clients.pop(client_id, None)
            self.suspended_clients.discard(client_id)
            if self._had_clients and not self.clients:
                self._grace_start = self.clock()
                self._grace_reason = '页面已关闭'

    def request_exit(self):
        '''请求退出'''
        with self._lock:
            self._exit_requested = True
            if self.exit_reason is None:
                self.exit_reason = '收到退出请求'

    def _absorb_suspend(self, now):
        '''把系统休眠掉的时间补偿掉，避免唤醒后立刻自杀'''
        last, self._last_check = self._last_check, now
        if last is None:
            return
        gap = now - last
        if gap < self.suspend_gap_seconds:
            return
        for client_id in list(self.clients):
            self.clients[client_id] += gap
        if self._grace_start is not None:
            self._grace_start += gap

    def should_exit(self):
        '''判断是否应该退出'''
        with self._lock:
            # 如果已显式请求退出，立即返回 True
            if self._exit_requested:
                return True

            now = self.clock()
            self._absorb_suspend(now)

            # 只清理超长期失联且未明确暂停的孤儿。冻结/bfcache 页面可能完全不运行
            # JavaScript，不能因没有心跳就当成已经关闭。
            expired_clients = [
                client_id for client_id, last_time in self.clients.items()
                if client_id not in self.suspended_clients
                and (now - last_time) > self.orphan_seconds
            ]

            for client_id in expired_clients:
                del self.clients[client_id]
                self.suspended_clients.discard(client_id)

            # 还有活跃客户端，重置宽限时间起点
            if self.clients:
                self._grace_start = None
                self._grace_reason = None
                return False

            # 如果从未有过客户端，则不启动宽限时间
            if not self._had_clients:
                return False

            # 设置宽限时间起点（第一次发现无客户端）
            if self._grace_start is None:
                self._grace_start = now
                self._grace_reason = f'{self.orphan_seconds:.0f} 秒内没有收到页面心跳'

            # 检查宽限时间是否已过
            if (now - self._grace_start) > self.grace_seconds:
                self.exit_reason = (
                    f'{self._grace_reason}，宽限 {self.grace_seconds:.0f} 秒后退出'
                )
                return True
            return False


class ControlledTasks:
    '''可控任务管理器'''
    
    def __init__(self):
        self._lock = threading.RLock()
        self._tasks = {}  # name -> {'thread', 'cancel_event', 'error'}
        self._closing = False
        
    def start(self, name, target):
        '''启动一个受控任务'''
        with self._lock:
            if self._closing:
                return False
            # 检查是否有同名活跃任务
            if name in self._tasks and self._tasks[name]['thread'].is_alive():
                return False
                
            # 创建取消事件
            cancel_event = threading.Event()
            
            # 定义包装目标函数
            def wrapped_target():
                try:
                    target(cancel_event)
                except Exception as e:
                    # 捕获并记录简短错误信息
                    with self._lock:
                        if name in self._tasks:
                            self._tasks[name]['error'] = str(e)
                            
            # Register before starting so a very fast target cannot finish (or
            # start a follow-up task) before its own state exists.
            thread = threading.Thread(target=wrapped_target, daemon=True)
            self._tasks[name] = {
                'thread': thread,
                'cancel_event': cancel_event,
                'error': None
            }
            try:
                thread.start()
            except BaseException:
                self._tasks.pop(name, None)
                raise
            
            return True
            
    def cancel(self, name):
        '''取消指定任务'''
        with self._lock:
            if name in self._tasks:
                self._tasks[name]['cancel_event'].set()
                return True
            return False
            
    def cancel_all(self):
        '''取消所有任务'''
        with self._lock:
            for task_info in self._tasks.values():
                task_info['cancel_event'].set()

    def stop_accepting(self):
        '''退出时禁止新任务，避免取消后又有请求启动后台工作。'''
        with self._lock:
            self._closing = True
                
    def join_all(self, timeout=None):
        '''等待所有任务完成'''
        with self._lock:
            threads = [task_info['thread'] for task_info in self._tasks.values()]
            
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in threads:
            if thread.is_alive() and thread is not threading.current_thread():
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                thread.join(remaining)
        return all(not thread.is_alive() for thread in threads)
                
    def status(self, name):
        '''获取任务状态'''
        with self._lock:
            if name not in self._tasks:
                return None
                
            task_info = self._tasks[name]
            return {
                'active': task_info['thread'].is_alive(),
                'error': task_info['error']
            }


class LifecycleController:
    '''生命周期控制器'''
    
    def __init__(self, lock, heartbeat, tasks, on_shutdown=None):
        self.lock = lock
        self.heartbeat = heartbeat
        self.tasks = tasks
        self.on_shutdown = on_shutdown

        self._shutdown_requested = False
        self._shutdown_completed = False
        self._shutdown_lock = threading.RLock()
        self.exit_reason = None

    def request_exit(self, reason=None):
        '''请求退出'''
        if self.exit_reason is None:
            self.exit_reason = reason or '收到退出请求'
        self._shutdown_requested = True
        self.tasks.stop_accepting()
        self.tasks.cancel_all()

    def poll(self):
        '''轮询检查是否应该终止'''
        # 如果已请求退出，立即返回 True
        if self._shutdown_requested:
            return True

        # 检查心跳监控器是否要求退出
        if self.heartbeat.should_exit():
            self.request_exit(getattr(self.heartbeat, 'exit_reason', None))
            return True

        return False
        
    def shutdown(self):
        '''执行关闭操作'''
        with self._shutdown_lock:
            if self._shutdown_completed:
                return
            self._shutdown_completed = True
            try:
                try:
                    self.request_exit()
                    if self.tasks.join_all(timeout=5.0) is False:
                        logging.getLogger('school_notice_assistant.lifecycle').warning(
                            '退出等待超时，仍有受控任务未结束'
                        )
                finally:
                    if self.on_shutdown:
                        self.on_shutdown()
            finally:
                self.lock.release()
