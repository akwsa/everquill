"""Conservative, local screening of text destined for Muse requests."""

import re

_CREDENTIAL = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"secret(?:[_-]?key)?|client[_-]?secret|token|password|passwd|private[_-]?key|"
    r"authorization)\b\s*(?:=|:|=>)\s*[\"']?"
    r"(?!none\b|null\b|false\b|true\b|example\b|placeholder\b|"
    r"your[_-]|<|\{|\$)[A-Za-z0-9_./+=!@#$%^&*:-]{8,}"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.~+/-]{12,}")
_URL_AUTH = re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]{4,}@")
_PROVIDER_TOKEN = re.compile(r"\b(?:sk-|ghp_|github_pat_|glpat-|xox[baprs]-)[A-Za-z0-9_-]{16,}\b")


def contains_sensitive_content(*texts):
    """Return only a boolean; never log, echo, or transform matched material."""
    return any(
        pattern.search(text)
        for text in texts
        for pattern in (_CREDENTIAL, _PRIVATE_KEY, _BEARER, _URL_AUTH, _PROVIDER_TOKEN)
    )
