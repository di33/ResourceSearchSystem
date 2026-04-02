import asyncio
import heapq
import logging
from typing import Any, Callable, Coroutine, List, Tuple

logging.basicConfig(level=logging.INFO)

class TaskManager:
    def __init__(self, max_retries: int = 3, timeout: int = 10):
        self.task_queue: List[Tuple[int, int, Callable[..., Coroutine[Any, Any, Any]]]] = []
        self.max_retries = max_retries
        self.timeout = timeout
        self.task_counter = 0

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
        """
        Execute a single task with retries and timeout.

        Args:
            task (Callable[..., Coroutine[Any, Any, Any]]): The coroutine function to execute.
        """
        retries = 0
        while retries <= self.max_retries:
            try:
                await asyncio.wait_for(task(), timeout=self.timeout)
                logging.info("Task completed successfully.")
                return
            except asyncio.TimeoutError:
                logging.warning("Task timed out. Retrying...")
            except Exception as e:
                logging.warning(f"Task failed with error: {e}. Retrying...")
            retries += 1
        logging.error("Task failed after maximum retries.")

    async def run(self):
        """
        Run tasks from the queue based on priority.
        """
        while self.task_queue:
            _, _, task = heapq.heappop(self.task_queue)
            await self._execute_task(task)

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