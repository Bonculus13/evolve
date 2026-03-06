"""
Permission cache: remember what was approved, never ask twice for the same thing.
Uses pattern-based matching so "ls /foo" and "ls /bar" both match an approved "ls" pattern.

Autonomous mode: when AUTONOMOUS=True, all actions are auto-approved and cached,
except those explicitly in the deny list (HARD_DENY_PATTERNS).
"""
import json
import re
import fnmatch
from pathlib import Path
from config import PERMISSIONS_FILE, ALWAYS_ALLOW_PATTERNS

# Set to True by orchestrator --auto-evolve flag
AUTONOMOUS = False

# These are NEVER allowed even in autonomous mode
HARD_DENY_PATTERNS = [
    r"rm\s+-rf\s+/(?!Users/jacobdieter/Projects/04_tools/evolve)",  # no rm -rf outside project
    r"sudo\b",
    r"mkfs\b",
    r"dd\s+if=",
    r"curl\b.*\|\s*(?:bash|sh|python)",  # no curl-pipe-execute
    r"wget\b.*\|\s*(?:bash|sh|python)",
    r"shutdown\b",
    r"reboot\b",
    r":(){ :|:& };:",  # fork bomb
]


def _load() -> dict:
    if PERMISSIONS_FILE.exists():
        return json.loads(PERMISSIONS_FILE.read_text())
    return {"approved": [], "denied": []}


def _save(data: dict):
    PERMISSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PERMISSIONS_FILE.write_text(json.dumps(data, indent=2))


def _matches_any(cmd: str, patterns: list[str]) -> bool:
    for p in patterns:
        if re.search(p, cmd):
            return True
    return False


def _semantic_match(cmd: str, approved: list[str]) -> bool:
    """Check if cmd semantically matches a previously approved pattern."""
    cmd_parts = cmd.split()
    if not cmd_parts:
        return False
    base = cmd_parts[0]
    for a in approved:
        a_parts = a.split()
        if not a_parts:
            continue
        if cmd == a:
            return True
        if a_parts[0] == base:
            if a.endswith("*") and fnmatch.fnmatch(cmd, a):
                return True
            cmd_flags = {p for p in cmd_parts[1:] if p.startswith("-")}
            a_flags = {p for p in a_parts[1:] if p.startswith("-")}
            if cmd_flags == a_flags:
                return True
    return False


def check(action_type: str, detail: str) -> tuple[bool, bool]:
    """
    Returns (allowed: bool, was_cached: bool).
    """
    # Hard deny always wins
    if action_type == "bash" and _matches_any(detail, HARD_DENY_PATTERNS):
        return False, True

    # Always-allow list
    if action_type == "bash" and _matches_any(detail, ALWAYS_ALLOW_PATTERNS):
        return True, True

    data = _load()

    # Check cached denials
    key = f"{action_type}:{detail}"
    for pattern in data["denied"]:
        if re.search(re.escape(pattern).replace(r"\*", ".*"), key, re.IGNORECASE):
            return False, True

    # Check cached approvals
    approved_keys = [a for a in data["approved"] if a.startswith(f"{action_type}:")]
    approved_details = [a[len(action_type) + 1:] for a in approved_keys]

    if action_type == "bash":
        if _semantic_match(detail, approved_details):
            return True, True
    else:
        for a in approved_details:
            if a == detail or fnmatch.fnmatch(detail, a):
                return True, True

    return False, False


def record(action_type: str, detail: str, allowed: bool, pattern: str | None = None):
    data = _load()
    key = f"{action_type}:{pattern or detail}"
    target = "approved" if allowed else "denied"
    if key not in data[target]:
        data[target].append(key)
    _save(data)


def ask_user(action_type: str, detail: str) -> bool:
    """Prompt user and cache their answer. Returns True if approved."""
    print(f"\n[PERMISSION] Agent wants to perform: {action_type}")
    print(f"  Detail: {detail}")
    ans = input("  Allow? [y/N/y:pattern]: ").strip().lower()

    if ans == "y":
        record(action_type, detail, True)
        return True
    elif ans.startswith("y:"):
        pattern = ans[2:].strip()
        record(action_type, detail, True, pattern=pattern)
        return True
    else:
        record(action_type, detail, False)
        return False


def request(action_type: str, detail: str) -> bool:
    """Full permission check: hard deny → cache → autonomous auto-approve → ask user."""
    allowed, cached = check(action_type, detail)
    if cached:
        if not allowed:
            print(f"[BLOCKED] Hard denied: {action_type}: {detail[:80]}")
        return allowed

    # Not cached yet
    if AUTONOMOUS:
        # Auto-approve and persist so future calls are instant
        print(f"[AUTO-APPROVED] {action_type}: {detail[:80]}")
        record(action_type, detail, True)
        return True

    return ask_user(action_type, detail)
