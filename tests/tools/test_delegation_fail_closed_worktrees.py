"""Fail-closed worktree isolation (261014 fart-off postmortem, Phase 0).

delegation.worktree_isolation_required:
  never        — legacy silent degrade to the shared workspace.
  when_writing / always — a child that cannot get an isolated worktree FAILS
                 BEFORE running (WorktreeIsolationError), never silently shares.

Acceptance proof per the repair plan: force worktree creation to fail and prove
the batch rejects before either child runs; reproduce the old shared-checkout
degrade as the documented legacy path.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tools import subagent_worktree as sw
from tools.delegate_tool_child_run import WorktreeIsolationError, _create_isolated_worktree


class _FakeAgent:
    """Minimal parent-agent double: only the workspace hint is consulted."""

    def __init__(self, cwd: str):
        self._hint_cwd = cwd


def _run_in(agent):
    """_create_isolated_worktree with the workspace hint pinned to the agent's cwd."""
    with mock.patch("agent.runtime_cwd.scope_terminal_cwd", return_value=agent._hint_cwd):
        return _create_isolated_worktree(agent, parent_task_id=None, subagent_id="t")


def _make_repo(root: Path) -> Path:
    import subprocess as sp

    def git(args, cwd):
        return sp.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)

    repo = root / "repo"
    repo.mkdir()
    git(["init", "-q"], repo)
    git(["config", "user.email", "t@t"], repo)
    git(["config", "user.name", "t"], repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    git(["add", "-A"], repo)
    git(["commit", "-q", "-m", "seed"], repo)
    return repo


class FailClosedWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hermes-failclosed-wt-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = _make_repo(self.tmp)
        self.agent = _FakeAgent(str(self.repo))

    def _run(self):
        # Pin the workspace hint to the fake agent's cwd: scope_terminal_cwd()
        # (consulted first in production) points at the test runner's checkout here.
        return _run_in(self.agent)

    # ── legacy mode stays legacy ────────────────────────────────────────

    def test_never_mode_degrades_silently_on_failure(self):
        with mock.patch("tools.delegate_tool_config._cfg", return_value={
            "worktree_isolation": True, "worktree_isolation_required": "never"}), \
             mock.patch.object(sw, "create_subagent_worktree", return_value=None):
            # Red trace of the OLD behavior: creation refuses, child still gets None
            # (shared workspace). Documented, not desired — but must not regress.
            self.assertIsNone(self._run())

    def test_isolation_off_entirely_returns_none(self):
        with mock.patch("tools.delegate_tool_config._cfg", return_value={
            "worktree_isolation": False, "worktree_isolation_required": "always"}):
            self.assertIsNone(self._run())

    # ── fail-closed modes ──────────────────────────────────────────────

    def test_creation_refusal_fails_closed_when_writing(self):
        with mock.patch("tools.delegate_tool_config._cfg", return_value={
            "worktree_isolation": True, "worktree_isolation_required": "when_writing"}), \
             mock.patch.object(sw, "create_subagent_worktree", return_value=None):
            with self.assertRaises(WorktreeIsolationError):
                self._run()

    def test_creation_exception_fails_closed_when_writing(self):
        with mock.patch("tools.delegate_tool_config._cfg", return_value={
            "worktree_isolation": True, "worktree_isolation_required": "when_writing"}), \
             mock.patch.object(sw, "create_subagent_worktree", side_effect=RuntimeError("boom")):
            with self.assertRaises(WorktreeIsolationError):
                self._run()

    def test_non_local_backend_fails_closed_when_required(self):
        with mock.patch("tools.delegate_tool_config._cfg", return_value={
            "worktree_isolation": True, "worktree_isolation_required": "always"}), \
             mock.patch.object(sw, "local_backend_active", return_value=False):
            with self.assertRaises(WorktreeIsolationError):
                self._run()

    def test_non_local_backend_degrades_in_legacy(self):
        with mock.patch("tools.delegate_tool_config._cfg", return_value={
            "worktree_isolation": True, "worktree_isolation_required": "never"}), \
             mock.patch.object(sw, "local_backend_active", return_value=False):
            self.assertIsNone(self._run())

    def test_outside_git_always_mode_fails_closed(self):
        plain = self.tmp / "notarepo"
        plain.mkdir()
        agent = _FakeAgent(str(plain))
        with mock.patch("tools.delegate_tool_config._cfg", return_value={
            "worktree_isolation": True, "worktree_isolation_required": "always"}):
            with self.assertRaises(WorktreeIsolationError):
                _run_in(agent)

    def test_outside_git_when_writing_is_not_an_error(self):
        # No repo = no shared checkout to corrupt: isolation trivially satisfied.
        plain = self.tmp / "notarepo2"
        plain.mkdir()
        agent = _FakeAgent(str(plain))
        with mock.patch("tools.delegate_tool_config._cfg", return_value={
            "worktree_isolation": True, "worktree_isolation_required": "when_writing"}):
            self.assertIsNone(_run_in(agent))

    def test_success_returns_worktree_info(self):
        with mock.patch("tools.delegate_tool_config._cfg", return_value={
            "worktree_isolation": True, "worktree_isolation_required": "when_writing"}):
            info = self._run()
        self.assertIsNotNone(info)
        import os as _os
        self.assertTrue(_os.path.realpath(info["path"]).startswith(_os.path.realpath(str(self.repo))))
        self.assertTrue(info["branch"].startswith("hermes-subagent/"))
        self.assertRegex(info["base_commit"], r"^[0-9a-f]{7,40}$")
        # cleanup the worktree we created
        sw.finalize_subagent_worktree(info, prune=True)

    def test_bad_mode_value_falls_back_to_never(self):
        from tools.delegate_tool_config import _get_worktree_isolation_required
        with mock.patch("tools.delegate_tool_config._cfg", return_value={
            "worktree_isolation_required": "sometimes"}):
            self.assertEqual(_get_worktree_isolation_required(), "never")


if __name__ == "__main__":
    unittest.main()
