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
RETRY_AT_BUFFER_SECONDS = 60
BUILTIN_DIAGNOSTIC_TAIL_LINES = 80
BUILTIN_DIAGNOSTIC_TAIL_CHARS = 65_536
BUILTIN_ADAPTERS = frozenset({"codex_cli", "claude_code_cli"})
BUILTIN_PATTERNS = (
    {
        "reason_class": "billing_required",
        # A bare ``402`` commonly appears as a source/prompt line number (for
        # example ``402- - approval: not_required``).  Require HTTP/error
        # context before treating the numeric status as billing evidence.
        # Do not treat a bare mention of "billing" as a provider diagnostic.
        # Agent transcripts routinely contain billing/account instructions and
        # source text that are unrelated to the runner's terminal failure.
        "pattern": r"\b(?:payment required|billing (?:required|disabled|error|failure)|(?:spend limit|monthly budget) (?:reached|exceeded|exhausted)|(?:http(?: status)?|status(?: code)?|error)\s*[:=#-]?\s*402)\b",
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
        "pattern": r"\b(?:rate limit|rate_limit|rate_limit_error|too many requests|(?:http(?: status)?|status(?: code)?|error)\s*[:=#-]?\s*429)\b",
        "cooldown_seconds": DEFAULT_COOLDOWNS["rate_limited"],
    },
    {
        "reason_class": "provider_overloaded",
        "pattern": r"\b(?:(?:selected\s+)?model\s+(?:is\s+)?(?:currently\s+)?at\s+capacity|overloaded|server overloaded|temporarily unavailable|overloaded_error|(?:http(?: status)?|status(?: code)?|error)\s*[:=#-]?\s*(?:503|529))\b",
        "cooldown_seconds": DEFAULT_COOLDOWNS["provider_overloaded"],
    },
    {
        "reason_class": "runner_configuration_error",
        "pattern": r"\b(?:unexpected argument|unrecognized arguments?|unknown option|invalid (?:option|argument))\b[^\n]*",
        "requires_attention": True,
    },
    {
        "reason_class": "auth_required",
        # Do not treat prose such as "unauthorized GPUs invalidate the run"
        # as authentication evidence. Codex writes prompt/context material to
        # stderr, so a bare occurrence of "unauthorized" is not an error
        # signal. Preserve the common standalone error form and explicit auth
        # diagnostics instead.
        "pattern": r"(?:\b(?:authentication required|invalid api key|login required|(?:http(?: status)?|status(?: code)?|error)\s*[:=#-]?\s*401)\b|(?:^|\n)[ \t]*(?:error[ \t]*[:=#-][ \t]*)?unauthorized(?:[ \t]*(?:$|\n)|[ \t]*[:.!]))",
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
    now = datetime.now(UTC)
    cooldown_seconds = _positive_int(match.get("cooldown_seconds"))
    if cooldown_seconds is None:
        cooldown_seconds = DEFAULT_COOLDOWNS.get(reason_class)
    parsed_retry_after = _cooldown_seconds_from_retry_at(str(match.get("message") or ""), now=now)
    if parsed_retry_after is not None:
        cooldown_seconds = parsed_retry_after
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
        record["cooldown_until"] = (now + timedelta(seconds=cooldown_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        "stderr": _diagnostic_tail(sources.get("stderr", "")),
        "final_output": _diagnostic_tail(sources.get("final_output", "")),
        "stdout": _diagnostic_tail(sources.get("stdout", "")),
    }
    # Availability diagnostics are terminal errors.  Within each preferred
    # source, use the latest matching diagnostic instead of allowing the
    # declaration order of patterns to select an earlier phrase quoted by the
    # agent.  stderr remains preferred over fallback final output and stdout.
    for source_name, text in text_by_source.items():
        latest: tuple[int, Mapping[str, Any], re.Match[str]] | None = None
        for pattern in BUILTIN_PATTERNS:
            found = _last_regex_match(str(pattern["pattern"]), text)
            if found is not None and (latest is None or found.start() > latest[0]):
                latest = (found.start(), pattern, found)
        if latest is not None:
            _, pattern, found = latest
            return {
                **pattern,
                "source": source_name,
                "matched_pattern": pattern["pattern"],
                "message": _matched_line(text, found),
            }
    return None


def _regex_search(pattern: str, text: str) -> re.Match[str] | None:
    try:
        return re.search(pattern, text, flags=re.IGNORECASE)
    except re.error:
        return None


def _last_regex_match(pattern: str, text: str) -> re.Match[str] | None:
    try:
        matches = re.finditer(pattern, text, flags=re.IGNORECASE)
        return next(reversed(list(matches)), None)
    except re.error:
        return None


def _diagnostic_tail(text: str) -> str:
    lines = text.splitlines()
    tail = "\n".join(lines[-BUILTIN_DIAGNOSTIC_TAIL_LINES:])
    return tail[-BUILTIN_DIAGNOSTIC_TAIL_CHARS:]


def _matched_line(text: str, found: re.Match[str]) -> str:
    start = text.rfind("\n", 0, found.start()) + 1
    end = text.find("\n", found.end())
    if end == -1:
        end = len(text)
    return text[start:end].strip() or found.group(0)


def _cooldown_seconds_from_retry_at(message: str, *, now: datetime) -> int | None:
    found = re.search(
        r"\btry again (?:at|after)\s+"
        r"(?:(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+"
        r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{4}))?\s+(?:at\s+)?)?"
        r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>[ap]\.?m\.?)\b",
        message,
        flags=re.IGNORECASE,
    )
    if not found:
        return None
    hour = int(found.group("hour"))
    minute = int(found.group("minute") or "0")
    if hour < 1 or hour > 12 or minute > 59:
        return None
    period = found.group("period").lower().replace(".", "")
    if period == "pm" and hour != 12:
        hour += 12
    elif period == "am" and hour == 12:
        hour = 0
    month_text = found.group("month")
    if month_text:
        month_lookup = {
            "jan": 1,
            "feb": 2,
            "mar": 3,
            "apr": 4,
            "may": 5,
            "jun": 6,
            "jul": 7,
            "aug": 8,
            "sep": 9,
            "oct": 10,
            "nov": 11,
            "dec": 12,
        }
        month = month_lookup[month_text[:3].lower()]
        day = int(found.group("day"))
        explicit_year = found.group("year")
        year = int(explicit_year) if explicit_year else now.year
        try:
            target = now.replace(
                year=year,
                month=month,
                day=day,
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
        except ValueError:
            return None
        if explicit_year is None and target < now - timedelta(hours=12):
            try:
                target = target.replace(year=target.year + 1)
            except ValueError:
                return None
    else:
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    seconds = int((target - now).total_seconds())
    if seconds <= 0:
        # An explicit past date and a just-expired time both mean retry soon;
        # neither should become an accidental next-day or next-year hold.
        if month_text or seconds >= -12 * 60 * 60:
            return RETRY_AT_BUFFER_SECONDS
        target += timedelta(days=1)
        seconds = int((target - now).total_seconds())
    return max(RETRY_AT_BUFFER_SECONDS, seconds + RETRY_AT_BUFFER_SECONDS)


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
