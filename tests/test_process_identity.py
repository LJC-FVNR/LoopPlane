from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from runtime.process_identity import (
    host_is_local,
    hostnames_match,
    pid_exists,
    process_start_time,
)


class ProcessIdentityTest(unittest.TestCase):
    def test_current_process_has_liveness_and_birth_identity(self) -> None:
        self.assertIs(pid_exists(os.getpid()), True)
        self.assertRegex(process_start_time(os.getpid()) or "", r"^proc:\d+$")

    def test_short_hostname_matches_its_fqdn(self) -> None:
        self.assertTrue(hostnames_match("worker", "worker.prod.example"))
        self.assertTrue(hostnames_match("WORKER.PROD.EXAMPLE.", "worker"))

    def test_distinct_fqdns_with_same_short_label_do_not_match(self) -> None:
        self.assertFalse(
            hostnames_match("worker.prod.example", "worker.lab.example")
        )

    def test_remote_fqdn_is_not_local_by_short_label_collision(self) -> None:
        with (
            patch("runtime.process_identity.socket.gethostname", return_value="worker"),
            patch("runtime.process_identity.socket.getfqdn", return_value="worker.prod.example"),
        ):
            self.assertIs(host_is_local("worker.lab.example"), False)


if __name__ == "__main__":
    unittest.main()
