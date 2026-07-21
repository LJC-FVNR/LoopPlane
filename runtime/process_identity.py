from __future__ import annotations

import os
import socket
from pathlib import Path


def pid_exists(pid: int) -> bool | None:
    """Return local PID liveness without treating permission denial as death."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def process_start_time(pid: int) -> str | None:
    """Return a stable local process birth identity when the platform exposes one."""

    try:
        stat_fields = (
            (Path("/proc") / str(pid) / "stat")
            .read_text(encoding="utf-8")
            .rsplit(") ", 1)[1]
            .split()
        )
        return f"proc:{stat_fields[19]}"
    except (OSError, IndexError):
        return None


def hostnames_match(left: str, right: str) -> bool:
    left_normalized = _normalized_hostname(left)
    right_normalized = _normalized_hostname(right)
    if not left_normalized or not right_normalized:
        return False
    if left_normalized == right_normalized:
        return True
    # A short hostname may legitimately be compared with its FQDN. Two
    # different FQDNs must not be collapsed merely because they share a short
    # label: that would authorize a PID probe in another host's namespace.
    left_is_short = "." not in left_normalized
    right_is_short = "." not in right_normalized
    if left_is_short == right_is_short:
        return False
    short = left_normalized if left_is_short else right_normalized
    fqdn = right_normalized if left_is_short else left_normalized
    return fqdn.split(".", 1)[0] == short


def host_is_local(host: str | None) -> bool | None:
    if host is None or not host.strip():
        return None
    candidate = _normalized_hostname(host)
    local_names = {
        normalized
        for value in (socket.gethostname(), socket.getfqdn())
        if (normalized := _normalized_hostname(value))
    }
    if candidate in local_names:
        return True
    if "." in candidate:
        # Never reduce an explicit FQDN to its short label. The kernel hostname
        # is commonly short, and two cluster nodes can share that label across
        # DNS domains.
        return False
    return candidate in {local.split(".", 1)[0] for local in local_names}


def hostname_aliases(value: str) -> set[str]:
    normalized = _normalized_hostname(value)
    if not normalized:
        return set()
    return {normalized, normalized.split(".", 1)[0]}


def _normalized_hostname(value: str) -> str:
    return value.strip().lower().rstrip(".")
