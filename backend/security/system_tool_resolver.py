from __future__ import annotations

import re
import shlex
from typing import Iterable

_DANGEROUS_BINARIES = {
    "rm",
    "rmdir",
    "del",
    "move",
    "mv",
    "chmod",
    "chown",
    "chgrp",
    "ln",
    "mount",
    "umount",
    "sudo",
    "su",
    "passwd",
    "useradd",
    "usermod",
    "groupadd",
    "curl",
    "wget",
    "pip",
    "pip3",
    "npm",
    "pnpm",
    "yarn",
    "apt",
    "apt-get",
    "yum",
    "dnf",
    "apk",
    "brew",
    "powershell",
    "pwsh",
    "cmd",
}

_BLOCKED_PATTERNS = [
    (re.compile(r"[;&|><`]"), "shell chaining/redirection is not allowed"),
    (re.compile(r"\$\("), "command substitution is not allowed"),
    (re.compile(r"\.\./|\.\.\\"), "path traversal is not allowed"),
    (re.compile(r"(^|\s)~(/|\s|$)"), "home directory expansion is not allowed"),
]

_WRITE_HINTS = {"echo", "tee", "touch", "mkdir", "cp", "python", "python3", "node", "sh", "bash"}


def _tokenize(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _is_allowed_absolute_path(token: str, allowed_roots: Iterable[str]) -> bool:
    for root in allowed_roots:
        root = str(root or "").rstrip("/")
        if not root:
            continue
        if token == root or token.startswith(root + "/"):
            return True
    return False


def validate_terminal_command(
    command: str,
    *,
    policy: str = "strict",
    allowed_roots: Iterable[str] = ("/workspace", "/tmp"),
) -> tuple[bool, str | None]:
    cmd = str(command or "").strip()
    if not cmd:
        return (False, "empty command")
    if len(cmd) > 2000:
        return (False, "command is too long")
    if policy == "allow_all":
        return (True, None)

    for pattern, reason in _BLOCKED_PATTERNS:
        if pattern.search(cmd):
            return (False, reason)

    tokens = _tokenize(cmd)
    if not tokens:
        return (False, "empty command")

    binary = tokens[0].lower()
    if binary in _DANGEROUS_BINARIES:
        return (False, f"command '{binary}' is blocked by security policy")

    for token in tokens[1:]:
        if not token or token.startswith("-"):
            continue
        if token.startswith("/") and not _is_allowed_absolute_path(token, allowed_roots):
            return (False, f"absolute path '{token}' is outside allowed roots")
        if re.match(r"^[A-Za-z]:\\", token):
            return (False, "host-style absolute paths are not allowed")

    if binary in _WRITE_HINTS and policy == "strict":
        return (False, f"command '{binary}' is blocked in strict terminal policy")

    return (True, None)