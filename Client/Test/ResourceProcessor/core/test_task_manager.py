import asyncio
import unittest
from ResourceProcessor.core.task_manager import TaskManager
class TestTaskManager(unittest.TestCase):

    def setUp(self):
        self.manager = TaskManager(max_retries=2, timeout=2)

    def test_max_concurrent_tasks(self):
        """
        测试最大并发任务数设置。
        """
        import time
        results = []
        async def slow_task(idx):
            results.append((idx, 'start', time.time()))
            await asyncio.sleep(0.5)
            results.append((idx, 'end', time.time()))
        async def test():
            self.manager.set_max_concurrent_tasks(2)
            for i in range(4):
                self.manager.add_task(1, lambda i=i: slow_task(i))
            start = time.time()
            await self.manager.run()
            end = time.time()
            # 4个任务，2并发，耗时应大于1s小于2s
            self.assertGreaterEqual(end - start, 1.0)
            self.assertLess(end - start, 2.5)
        asyncio.run(test())

    def test_performance_metrics(self):
        """
        测试性能监控接口。
        """
        async def dummy_task():
            await asyncio.sleep(0.1)
        async def test():
            self.manager.set_max_concurrent_tasks(1)
            for _ in range(3):
                self.manager.add_task(1, dummy_task)
            await self.manager.run()
            metrics = self.manager.get_performance_metrics()
            self.assertEqual(metrics['completed_tasks'], 3)
            self.assertEqual(metrics['failed_tasks'], 0)
            self.assertEqual(metrics['queue_length'], 0)
            self.assertGreater(metrics['average_task_time'], 0)
        asyncio.run(test())

    async def successful_task(self):
        await asyncio.sleep(0.5)

    async def failing_task(self):
        raise ValueError("Intentional failure.")

    async def slow_task(self):
        await asyncio.sleep(3)

    def test_successful_task(self):
        async def test():
            self.manager.add_task(1, self.successful_task)
            await self.manager.run()
        asyncio.run(test())

    def test_failing_task(self):
        async def test():
            self.manager.add_task(1, self.failing_task)
            await self.manager.run()
        asyncio.run(test())

    def test_slow_task(self):
        async def test():
            self.manager.add_task(1, self.slow_task)
            await self.manager.run()
        asyncio.run(test())

if __name__ == "__main__":
    unittest.main()