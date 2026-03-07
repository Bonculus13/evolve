#!/usr/bin/env python3
"""
Evolve Orchestrator — runs on your Claude Code subscription, no API credits needed.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import agent
import memory as mem
import portfolio
import profiler
from config import DATA_DIR, EVOLUTION_LOG
from evolution_engine import EvolutionEngine, build_patch_fingerprint


EVOLVE_TASK = """\
You are in autonomous self-improvement mode for the evolve orchestrator at:
  /Users/jacobdieter/Projects/04_tools/evolve/

Execution target:
- Identify one high-utility change with clear expected impact.
- Include hypothesis, implementation, verification, and rollback note.
- Prefer changes that improve intelligence speed, reliability, or watchability.

Mandatory steps:
1. Read context and recent failures.
2. Implement one safe, concrete improvement.
3. Verify with: cd /Users/jacobdieter/Projects/04_tools/evolve && python3 data/smoke_test.py
4. Write lesson to data/memory_log.jsonl with observed result and next action.
"""

ASSESS_TASK = """\
Assessment mode:
1. Read recent memory + evolution logs.
2. Update a now/next/later plan with specific technical steps.
3. Identify weakest modules and propose one high-confidence improvement.
4. Save structured next task into data/next_task.json.
"""

STABILIZE_TASK = """\
Stabilization mode:
1. Focus only on hardening and regression prevention.
2. Improve tests, guards, metrics, and failure handling.
3. Verify smoke tests and ensure clean imports.
"""

CHALLENGE_TASK = """\
Challenge mode (fun + exploration):
1. You have a constrained cycle: one small patch and one strong verification.
2. Prioritize novelty and observability.
3. Emit clear narrative: hypothesis -> action -> result.
"""

_running = True
ENGINE = EvolutionEngine()
LAST_RESULTS: list[bool] = []
TASK_QUEUE_FILE = DATA_DIR / "task_queue.json"
NEXT_TASK_FILE = DATA_DIR / "next_task.json"
_AUTH_PROBE_CACHE = {"ts": 0.0, "ok": False}

ORCHESTRATOR_CAPABILITY_TAGS = [
    "task_orchestration",
    "self_patch",
    "troubleshooting",
    "permissions_cache",
    "memory",
    "tool_execution",
    "evolution_engine",
    "fitness_optimization",
    "timeline_visualization",
]


def _provider_probe(provider: str) -> dict:
    cmd = []
    prompt = "Reply with OK only."
    if provider == "claude":
        exe = agent.CLAUDE_BIN
        cmd = [exe, "--print"]
    elif provider == "gemini":
        exe = agent.GEMINI_BIN
        cmd = [exe, "--prompt", prompt]
    elif provider == "codex":
        exe = agent.CODEX_BIN
        cmd = [exe, "exec", "--skip-git-repo-check", "--cd", str(Path(__file__).parent), "--json", prompt]
    else:
        return {"provider": provider, "available": False, "ready": False, "reason": "unknown provider"}

    available = bool(shutil.which(exe) or shutil.which(Path(exe).name))
    if not available:
        return {"provider": provider, "available": False, "ready": False, "reason": "binary not found"}

    try:
        proc = subprocess.run(
            cmd,
            input=prompt if provider == "claude" else None,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent),
            timeout=25,
        )
        combined = (proc.stdout + "\n" + proc.stderr).lower()
        if "not logged in" in combined or "please run /login" in combined:
            return {"provider": provider, "available": True, "ready": False, "reason": "auth required"}
        if "opening authentication page in your browser" in combined or "do you want to continue?" in combined:
            return {"provider": provider, "available": True, "ready": False, "reason": "interactive auth prompt"}
        if "panicked" in combined and "reqwest-internal-sync-runtime" in combined:
            return {"provider": provider, "available": True, "ready": False, "reason": "runtime panic in sandbox"}
        if proc.returncode != 0:
            return {"provider": provider, "available": True, "ready": False, "reason": f"exit {proc.returncode}"}
        return {"provider": provider, "available": True, "ready": True, "reason": "ok"}
    except subprocess.TimeoutExpired:
        return {"provider": provider, "available": True, "ready": False, "reason": "timeout"}
    except Exception as e:
        return {"provider": provider, "available": True, "ready": False, "reason": str(e)}


def preflight_providers() -> int:
    providers = ["claude", "gemini", "codex"]
    print("=== PROVIDER PREFLIGHT ===")
    results = [_provider_probe(p) for p in providers]
    for r in results:
        status = "READY" if r.get("ready") else "BLOCKED"
        print(f"- {r['provider']}: {status} ({r.get('reason', 'unknown')})")

    ready = [r["provider"] for r in results if r.get("ready")]
    if ready:
        os.environ["EVOLVE_PROVIDER_ORDER"] = ",".join(ready + [p for p in providers if p not in ready])
        print(f"Recommended EVOLVE_PROVIDER_ORDER={os.environ['EVOLVE_PROVIDER_ORDER']}")
        return 0

    print("No ready providers detected. Authenticate at least one provider before auto-evolve.")
    return 1


def _pop_task() -> str | None:
    if not TASK_QUEUE_FILE.exists():
        return None
    try:
        tasks = json.loads(TASK_QUEUE_FILE.read_text())
        if not isinstance(tasks, list) or not tasks:
            return None
        task = tasks.pop(0)
        TASK_QUEUE_FILE.write_text(json.dumps(tasks, indent=2))
        return task if isinstance(task, str) else None
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[WARN] Corrupt task queue: {e}", flush=True)
        return None
    except Exception as e:
        print(f"[WARN] Task queue read error: {e}", flush=True)
        return None


def _repo_dirty() -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent),
            timeout=5,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def _head_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent),
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _auto_push(cycle: int) -> bool:
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent),
            timeout=8,
        ).stdout.strip()
        if not status:
            return False
        material_lines = []
        for line in status.splitlines():
            path = line[3:].strip()
            if not path:
                continue
            if path.startswith("data/logs/"):
                continue
            if path in {"data/memory.json", "data/evolution_state.json", "data/provider_status.json", "data/benchmarks.json"}:
                continue
            material_lines.append(path)
        if not material_lines:
            print(f"[GIT] Skipping push for cycle {cycle} (no material code changes).", flush=True)
            return False

        subprocess.run(
            [
                "git",
                "-c",
                "advice.addIgnoredFile=false",
                "add",
                "-A",
                "--",
                ":!.env",
                ":!data/memory.json",
                ":!data/permissions.json",
                ":!data/user_profile.json",
                ":!data/.current_prompt.txt",
                ":!*.bak",
            ],
            cwd=str(Path(__file__).parent),
            check=False,
        )
        subprocess.run(
            [
                "git",
                "commit",
                "-m",
                f"auto-evolve cycle {cycle}\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>",
            ],
            capture_output=True,
            cwd=str(Path(__file__).parent),
            check=False,
        )
        subprocess.run(["git", "push"], capture_output=True, cwd=str(Path(__file__).parent), check=False)
        print(f"[GIT] Pushed changes from cycle {cycle}", flush=True)
        return True
    except Exception as e:
        print(f"[GIT] Push failed: {e}", flush=True)
        return False


def _safe_rollback_if_needed(cycle: int) -> bool:
    if not ENGINE.should_rollback(LAST_RESULTS[-8:]):
        return False
    try:
        msg = f"auto-rollback cycle {cycle} due to success-rate degradation"
        result = subprocess.run(
            ["git", "revert", "--no-edit", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent),
            timeout=20,
        )
        if result.returncode == 0:
            print(f"[ROLLBACK] {msg}")
            return True
    except Exception:
        pass
    return False


def _handle_sigint(sig, frame):
    global _running
    print("\n\n[Stopping after current cycle — Ctrl+C again to force quit]")
    _running = False
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def _parse_interval(spec: str) -> int:
    spec = spec.strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600}
    if spec[-1] in multipliers:
        return int(spec[:-1]) * multipliers[spec[-1]]
    return int(spec)


def _fmt_interval(seconds: int) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _task_for_cycle_type(cycle_type: str) -> tuple[str, str]:
    if cycle_type == "assess":
        return "assess", ASSESS_TASK
    if cycle_type == "stabilize":
        return "stabilize", STABILIZE_TASK
    if cycle_type == "challenge":
        return "challenge", CHALLENGE_TASK
    return "mutate", EVOLVE_TASK


def _extract_files_from_response(text: str) -> list[str]:
    # Lightweight heuristic: learn from explicit file mentions in model output.
    out = []
    for token in (text or "").split():
        tok = token.strip(".,:;()[]{}\"'")
        if tok.endswith(".py") and "/" not in tok:
            out.append(tok)
    return sorted(set(out))[:8]


def _build_horizon_plan() -> tuple[list[str], list[str], list[str]]:
    weak = ENGINE.weakest_capabilities(top_n=6)
    now = [
        "Improve failure classification and adaptive retries",
        "Increase benchmark signal quality for fitness",
    ]
    if weak:
        now.append(f"Improve weak capability: {weak[0]}")

    next_ = [
        "Expand A/B mutation search space safely",
        "Refine novelty and utility estimators",
        "Tune bandit exploration/exploitation balance",
        "Improve dashboard branch visualization",
    ]
    later = [
        "Cross-app portfolio transfer automation",
        "Autonomous milestone-based permission unlocks",
        "Continuous weekly retrospective auto-actions",
        "Long-horizon architecture mutation campaigns",
    ]
    return now, next_, later


def _simulate_dry_run_guard(task: str) -> str:
    if os.environ.get("EVOLVE_DRY_RUN", "0") != "1":
        level = int(ENGINE.state.get("milestone_level", 0))
        if level <= 0:
            return task + "\n[SAFETY SCOPE] Keep patches single-file and low-risk."
        if level == 1:
            return task + "\n[SAFETY SCOPE] Multi-file patches allowed with explicit rollback note."
        if level >= 2:
            return task + "\n[SAFETY SCOPE] Broader architectural updates allowed; still require verification."
        return task
    guard = "\n[DRY-RUN MODE] Do not edit files. Produce an exact patch plan, risks, and verify command only."
    return task + guard


def _portfolio_transfer_hook() -> None:
    # Transfer one lightweight lesson from active apps into engine memory each cycle.
    apps = portfolio.list_active_apps()
    if not apps:
        return
    sample = apps[0]
    lesson = f"Portfolio signal from {sample.get('name')}: tags={','.join(sample.get('capability_tags', []))[:120]}"
    ENGINE.record_portfolio_transfer(sample.get("name", "unknown"), lesson)


def _apply_provider_policy() -> None:
    outcomes = mem.get_all().get("provider_outcomes", [])
    if not outcomes:
        return
    ranked: dict[str, list[int]] = {}
    for row in outcomes[-300:]:
        provider = str(row.get("provider", ""))
        if not provider:
            continue
        score = 1 if row.get("success") else 0
        ranked.setdefault(provider, []).append(score)
    if not ranked:
        return
    order = sorted(
        ranked.items(),
        key=lambda kv: (sum(kv[1]) / max(1, len(kv[1]))),
        reverse=True,
    )
    providers = ",".join([name for name, _ in order])
    if providers:
        ranked = [p for p in providers.split(",") if p]
        for fallback in ("codex", "gemini", "claude"):
            if fallback not in ranked:
                ranked.append(fallback)
        os.environ["EVOLVE_PROVIDER_ORDER"] = ",".join(ranked)


def _auth_guard_active() -> bool:
    """Stop autonomous loops when provider auth is clearly missing."""
    if os.environ.get("EVOLVE_AUTH_GUARD", "1") == "0":
        return False
    history = mem.get_all().get("task_history", [])
    recent = history[-3:]
    if len(recent) < 2:
        return False
    failed = [r for r in recent if not r.get("success")]
    if len(failed) < 2:
        return False
    suspected = all("/login" in str(r.get("notes", "")).lower() or "not logged in" in str(r.get("notes", "")).lower() or "authentication" in str(r.get("notes", "")).lower() for r in failed[-2:])
    if not suspected:
        return False
    return not _provider_auth_probe()


def _provider_auth_probe() -> bool:
    """Live probe to avoid stale auth guard state."""
    now = time.time()
    if now - _AUTH_PROBE_CACHE["ts"] < 60:
        return bool(_AUTH_PROBE_CACHE["ok"])
    try:
        cmd = [agent.CLAUDE_BIN, "--print"]
        proc = subprocess.run(
            cmd,
            input="Reply with OK only.",
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent),
            timeout=20,
        )
        combined = (proc.stdout + "\n" + proc.stderr).lower()
        # Auth is 'ok' only if no patterns suggest a blocking prompt or missing session.
        ok = (
            "not logged in" not in combined and 
            "please run /login" not in combined and 
            "opening authentication page" not in combined and 
            "do you want to continue" not in combined and
            proc.returncode == 0
        )
    except Exception:
        ok = False
    _AUTH_PROBE_CACHE["ts"] = now
    _AUTH_PROBE_CACHE["ok"] = ok
    return ok


def _run_single_cycle(cycle_number: int) -> dict:
    _apply_provider_policy()
    queued = _pop_task()
    head_before = _head_commit()
    recent_failures = ENGINE.state.get("failure_classes", {})
    dominant_failure = "unknown"
    if isinstance(recent_failures, dict) and recent_failures:
        dominant_failure = max(recent_failures.items(), key=lambda kv: kv[1])[0]
    retry_budget = ENGINE.adaptive_retry_budget(dominant_failure)

    if queued:
        cycle_type = "queued"
        task = queued
        trials = []
    else:
        cycle_type = ENGINE.choose_cycle_type(has_queued_task=False)
        mode, base_task = _task_for_cycle_type(cycle_type)
        weak_caps = ENGINE.weakest_capabilities(top_n=3)
        if weak_caps:
            base_task += (
                "\n\nWeak capability targets (prioritize these modules first):\n- "
                + "\n- ".join(weak_caps)
            )

        if mode == "mutate":
            trials = ENGINE.mutation_trials(_simulate_dry_run_guard(base_task), trial_count=int(os.environ.get("EVOLVE_MAX_TRIALS", "3")))
        else:
            trials = [{"id": f"single_{mode}", "style": mode, "prompt": _simulate_dry_run_guard(base_task)}]
        task = ""

    if queued:
        result = agent.run_task(task, max_retries=retry_budget)
        chosen = {
            "id": "queued",
            "style": "queued",
            "result": result,
            "hypothesis": "Execute queued user priority task",
            "action": "run_task(queue_item)",
        }
    else:
        candidates = []
        provider_blocked = False
        for t in trials:
            if provider_blocked:
                print(f"[CYCLE] Skipping trial {t['id']} — provider blocked on prior trial.", flush=True)
                break

            hypothesis = f"Trial {t['style']} should improve {cycle_type} utility with measurable outcome"
            confidence = ENGINE.confidence_score(hypothesis, cycle_type)
            if confidence < 0.35:
                continue

            res = agent.run_task(t["prompt"], max_retries=retry_budget)
            duration = float(res.get("duration_s", 0.0))
            success = bool(res.get("success"))
            final_text = str(res.get("final_response", ""))

            # Early termination: if provider is auth-blocked or rate-limited, skip remaining trials.
            if not success and res.get("rate_limited"):
                provider_blocked = True
            if not success:
                failure_text = (final_text or "").lower()
                if any(p in failure_text for p in agent._AUTH_FAILURE_PATTERNS):
                    provider_blocked = True

            files_changed = _extract_files_from_response(final_text)
            novelty = ENGINE.estimate_novelty(files_changed, t["style"])
            regression_risk = 0.45 if not success else 0.15
            utility_est = 0.75 if success else 0.25
            if success and not files_changed:
                # Prevent no-op responses from looking like high-value wins.
                utility_est = 0.15
                regression_risk = max(regression_risk, 0.35)
            fit = ENGINE.fitness_score(success, duration, novelty, regression_risk, utility_est)
            candidates.append(
                {
                    "id": t["id"],
                    "style": t["style"],
                    "result": res,
                    "fitness": fit,
                    "hypothesis": hypothesis,
                    "action": f"run_task({t['id']})",
                    "files": files_changed,
                    "confidence": confidence,
                    "novelty": novelty,
                    "risk": regression_risk,
                    "utility": utility_est,
                }
            )

        if not candidates:
            fallback = agent.run_task(_simulate_dry_run_guard(EVOLVE_TASK), max_retries=retry_budget)
            candidates = [{
                "id": "fallback",
                "style": "mutate",
                "result": fallback,
                "fitness": ENGINE.fitness_score(bool(fallback.get("success")), float(fallback.get("duration_s", 0.0)), 0.5, 0.4, 0.5),
                "hypothesis": "Fallback mutation attempt",
                "action": "run_task(fallback)",
                "files": [],
                "confidence": 0.5,
                "novelty": 0.5,
                "risk": 0.4,
                "utility": 0.5,
            }]

        chosen = max(candidates, key=lambda c: c["fitness"])

    result = chosen["result"]
    success = bool(result.get("success"))
    duration_s = float(result.get("duration_s", 0.0))
    final_text = str(result.get("final_response", ""))
    files_changed = chosen.get("files") or _extract_files_from_response(final_text)

    novelty = float(chosen.get("novelty", ENGINE.estimate_novelty(files_changed, chosen.get("style", ""))))
    risk = float(chosen.get("risk", 0.2 if success else 0.6))
    utility_est = float(chosen.get("utility", 0.7 if success else 0.2))
    if success and not files_changed:
        utility_est = min(utility_est, 0.15)
        risk = max(risk, 0.35)
    fitness = float(chosen.get("fitness", ENGINE.fitness_score(success, duration_s, novelty, risk, utility_est)))

    failure_class = "none"
    if not success:
        failure_class = ENGINE.classify_failure(str(result.get("final_response", "")))

    fingerprint = build_patch_fingerprint(files_changed, chosen.get("hypothesis", ""))

    summary = ENGINE.record_cycle(
        cycle_type="mutate" if cycle_type == "queued" else cycle_type,
        fitness=fitness,
        success=success,
        files_changed=files_changed,
        provider=str(result.get("provider", "unknown")),
        failure_class=failure_class,
        fingerprint=fingerprint,
    )

    now, next_, later = _build_horizon_plan()
    ENGINE.update_planner(now, next_, later)

    # Benchmark update (latency/reliability/throughput/prompt quality).
    recent = LAST_RESULTS[-9:] + [success]
    recent_rate = sum(1 for x in recent if x) / max(1, len(recent))
    throughput = 1.0 / max(1.0, duration_s)
    prompt_quality = min(1.0, 0.4 + 0.6 * fitness)
    ENGINE.update_benchmark(duration_s * 1000.0, recent_rate, throughput, prompt_quality)

    retro = ENGINE.weekly_retrospective_if_due()
    if retro:
        ENGINE.log_event({"ts": time.time(), "event": "weekly_retro", "retro": retro})

    ENGINE.log_timeline(
        hypothesis=chosen.get("hypothesis", ""),
        action=chosen.get("action", ""),
        result=f"success={success} fitness={fitness} files={','.join(files_changed)}",
        fitness=fitness,
        cycle_type=cycle_type,
        branch=chosen.get("id", "single"),
    )

    # Causal metadata in memory log for learning.
    mem.record_evolution(
        description=f"cycle_{cycle_number}_{cycle_type}_{chosen.get('id', 'single')}",
        files_changed=files_changed,
        reason=f"hypothesis={chosen.get('hypothesis','')[:120]} | fitness={fitness}",
    )
    mem.record_causal_event(
        hypothesis=chosen.get("hypothesis", ""),
        expected="Increase utility and/or reliability while keeping regression risk low",
        observed=f"success={success} fitness={fitness} files={','.join(files_changed)}",
        fitness=fitness,
    )
    mem.compress_memory()

    _portfolio_transfer_hook()

    pushed = False
    if success:
        pushed = _auto_push(cycle_number)
    else:
        print("[GIT] Skipping push: cycle failed.")
    if pushed:
        _safe_rollback_if_needed(cycle_number)

    head_after = _head_commit()
    return {
        "cycle": cycle_number,
        "cycle_type": cycle_type,
        "selected_trial": chosen.get("id", "single"),
        "success": success,
        "fitness": fitness,
        "files_changed": files_changed,
        "duration_s": duration_s,
        "summary": summary,
        "head_before": head_before,
        "head_after": head_after,
    }


def print_status() -> None:
    print("\n=== EVOLVE STATUS ===")
    print("\n-- Memory --")
    print(mem.get_memory_context())

    log_file = DATA_DIR / "memory_log.jsonl"
    if log_file.exists():
        lines = [ln for ln in log_file.read_text().splitlines() if ln.strip()]
        print(f"\n-- Memory Log (last 5 of {len(lines)}) --")
        for line in lines[-5:]:
            try:
                e = json.loads(line)
                print(f"  {e.get('lesson', '')[:100]}")
            except Exception:
                print(f"  {line[:100]}")

    if EVOLUTION_LOG.exists():
        try:
            log = json.loads(EVOLUTION_LOG.read_text())
        except Exception:
            log = []
        print(f"\n-- Evolution Log ({len(log)} entries) --")
        for entry in log[-3:]:
            print(f"  [{entry.get('description')}] {entry.get('reason', '')[:100]}")

    print("\n-- Engine --")
    st = ENGINE.state
    print(f"  goal_pack={st.get('goal_pack')} curriculum_stage={st.get('curriculum_stage')} milestone={st.get('milestone_level')}")
    print(f"  last_fitness={st.get('last_fitness', 0):.4f} cycles={st.get('cycle_count', 0)}")
    print("=" * 20)


def auto_evolve_loop(max_cycles: int | None = None, pause_s: int = 5) -> None:
    global _running
    signal.signal(signal.SIGINT, _handle_sigint)

    print(f"\n{'='*60}")
    print("AUTO-EVOLVE — Ctrl+C to stop gracefully")
    print(f"{'='*60}")

    cycle = 0
    sync_every = int(os.environ.get("EVOLVE_GDRIVE_SYNC_EVERY", "50"))
    while _running:
        if max_cycles and cycle >= max_cycles:
            print(f"\n[Done] Completed {cycle} cycles.")
            break
        if _auth_guard_active():
            print("\n[AUTH GUARD] Provider login required. Run provider login, then restart auto-evolve.")
            break

        cycle += 1
        print(f"\n{'─'*60}")
        print(f"[CYCLE {cycle}] {time.strftime('%H:%M:%S')}")
        print(f"{'─'*60}")

        cycle_result = _run_single_cycle(cycle)
        LAST_RESULTS.append(bool(cycle_result["success"]))
        if len(LAST_RESULTS) > 100:
            del LAST_RESULTS[:-100]

        momentum = cycle_result.get("summary", {}).get("momentum", 0)
        eps = cycle_result.get("summary", {}).get("epsilon", 0.18)
        streak_label = f"+{momentum}" if momentum > 0 else str(momentum)
        print(
            f"[CYCLE RESULT] type={cycle_result['cycle_type']} trial={cycle_result['selected_trial']} "
            f"success={cycle_result['success']} fitness={cycle_result['fitness']:.4f} "
            f"momentum={streak_label} epsilon={eps:.2f}"
        )

        if sync_every > 0 and cycle % sync_every == 0:
            if _repo_dirty():
                print("[GDRIVE] Syncing to Google Drive...")
                result = profiler.sync_to_gdrive()
                print(f"[GDRIVE] {result}")
            else:
                print("[GDRIVE] No changes - skipping sync")

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


def schedule_loop(interval_s: int, max_cycles: int | None = None) -> None:
    global _running
    signal.signal(signal.SIGINT, _handle_sigint)

    print(f"\n{'='*60}")
    print(f"SCHEDULE MODE — every {_fmt_interval(interval_s)}, Ctrl+C to stop")
    print(f"{'='*60}")

    cycle = 0
    while _running:
        if max_cycles and cycle >= max_cycles:
            print(f"\n[Done] Completed {cycle} scheduled cycles.")
            break
        if _auth_guard_active():
            print("\n[AUTH GUARD] Provider login required. Run provider login, then restart schedule mode.")
            break

        cycle += 1
        print(f"\n{'─'*60}")
        print(f"[SCHEDULED CYCLE {cycle}] {time.strftime('%H:%M:%S')}")
        print(f"{'─'*60}")

        cycle_result = _run_single_cycle(cycle)
        LAST_RESULTS.append(bool(cycle_result["success"]))
        if len(LAST_RESULTS) > 100:
            del LAST_RESULTS[:-100]

        if not _running:
            break

        if max_cycles is None or cycle < max_cycles:
            next_run = time.time() + interval_s
            next_str = time.strftime("%H:%M:%S", time.localtime(next_run))
            print(f"\n[Next cycle at {next_str} — sleeping {_fmt_interval(interval_s)}]")
            remaining = interval_s
            while remaining > 0 and _running:
                time.sleep(min(remaining, 1))
                remaining -= 1

    print(f"\n[SCHEDULE] Stopped after {cycle} cycles.")
    print_status()


def repl() -> None:
    print("Evolve — enter a task, 'evolve', 'auto', 'schedule 1h', 'goal <name>', 'status', or 'quit'.")
    while True:
        try:
            task = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not task:
            continue
        lower = task.lower()

        if lower in ("quit", "exit", "q"):
            break
        if lower == "status":
            print_status()
            continue
        if lower == "evolve":
            _run_single_cycle(ENGINE.state.get("cycle_count", 0) + 1)
            continue
        if lower == "auto":
            auto_evolve_loop()
            continue
        if lower.startswith("schedule"):
            parts = task.split()
            interval = parts[1] if len(parts) > 1 else "1h"
            try:
                schedule_loop(interval_s=_parse_interval(interval))
            except ValueError:
                print(f"Invalid interval: {interval}. Use e.g. 30s, 5m, 1h")
            continue
        if lower.startswith("goal "):
            _, goal_name = task.split(" ", 1)
            selected = ENGINE.set_goal_pack(goal_name.strip())
            print(f"Goal pack set to: {selected}")
            continue

        result = agent.run_task(task)
        LAST_RESULTS.append(bool(result.get("success")))


def main() -> None:
    args = sys.argv[1:]

    if not args:
        repl()
        return

    if "--status" in args:
        print_status()
        return

    if "--preflight-providers" in args:
        sys.exit(preflight_providers())

    if "--goal-pack" in args:
        idx = args.index("--goal-pack")
        try:
            goal_name = args[idx + 1]
        except (IndexError, ValueError):
            print("Usage: --goal-pack <utility|speed|autonomy|fun_demo>")
            sys.exit(1)
        print(f"Goal pack set to: {ENGINE.set_goal_pack(goal_name)}")
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
            print('Usage: --archive-app NAME --reason "why"')
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

    if "--schedule" in args:
        idx = args.index("--schedule")
        try:
            interval_s = _parse_interval(args[idx + 1])
        except (IndexError, ValueError):
            print("Usage: --schedule INTERVAL (e.g. 30s, 5m, 1h)")
            sys.exit(1)

        max_cycles = None
        if "--cycles" in args:
            cidx = args.index("--cycles")
            try:
                max_cycles = int(args[cidx + 1])
            except (IndexError, ValueError):
                pass
        schedule_loop(interval_s=interval_s, max_cycles=max_cycles)
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
        _run_single_cycle(ENGINE.state.get("cycle_count", 0) + 1)
        return

    task = " ".join(a for a in args if not a.startswith("--"))
    result = agent.run_task(task)
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
