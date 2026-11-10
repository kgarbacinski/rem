"""Keep credentials out of digests, prompts and memory. Best effort, by pattern.

Two jobs with opposite risks. Redacting a transcript digest should catch as much
as possible. Scanning proposed memory content must not flag ordinary prose or
code, or honest memories get rejected. So assignments like `token = x` are only
treated as secrets when the value looks like one.
"""
import re

MARK = "[REDACTED]"

_TOKENS = [
    re.compile(r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----.{0,6000}?-----END [A-Z ]{0,30}PRIVATE KEY-----", re.S),
    re.compile(r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----(?:\s*[A-Za-z0-9+/=]{40,})+"),
    re.compile(r"\bsk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    re.compile(r"\b(?:gsk|shpat|shpss)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bwhsec_[A-Za-z0-9]{20,}"),
    re.compile(r"\bya29\.[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
]
# Secrets that sit inside a larger construct: keep the construct, mask the secret.
_EMBEDDED = [
    (re.compile(r"(?i)(Authorization['\"]?\s*[:=]\s*['\"]?(?:Bearer|Basic)\s+)([A-Za-z0-9_\-\.=+/]{8,})"), 2),
    (re.compile(r"\b(Bearer\s+)(?=[A-Za-z_\-\.=+/]*\d)([A-Za-z0-9_\-\.=+/]{20,})"), 2),
    (re.compile(r"\b([a-z][a-z0-9+.\-]{1,20}://[^\s:/@]{0,64}:)([^\s@]{3,128})(@)"), 2),
    (re.compile(r"(\bcurl\b[^\n]{0,300}?\s(?:-u|--user)[\s=]+['\"]?[^\s:'\"]{1,64}:)([^\s'\"]{3,128})"), 2),
    (re.compile(r"(\b(?:mysql|mysqldump)\b[^\n]{0,80}?\s-p)(?!ort|rotocol|ipe)([^\s=]{4,})"), 2),
    (re.compile(r"(\b(?:sshpass|docker login)\b[^\n]{0,120}?\s-p\s+['\"]?)([^\s'\"]{4,128})"), 2),
    (re.compile(r"(\s--password[\s=]+['\"]?)([^\s'\"]{4,128})"), 2),
]

_PASSWORD_WORDS = r"password|passwd|passphrase|pwd|has[łl]o"
_TOKEN_WORDS = r"secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key|client[_\-]?secret|credentials?|_KEY|auth"
_ASSIGN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(['\"]?)([A-Za-z0-9_\-]{0,40}?(?:" + _PASSWORD_WORDS + "|" + _TOKEN_WORDS + r"))\1"
    r"(?![A-Za-z0-9])"
    r"(\s*(?:=|:|\bis\b|\bto\b)\s*)"
    r"(['\"]?)([^\s'\",;]{4,200})",
    re.I,
)
_PASSWORD_KEY = re.compile(r"(?i)(?:" + _PASSWORD_WORDS + r")\Z")
_CODE_VALUE = re.compile(r"\A(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+|[A-Za-z_]\w*\(|\w+\[)")
_PLAIN_WORDS = {"undefined", "none", "null", "true", "false", "required", "optional", "string",
                "changeme", "example", "redacted", "hidden", "unknown", "empty", "missing"}


def _switches(value: str) -> int:
    """How often the character class changes: random tokens switch a lot, names do not."""
    def kind(ch):
        return "d" if ch.isdigit() else "u" if ch.isupper() else "l" if ch.islower() else ""
    kinds = [kind(ch) for ch in value if kind(ch)]
    return sum(1 for a, b in zip(kinds, kinds[1:]) if a != b)


def _looks_secret(value: str, password_key: bool, env_style: bool = False) -> bool:
    value = value.strip("`*_()[]{}<>.")
    if password_key and env_style and len(value) >= 4 and MARK.strip("[]") not in value \
            and value.lower() not in _PLAIN_WORDS and not value.startswith(("$", "<", "{")) \
            and not _CODE_VALUE.match(value) and "(" not in value:
        return True  # PGPASSWORD=whatever: in an assignment, any value is the password
    if not password_key:
        # A token-like key (auth, secret, _KEY...) is common in prose and config, so the
        # value itself has to look random, not like a name, a file or a constant.
        return (len(value) >= 8 and MARK.strip("[]") not in value and "(" not in value
                and not value.startswith(("$", "<", "{", "~", "/", "./", "http"))
                and not _CODE_VALUE.match(value) and _switches(value) >= 3)
    if len(value) < (6 if password_key else 8) or MARK.strip("[]") in value:
        return False
    if value.lower() in _PLAIN_WORDS or value.startswith(("$", "<", "{", "~", "/", "./", "http")):
        return False
    if _CODE_VALUE.match(value) or "(" in value:
        return False
    has_digit = any(ch.isdigit() for ch in value)
    has_upper = any(ch.isupper() for ch in value)
    has_lower = any(ch.islower() for ch in value)
    has_symbol = any(not ch.isalnum() for ch in value)
    if value.isdigit():
        return password_key
    if has_digit and (has_upper or has_lower):
        return True
    if has_upper and has_lower and (has_symbol or len(value) >= 12) and not value.istitle():
        return True
    return has_symbol and len(value) >= 20  # a passphrase-like run


# "login: me@example.com Hunter2!!" : a password given right after an e-mail address.
_AFTER_EMAIL = re.compile(r"[\w.+\-]{1,64}@[\w\-]{1,64}(?:\.[\w\-]{1,24}){1,4}(\s*[/,;|]?\s+)(\S{8,64})")


def _after_email(m) -> bool:
    value = m.group(2)
    return (any(ch.isdigit() for ch in value) and any(ch.isalpha() for ch in value)
            and any(not ch.isalnum() for ch in value.rstrip(".,;:)")) and "@" not in value[:-3].replace("@@", "")
            and not value.startswith(("http", "(", "<")) and _looks_secret(value, True)) or (
            any(ch.isdigit() for ch in value) and any(ch.isupper() for ch in value)
            and any(ch.islower() for ch in value) and any(not ch.isalnum() for ch in value.rstrip(".,;:)")))


def find_secrets(text: str) -> set:
    """Every substring that looks like a credential."""
    found = set()
    for pat in _TOKENS:
        found.update(m.group(0) for m in pat.finditer(text))
    for pat, group in _EMBEDDED:
        found.update(m.group(group) for m in pat.finditer(text))
    for m in _ASSIGN.finditer(text):
        if _looks_secret(m.group(5), bool(_PASSWORD_KEY.search(m.group(2))), m.group(3) == "="):
            found.add(m.group(5))
    for m in _AFTER_EMAIL.finditer(text):
        if _after_email(m):
            found.add(m.group(2))
    return {s for s in found if MARK not in s}


def redact(text: str) -> str:
    if not text:
        return text
    for pat in _TOKENS:
        text = pat.sub(MARK, text)
    for pat, group in _EMBEDDED:
        text = pat.sub(lambda m, g=group: m.group(0).replace(m.group(g), MARK, 1), text)

    def assign(m):
        if _looks_secret(m.group(5), bool(_PASSWORD_KEY.search(m.group(2))), m.group(3) == "="):
            return m.group(0)[: m.start(5) - m.start(0)] + MARK
        return m.group(0)

    text = _ASSIGN.sub(assign, text)
    return _AFTER_EMAIL.sub(lambda m: m.group(0)[: m.start(2) - m.start(0)] + MARK if _after_email(m) else m.group(0), text)


def has_secret(text: str) -> bool:
    return bool(find_secrets(text))


def new_secrets(text: str, already_present: str = "") -> set:
    """Secrets in text that are not already in the user's own files."""
    return find_secrets(text) - find_secrets(already_present)
