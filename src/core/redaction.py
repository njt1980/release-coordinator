"""Redact secrets and personal data from text before it is sent to an external model.

Pull request diffs, titles and descriptions, and the assistant's tool
results, can carry credentials (a connection string committed by mistake,
a token in a test) and personal data (e-mail addresses). Every text sent to
the model passes through ``redact`` first; what was removed is counted per
kind, so an assessment can say what it held back.

Patterns are deliberately broad (a false redaction costs a little context;
a missed secret leaks it) but are not a guarantee: they find well-known
shapes, not every secret. Documented in docs/access-requirements.md.
"""

import re
from collections import Counter
from typing import Final

from pydantic import BaseModel

# (kind, pattern, group to replace; 0 = the whole match). Order matters:
# blocks and URL credentials before the generic key=value forms.
_PATTERNS: Final[tuple[tuple[str, re.Pattern[str], int], ...]] = (
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
        0,
    ),
    ("url_credentials", re.compile(r"(?<=://)[^/\s:@'\"]+:[^/\s@'\"]+(?=@)"), 0),
    ("token", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), 0),
    ("token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"), 0),
    ("token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), 0),
    ("token", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    ("token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), 0),
    ("token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), 0),
    ("token", re.compile(r"(?i)(?<=\bbearer )[A-Za-z0-9._~+/-]{16,}=*"), 0),
    ("token", re.compile(r"(?i)(?<=[?&]sig=)[A-Za-z0-9%+/=]{16,}"), 0),
    (
        "connection_secret",
        re.compile(
            r"(?i)\b(?:password|pwd|accountkey|sharedaccesskey|sharedaccesssignature|clientsecret|"
            r"client_secret)\s*=\s*([^;'\"\s]+)"
        ),
        1,
    ),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b[\w.-]*(?:api[_-]?key|secret|token|password|passwd|credential)[\w.-]*['\"]?\s*[:=]\s*"
            r"['\"]([^'\"\s]{8,})['\"]"
        ),
        1,
    ),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), 0),
)


class Redacted(BaseModel):
    """Text with secrets and personal data replaced, and how many of each kind."""

    text: str
    counts: dict[str, int]


def redact(text: str) -> Redacted:
    """Replace every match with ``[redacted:<kind>]`` and count them per kind."""
    counts: Counter[str] = Counter()
    for kind, pattern, group in _PATTERNS:

        def replace(match: re.Match[str], kind: str = kind, group: int = group) -> str:
            value = match.group(group)
            if value.startswith("[redacted:"):
                return match.group(0)
            counts[kind] += 1
            start, end = match.span(group)
            whole_start = match.start(0)
            whole = match.group(0)
            return whole[: start - whole_start] + f"[redacted:{kind}]" + whole[end - whole_start :]

        text = pattern.sub(replace, text)
    return Redacted(text=text, counts=dict(counts))


def merge_counts(*counts: dict[str, int]) -> dict[str, int]:
    """Sum several redaction counts."""
    total: Counter[str] = Counter()
    for count in counts:
        total.update(count)
    return dict(total)
