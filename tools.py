"""
Tool implementations for the agent.
Each function is called when Claude requests a tool use.
"""
import subprocess
import json
import time
from pathlib import Path
import permissions
import memory as mem
from config import SOURCE_DIR, DATA_DIR, LOGS_DIR


# ── Tool definitions sent to Claude API ──────────────────────────────────────

TOOL_DEFS = [
    {
        "name": "bash",
        "description": (
            "Execute a shell command. Use for file operations, running scripts, "
            "installing packages, reading command output. "
            "Prefer targeted commands over broad ones. "
            "Commands previously approved by the user are auto-approved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file (creates or overwrites).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "patch_self",
        "description": (
            "Modify one of this agent's own source files to improve behavior, "
            "fix bugs, or add capabilities. Only modify files within the evolve/ directory. "
            "Use this for self-improvement when you identify a concrete enhancement. "
            "WARNING: Requires writing the entire file. Prefer patch_diff for targeted edits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Filename within the evolve/ dir (e.g. 'tools.py')"},
                "content": {"type": "string", "description": "New full content of the file"},
                "reason": {"type": "string", "description": "Why this change improves the system"},
            },
            "required": ["file", "content", "reason"],
        },
    },
    {
        "name": "patch_diff",
        "description": (
            "Make a targeted edit to one of this agent's own source files by replacing "
            "an exact string with new content. Much safer than patch_self for small changes "
            "because you only need to provide the specific text to change, not the whole file. "
            "The old_string must match exactly (including whitespace/indentation). "
            "Use this instead of patch_self whenever possible."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Filename within the evolve/ dir (e.g. 'tools.py')"},
                "old_string": {"type": "string", "description": "Exact text to find and replace (must be unique in the file)"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "reason": {"type": "string", "description": "Why this change improves the system"},
            },
            "required": ["file", "old_string", "new_string", "reason"],
        },
    },
    {
        "name": "memory_write",
        "description": (
            "Persist a learned pattern, insight, or note to long-term memory. "
            "Use this to remember things that will help future tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["pattern", "note", "key_value"],
                    "description": "'pattern' for efficiency patterns, 'note' for freeform notes, 'key_value' for structured data"
                },
                "content": {"type": "string", "description": "The thing to remember"},
                "key": {"type": "string", "description": "For key_value type: the key to store under"},
            },
            "required": ["type", "content"],
        },
    },
    {
        "name": "memory_read",
        "description": "Read all persistent memory (patterns, notes, history summary).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_self",
        "description": "List all source files in the evolve/ directory with sizes.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "git_auto_commit",
        "description": (
            "Stage all changes in the evolve/ directory, commit with a descriptive message, "
            "and push to the remote. Use after making improvements or fixes to persist them. "
            "The commit message should summarize what changed and why."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Descriptive commit message summarizing the changes"},
            },
            "required": ["message"],
        },
    },
]


# ── Tool execution ────────────────────────────────────────────────────────────

class ToolResult:
    def __init__(self, tool_use_id: str, content: str, is_error: bool = False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error

    def to_api_block(self) -> dict:
        return {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": self.content,
            "is_error": self.is_error,
        }


def execute(tool_name: str, tool_input: dict, tool_use_id: str,
            intervention_counter: list) -> ToolResult:
    """Dispatch a tool call, handle permissions, return result."""
    try:
        if tool_name == "bash":
            return _bash(tool_input, tool_use_id, intervention_counter)
        elif tool_name == "read_file":
            return _read_file(tool_input, tool_use_id)
        elif tool_name == "write_file":
            return _write_file(tool_input, tool_use_id, intervention_counter)
        elif tool_name == "patch_self":
            return _patch_self(tool_input, tool_use_id, intervention_counter)
        elif tool_name == "patch_diff":
            return _patch_diff(tool_input, tool_use_id, intervention_counter)
        elif tool_name == "memory_write":
            return _memory_write(tool_input, tool_use_id)
        elif tool_name == "memory_read":
            return ToolResult(tool_use_id, mem.get_memory_context())
        elif tool_name == "list_self":
            return _list_self(tool_use_id)
        elif tool_name == "git_auto_commit":
            return _git_auto_commit(tool_input, tool_use_id, intervention_counter)
        else:
            return ToolResult(tool_use_id, f"Unknown tool: {tool_name}", is_error=True)
    except Exception as e:
        return ToolResult(tool_use_id, f"Tool error: {e}", is_error=True)


def _bash(inp: dict, tid: str, counter: list) -> ToolResult:
    cmd = inp["command"]
    timeout = inp.get("timeout", 30)

    allowed = permissions.request("bash", cmd)
    if not allowed:
        counter.append(1)
        return ToolResult(tid, f"[DENIED] Command not allowed: {cmd}", is_error=True)

    # Log command
    _log_action("bash", cmd)

    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        timeout=timeout, cwd=str(SOURCE_DIR)
    )
    output = result.stdout
    if result.stderr:
        output += f"\n[stderr] {result.stderr}"
    if result.returncode != 0:
        output += f"\n[exit code {result.returncode}]"
    return ToolResult(tid, output or "(no output)")


def _read_file(inp: dict, tid: str) -> ToolResult:
    path = Path(inp["path"])
    if not path.is_absolute():
        path = SOURCE_DIR / path
    if not path.exists():
        return ToolResult(tid, f"File not found: {path}", is_error=True)
    return ToolResult(tid, path.read_text())


def _write_file(inp: dict, tid: str, counter: list) -> ToolResult:
    path_str = inp["path"]
    path = Path(path_str)
    if not path.is_absolute():
        path = SOURCE_DIR / path

    allowed = permissions.request("write_file", str(path))
    if not allowed:
        counter.append(1)
        return ToolResult(tid, f"[DENIED] Write not allowed: {path}", is_error=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(inp["content"])
    _log_action("write_file", str(path))
    return ToolResult(tid, f"Written: {path}")


def _patch_self(inp: dict, tid: str, counter: list) -> ToolResult:
    fname = inp["file"]
    reason = inp["reason"]

    # Safety: only allow files within evolve/ dir
    target = SOURCE_DIR / fname
    if not str(target.resolve()).startswith(str(SOURCE_DIR.resolve())):
        return ToolResult(tid, f"[BLOCKED] Cannot modify files outside evolve/: {fname}", is_error=True)

    allowed = permissions.request("patch_self", fname)
    if not allowed:
        counter.append(1)
        return ToolResult(tid, f"[DENIED] patch_self not allowed for: {fname}", is_error=True)

    # Back up original
    original_content = None
    if target.exists():
        backup = target.with_suffix(target.suffix + ".bak")
        original_content = target.read_text()
        backup.write_text(original_content)

    target.write_text(inp["content"])

    # Verify syntax is valid before declaring success
    if fname.endswith(".py"):
        verify = subprocess.run(
            ["python3", "-m", "py_compile", str(target)],
            capture_output=True, text=True, cwd=str(SOURCE_DIR)
        )
        if verify.returncode != 0:
            # Restore backup on syntax error
            if original_content is not None:
                target.write_text(original_content)
            err = verify.stderr.strip()
            return ToolResult(tid, f"[SYNTAX ERROR] Patch rejected, original restored.\n{err}", is_error=True)

    mem.record_evolution(f"Patched {fname}", [fname], reason)
    _log_action("patch_self", f"{fname}: {reason}")
    return ToolResult(tid, f"Patched {fname}. Reason: {reason}")


def _patch_diff(inp: dict, tid: str, counter: list) -> ToolResult:
    fname = inp["file"]
    old_string = inp["old_string"]
    new_string = inp["new_string"]
    reason = inp["reason"]

    # Safety: only allow files within evolve/ dir
    target = SOURCE_DIR / fname
    if not str(target.resolve()).startswith(str(SOURCE_DIR.resolve())):
        return ToolResult(tid, f"[BLOCKED] Cannot modify files outside evolve/: {fname}", is_error=True)

    if not target.exists():
        return ToolResult(tid, f"[ERROR] File not found: {fname}", is_error=True)

    allowed = permissions.request("patch_self", fname)
    if not allowed:
        counter.append(1)
        return ToolResult(tid, f"[DENIED] patch_diff not allowed for: {fname}", is_error=True)

    original_content = target.read_text()

    # Verify old_string exists exactly once
    count = original_content.count(old_string)
    if count == 0:
        return ToolResult(tid, f"[ERROR] old_string not found in {fname}. Check exact whitespace/indentation.", is_error=True)
    if count > 1:
        return ToolResult(tid, f"[ERROR] old_string found {count} times in {fname}. Provide more context to make it unique.", is_error=True)

    # Back up and apply
    backup = target.with_suffix(target.suffix + ".bak")
    backup.write_text(original_content)
    new_content = original_content.replace(old_string, new_string, 1)
    target.write_text(new_content)

    # Verify syntax for Python files
    if fname.endswith(".py"):
        verify = subprocess.run(
            ["python3", "-m", "py_compile", str(target)],
            capture_output=True, text=True, cwd=str(SOURCE_DIR)
        )
        if verify.returncode != 0:
            target.write_text(original_content)
            err = verify.stderr.strip()
            return ToolResult(tid, f"[SYNTAX ERROR] Patch rejected, original restored.\n{err}", is_error=True)

    mem.record_evolution(f"patch_diff {fname}", [fname], reason)
    _log_action("patch_diff", f"{fname}: {reason}")
    return ToolResult(tid, f"patch_diff applied to {fname}. Reason: {reason}")


def _memory_write(inp: dict, tid: str) -> ToolResult:
    t = inp["type"]
    content = inp["content"]
    if t == "pattern":
        mem.append_to_list("learned_patterns", content)
    elif t == "note":
        mem.append_to_list("persistent_notes", content)
    elif t == "key_value":
        key = inp.get("key", "misc")
        mem.set_key(key, content)
    return ToolResult(tid, f"Memory saved: [{t}] {content[:80]}")


def _list_self(tid: str) -> ToolResult:
    files = sorted(SOURCE_DIR.glob("*.py"))
    lines = [f"{f.name} ({f.stat().st_size} bytes)" for f in files]
    return ToolResult(tid, "\n".join(lines))


def _git_auto_commit(inp: dict, tid: str, counter: list) -> ToolResult:
    message = inp["message"]

    allowed = permissions.request("bash", "git commit & push")
    if not allowed:
        counter.append(1)
        return ToolResult(tid, "[DENIED] git_auto_commit not allowed", is_error=True)

    try:
        # Stage all changes in the evolve/ directory
        add = subprocess.run(
            ["git", "add", "-A", "."],
            capture_output=True, text=True, timeout=15, cwd=str(SOURCE_DIR)
        )
        if add.returncode != 0:
            return ToolResult(tid, f"[ERROR] git add failed: {add.stderr}", is_error=True)

        # Check if there's anything to commit
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True, text=True, timeout=10, cwd=str(SOURCE_DIR)
        )
        if status.returncode == 0:
            return ToolResult(tid, "Nothing to commit — working tree clean.")

        # Commit
        commit = subprocess.run(
            ["git", "commit", "-m", message],
            capture_output=True, text=True, timeout=30, cwd=str(SOURCE_DIR)
        )
        if commit.returncode != 0:
            return ToolResult(tid, f"[ERROR] git commit failed: {commit.stderr}", is_error=True)

        # Push
        push = subprocess.run(
            ["git", "push"],
            capture_output=True, text=True, timeout=60, cwd=str(SOURCE_DIR)
        )
        if push.returncode != 0:
            return ToolResult(tid, f"Committed but push failed: {push.stderr}", is_error=True)

        _log_action("git_auto_commit", message)
        return ToolResult(tid, f"Committed and pushed: {message}\n{commit.stdout.strip()}")
    except subprocess.TimeoutExpired:
        return ToolResult(tid, "[ERROR] git operation timed out", is_error=True)


def _log_action(action: str, detail: str):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / "actions.jsonl"
    entry = json.dumps({"ts": time.time(), "action": action, "detail": detail[:500]})
    with open(log_file, "a") as f:
        f.write(entry + "\n")
