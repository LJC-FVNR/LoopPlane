from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.adapters.base import AdapterInput


DEFAULT_COOLDOWNS = {
    "rate_limited": 900,
    "usage_limit_exhausted": 18_000,
    "provider_overloaded": 300,
}
BUILTIN_ADAPTERS = frozenset({"codex_cli", "claude_code_cli"})
BUILTIN_PATTERNS = (
    {
        "reason_class": "billing_required",
        "pattern": r"\b(402|billing|payment required|spend limit|monthly budget)\b",
        "requires_attention": True,
    },
    {
        "reason_class": "credits_exhausted",
        "pattern": r"\b(credit balance|credits? exhausted|out of credits|insufficient_quota|exceeded your current quota)\b",
        "requires_attention": True,
    },
    {
        "reason_class": "usage_limit_exhausted",
        "pattern": r"\b(usage limit|weekly limit|five[- ]hour limit|5[- ]hour limit)\b",
        "cooldown_seconds": DEFAULT_COOLDOWNS["usage_limit_exhausted"],
    },
    {
        "reason_class": "rate_limited",
        "pattern": r"\b(rate limit|rate_limit|rate_limit_error|too many requests|429)\b",
        "cooldown_seconds": DEFAULT_COOLDOWNS["rate_limited"],
    },
    {
        "reason_class": "provider_overloaded",
        "pattern": r"\b(overloaded|server overloaded|temporarily unavailable|503|529|overloaded_error)\b",
        "cooldown_seconds": DEFAULT_COOLDOWNS["provider_overloaded"],
    },
    {
        "reason_class": "auth_required",
        "pattern": r"\b(authentication required|unauthorized|invalid api key|login required|401)\b",
        "requires_attention": True,
    },
)


def classify_runner_availability(
    adapter_input: AdapterInput,
    *,
    exit_code: int,
    timed_out: bool,
    stdout_path: Path,
    stderr_path: Path,
    final_output_path: Path,
) -> dict[str, Any] | None:
    if exit_code == 0 or timed_out:
        return None
    policy = _availability_policy(adapter_input)
    if policy.get("enabled") is False:
        return None
    sources = {
        "stdout": _read_text(stdout_path),
        "stderr": _read_text(stderr_path),
        "final_output": _read_text(final_output_path),
    }
    custom = _match_custom_classifier(policy, sources)
    match = custom
    if match is None and _builtin_classifiers_enabled(adapter_input, policy):
        match = _match_builtin_classifier(sources)
    if match is None:
        return None
    reason_class = str(match.get("reason_class") or "unknown_runner_unavailable")
    cooldown_seconds = _positive_int(match.get("cooldown_seconds"))
    if cooldown_seconds is None:
        cooldown_seconds = DEFAULT_COOLDOWNS.get(reason_class)
    requires_attention = bool(match.get("requires_attention"))
    if cooldown_seconds is None:
        requires_attention = True
    scope = _availability_scope(adapter_input, policy)
    evidence = {
        "source": match.get("source"),
        "matched_pattern": match.get("matched_pattern"),
        "message_excerpt": _excerpt(str(match.get("message") or "")),
    }
    record: dict[str, Any] = {
        "status": "unavailable",
        "reason_class": reason_class,
        "recoverability": "manual" if requires_attention else "auto_after_cooldown",
        "scope": scope,
        "requires_attention": requires_attention,
        "confidence": str(match.get("confidence") or ("medium" if custom else "high")),
        "fingerprint": _availability_fingerprint(
            adapter=adapter_input.adapter,
            reason_class=reason_class,
            scope=scope,
            message=str(match.get("message") or ""),
        ),
        "evidence": evidence,
    }
    if cooldown_seconds is not None and cooldown_seconds > 0:
        record["retry_after_seconds"] = cooldown_seconds
        record["cooldown_until"] = (datetime.now(UTC) + timedelta(seconds=cooldown_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return record


def _availability_policy(adapter_input: AdapterInput) -> Mapping[str, Any]:
    options = adapter_input.runner_config.get("adapter_options")
    if not isinstance(options, Mapping):
        return {}
    for key in ("runner_availability", "availability_policy"):
        value = options.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _builtin_classifiers_enabled(adapter_input: AdapterInput, policy: Mapping[str, Any]) -> bool:
    configured = policy.get("builtin_classifiers", policy.get("use_builtin_classifiers"))
    if isinstance(configured, bool):
        return configured
    return adapter_input.adapter in BUILTIN_ADAPTERS


def _availability_scope(adapter_input: AdapterInput, policy: Mapping[str, Any]) -> dict[str, str]:
    raw_scope = policy.get("scope")
    if isinstance(raw_scope, Mapping):
        scope_type = str(raw_scope.get("type") or "runner").strip().lower()
        scope_key = str(raw_scope.get("key") or "").strip()
        if scope_type in {"runner", "credential", "provider"} and scope_key:
            return {"type": scope_type, "key": scope_key}
    return {"type": "runner", "key": adapter_input.runner_id}


def _match_custom_classifier(policy: Mapping[str, Any], sources: Mapping[str, str]) -> dict[str, Any] | None:
    classifiers = policy.get("classifiers")
    if not isinstance(classifiers, Sequence) or isinstance(classifiers, (str, bytes)):
        return None
    for classifier in classifiers:
        if not isinstance(classifier, Mapping):
            continue
        match = classifier.get("match")
        if not isinstance(match, Mapping):
            continue
        for field, source_name in (
            ("stdout_regex", "stdout"),
            ("stderr_regex", "stderr"),
            ("final_output_regex", "final_output"),
            ("any_regex", "any"),
        ):
            pattern = match.get(field)
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            text = "\n".join(sources.values()) if source_name == "any" else sources.get(source_name, "")
            found = _regex_search(pattern, text)
            if found:
                return {
                    **dict(classifier),
                    "source": source_name,
                    "matched_pattern": pattern,
                    "message": found.group(0),
                }
    return None


def _match_builtin_classifier(sources: Mapping[str, str]) -> dict[str, Any] | None:
    text_by_source = {
        "stderr": sources.get("stderr", ""),
        "final_output": sources.get("final_output", ""),
        "stdout": sources.get("stdout", ""),
    }
    for pattern in BUILTIN_PATTERNS:
        for source_name, text in text_by_source.items():
            found = _regex_search(str(pattern["pattern"]), text)
            if found:
                return {
                    **pattern,
                    "source": source_name,
                    "matched_pattern": pattern["pattern"],
                    "message": found.group(0),
                }
    return None


def _regex_search(pattern: str, text: str) -> re.Match[str] | None:
    try:
        return re.search(pattern, text, flags=re.IGNORECASE)
    except re.error:
        return None


def _availability_fingerprint(*, adapter: str, reason_class: str, scope: Mapping[str, str], message: str) -> str:
    material = "|".join(
        (
            adapter,
            reason_class,
            str(scope.get("type") or ""),
            str(scope.get("key") or ""),
            re.sub(r"\s+", " ", message.strip().lower())[:200],
        )
    )
    return "sha256:" + sha256(material.encode("utf-8")).hexdigest()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _excerpt(text: str, *, limit: int = 280) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
