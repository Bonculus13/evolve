"""Core evolutionary intelligence engine for orchestrator decision-making."""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DATA_DIR


STATE_FILE = DATA_DIR / "evolution_state.json"
EVENT_LOG_FILE = DATA_DIR / "logs" / "evolution_events.jsonl"
BENCHMARK_FILE = DATA_DIR / "benchmarks.json"
TIMELINE_FILE = DATA_DIR / "evolution_timeline.jsonl"


GOAL_PACKS: dict[str, dict[str, float]] = {
    "utility": {"success": 0.35, "novelty": 0.15, "speed": 0.20, "risk": 0.15, "utility": 0.15},
    "speed": {"success": 0.25, "novelty": 0.10, "speed": 0.40, "risk": 0.10, "utility": 0.15},
    "autonomy": {"success": 0.30, "novelty": 0.20, "speed": 0.10, "risk": 0.20, "utility": 0.20},
    "fun_demo": {"success": 0.20, "novelty": 0.35, "speed": 0.10, "risk": 0.10, "utility": 0.25},
}


@dataclass
class TrialResult:
    provider: str
    cycle_type: str
    prompt_id: str
    success: bool
    duration_s: float
    confidence: float
    novelty: float
    regression_risk: float
    utility_estimate: float
    hypothesis: str
    action: str
    result: str


class EvolutionEngine:
    def __init__(self, state_file: Path = STATE_FILE):
        self.state_file = state_file
        self.state = self._load_state()

    def _default_state(self) -> dict[str, Any]:
        return {
            "created_at": time.time(),
            "goal_pack": "utility",
            "cycle_count": 0,
            "curriculum_stage": "reliability",
            "milestone_level": 0,
            "last_fitness": 0.0,
            "fitness_history": [],
            "weekly_retrospectives": [],
            "planner": {
                "now": [],
                "next": [],
                "later": [],
                "updated_at": 0,
            },
            "bandit": {
                "arms": {
                    "mutate": {"n": 0, "reward": 0.0},
                    "assess": {"n": 0, "reward": 0.0},
                    "stabilize": {"n": 0, "reward": 0.0},
                    "challenge": {"n": 0, "reward": 0.0},
                },
                "epsilon": 0.18,
            },
            "regression_fingerprints": [],
            "capability_graph": {},
            "tool_policy": {},
            "benchmark": {
                "latency_ms": [],
                "success_rate": [],
                "throughput": [],
                "prompt_quality": [],
            },
            "failure_classes": {},
            "portfolio_transfers": [],
            "last_week_key": "",
        }

    def _load_state(self) -> dict[str, Any]:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                if isinstance(data, dict):
                    # Merge with defaults so new keys are always present
                    defaults = self._default_state()
                    for k, v in defaults.items():
                        if k not in data:
                            data[k] = v
                    return data
            except (json.JSONDecodeError, ValueError):
                # Corrupt state — preserve and reset
                try:
                    corrupt = self.state_file.with_suffix(".json.corrupt")
                    corrupt.write_text(self.state_file.read_text())
                except Exception:
                    pass
            except Exception:
                pass
        return self._default_state()

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, indent=2))

    def reload(self) -> None:
        self.state = self._load_state()

    def set_goal_pack(self, goal_pack: str) -> str:
        if goal_pack not in GOAL_PACKS:
            return self.state.get("goal_pack", "utility")
        self.state["goal_pack"] = goal_pack
        self.save()
        return goal_pack

    def get_goal_pack(self) -> str:
        return self.state.get("goal_pack", "utility")

    def momentum(self, window: int = 8) -> int:
        """Return current streak: positive = consecutive successes, negative = consecutive failures."""
        hist = self.state.get("fitness_history", [])
        if not hist:
            return 0
        # Use threshold of 0.5 to classify success/failure from fitness
        streak = 0
        direction = None
        for f in reversed(hist[-window:]):
            is_success = f >= 0.5
            if direction is None:
                direction = is_success
            if is_success == direction:
                streak += 1
            else:
                break
        return streak if direction else -streak

    def adaptive_epsilon(self) -> float:
        """Modulate exploration rate based on momentum. Winning streak -> exploit. Losing -> explore."""
        base = float(self.state.get("bandit", {}).get("epsilon", 0.18))
        m = self.momentum()
        if m >= 3:
            # Winning streak: halve epsilon (exploit what works)
            return max(0.05, base * 0.5)
        if m <= -3:
            # Losing streak: double epsilon (explore alternatives)
            return min(0.50, base * 2.0)
        return base

    def choose_cycle_type(self, has_queued_task: bool = False) -> str:
        if has_queued_task:
            return "queued"

        # Stagnation breaker + challenge cycles make evolution more interesting to watch.
        if self.detect_stagnation(window=8):
            return "challenge"

        if self.should_run_challenge_cycle():
            return "challenge"

        arms = self.state.get("bandit", {}).get("arms", {})
        epsilon = self.adaptive_epsilon()
        total_n = max(1, sum(max(0, int(v.get("n", 0))) for v in arms.values()))

        if random.random() < epsilon:
            return random.choice(list(arms.keys()))

        def ucb_score(name: str, arm: dict[str, Any]) -> float:
            n = max(1, int(arm.get("n", 0)))
            mean = float(arm.get("reward", 0.0)) / n
            return mean + (2.0 * (total_n ** 0.5) / (n ** 0.5))

        return max(arms.items(), key=lambda kv: ucb_score(kv[0], kv[1]))[0]

    def should_run_challenge_cycle(self) -> bool:
        c = int(self.state.get("cycle_count", 0))
        return c > 0 and c % 7 == 0

    def mutation_trials(self, base_task: str, trial_count: int = 3) -> list[dict[str, str]]:
        templates = [
            ("precision", "Prioritize a minimal high-confidence patch with explicit verify command."),
            ("novelty", "Prioritize a novel capability improvement with measurable utility gain."),
            ("hardening", "Prioritize reliability and regression prevention with defensive checks."),
        ]
        trials = []
        for i, (style, guide) in enumerate(templates[:max(1, trial_count)]):
            trials.append(
                {
                    "id": f"trial_{i+1}_{style}",
                    "style": style,
                    "prompt": f"{base_task}\n\n[TRIAL STYLE: {style}] {guide}",
                }
            )
        return trials

    def estimate_novelty(self, changed_files: list[str], description: str) -> float:
        sig = ("|".join(sorted(changed_files)) + "::" + description[:120]).lower()
        fp = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16]
        prior = set(self.state.get("regression_fingerprints", []))
        return 0.95 if fp not in prior else 0.25

    def classify_failure(self, notes: str) -> str:
        text = (notes or "").lower()
        if "not logged in" in text or "/login" in text or "auth" in text:
            return "auth"
        if "rate_limited" in text or "429" in text:
            return "rate_limit"
        if "import" in text or "module" in text:
            return "import_error"
        if "permission" in text:
            return "permission"
        if "timeout" in text:
            return "timeout"
        return "unknown"

    def adaptive_retry_budget(self, failure_class: str) -> int:
        mapping = {
            "auth": 0,
            "rate_limit": 1,
            "import_error": 2,
            "permission": 1,
            "timeout": 3,
            "unknown": 2,
        }
        return mapping.get(failure_class, 2)

    def confidence_score(self, hypothesis: str, cycle_type: str) -> float:
        base = 0.55 if cycle_type == "mutate" else 0.7
        if "verify" in hypothesis.lower():
            base += 0.1
        if "new capability" in hypothesis.lower():
            base -= 0.05
        return max(0.05, min(0.98, base))

    def fitness_score(
        self,
        success: bool,
        duration_s: float,
        novelty: float,
        regression_risk: float,
        utility_estimate: float,
    ) -> float:
        gp = GOAL_PACKS.get(self.get_goal_pack(), GOAL_PACKS["utility"])
        success_v = 1.0 if success else 0.0
        speed_v = 1.0 / max(1.0, duration_s / 30.0)
        risk_v = 1.0 - max(0.0, min(1.0, regression_risk))

        score = (
            gp["success"] * success_v
            + gp["novelty"] * novelty
            + gp["speed"] * speed_v
            + gp["risk"] * risk_v
            + gp["utility"] * utility_estimate
        )
        return round(max(0.0, min(1.0, score)), 4)

    def detect_stagnation(self, window: int = 8, min_delta: float = 0.03) -> bool:
        hist = self.state.get("fitness_history", [])
        if len(hist) < window:
            return False
        recent = hist[-window:]
        return (max(recent) - min(recent)) < min_delta

    def update_curriculum_stage(self) -> str:
        hist = self.state.get("fitness_history", [])
        if not hist:
            return self.state.get("curriculum_stage", "reliability")

        recent = hist[-12:]
        avg = sum(recent) / len(recent)
        if avg < 0.45:
            stage = "reliability"
        elif avg < 0.70:
            stage = "architecture"
        else:
            stage = "aggressive_autonomy"
        self.state["curriculum_stage"] = stage
        return stage

    def milestone_unlocks(self) -> dict[str, Any]:
        hist = self.state.get("fitness_history", [])
        if len(hist) < 10:
            return {"level": self.state.get("milestone_level", 0), "unlocked": []}

        avg = sum(hist[-10:]) / 10.0
        level = int(self.state.get("milestone_level", 0))
        unlocked = []
        thresholds = [0.50, 0.65, 0.78]
        while level < len(thresholds) and avg >= thresholds[level]:
            level += 1
            unlocked.append(f"milestone_{level}")

        self.state["milestone_level"] = level
        return {"level": level, "unlocked": unlocked}

    def should_rollback(self, recent_results: list[bool]) -> bool:
        if len(recent_results) < 6:
            return False
        rate = sum(1 for x in recent_results if x) / len(recent_results)
        return rate < 0.45

    def update_bandit(self, arm: str, reward: float) -> None:
        bandit = self.state.setdefault("bandit", {}).setdefault("arms", {})
        data = bandit.setdefault(arm, {"n": 0, "reward": 0.0})
        data["n"] = int(data.get("n", 0)) + 1
        data["reward"] = float(data.get("reward", 0.0)) + reward

    def update_capability_graph(self, files_changed: list[str], reward: float) -> None:
        graph = self.state.setdefault("capability_graph", {})
        for f in files_changed:
            node = graph.setdefault(f, {"count": 0, "reward": 0.0})
            node["count"] += 1
            node["reward"] += reward

    def weakest_capabilities(self, top_n: int = 5) -> list[str]:
        graph = self.state.get("capability_graph", {})
        if not graph:
            return []
        ranked = sorted(
            graph.items(),
            key=lambda kv: (kv[1].get("reward", 0.0) / max(1, kv[1].get("count", 1))),
        )
        return [name for name, _ in ranked[:top_n]]

    def update_planner(self, now: list[str], next_: list[str], later: list[str]) -> None:
        self.state["planner"] = {
            "now": now[:5],
            "next": next_[:8],
            "later": later[:12],
            "updated_at": time.time(),
        }

    def log_event(self, payload: dict[str, Any]) -> None:
        EVENT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENT_LOG_FILE, "a") as f:
            f.write(json.dumps(payload) + "\n")

    def log_timeline(
        self,
        hypothesis: str,
        action: str,
        result: str,
        fitness: float,
        cycle_type: str,
        branch: str,
    ) -> None:
        TIMELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.time(),
            "cycle": int(self.state.get("cycle_count", 0)),
            "cycle_type": cycle_type,
            "branch": branch,
            "hypothesis": hypothesis[:220],
            "action": action[:220],
            "result": result[:300],
            "fitness": fitness,
            "goal_pack": self.get_goal_pack(),
        }
        with open(TIMELINE_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def update_benchmark(self, latency_ms: float, success_rate: float, throughput: float, prompt_quality: float) -> None:
        bench = self.state.setdefault("benchmark", {})
        for key, value in {
            "latency_ms": latency_ms,
            "success_rate": success_rate,
            "throughput": throughput,
            "prompt_quality": prompt_quality,
        }.items():
            seq = bench.setdefault(key, [])
            seq.append(round(float(value), 4))
            if len(seq) > 100:
                del seq[:-100]

        BENCHMARK_FILE.parent.mkdir(parents=True, exist_ok=True)
        BENCHMARK_FILE.write_text(json.dumps(bench, indent=2))

    def weekly_retrospective_if_due(self) -> dict[str, Any] | None:
        now = datetime.now()
        week_key = f"{now.year}-W{now.isocalendar().week:02d}"
        if self.state.get("last_week_key") == week_key:
            return None

        hist = self.state.get("fitness_history", [])
        last = hist[-30:]
        gains = round((max(last) - min(last)), 4) if last else 0.0
        retro = {
            "week": week_key,
            "ts": time.time(),
            "avg_fitness": round(sum(last) / len(last), 4) if last else 0.0,
            "best_fitness": max(last) if last else 0.0,
            "fitness_gain": gains,
            "next_actions": self.weakest_capabilities(10),
        }
        self.state.setdefault("weekly_retrospectives", []).append(retro)
        self.state["last_week_key"] = week_key
        return retro

    def record_portfolio_transfer(self, source_app: str, lesson: str) -> None:
        entries = self.state.setdefault("portfolio_transfers", [])
        entries.append({"ts": time.time(), "app": source_app, "lesson": lesson[:300]})
        if len(entries) > 200:
            del entries[:-200]

    def record_cycle(self, *, cycle_type: str, fitness: float, success: bool, files_changed: list[str],
                     provider: str, failure_class: str, fingerprint: str) -> dict[str, Any]:
        self.state["cycle_count"] = int(self.state.get("cycle_count", 0)) + 1
        hist = self.state.setdefault("fitness_history", [])
        hist.append(float(fitness))
        if len(hist) > 500:
            del hist[:-500]

        self.state["last_fitness"] = float(fitness)
        self.update_bandit(cycle_type, fitness)
        self.update_capability_graph(files_changed, fitness)
        if fingerprint:
            fps = self.state.setdefault("regression_fingerprints", [])
            if fingerprint not in fps:
                fps.append(fingerprint)
            if len(fps) > 500:
                del fps[:-500]

        failures = self.state.setdefault("failure_classes", {})
        failures[failure_class] = failures.get(failure_class, 0) + (0 if success else 1)

        stage = self.update_curriculum_stage()
        milestone = self.milestone_unlocks()

        summary = {
            "cycle_count": self.state["cycle_count"],
            "fitness": fitness,
            "stage": stage,
            "milestone_level": milestone["level"],
            "unlocked": milestone["unlocked"],
            "goal_pack": self.get_goal_pack(),
            "provider": provider,
            "momentum": self.momentum(),
            "epsilon": round(self.adaptive_epsilon(), 4),
        }
        self.log_event({"ts": time.time(), "event": "cycle_recorded", **summary})
        self.save()
        return summary


def build_patch_fingerprint(files_changed: list[str], reason: str) -> str:
    sig = "|".join(sorted(files_changed)) + "::" + reason[:200]
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:18]
