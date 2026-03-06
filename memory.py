"""
Persistent memory: stores learned patterns, task outcomes, efficiency insights.
The agent reads this at session start and writes to it as it learns.
"""
import json
import time
from pathlib import Path
from config import MEMORY_FILE, EVOLUTION_LOG


def _load_file(path: Path) -> dict | list:
    if path.exists():
        return json.loads(path.read_text())
    return {} if path == MEMORY_FILE else []


def _save_file(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def get_all() -> dict:
    data = _load_file(MEMORY_FILE)
    if not isinstance(data, dict):
        return {}
    return data


def set_key(key: str, value):
    data = get_all()
    data[key] = value
    _save_file(MEMORY_FILE, data)


def get_key(key: str, default=None):
    return get_all().get(key, default)


def append_to_list(key: str, item):
    data = get_all()
    lst = data.get(key, [])
    if key in {"learned_patterns", "persistent_notes"} and item in lst:
        return
    lst.append(item)
    if key in {"task_history", "provider_outcomes", "causal_log"} and len(lst) > 1000:
        lst = lst[-1000:]
    data[key] = lst
    _save_file(MEMORY_FILE, data)


def compress_memory():
    """Prune low-signal repetition while keeping recent/high-signal context."""
    data = get_all()
    patterns = data.get("learned_patterns", [])
    deduped_patterns = []
    seen = set()
    for p in patterns:
        key = str(p).strip().lower()[:120]
        if key and key not in seen:
            seen.add(key)
            deduped_patterns.append(p)
    data["learned_patterns"] = deduped_patterns[-100:]
    for key in ("task_history", "provider_outcomes", "causal_log"):
        seq = data.get(key, [])
        if isinstance(seq, list) and len(seq) > 1000:
            data[key] = seq[-1000:]
    _save_file(MEMORY_FILE, data)


def record_task(task: str, success: bool, approach: str, interventions: int,
                duration_s: float, notes: str = "", rate_limited: bool = False):
    entry = {
        "timestamp": time.time(),
        "task": task,
        "success": success,
        "rate_limited": rate_limited,
        "approach": approach,
        "human_interventions": interventions,
        "duration_s": round(duration_s, 2),
        "notes": notes,
    }
    append_to_list("task_history", entry)
    # Also write to append-only JSONL log for easy tail/grep analysis
    from config import LOGS_DIR
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / "tasks.jsonl"
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def record_evolution(description: str, files_changed: list[str], reason: str):
    data = _load_file(EVOLUTION_LOG)
    if not isinstance(data, list):
        data = []
    data.append({
        "timestamp": time.time(),
        "description": description,
        "files_changed": files_changed,
        "reason": reason,
    })
    _save_file(EVOLUTION_LOG, data)


def record_causal_event(hypothesis: str, expected: str, observed: str, fitness: float):
    """Store causal patch metadata to improve future planning decisions."""
    append_to_list(
        "causal_log",
        {
            "timestamp": time.time(),
            "hypothesis": hypothesis[:240],
            "expected": expected[:240],
            "observed": observed[:320],
            "fitness": round(float(fitness), 4),
        },
    )


def get_efficiency_summary() -> str:
    """Return a text summary of past performance for the agent to learn from."""
    data = get_all()
    history = data.get("task_history", [])
    if not history:
        return "No task history yet."

    total = len(history)
    successes = sum(1 for t in history if t["success"])
    avg_interventions = sum(t["human_interventions"] for t in history) / total
    avg_duration = sum(t["duration_s"] for t in history) / total
    learned = data.get("learned_patterns", [])

    lines = [
        f"Task history: {total} tasks, {successes} succeeded",
        f"Avg human interventions per task: {avg_interventions:.1f}",
        f"Avg task duration: {avg_duration:.1f}s",
        f"Learned patterns: {len(learned)}",
    ]
    if learned:
        lines.append("Key patterns: " + "; ".join(learned[-5:]))
    return "\n".join(lines)


def get_evolution_summary() -> str:
    """Summarize recent self-patches so agent knows what was already tried."""
    data = _load_file(EVOLUTION_LOG)
    if not isinstance(data, list) or not data:
        return "No self-patches yet."

    # Count file change frequency
    file_counts: dict = {}
    for entry in data:
        for f in entry.get("files_changed", []):
            file_counts[f] = file_counts.get(f, 0) + 1

    lines = [f"Total self-patches: {len(data)}"]

    if file_counts:
        ranked = sorted(file_counts.items(), key=lambda x: x[1], reverse=True)
        lines.append("Files changed most: " + ", ".join(f"{f}({n}x)" for f, n in ranked[:5]))

    recent = data[-5:]
    lines.append("Recent patches:")
    for e in recent:
        ts = time.strftime("%m-%d %H:%M", time.localtime(e.get("timestamp", 0)))
        files = ", ".join(e.get("files_changed", []))
        reason = e.get("reason", "")[:80]
        lines.append(f"  [{ts}] {files}: {reason}")

    return "\n".join(lines)


def get_recent_lessons(n: int = 10) -> list[str]:
    """Read the last n unique lessons from data/memory_log.jsonl.

    Deduplicates by first-60-char prefix so repeated identical lessons
    don't fill the context window. Returns the most recent unique ones.
    """
    log_file = MEMORY_FILE.parent / "memory_log.jsonl"
    if not log_file.exists():
        return []
    all_lessons = []
    for line in log_file.read_text().strip().splitlines():
        try:
            entry = json.loads(line)
            lesson = entry.get("lesson", "").strip()
            if lesson:
                all_lessons.append(lesson)
        except Exception:
            pass
    # Deduplicate from the end (keep most recent unique lessons)
    seen_prefixes: set[str] = set()
    unique: list[str] = []
    for lesson in reversed(all_lessons):
        prefix = lesson[:60].lower()
        if prefix not in seen_prefixes:
            seen_prefixes.add(prefix)
            unique.append(lesson)
        if len(unique) >= n:
            break
    unique.reverse()
    return unique


def get_failure_summary(n: int = 5) -> str:
    """Return recent failed tasks with notes so agents can avoid repeating mistakes."""
    data = get_all()
    history = data.get("task_history", [])
    failures = [t for t in history if not t.get("success")]
    if not failures:
        return ""
    recent = failures[-n:]
    lines = [f"Recent failures ({len(failures)} total):"]
    for t in recent:
        ts = time.strftime("%m-%d %H:%M", time.localtime(t.get("timestamp", 0)))
        task_snippet = t.get("task", "")[:80]
        notes = t.get("notes", "").strip()
        lines.append(f"  [{ts}] {task_snippet}")
        if notes:
            lines.append(f"    reason: {notes[:200]}")
    return "\n".join(lines)


def get_success_trend(recent_n: int = 10) -> str:
    """Compare success rate of last N tasks vs overall to show if system is improving."""
    data = get_all()
    history = data.get("task_history", [])
    if len(history) < 2:
        return ""
    total = len(history)
    overall_rate = sum(1 for t in history if t["success"]) / total
    recent = history[-recent_n:]
    recent_rate = sum(1 for t in recent if t["success"]) / len(recent)
    delta = recent_rate - overall_rate
    trend = "IMPROVING" if delta > 0.05 else ("DEGRADING" if delta < -0.05 else "STABLE")
    return (
        f"Success trend ({len(recent)} recent vs {total} total): "
        f"recent={recent_rate:.0%}, overall={overall_rate:.0%} → {trend}"
    )


def get_memory_context() -> str:
    """Full context block for agent system prompt."""
    data = get_all()
    sections = []

    summary = get_efficiency_summary()
    trend = get_success_trend()
    if trend:
        summary += f"\n{trend}"
    sections.append(f"## Performance History\n{summary}")

    failure_summary = get_failure_summary()
    if failure_summary:
        sections.append(f"## Recent Failures\n{failure_summary}")

    evo_summary = get_evolution_summary()
    sections.append(f"## Self-Patch History\n{evo_summary}")

    lessons = get_recent_lessons(10)
    if lessons:
        sections.append("## Recent Lessons (from memory_log.jsonl)\n" + "\n".join(f"- {l}" for l in lessons))

    patterns = data.get("learned_patterns", [])
    if patterns:
        sections.append("## Learned Efficiency Patterns\n" + "\n".join(f"- {p}" for p in patterns))

    notes = data.get("persistent_notes", [])
    if notes:
        sections.append("## Persistent Notes\n" + "\n".join(f"- {n}" for n in notes[-10:]))

    causal = data.get("causal_log", [])
    if causal:
        lines = []
        for c in causal[-6:]:
            ts = time.strftime("%m-%d %H:%M", time.localtime(c.get("timestamp", 0)))
            lines.append(
                f"- [{ts}] fit={c.get('fitness', 0):.2f} h={c.get('hypothesis', '')[:80]} -> {c.get('observed', '')[:90]}"
            )
        sections.append("## Causal Metadata\n" + "\n".join(lines))

    return "\n\n".join(sections) if sections else "No prior memory."
