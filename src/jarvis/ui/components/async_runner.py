"""Runs an asyncio event loop on a background thread so agent work never blocks Qt.

Coroutines are submitted from the Qt thread and return concurrent.futures.Future objects.
Results get back to the UI via Qt signals emitted from the worker thread; Qt queues them
onto the receiver's (main) thread automatically.
"""

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from typing import Any


class AsyncRunner:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="jarvis-agent", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self, timeout: float = 5.0) -> None:
        if not self.loop.is_running():
            return

        async def _shutdown() -> None:
            current = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not current]
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        try:
            self.submit(_shutdown()).result(timeout)
        except (concurrent.futures.TimeoutError, RuntimeError):
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout)
