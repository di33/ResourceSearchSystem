import asyncio
import unittest
from task_manager import TaskManager

class TestTaskManager(unittest.TestCase):
    def setUp(self):
        self.manager = TaskManager(max_retries=2, timeout=2)

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