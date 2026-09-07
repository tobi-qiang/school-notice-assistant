
import unittest
import threading
import time
import tempfile
import os
from unittest.mock import MagicMock
from unittest.mock import patch
import pytest
from app import lifecycle as lifecycle_module
from app.lifecycle import SingleInstanceLock, HeartbeatMonitor, ControlledTasks, LifecycleController


class FakeClock:
    def __init__(self, start_time=0):
        self.time = start_time
        
    def __call__(self):
        return self.time
        
    def advance(self, seconds):
        self.time += seconds


class TestSingleInstanceLock(unittest.TestCase):
    def test_lock_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = os.path.join(tmpdir, 'test.lock')
            lock = SingleInstanceLock(lock_path)
            
            # 第一次获取锁应该成功
            self.assertTrue(lock.acquire())
            self.assertTrue(lock.acquired)
            
            # 释放锁
            lock.release()
            self.assertFalse(lock.acquired)
            
    def test_lock_context_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = os.path.join(tmpdir, 'test.lock')
            
            # 使用上下文管理器
            with SingleInstanceLock(lock_path) as lock:
                self.assertTrue(lock.acquired)
                
            # 退出上下文后锁应该被释放
            self.assertFalse(lock.acquired)


class TestHeartbeatMonitor(unittest.TestCase):
    def test_heartbeat_and_disconnect(self):
        clock = FakeClock()
        monitor = HeartbeatMonitor(clock=clock)
        
        # 添加心跳
        monitor.heartbeat('client1')
        self.assertIn('client1', monitor.clients)
        
        # 断开连接
        monitor.disconnect('client1')
        self.assertNotIn('client1', monitor.clients)
        
    def test_should_exit_no_clients(self):
        clock = FakeClock()
        monitor = HeartbeatMonitor(grace_seconds=10, orphan_seconds=15, clock=clock)
        
        # 没有客户端，不应该退出
        self.assertFalse(monitor.should_exit())
        
    def test_should_exit_with_grace_period(self):
        clock = FakeClock()
        monitor = HeartbeatMonitor(grace_seconds=10, orphan_seconds=15, clock=clock)
        
        # 添加客户端心跳
        monitor.heartbeat('client1')
        self.assertFalse(monitor.should_exit())
        
        # 移除客户端
        monitor.disconnect('client1')
        
        # 在宽限期内，不应该退出
        clock.advance(5)
        self.assertFalse(monitor.should_exit())
        
        # 超过宽限期，应该退出
        clock.advance(6)  # 总共11秒，超过10秒的宽限期
        self.assertTrue(monitor.should_exit())
        
    def test_request_exit_immediate(self):
        clock = FakeClock()
        monitor = HeartbeatMonitor(clock=clock)

        # 请求退出应该立即生效
        monitor.request_exit()
        self.assertTrue(monitor.should_exit())

    @staticmethod
    def poll_for(monitor, clock, seconds, step=1):
        '''按主循环的节奏推进时钟，返回最后一次判定结果'''
        result = False
        remaining = seconds
        while remaining > 0:
            clock.advance(min(step, remaining))
            remaining -= step
            result = monitor.should_exit()
        return result

    def test_throttled_background_tab_keeps_app_alive(self):
        '''浏览器把后台标签页的心跳降频到 1 分钟一次时不能退出'''
        clock = FakeClock()
        monitor = HeartbeatMonitor(grace_seconds=10, orphan_seconds=86400, clock=clock)

        monitor.heartbeat('client1')
        for _ in range(5):
            self.assertFalse(self.poll_for(monitor, clock, 60))
            monitor.heartbeat('client1')

        # 即使十分钟没有心跳，也不能把可能冻结的后台页当成已关闭。
        self.assertFalse(self.poll_for(monitor, clock, 600))

        # 真正关闭会明确 disconnect，仍在十秒宽限后退出。
        monitor.disconnect('client1')
        self.assertTrue(self.poll_for(monitor, clock, 11))

    def test_browser_frozen_client_does_not_expire(self):
        clock = FakeClock()
        monitor = HeartbeatMonitor(grace_seconds=10, orphan_seconds=180, clock=clock)

        monitor.heartbeat('client1')
        monitor.suspend('client1')
        self.assertFalse(self.poll_for(monitor, clock, 600))
        self.assertIn('client1', monitor.suspended_clients)

        monitor.heartbeat('client1')
        self.assertNotIn('client1', monitor.suspended_clients)

    def test_one_tab_disconnect_does_not_remove_other_tab(self):
        clock = FakeClock()
        monitor = HeartbeatMonitor(grace_seconds=10, orphan_seconds=180, clock=clock)

        monitor.heartbeat('tab-1')
        monitor.heartbeat('tab-2')
        monitor.disconnect('tab-1')
        self.assertFalse(self.poll_for(monitor, clock, 20))
        self.assertIn('tab-2', monitor.clients)

        monitor.disconnect('tab-2')
        self.assertTrue(self.poll_for(monitor, clock, 11))

    def test_system_suspend_does_not_expire_clients(self):
        '''整机休眠期间的时间不算心跳过期'''
        clock = FakeClock()
        monitor = HeartbeatMonitor(
            grace_seconds=10, orphan_seconds=180, suspend_gap_seconds=30, clock=clock
        )

        monitor.heartbeat('client1')
        self.assertFalse(monitor.should_exit())

        # 合盖休眠一小时后唤醒：轮询停摆了 3600 秒，不应立刻退出
        clock.advance(3600)
        self.assertFalse(monitor.should_exit())

        # 唤醒后页面仍然不发心跳，才按正常超时退出
        self.assertTrue(self.poll_for(monitor, clock, 200))

    def test_reload_disconnect_then_heartbeat_cancels_grace(self):
        '''刷新页面时 pagehide 会上报断开，新页面的心跳要能取消宽限倒计时'''
        clock = FakeClock()
        monitor = HeartbeatMonitor(grace_seconds=10, orphan_seconds=180, clock=clock)

        monitor.heartbeat('client1')
        self.assertFalse(monitor.should_exit())

        monitor.disconnect('client1')
        clock.advance(2)
        monitor.heartbeat('client1')

        self.assertFalse(self.poll_for(monitor, clock, 30))

    def test_exit_reason_explains_shutdown(self):
        clock = FakeClock()
        monitor = HeartbeatMonitor(grace_seconds=10, orphan_seconds=180, clock=clock)

        monitor.heartbeat('client1')
        monitor.disconnect('client1')
        self.assertTrue(self.poll_for(monitor, clock, 15))
        self.assertIn('页面已关闭', monitor.exit_reason)


class TestControlledTasks(unittest.TestCase):
    def test_start_and_status(self):
        tasks = ControlledTasks()
        
        def task_target(cancel_event):
            # 简单的任务函数
            time.sleep(0.1)
            
        # 启动任务
        result = tasks.start('test_task', task_target)
        self.assertTrue(result)
        
        # 检查任务状态
        status = tasks.status('test_task')
        self.assertIsNotNone(status)
        self.assertTrue(status['active'] or status['error'] is not None)
        
    def test_cancel_task(self):
        tasks = ControlledTasks()
        
        def task_target(cancel_event):
            # 任务会检查取消事件
            while not cancel_event.is_set():
                time.sleep(0.01)
                
        # 启动任务
        tasks.start('test_task', task_target)
        
        # 取消任务
        result = tasks.cancel('test_task')
        self.assertTrue(result)
        
    def test_task_error_capture(self):
        tasks = ControlledTasks()
        
        def failing_task(cancel_event):
            raise ValueError('Test error')
            
        # 启动会失败的任务
        tasks.start('failing_task', failing_task)
        
        # 等待任务完成
        time.sleep(0.1)
        
        # 检查错误是否被捕获
        status = tasks.status('failing_task')
        self.assertIsNotNone(status)
        self.assertIn('Test error', status['error'])


class TestLifecycleController(unittest.TestCase):
    def test_poll_without_exit(self):
        lock = MagicMock()
        heartbeat = MagicMock()
        heartbeat.should_exit.return_value = False
        tasks = MagicMock()
        
        controller = LifecycleController(lock, heartbeat, tasks)
        
        # 不应该退出
        self.assertFalse(controller.poll())
        
    def test_poll_with_heartbeat_exit(self):
        lock = MagicMock()
        heartbeat = MagicMock()
        heartbeat.should_exit.return_value = True
        tasks = MagicMock()
        
        controller = LifecycleController(lock, heartbeat, tasks)
        
        # 心跳监控器要求退出，应该返回 True
        self.assertTrue(controller.poll())
        
        # 再次检查也应该返回 True
        self.assertTrue(controller.poll())
        
    def test_request_exit(self):
        lock = MagicMock()
        heartbeat = MagicMock()
        heartbeat.should_exit.return_value = False
        tasks = ControlledTasks()
        
        controller = LifecycleController(lock, heartbeat, tasks)
        
        # 请求退出
        controller.request_exit()
        
        # 应该立即返回 True
        self.assertTrue(controller.poll())
        
    def test_shutdown(self):
        lock = MagicMock()
        lock.acquired = True
        heartbeat = MagicMock()
        tasks = MagicMock()
        on_shutdown = MagicMock()
        
        controller = LifecycleController(lock, heartbeat, tasks, on_shutdown)
        
        # 执行关闭
        controller.shutdown()
        
        # 验证调用了相应的方法
        tasks.cancel_all.assert_called_once()
        tasks.join_all.assert_called_once_with(timeout=5.0)
        on_shutdown.assert_called_once()
        lock.release.assert_called_once()


if __name__ == '__main__':
    unittest.main()


def test_unlock_error_still_closes_descriptor_and_allows_reacquire(tmp_path, monkeypatch):
    lock = SingleInstanceLock(tmp_path / 'app.lock')
    assert lock.acquire()
    descriptor = lock.fp
    def fail(*args):
        raise OSError('synthetic unlock failure')
    with monkeypatch.context() as scoped:
        if lifecycle_module.PLATFORM_WINDOWS:
            scoped.setattr(lifecycle_module.msvcrt, 'locking', fail)
        else:
            scoped.setattr(lifecycle_module.fcntl, 'flock', fail)
        lock.release()
    assert descriptor.closed
    assert not lock.acquired and lock.fp is None
    contender = SingleInstanceLock(tmp_path / 'app.lock')
    try:
        assert contender.acquire()
    finally:
        contender.release()


def test_lock_keeps_stable_file_and_excludes_contenders(tmp_path):
    path = tmp_path / 'app.lock'
    path.write_bytes(b'unchanged')
    inode = path.stat().st_ino
    first, second, third = (SingleInstanceLock(path) for _ in range(3))
    try:
        assert first.acquire()
        assert not second.acquire()
        first.release()
        assert path.exists()
        assert path.read_bytes() == b'unchanged'
        assert path.stat().st_ino == inode
        assert second.acquire()
        assert not third.acquire()
    finally:
        first.release()
        second.release()
        third.release()


def test_lock_path_error_is_not_reported_as_another_instance(tmp_path):
    lock = SingleInstanceLock(tmp_path / 'missing-parent' / 'app.lock')
    with pytest.raises(FileNotFoundError):
        lock.acquire()
    assert lock.fp is None


@pytest.mark.parametrize('stage', ['cancel', 'join', 'callback'])
def test_shutdown_failure_still_runs_resource_cleanup_and_releases_lock(tmp_path, stage):
    lock = SingleInstanceLock(tmp_path / 'app.lock')
    assert lock.acquire()
    tasks, cleanup = MagicMock(), MagicMock()
    if stage == 'cancel':
        tasks.cancel_all.side_effect = RuntimeError('cleanup regression')
    elif stage == 'join':
        tasks.join_all.side_effect = RuntimeError('cleanup regression')
    else:
        cleanup.side_effect = RuntimeError('cleanup regression')
    controller = LifecycleController(lock, HeartbeatMonitor(), tasks, cleanup)
    with pytest.raises(RuntimeError, match='cleanup regression'):
        controller.shutdown()
    assert not lock.acquired
    cleanup.assert_called_once()
    controller.shutdown()
    cleanup.assert_called_once()


def test_exit_rejects_new_tasks_but_ordinary_cancel_all_does_not():
    tasks = ControlledTasks()
    tasks.cancel_all()
    assert tasks.start('before', lambda event: None)
    assert tasks.join_all(timeout=1)
    controller = LifecycleController(MagicMock(), HeartbeatMonitor(), tasks)
    controller.request_exit()
    assert not tasks.start('after', lambda event: None)


def test_join_all_uses_one_total_deadline():
    tasks = ControlledTasks()
    threads = [MagicMock(), MagicMock(), MagicMock()]
    for thread in threads:
        thread.is_alive.return_value = True
    tasks._tasks = {str(i): {'thread': t} for i, t in enumerate(threads)}
    with patch.object(lifecycle_module.time, 'monotonic', side_effect=[10, 10, 14, 16]):
        assert tasks.join_all(timeout=5) is False
    assert [t.join.call_args.args[0] for t in threads] == [5, 1, 0]


def test_task_can_request_shutdown_without_joining_itself():
    tasks = ControlledTasks()
    finished = threading.Event()
    errors = []
    def target(event):
        try:
            assert tasks.join_all(timeout=0.1) is False
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    assert tasks.start('self', target)
    assert finished.wait(2)
    assert tasks.join_all(timeout=2)
    assert not errors


def test_concurrent_shutdown_cleans_up_only_once():
    entered, release = threading.Event(), threading.Event()
    cleanup = MagicMock(side_effect=lambda: (entered.set(), release.wait(2)))
    lock = MagicMock()
    controller = LifecycleController(lock, HeartbeatMonitor(), ControlledTasks(), cleanup)
    threads = [threading.Thread(target=controller.shutdown) for _ in range(2)]
    try:
        threads[0].start()
        assert entered.wait(2)
        threads[1].start()
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(2)
    cleanup.assert_called_once()
    lock.release.assert_called_once()
