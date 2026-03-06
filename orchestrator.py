#!/usr/bin/env python3
"""
Evolve Orchestrator — runs on your Claude Code subscription, no API credits needed.

Usage:
  python3 orchestrator.py                          # interactive REPL
  python3 orchestrator.py "your task"              # single task
  python3 orchestrator.py --auto-evolve            # continuous autonomous evolution loop
  python3 orchestrator.py --auto-evolve --cycles N # run N cycles then stop
  python3 orchestrator.py --status                 # memory + evolution log
  python3 orchestrator.py --register-app NAME PATH TAG1,TAG2
  python3 orchestrator.py --replacement-report
  python3 orchestrator.py --archive-app NAME --reason "why"
"""
import sys
import json
import time
import signal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import agent
import memory as mem
import portfolio
from config import PERMISSIONS_FILE, EVOLUTION_LOG, DATA_DIR


EVOLVE_TASK = """\
You are in autonomous self-improvement mode for the evolve orchestrator at:
  /Users/jacobdieter/Projects/04_tools/evolve/

Steps:
1. Check for a pre-planned task: cat /Users/jacobdieter/Projects/04_tools/evolve/data/next_task.json 2>/dev/null
   - If the file exists and contains a valid JSON plan, execute that plan directly.
   - The plan will have: {"file": "<file to edit>", "change": "<description>", "verify": "<verification command>", "reason": "<why>"}
   - After executing a pre-planned task, DELETE the file: rm /Users/jacobdieter/Projects/04_tools/evolve/data/next_task.json
2. If no pre-planned task exists, proceed with your own judgment:
   a. List all .py files: ls /Users/jacobdieter/Projects/04_tools/evolve/*.py
   b. Read the files to understand the current codebase.
   c. Identify ONE concrete, safe improvement. Good targets:
      - Add a new capability (e.g. web search, summarization, git operations)
      - Improve error handling or output clarity
      - Make the evolution loop smarter (e.g. track which files changed most)
      - Add structured logging to data/logs/
3. Implement the change by editing the relevant file(s) directly.
   PREFER patch_diff over patch_self: patch_diff does targeted old→new string replacement
   and is much safer (fails immediately if old_string not found, no risk of losing file content).
   Only use patch_self if adding a large new section or the whole file needs rewriting.
4. Verify: cd /Users/jacobdieter/Projects/04_tools/evolve && python3 data/smoke_test.py
5. Append a lesson to data/memory_log.jsonl:
   python3 -c "
import json, time
entry = {'ts': time.time(), 'lesson': 'DESCRIBE WHAT YOU CHANGED AND WHY'}
with open('data/memory_log.jsonl', 'a') as f: f.write(json.dumps(entry) + chr(10))
"

Be decisive. Make a real, meaningful change. Avoid cosmetic edits.
"""

ASSESS_TASK = """\
Review recent activity, research AI trends, profile the user and environment, then write a structured next-task plan:

1. Read memory and recent logs:
   tail -20 /Users/jacobdieter/Projects/04_tools/evolve/data/memory_log.jsonl 2>/dev/null
   ls -la /Users/jacobdieter/Projects/04_tools/evolve/*.py

2. Profile Jake's environment and hardware:
   python3 -c "
import sys; sys.path.insert(0, '/Users/jacobdieter/Projects/04_tools/evolve')
from profiler import build_profile, get_profile_summary
build_profile()
print(get_profile_summary())
"
   Also run: system_profiler SPHardwareDataType 2>/dev/null | grep -E "Model|Processor|Memory|GPU"
   And: ioreg -l | grep -i "gpu\|metal\|neural" 2>/dev/null | head -10

3. Research latest AI developments to inform evolution direction:
   Search for: "latest AI agent frameworks 2025 autonomous"
   Search for: "Claude API new features tools 2025"
   Search for: "self-improving AI code generation best practices"
   Use WebSearch tool if available, otherwise use:
   curl -s "https://hnrss.org/newest?q=AI+agent&count=5" 2>/dev/null | grep -o '<title>[^<]*' | head -10

4. Based on Jake's profile, hardware, AI news, and past lessons — identify the most impactful next improvement.
   Consider: Does new hardware suggest new capabilities? Do AI trends suggest better approaches?

5. Write a structured plan to data/next_task.json so the next EVOLVE cycle executes it directly:
   python3 -c "
import json
plan = {
    'file': '<filename like memory.py or tools.py>',
    'change': '<exact description of what to change>',
    'verify': 'python3 -c \\"import tools, memory, permissions, agent, orchestrator; print(chr(79)+chr(75))\\"',
    'reason': '<why this improves the system>'
}
with open('/Users/jacobdieter/Projects/04_tools/evolve/data/next_task.json', 'w') as f:
    f.write(json.dumps(plan, indent=2))
"
6. Also write a summary lesson to data/memory_log.jsonl:
   python3 -c "
import json, time
entry = {'ts': time.time(), 'lesson': 'NEXT IMPROVEMENT PLAN: <your plan here>'}
with open('data/memory_log.jsonl', 'a') as f: f.write(json.dumps(entry) + chr(10))
"
"""

_running = True
ORCHESTRATOR_CAPABILITY_TAGS = [
    "task_orchestration",
    "self_patch",
    "troubleshooting",
    "permissions_cache",
    "memory",
    "tool_execution",
]


TASK_QUEUE_FILE = DATA_DIR / "task_queue.json"


def _pop_task() -> str | None:
    """Pop the next task from the queue file, if any."""
    if not TASK_QUEUE_FILE.exists():
        return None
    try:
        tasks = json.loads(TASK_QUEUE_FILE.read_text())
        if not tasks:
            return None
        task = tasks.pop(0)
        TASK_QUEUE_FILE.write_text(json.dumps(tasks, indent=2))
        return task
    except Exception:
        return None


def _auto_push(cycle: int):
    """Commit and push any changes after each cycle."""
    try:
        import subprocess
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=str(Path(__file__).parent)).stdout.strip()
        if not status:
            return
        subprocess.run(["git", "add", "-A", "--", ":!.env", ":!data/memory.json", ":!data/permissions.json", ":!data/user_profile.json", ":!data/.current_prompt.txt", ":!*.bak"], cwd=str(Path(__file__).parent))
        subprocess.run(["git", "commit", "-m", f"auto-evolve cycle {cycle}\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"], capture_output=True, cwd=str(Path(__file__).parent))
        subprocess.run(["git", "push"], capture_output=True, cwd=str(Path(__file__).parent))
        print(f"[GIT] Pushed changes from cycle {cycle}", flush=True)
    except Exception as e:
        print(f"[GIT] Push failed: {e}", flush=True)


def _handle_sigint(sig, frame):
    global _running
    print("\n\n[Stopping after current cycle — Ctrl+C again to force quit]")
    _running = False
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def print_status():
    print("\n=== EVOLVE STATUS ===")
    print("\n-- Memory --")
    print(mem.get_memory_context())

    log_file = Path("data/memory_log.jsonl")
    if log_file.exists():
        lines = log_file.read_text().strip().splitlines()
        print(f"\n-- Memory Log (last 5 of {len(lines)}) --")
        for line in lines[-5:]:
            try:
                e = json.loads(line)
                print(f"  {e.get('lesson', '')[:100]}")
            except Exception:
                print(f"  {line[:100]}")

    if EVOLUTION_LOG.exists():
        log = json.loads(EVOLUTION_LOG.read_text())
        print(f"\n-- Evolution Log ({len(log)} entries) --")
        for entry in log[-3:]:
            print(f"  [{entry.get('description')}] {entry.get('reason', '')[:80]}")

    print("=" * 20)


def auto_evolve_loop(max_cycles: int | None = None, pause_s: int = 5):
    global _running
    signal.signal(signal.SIGINT, _handle_sigint)

    print(f"\n{'='*60}")
    print("AUTO-EVOLVE — Ctrl+C to stop gracefully")
    print(f"{'='*60}")

    cycle = 0
    while _running:
        if max_cycles and cycle >= max_cycles:
            print(f"\n[Done] Completed {cycle} cycles.")
            break

        cycle += 1
        print(f"\n{'─'*60}")
        print(f"[CYCLE {cycle}] {time.strftime('%H:%M:%S')}")
        print(f"{'─'*60}")

        # Check task queue first
        queued = _pop_task()
        if queued:
            print(f"[CYCLE TYPE] Queued task: {queued[:60]}")
            agent.run_task(queued)
        elif cycle % 4 == 0:
            print("[CYCLE TYPE] Assessment")
            agent.run_task(ASSESS_TASK)
        else:
            print("[CYCLE TYPE] Self-improvement")
            agent.run_task(EVOLVE_TASK)

        # Auto-commit and push after each successful cycle
        _auto_push(cycle)

        if not _running:
            break

        if max_cycles is None or cycle < max_cycles:
            print(f"\n[Pausing {pause_s}s — Ctrl+C to stop]")
            for _ in range(pause_s):
                if not _running:
                    break
                time.sleep(1)

    print(f"\n[AUTO-EVOLVE] Stopped after {cycle} cycles.")
    print_status()


def repl():
    print("Evolve — enter a task, 'evolve', 'auto', 'status', or 'quit'.")
    while True:
        try:
            task = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not task:
            continue
        if task.lower() in ("quit", "exit", "q"):
            break
        if task.lower() == "status":
            print_status()
        elif task.lower() == "evolve":
            agent.run_task(EVOLVE_TASK)
        elif task.lower() == "auto":
            auto_evolve_loop()
        else:
            agent.run_task(task)


def main():
    args = sys.argv[1:]

    if not args:
        repl()
        return

    if "--status" in args:
        print_status()
        return

    if "--register-app" in args:
        idx = args.index("--register-app")
        try:
            name = args[idx + 1]
            path = args[idx + 2]
            tags = [t.strip() for t in args[idx + 3].split(",") if t.strip()]
        except (IndexError, ValueError):
            print("Usage: --register-app NAME PATH TAG1,TAG2")
            sys.exit(1)
        portfolio.register_app(name=name, path=path, capability_tags=tags)
        print(f"Registered app: {name}")
        return

    if "--replacement-report" in args:
        report = portfolio.build_replacement_report(ORCHESTRATOR_CAPABILITY_TAGS)
        print(portfolio.render_replacement_report(report))
        return

    if "--archive-app" in args:
        idx = args.index("--archive-app")
        try:
            name = args[idx + 1]
        except (IndexError, ValueError):
            print("Usage: --archive-app NAME --reason \"why\"")
            sys.exit(1)

        reason = "replaced by evolve orchestrator"
        if "--reason" in args:
            ridx = args.index("--reason")
            try:
                reason = args[ridx + 1]
            except (IndexError, ValueError):
                pass

        entry = portfolio.archive_app(name=name, reason=reason)
        print(f"Archived {entry['app']} -> {entry['to']}")
        return

    if "--auto-evolve" in args:
        max_cycles = None
        if "--cycles" in args:
            idx = args.index("--cycles")
            try:
                max_cycles = int(args[idx + 1])
            except (IndexError, ValueError):
                pass
        pause = 5
        if "--pause" in args:
            idx = args.index("--pause")
            try:
                pause = int(args[idx + 1])
            except (IndexError, ValueError):
                pass
        auto_evolve_loop(max_cycles=max_cycles, pause_s=pause)
        return

    if "--evolve" in args:
        agent.run_task(EVOLVE_TASK)
        return

    task = " ".join(a for a in args if not a.startswith("--"))
    result = agent.run_task(task)
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
