from __future__ import annotations

import unittest

from runtime.adapters.policy import (
    VERSION_CONTROL_MANAGER_ONLY_OPERATIONS,
    WORKER_DENIED_GIT_OPERATIONS,
    CommandPolicyViolation,
    assert_command_allowed,
    classify_command,
    enforce_command_policy,
)


class AdapterCommandPolicyTest(unittest.TestCase):
    def test_worker_allows_git_status_and_diff_inspection(self) -> None:
        for command in (
            ("git", "status", "--short"),
            ("git", "-C", ".", "diff", "--", "src/app.py"),
        ):
            with self.subTest(command=command):
                decision = enforce_command_policy(role="worker", command=command)

                self.assertTrue(decision.allowed)
                self.assertEqual(decision.decision, "allowed_git_read_only")

    def test_worker_allows_write_oriented_git_operations_by_default(self) -> None:
        commands = {
            "commit": ("git", "commit", "-m", "worker checkpoint"),
            "reset": ("git", "reset", "--hard", "HEAD"),
            "clean": ("git", "clean", "-fdx"),
            "checkout": ("git", "checkout", "main"),
            "switch": ("git", "switch", "main"),
            "branch -D": ("git", "branch", "-D", "old-topic"),
            "rebase": ("git", "rebase", "main"),
            "push": ("git", "push", "origin", "HEAD"),
            "update-ref": ("git", "update-ref", "refs/heads/main", "HEAD"),
            "tag": ("git", "tag", "v1"),
            "stash": ("git", "stash"),
            "gc": ("git", "gc"),
        }
        self.assertEqual(tuple(commands), WORKER_DENIED_GIT_OPERATIONS)

        for operation, command in commands.items():
            with self.subTest(operation=operation):
                decision = enforce_command_policy(role="worker", command=command)

                self.assertTrue(decision.allowed)
                self.assertEqual(decision.decision, "allowed_unattended_git_write")
                self.assertEqual(decision.operation, operation)

    def test_legacy_risky_approval_policy_blocks_worker_git_write(self) -> None:
        decision = enforce_command_policy(
            role="worker",
            command=("git", "commit", "-m", "worker checkpoint"),
            permission_policy={"require_approval_for_risky_commands": True},
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.decision, "blocked_git_write")
        self.assertEqual(decision.operation, "commit")

    def test_recovery_worker_allows_shell_wrapped_git_write_after_read_only_command(self) -> None:
        decision = enforce_command_policy(
            role="recovery_worker",
            command=("bash", "-lc", "git status --short && git commit -m nope"),
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.decision, "allowed_unattended_git_write")
        self.assertEqual(decision.operation, "commit")

    def test_worker_allows_loopplane_checkpoint_and_rollback_by_default(self) -> None:
        commands = (
            ("loopplane", "vc", "checkpoint", "--reason", "worker"),
            ("python3", "scripts/loopplane", "vc", "rollback", "--checkpoint", "cp_1"),
        )
        self.assertEqual(
            VERSION_CONTROL_MANAGER_ONLY_OPERATIONS,
            ("loopplane vc checkpoint", "loopplane vc rollback"),
        )

        for command in commands:
            with self.subTest(command=command):
                decision = enforce_command_policy(role="worker", command=command)

                self.assertTrue(decision.allowed)
                self.assertIn("allowed_unattended_loopplane_vc_", decision.decision)

    def test_version_control_manager_path_allows_managed_checkpoint_operations(self) -> None:
        for command in (
            ("loopplane", "vc", "checkpoint", "--reason", "manual_checkpoint"),
            ("loopplane", "vc", "rollback", "--checkpoint", "cp_1"),
            ("git", "update-ref", "refs/loopplane/wf_x/checkpoints/cp_1", "abc123"),
        ):
            with self.subTest(command=command):
                decision = enforce_command_policy(role="version_control_manager", command=command)

                self.assertTrue(decision.allowed, decision.reason)

    def test_permission_policy_can_disable_command_execution(self) -> None:
        decision = enforce_command_policy(
            role="worker",
            command=("git", "status"),
            permission_policy={"allow_command_execution": False},
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.decision, "command_execution_disabled")

    def test_assert_command_allowed_raises_for_legacy_blocked_worker_git_write(self) -> None:
        with self.assertRaises(CommandPolicyViolation):
            assert_command_allowed(
                role="worker",
                command=("git", "tag", "v1"),
                permission_policy={"require_approval_for_risky_commands": True},
            )

    def test_classifier_reports_uncovered_git_command_without_blocking(self) -> None:
        classification = classify_command(("git", "show", "--stat"))

        self.assertEqual(classification.decision, "git_unclassified")
        self.assertEqual(classification.operation, "show")


if __name__ == "__main__":
    unittest.main()
