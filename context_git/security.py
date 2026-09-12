"""security — the redaction line between project data and the Context Object.

Two layers of defence:

1. **Path-level denial** (common.is_forbidden_path): sensitive files are
   never read in the first place — .env, keys, credentials, anything under
   .ssh/.aws/.kube.
2. **Content-level redaction** (this module): every string about to enter a
   Context Object passes through ``redact()``, which detects credential-shaped
   text and replaces it with a typed placeholder like ``[REDACTED:api-key]``.

Filtering happens **before** data enters the Context Object — never
"save now, scrub later".

The patterns are shape-based (prefix entropy, assignment right-hand sides,
header forms) rather than a vendor list, so we also catch keys we have
never heard of. False positives (over-redaction) are acceptable;
false negatives are not.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# --------------------------------------------------------------------------
# Layer 1: redaction patterns (most specific first)
# --------------------------------------------------------------------------

_TOKEN_PATTERNS = []

_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN (?P<label>(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY|"
    r"PGP PRIVATE KEY BLOCK)-----.*?-----END (?P=label)-----",
    re.DOTALL,
)


def _p(pattern, tag):
    _TOKEN_PATTERNS.append((re.compile(pattern), tag))


# --- known credential shapes -------------------------------------------------
_p(r"sk-proj-[A-Za-z0-9_-]{20,}", "api-key")                 # OpenAI project key
_p(r"sk-ant-[A-Za-z0-9_-]{20,}", "api-key")                  # Anthropic
_p(r"sk-[A-Za-z0-9_-]{20,}", "api-key")                      # OpenAI-style
_p(r"sk-[A-Za-z0-9_-]{4,}", "api-key")                       # short/fake keys
_p(r"AIza[0-9A-Za-z_-]{35}", "api-key")                      # Google
_p(r"xox[baprs]-[A-Za-z0-9-]{10,}", "token")                 # Slack
_p(r"github_pat_[A-Za-z0-9_]{22,}", "github-token")          # GitHub fine-grained
_p(r"gh[pousr]_[A-Za-z0-9]{36,}", "github-token")            # GitHub classic
_p(r"gh[pousr]_[A-Za-z0-9_-]{4,}", "github-token")            # short/fake tokens
_p(r"glpat-[A-Za-z0-9_-]{20,}", "gitlab-token")              # GitLab
_p(r"np_[A-Za-z0-9]{30,}", "npm-token")
_p(r"pypi-[A-Za-z0-9_]{20,}", "pypi-token")
_p(r"AKIA[0-9A-Z]{16}", "aws-access-key")                    # AWS
_p(r"ASIA[0-9A-Z]{16}", "aws-access-key")
_p(r"(?i)aws(.{0,20})?(secret|private)[-_ ]?(access)?[-_ ]?key(.{0,10})?[:=]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?", "aws-secret-key")
_p(r"sk_live_[A-Za-z0-9]{10,}", "stripe-secret-key")         # Stripe
_p(r"rk_live_[A-Za-z0-9]{10,}", "stripe-secret-key")
_p(r"sq0atp-[A-Za-z0-9_-]{20,}", "token")                    # Square
_p(r"ya29\.[A-Za-z0-9_-]{20,}", "oauth-token")               # Google OAuth
_p(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "jwt")
_p(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}", "bearer-token")
_p(r"(?i)authorization\s*:\s*\S+.*", "authorization-header")
_p(r"(?i)cookie\s*:\s*\S+.*", "cookie-header")
_p(r"(?i)set-cookie\s*:.+", "cookie-header")
_p(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----", "private-key")

# --- keyword assignment forms: password=..., token: ..., SECRET_KEY="..." ----
_KEYWORDS = [
    "password", "passwd", "pwd", "pass",
    "secret", "secret_key", "secrettoken",
    "access_token", "refresh_token", "auth_token", "id_token",
    "api_key", "apikey", "api_secret", "client_secret",
    "private_key", "privatekey",
    "authorization", "credentials", "credential",
    "session_token", "session_key", "signing_key", "encryption_key",
    "db_pass", "db_password", "database_url", "db_url",
    "smtp_pass", "smtp_password", "mail_password",
    "aws_secret", "token",
]
_KW_ALT = "|".join(_KEYWORDS)

_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(" + _KW_ALT + r")(\w*)\b"          # keyword (+suffix like _key)
    r"(\s*)"                                     # spacing
    r"([:=])\s*"                                 # separator
    r"(['\"]?)"                                  # opening quote
    r"([^\s'\"`,;)\]}]{4,})"                     # value
    r"(['\"]?)",                                 # closing quote
)

# scheme://user:password@host — URL-embedded credentials
_URL_CREDENTIALS_RE = re.compile(
    r"(?i)\b(\w+)\s*:\s*(//)?([^\s:/@]+):([^\s@/]{3,})@"
)

# long random-looking string right after a suspicious context word
_SUSPICIOUS_CONTEXT_RE = re.compile(
    r"(?i)\b(key|token|secret|password|credential)s?\b\s*[:=—-]?\s*['\"]?"
    r"([A-Za-z0-9+/_=-]{32,})"
)


def redact(text):
    """Return ``text`` with credential-shaped content replaced.

    Idempotent: redacting already-redacted text returns it unchanged.
    Never raises — redaction failing open is unacceptable, so any internal
    error collapses to a fully redacted string.
    """
    if not text:
        return text
    try:
        out = str(text)
        out = _PRIVATE_KEY_BLOCK_RE.sub("[REDACTED:private-key]", out)
        for pattern, tag in _TOKEN_PATTERNS:
            out = pattern.sub("[REDACTED:{}]".format(tag), out)
        out = _URL_CREDENTIALS_RE.sub(r"\1://\3:[REDACTED:url-password]@", out)
        out = _ASSIGNMENT_RE.sub(
            lambda m: "{}{}{}{}[REDACTED:credential-value]{}".format(
                m.group(1), m.group(2), m.group(3), m.group(4), m.group(7)
            ),
            out,
        )
        out = _SUSPICIOUS_CONTEXT_RE.sub(r"\1: [REDACTED:high-entropy-value]", out)
        return out
    except Exception:  # pragma: no cover — never fail open
        return REDACTED


# --------------------------------------------------------------------------
# Layer 2: residual scan — a *different* detector set, used as second opinion
# --------------------------------------------------------------------------

_RESIDUAL_CHECKS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic key"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "OpenAI-style key"),
    (re.compile(r"sk-[A-Za-z0-9_-]{4,}"), "OpenAI-style key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "GitHub token"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9_-]{4,}"), "GitHub token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{22,}"), "GitHub fine-grained token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "GitLab token"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "Google API key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block"),
    (re.compile(r"-----END [A-Z ]*PRIVATE KEY-----"), "private key block tail"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
    (re.compile(r"(?i)\bpassword\s*[:=]\s*['\"]?[^\s'\"]{4,}"), "plaintext password"),
    (re.compile(r"(?i)\bsecret\s*[:=]\s*['\"]?[^\s'\"]{4,}"), "plaintext secret"),
    (re.compile(r"(?i)\bapi[_-]?key\s*[:=]\s*['\"]?[^\s'\"]{4,}"), "plaintext API key"),
    (re.compile(r"(?i)\btoken\s*[:=]\s*['\"]?[^\s'\"]{4,}"), "plaintext token"),
    (re.compile(r"(?i)authorization\s*:\s*\S+"), "Authorization header"),
]


def scan_text(text):
    """Scan a text blob for secrets that escaped redaction.

    Returns a list of ``(reason, safe_snippet)`` tuples; empty means clean.
    The snippet is itself redacted so findings can be printed safely.
    Used by ``context-git verify`` and the test suite.
    """
    findings = []
    for pattern, reason in _RESIDUAL_CHECKS:
        for m in pattern.finditer(text):
            snippet = m.group(0)
            if "[REDACTED" in snippet:
                continue
            findings.append((reason, redact(snippet)[:80]))
    return findings


def scan_object(obj):
    """Deep-scan any JSON-serialisable structure. Returns findings list."""
    import json as _json
    try:
        blob = _json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = str(obj)
    return scan_text(blob)
