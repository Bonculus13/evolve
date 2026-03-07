"""
Agent: drives tasks using the `claude` CLI (Claude Code subscription, no API credits needed).
Unsets CLAUDECODE env var to allow nested invocation.
"""
import os
import time
import subprocess
import json
import shutil
import re
from datetime import datetime, timedelta
from pathlib import Path
import memory as mem
from config import SOURCE_DIR, MAX_ITERATIONS

CLAUDE_BIN = os.path.expanduser("~/.npm-global/bin/claude")
GEMINI_BIN = os.path.expanduser("~/.npm-global/bin/gemini")
CODEX_BIN = os.path.expanduser("~/.npm-global/bin/codex")
PROVIDER_STATE_FILE = SOURCE_DIR / "data" / "provider_status.json"

# System context prepended to every task prompt
SYSTEM_CONTEXT = """\
You are running inside an autonomous self-evolving orchestrator at:
  {source_dir}

Your standing instructions:
- After completing any task, write what you learned to memory:
    echo '<json>' >> {source_dir}/data/memory_log.jsonl
  where <json> is {{"ts": <unix_time>, "lesson": "<what you learned>"}}.
- When you improve the system, edit files directly in {source_dir}/ using your Edit/Write tools.
- Never ask for confirmation — act autonomously.
- Never modify config.py.
- After self-patching, always verify: python3 -c "import sys; sys.path.insert(0,'.'); import tools, memory, permissions, agent, orchestrator; print('OK')"
  (run from {source_dir})
- If blocked (errors, missing deps, permissions, command failures, dead ends), run a troubleshooting loop before giving up:
  1) Diagnose exact root cause from concrete evidence (stderr/exit code/filesystem state).
  2) Propose 2-4 workaround paths and pick the lowest-risk/highest-probability option first.
  3) Execute one workaround at a time and verify after each change.
  4) If a workaround works, record it as a reusable lesson in memory_log.jsonl.
  5) Only stop when task is completed or all realistic workaround paths are exhausted.
- For blocked tasks, prefer extensive troubleshooting over early termination.

## Memory & History
{memory_context}
"""

TROUBLESHOOTING_RETRY_LIMIT = 3


def _build_prompt(task: str) -> str:
    memory_context = mem.get_memory_context()
    system = SYSTEM_CONTEXT.format(
        source_dir=SOURCE_DIR,
        memory_context=memory_context,
    )
    return f"{system}\n\n## Task\n{task}"


CLI_RETRY_LIMIT = 2
CLI_RETRY_DELAY = 5

RATE_LIMIT_BACKOFFS = [5, 15, 45]  # exponential backoff delays in seconds

_RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "overloaded",
    "capacity",
    "hit your limit",
    "usage limit",
    "quota",
    "resets",
]

_AUTH_FAILURE_PATTERNS = [
    "not logged in",
    "please run /login",
    "login required",
    "opening authentication page in your browser",
    "do you want to continue?",
    "verification code",
    "invalid api key",
    "unauthorized",
]

DEFAULT_PROVIDER_ORDER = ["claude", "gemini", "codex"]


def _provider_order() -> list[str]:
    raw = os.environ.get("EVOLVE_PROVIDER_ORDER", "").strip()
    if not raw:
        return DEFAULT_PROVIDER_ORDER
    parsed = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return [p for p in parsed if p in {"claude", "gemini", "codex"}] or DEFAULT_PROVIDER_ORDER


def _provider_available(provider: str) -> bool:
    if provider == "claude":
        return bool(shutil.which(CLAUDE_BIN) or shutil.which("claude"))
    if provider == "gemini":
        return bool(shutil.which(GEMINI_BIN) or shutil.which("gemini"))
    if provider == "codex":
        return bool(shutil.which(CODEX_BIN) or shutil.which("codex"))
    return False


def _save_provider_state(active: str, reason: str = "", attempts: dict | None = None):
    try:
        PROVIDER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "timestamp": time.time(),
            "active_provider": active,
            "reason": reason,
            "attempts": attempts or {},
        }
        PROVIDER_STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def _is_rate_limit_error(stderr: str, output_lines: list, final_text: str = "") -> bool:
    """Check if the error is a rate limit / overload error."""
    if _is_auth_failure(stderr, output_lines, final_text):
        return False
    text = (stderr + " " + " ".join(output_lines[-5:]) + " " + (final_text or "")).lower()
    return any(p in text for p in _RATE_LIMIT_PATTERNS)


def _is_auth_failure(stderr: str, output_lines: list, final_text: str = "") -> bool:
    text = (stderr + " " + " ".join(output_lines[-8:]) + " " + (final_text or "")).lower()
    return any(p in text for p in _AUTH_FAILURE_PATTERNS)


def _is_hard_provider_failure(stderr: str, output_lines: list, provider: str) -> bool:
    text = (stderr + " " + " ".join(output_lines[-20:])).lower()
    if provider == "codex" and "reqwest-internal-sync-runtime" in text and "panicked" in text:
        return True
    return False


def _extract_reset_cooldown_seconds(text: str) -> int | None:
    """Parse reset time text like 'resets 12pm' and return cooldown seconds."""
    if not text:
        return None
    m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text.lower())
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or "0")
    meridiem = m.group(3)
    if meridiem == "pm" and hour != 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    now = datetime.now()
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return int((reset - now).total_seconds())


def _spawn_cli(cmd: list, prompt: str, env: dict, provider: str) -> tuple[bool, list, list, str, object, str]:
    """Internal helper to spawn a CLI process and parse its stream-json output."""
    success = False
    output_lines = []
    tool_calls = []
    final_text = ""
    stderr = ""
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(SOURCE_DIR),
        )

        if provider == "claude":
            proc.stdin.write(prompt)
        proc.stdin.close()

        for raw_line in proc.stdout:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            output_lines.append(raw_line)

            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            if etype == "assistant":
                content = event.get("message", {}).get("content", [])
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            txt = block.get("text", "").strip()
                            if txt:
                                print(f"\n[{provider.capitalize()}] {txt[:500]}", flush=True)
                                final_text = txt
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            summary = str(inp)[:120]
                            print(f"[TOOL] {name}({summary})", flush=True)
                            tool_calls.append(name)

            elif etype == "result":
                result_type = event.get("subtype", "")
                if result_type == "success":
                    final_text = event.get("result", "")
                    print(f"\n[RESULT] {final_text[:300]}", flush=True)
                    success = True
                elif result_type == "error_max_turns":
                    print("[WARN] Max turns reached — task incomplete, marking as failure")
                    success = False
                else:
                    print(f"[RESULT] {event}")
            elif provider == "codex":
                msg = event.get("message") or event.get("content") or event.get("output")
                if isinstance(msg, str) and msg.strip():
                    final_text = msg.strip()
                elif isinstance(msg, list):
                    for part in msg:
                        if isinstance(part, dict):
                            txt = part.get("text", "").strip()
                            if txt:
                                final_text = txt

        stderr = proc.stderr.read()
        proc.wait()

        # Some CLIs return successful plain/JSON lines without a "result.success" event.
        if not success and proc.returncode == 0 and output_lines and not _is_rate_limit_error(stderr, output_lines):
            if not final_text:
                final_text = output_lines[-1][:1000]
            success = True

    except Exception as e:
        print(f"[ERROR] Spawning {provider} failed: {e}")
        if proc:
            proc.kill()

    return success, output_lines, tool_calls, final_text, proc, stderr


def _run_once(prompt: str, env: dict, label: str = "", provider: str = "claude") -> tuple[bool, list, list, str, object, str]:
    """Run provider CLI with prompt. Returns (success, output_lines, tool_calls, final_text, proc, stderr).
    Retries up to CLI_RETRY_LIMIT times with CLI_RETRY_DELAY seconds between attempts on subprocess failure."""
    if provider == "claude":
        cmd = [
            CLAUDE_BIN,
            "--print",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            "--add-dir", str(SOURCE_DIR),
        ]
    elif provider == "gemini":
        cmd = [
            GEMINI_BIN,
            "--yolo",
            "--output-format",
            "stream-json",
            "--prompt",
            prompt,
        ]
    elif provider == "codex":
        cmd = [
            CODEX_BIN,
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--cd",
            str(SOURCE_DIR),
            "--json",
            prompt,
        ]
    else:
        return False, [], [], "", None, f"unsupported provider: {provider}"

    for cli_attempt in range(1 + CLI_RETRY_LIMIT):
        if label:
            print(f"[AGENT] Spawning {provider} CLI {label}...", flush=True)
        else:
            print(f"[AGENT] Spawning {provider} CLI...", flush=True)

        if cli_attempt > 0:
            print(f"[AGENT] CLI retry {cli_attempt}/{CLI_RETRY_LIMIT} after {CLI_RETRY_DELAY}s delay...", flush=True)

        success, output_lines, tool_calls, final_text, proc, stderr = _spawn_cli(cmd, prompt, env, provider)
        subprocess_failed = (proc.returncode not in (0, None) and not success) if proc else True

        if subprocess_failed and not success:
            print(f"[ERROR] {provider} exited {proc.returncode if proc else 'N/A'}")
            if stderr:
                print(f"  stderr: {stderr[:300]}")

        # Check for rate limit errors — these get their own exponential backoff
        if (subprocess_failed or not success) and _is_rate_limit_error(stderr, output_lines, final_text):
            for rl_attempt, delay in enumerate(RATE_LIMIT_BACKOFFS):
                print(f"[AGENT] Rate limit detected — backing off {delay}s "
                      f"(attempt {rl_attempt + 1}/{len(RATE_LIMIT_BACKOFFS)})...", flush=True)
                time.sleep(delay)
                success, output_lines, tool_calls, final_text, proc, stderr = _spawn_cli(cmd, prompt, env, provider)
                # If succeeded or no longer a rate limit error, stop backing off
                if success or not _is_rate_limit_error(stderr, output_lines, final_text):
                    break
            # After rate limit retries, return whatever we have
            break

        # If auth/hard provider failure occurs, avoid expensive local retries.
        if _is_auth_failure(stderr, output_lines, final_text):
            break
        if _is_hard_provider_failure(stderr, output_lines, provider):
            break
        # If subprocess succeeded (even if task failed), or we exhausted retries, return
        if not subprocess_failed or cli_attempt >= CLI_RETRY_LIMIT:
            break

        # Wait before retrying
        time.sleep(CLI_RETRY_DELAY)

    return success, output_lines, tool_calls, final_text, proc, stderr


def _extract_failure_context(final_text: str, stderr: str, output_lines: list, proc) -> tuple[str, str]:
    rc = proc.returncode if proc is not None else "N/A"
    signal = (final_text or stderr or "")
    if not signal and output_lines:
        signal = output_lines[-1]
    signal = signal[-800:].strip() if signal else "no output captured"
    return str(rc), signal


def _build_troubleshooting_prefix(attempt_idx: int, rc: str, signal: str) -> str:
    common = (
        f"[RETRY CONTEXT] Previous attempt failed (exit_code={rc}).\n"
        f"Last failure signal:\n{signal[:500]}\n\n"
    )
    if attempt_idx == 1:
        return common + (
            "Troubleshooting pass 1:\n"
            "- Identify exact root cause from evidence.\n"
            "- Run focused diagnostics.\n"
            "- Try the most likely workaround.\n\n"
        )
    if attempt_idx == 2:
        return common + (
            "Troubleshooting pass 2:\n"
            "- Assume previous workaround was insufficient.\n"
            "- Try a materially different workaround strategy.\n"
            "- Verify result with explicit checks.\n\n"
        )
    return common + (
        "Troubleshooting pass 3:\n"
        "- Use conservative fallback path to complete task.\n"
        "- If full success is impossible, still deliver maximum partial completion with evidence.\n\n"
    )


def run_task(task: str, max_retries: int = TROUBLESHOOTING_RETRY_LIMIT) -> dict:
    start = time.time()
    print(f"\n[AGENT] Starting task")
    print("─" * 60)

    # Strip vars that prevent nested Claude Code or force API-key billing
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_API_KEY")}

    prompt = _build_prompt(task)
    # Write prompt to temp file for debugging
    prompt_file = SOURCE_DIR / "data" / ".current_prompt.txt"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(prompt)

    success = False
    output_lines = []
    tool_calls = []
    final_text = ""
    proc = None
    stderr = ""
    selected_provider = "claude"
    attempts_by_provider = {}
    any_rate_limited = False
    rate_limit_text = ""

    for provider in _provider_order():
        if not _provider_available(provider):
            attempts_by_provider[provider] = "unavailable"
            continue

        selected_provider = provider
        print(f"[AGENT] Provider attempt: {provider}")
        success, output_lines, tool_calls, final_text, proc, stderr = _run_once(
            prompt, env, provider=provider
        )
        auth_blocked = _is_auth_failure(stderr, output_lines, final_text)
        if auth_blocked:
            print(f"[AGENT] {provider} auth/login required; skipping further retries for this provider.")

        # Structured troubleshooting retries on failure, within the same provider.
        for attempt_idx in range(1, max_retries + 1):
            if success or auth_blocked:
                break
            rc, failure_signal = _extract_failure_context(final_text, stderr, output_lines, proc)
            retry_prefix = _build_troubleshooting_prefix(attempt_idx, rc, failure_signal)
            retry_prompt = _build_prompt(retry_prefix + task)
            print(f"\n[AGENT] {provider} attempt {attempt_idx} failed - troubleshooting pass {attempt_idx}...")
            success, out2, tc2, ft2, proc2, stderr2 = _run_once(
                retry_prompt, env, label=f"(troubleshoot-{attempt_idx})", provider=provider
            )
            output_lines += out2
            tool_calls += tc2
            if ft2:
                final_text = ft2
            if proc2 is not None:
                proc = proc2
            if stderr2:
                stderr = stderr2
            if _is_auth_failure(stderr, output_lines, final_text):
                auth_blocked = True
                print(f"[AGENT] {provider} auth/login required during troubleshooting; stopping retries.")
                break

        attempts_by_provider[provider] = "success" if success else "failed"
        if success:
            _save_provider_state(provider, "task_succeeded", attempts_by_provider)
            break
        if _is_rate_limit_error(stderr, output_lines, final_text):
            any_rate_limited = True
            rate_limit_text = ((final_text or "") + "\n" + (stderr or "") + "\n" + "\n".join(output_lines[-8:])).strip()
            _save_provider_state(provider, "rate_limited_failover", attempts_by_provider)
            continue

    if not success:
        _save_provider_state(selected_provider, "all_providers_failed", attempts_by_provider)

    if success and max_retries > 0:
        mem.append_to_list(
            "learned_patterns",
            "When blocked, run staged troubleshooting: diagnose root cause, try distinct workaround paths, and verify each step.",
        )

    duration = time.time() - start

    failure_notes = ""
    if not success:
        rc = proc.returncode if proc is not None else "N/A"
        prefix = f"rc={rc} out_lines={len(output_lines)} tools={len(tool_calls)}: "
        failure_notes = prefix + (final_text or stderr or "no output captured")[-200:].strip()
        if any_rate_limited:
            failure_notes = "rate_limited=true " + failure_notes

    # Track provider effectiveness by hour to enable tool-use policy learning.
    hour_key = datetime.now().strftime("%H")
    mem.append_to_list(
        "provider_outcomes",
        {
            "ts": time.time(),
            "hour": hour_key,
            "provider": selected_provider,
            "success": success,
            "tool_calls": len(tool_calls),
            "duration_s": round(duration, 2),
        },
    )

    mem.record_task(
        task=task[:120],
        success=success,
        approach=f"{selected_provider}-cli, {len(tool_calls)} tools: {', '.join(set(tool_calls))[:80]}",
        interventions=0,
        duration_s=duration,
        notes=failure_notes,
        rate_limited=any_rate_limited,
    )

    if not success and any_rate_limited:
        cooldown_s = _extract_reset_cooldown_seconds(rate_limit_text) or 3600
        print(f"[AGENT] Rate limit cooldown engaged for {cooldown_s}s", flush=True)
        time.sleep(cooldown_s)

    print("\n" + "─" * 60)
    print(f"[AGENT] Done. success={success}, tools_used={len(tool_calls)}, time={duration:.1f}s")

    return {
        "success": success,
        "interventions": 0,
        "duration_s": duration,
        "final_response": final_text,
        "rate_limited": any_rate_limited,
        "provider": selected_provider,
    }
