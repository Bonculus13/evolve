# Changelog

## v0.2.0 — 2026-03-06 (Auto-evolved)

### Features Added by Agent (Cycles 1–15+)

**Memory & Feedback Loop**
- `get_evolution_summary()` — injects self-patch history into every agent prompt so the agent knows what's already been tried
- `get_recent_lessons()` — reads `data/memory_log.jsonl` back into agent context (previously write-only — lessons were recorded but never re-read)
- `get_failure_summary()` — surfaces recent failed tasks with diagnostic notes so agent avoids repeating mistakes
- `get_success_trend()` — compares recent-10 vs overall success rate, labels IMPROVING/STABLE/DEGRADING
- Lesson deduplication — `get_recent_lessons()` deduplicates by prefix to prevent context window waste in long loops
- `data/logs/tasks.jsonl` — append-only per-task outcome log for easy `tail`/`grep` without parsing `memory.json`

**Self-Patching Safety**
- Syntax validation in `patch_self` — runs `python3 -m py_compile` after every patch; auto-restores backup on syntax error
- `patch_diff` tool — targeted `old_string → new_string` replacement instead of full-file rewrites; fails fast if `old_string` not found or ambiguous
- Expanded verification — imports `agent` and `orchestrator` modules in post-patch checks, not just `tools/memory/permissions`

**Orchestration Intelligence**
- `data/next_task.json` handoff — ASSESS cycles write a structured `{file, change, verify, reason}` plan; EVOLVE cycles execute it directly instead of deciding from scratch each time
- Fixed `error_max_turns` success bug — max-turns tasks were incorrectly recorded as successes; corrected to `success=False`
- Enriched failure notes — captures returncode, output line count, tool count in failure records
- Fixed `else: success=True` regression — a bare else clause was overriding the corrected max-turns failure flag
- Fixed verify command mismatch in ASSESS template — plans now use 5-module verify (was 3), catching breakage to agent.py/orchestrator.py

**New Modules (Human-added)**
- `profiler.py` — learns Jake's behavior from zsh history, Chrome history, recent files, running apps, hardware (GPU/Neural Engine). Syncs to Google Drive.
- `dashboard.py` — Flask web GUI at `http://localhost:7842` with live log stream, start/stop/one-cycle controls, task queue, memory viewer, GDrive sync

---

## v0.1.0 — 2026-03-06 (Initial Build)

- `orchestrator.py` — REPL + CLI entry point with `--auto-evolve`, `--evolve`, `--status` modes
- `agent.py` — Claude CLI subprocess driver (uses Claude Code subscription, no API credits)
- `tools.py` — Tool registry: `bash`, `read_file`, `write_file`, `patch_self`, `memory_write`, `memory_read`, `list_self`
- `permissions.py` — Permission cache with semantic matching; autonomous mode; hard-deny list
- `memory.py` — Persistent JSON memory, task history, efficiency metrics
- `config.py` — Central config with `.env` auto-load
