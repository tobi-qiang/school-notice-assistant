import threading
import time
import os
import sys

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
            
        try:
            # 创建锁文件
            self.fp = open(self.path, 'w')
            
            if PLATFORM_WINDOWS:
                # Windows 使用 msvcrt.locking 锁定第一个字节
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                # Unix/Linux 使用 fcntl
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                
            self.acquired = True
            return True
        except (IOError, OSError):
            # 获取锁失败，关闭文件
            if self.fp:
                self.fp.close()
                self.fp = None
            return False
            
    def release(self):
        '''释放锁'''
        if not self.acquired:
            return
            
        try:
            if PLATFORM_WINDOWS:
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
                
            self.fp.close()
            self.fp = None
            self.acquired = False
            
            # 删除锁文件
            try:
                os.remove(self.path)
            except OSError:
                pass  # 忽略删除失败
        except (IOError, OSError):
            pass  # 忽略解锁失败
            
    def __enter__(self):
        if self.acquire():
            return self
        else:
            raise RuntimeError('Failed to acquire lock')
            
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class HeartbeatMonitor:
    '''心跳监控器'''
    
    def __init__(self, grace_seconds=10, stale_seconds=15, clock=time.monotonic):
        self.grace_seconds = grace_seconds
        self.stale_seconds = stale_seconds
        self.clock = clock
        
        self.clients = {}  # client_id -> last_heartbeat_time
        self._lock = threading.RLock()
        self._exit_requested = False
        self._had_clients = False  # 是否曾经有过客户端
        
    def heartbeat(self, client_id):
        '''接收客户端心跳'''
        with self._lock:
            self.clients[client_id] = self.clock()
            self._had_clients = True
            
    def disconnect(self, client_id):
        '''客户端断开连接'''
        with self._lock:
            self.clients.pop(client_id, None)
            if self._had_clients and not self.clients:
                self._grace_start = self.clock()
            
    def request_exit(self):
        '''请求退出'''
        with self._lock:
            self._exit_requested = True
            
    def should_exit(self):
        '''判断是否应该退出'''
        with self._lock:
            # 如果已显式请求退出，立即返回 True
            if self._exit_requested:
                return True
                
            now = self.clock()
            
            # 清理过期的客户端
            expired_clients = [
                client_id for client_id, last_time in self.clients.items()
                if (now - last_time) > self.stale_seconds
            ]
            
            for client_id in expired_clients:
                del self.clients[client_id]
                
            # 检查是否所有客户端都已消失/过期
            if not self.clients:
                # 如果从未有过客户端，则不启动宽限时间
                if not self._had_clients:
                    return False
                    
                # 设置宽限时间起点（第一次发现无客户端）
                if not hasattr(self, '_grace_start'):
                    self._grace_start = now
                    
                # 检查宽限时间是否已过
                return (now - self._grace_start) > self.grace_seconds
            else:
                # 还有活跃客户端，重置宽限时间起点
                if hasattr(self, '_grace_start'):
                    delattr(self, '_grace_start')
                    
            return False


class ControlledTasks:
    '''可控任务管理器'''
    
    def __init__(self):
        self._lock = threading.RLock()
        self._tasks = {}  # name -> {'thread', 'cancel_event', 'error'}
        
    def start(self, name, target):
        '''启动一个受控任务'''
        with self._lock:
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
                
    def join_all(self, timeout=None):
        '''等待所有任务完成'''
        with self._lock:
            threads = [task_info['thread'] for task_info in self._tasks.values()]
            
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout)
                
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
        
    def request_exit(self):
        '''请求退出'''
        self._shutdown_requested = True
        self.tasks.cancel_all()
        
    def poll(self):
        '''轮询检查是否应该终止'''
        # 如果已请求退出，立即返回 True
        if self._shutdown_requested:
            return True
            
        # 检查心跳监控器是否要求退出
        if self.heartbeat.should_exit():
            self.request_exit()
            return True
            
        return False
        
    def shutdown(self):
        '''执行关闭操作'''
        if self._shutdown_completed:
            return
            
        # 取消所有任务
        self.tasks.cancel_all()
        
        # 等待任务完成（有限时间）
        self.tasks.join_all(timeout=5.0)
        
        # 调用关闭回调
        if self.on_shutdown:
            try:
                self.on_shutdown()
            except Exception:
                pass  # 忽略关闭回调中的错误
                
        # 释放锁
        if self.lock.acquired:
            self.lock.release()
            
        self._shutdown_completed = True
