import asyncio
import heapq
import logging
from typing import Any, Callable, Coroutine, List, Tuple

logging.basicConfig(level=logging.INFO)


import time
from collections import deque

class TaskManager:
    def __init__(self, max_retries: int = 3, timeout: int = 10, max_concurrent_tasks: int = 5):
        self.task_queue: List[Tuple[int, int, Callable[..., Coroutine[Any, Any, Any]]]] = []
        self.max_retries = max_retries
        self.timeout = timeout
        self.task_counter = 0
        self.max_concurrent_tasks = max_concurrent_tasks
        self._running_tasks = set()
        self._completed_tasks = 0
        self._failed_tasks = 0
        self._task_times = deque(maxlen=1000)  # 记录最近1000个任务耗时

    def set_max_concurrent_tasks(self, n: int):
        self.max_concurrent_tasks = n

    def get_performance_metrics(self):
        avg_time = sum(self._task_times) / len(self._task_times) if self._task_times else 0
        return {
            'queue_length': len(self.task_queue),
            'running_tasks': len(self._running_tasks),
            'completed_tasks': self._completed_tasks,
            'failed_tasks': self._failed_tasks,
            'average_task_time': avg_time
        }

    def add_task(self, priority: int, task: Callable[..., Coroutine[Any, Any, Any]]):
        """
        Add a task to the queue with a given priority.

        Args:
            priority (int): The priority of the task (lower value = higher priority).
            task (Callable[..., Coroutine[Any, Any, Any]]): The coroutine function to execute.
        """
        heapq.heappush(self.task_queue, (priority, self.task_counter, task))
        self.task_counter += 1

    async def _execute_task(self, task: Callable[..., Coroutine[Any, Any, Any]]):
        retries = 0
        start_time = time.time()
        try:
            self._running_tasks.add(task)
            while retries <= self.max_retries:
                try:
                    await asyncio.wait_for(task(), timeout=self.timeout)
                    logging.info("Task completed successfully.")
                    self._completed_tasks += 1
                    break
                except asyncio.TimeoutError:
                    logging.warning("Task timed out. Retrying...")
                except Exception as e:
                    logging.warning(f"Task failed with error: {e}. Retrying...")
                retries += 1
            else:
                logging.error("Task failed after maximum retries.")
                self._failed_tasks += 1
        finally:
            elapsed = time.time() - start_time
            self._task_times.append(elapsed)
            self._running_tasks.discard(task)


    async def run(self):
        """
        Run tasks from the queue based on priority, respecting max_concurrent_tasks.
        """
        semaphore = asyncio.Semaphore(self.max_concurrent_tasks)

        async def sem_task(task):
            async with semaphore:
                await self._execute_task(task)

        tasks = []
        while self.task_queue:
            _, _, task = heapq.heappop(self.task_queue)
            tasks.append(asyncio.create_task(sem_task(task)))
        if tasks:
            await asyncio.gather(*tasks)

# Example usage
async def example_task():
    logging.info("Executing example task...")
    await asyncio.sleep(1)

if __name__ == "__main__":
    async def main():
        manager = TaskManager()
        manager.add_task(1, example_task)
        manager.add_task(2, example_task)
        await manager.run()

    asyncio.run(main())