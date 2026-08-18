
import unittest
import threading
import time
import tempfile
import os
from unittest.mock import MagicMock
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
        monitor = HeartbeatMonitor(grace_seconds=10, stale_seconds=15, clock=clock)
        
        # 没有客户端，不应该退出
        self.assertFalse(monitor.should_exit())
        
    def test_should_exit_with_grace_period(self):
        clock = FakeClock()
        monitor = HeartbeatMonitor(grace_seconds=10, stale_seconds=15, clock=clock)
        
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
