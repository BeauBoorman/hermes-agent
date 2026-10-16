"""Poison-event requeue bound: a watch event whose injection keeps failing must be
dropped after _WATCH_REQUEUE_MAX_ATTEMPTS requeues, not requeued forever.
Regression test for the 261016 self-injection flood (14.5k injections / 8h).

Uses a real GatewayRunner (constructed like test_multiplex_routing_authz.py) with
only _inject_watch_notification stubbed, so the drain path runs with its full
sibling machinery.
"""
import asyncio
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.run_notifications import _WATCH_REQUEUE_MAX_ATTEMPTS  # noqa: E402


def _evt():
    return {
        "type": "watch_match", "pattern": "x", "output": "y",
        "session_id": "proc_test", "session_key": "agent:infra:discord:thread:t:t",
        "platform": "discord", "chat_id": "1560555402180169789",
        "thread_id": "1560555402180169789", "user_id": "u", "user_name": "n",
        "message_id": "m", "scope_id": "1519593231610937414",
        "owner_task_id": "t1", "task_id": "t1", "command": "true",
    }


def _runner(inject_result, home):
    runner = GatewayRunner(GatewayConfig(sessions_dir=Path(home) / "sessions"))
    runner._inject_ret = inject_result

    async def _stub(text, evt, **kw):
        return runner._inject_ret

    runner._inject_watch_notification = _stub
    return runner


class WatchRequeueBoundTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.mkdtemp(prefix="watch-requeue-")

    def test_poison_event_dropped_after_bound(self):
        runner = _runner(False, self._home)  # delivery always refused — the poison condition
        q = queue.Queue()
        q.put(_evt())
        try:
            # Drain repeatedly at the watcher cadence; pre-fix code never empties q.
            for _ in range(_WATCH_REQUEUE_MAX_ATTEMPTS + 2):
                asyncio.run(runner._drain_watch_notifications(q))
            self.assertTrue(q.empty(), "poison watch event must be dropped after bounded retries")
        finally:
            runner.close_all_session_db_handles()

    def test_delivered_event_not_requeued(self):
        runner = _runner(True, self._home)
        q = queue.Queue()
        q.put(_evt())
        try:
            asyncio.run(runner._drain_watch_notifications(q))
            self.assertTrue(q.empty())
        finally:
            runner.close_all_session_db_handles()


if __name__ == "__main__":
    unittest.main()
