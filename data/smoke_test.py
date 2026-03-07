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


# 23. fitness_ema returns smoothed value
def test_fitness_ema():
    from evolution_engine import EvolutionEngine
    from pathlib import Path
    import tempfile, json
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text(json.dumps({"fitness_history": [0.3, 0.5, 0.7, 0.6, 0.8]}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        ema = engine.fitness_ema()
        assert isinstance(ema, float), f"expected float, got {type(ema)}"
        assert 0.0 <= ema <= 1.0, f"ema {ema} out of range"
        # EMA should be pulled toward recent high values
        assert ema > 0.5, f"ema {ema} should be > 0.5 given upward trend"
    finally:
        tmp.unlink(missing_ok=True)
    # Empty history
    tmp.write_text(json.dumps({"fitness_history": []}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        assert engine.fitness_ema() == 0.0, "empty history should return 0.0"
    finally:
        tmp.unlink(missing_ok=True)

check("evolution_engine.fitness_ema smoothed value", test_fitness_ema)


# 24. fitness_trend classifies direction correctly
def test_fitness_trend():
    from evolution_engine import EvolutionEngine
    from pathlib import Path
    import tempfile, json
    tmp = Path(tempfile.mktemp(suffix=".json"))
    # Improving
    tmp.write_text(json.dumps({"fitness_history": [0.3, 0.4, 0.5, 0.6, 0.7]}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        assert engine.fitness_trend() == "improving", f"expected improving, got {engine.fitness_trend()}"
    finally:
        tmp.unlink(missing_ok=True)
    # Declining
    tmp.write_text(json.dumps({"fitness_history": [0.8, 0.7, 0.6, 0.5, 0.4]}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        assert engine.fitness_trend() == "declining", f"expected declining, got {engine.fitness_trend()}"
    finally:
        tmp.unlink(missing_ok=True)
    # Flat
    tmp.write_text(json.dumps({"fitness_history": [0.5, 0.5, 0.5, 0.5]}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        assert engine.fitness_trend() == "flat", f"expected flat, got {engine.fitness_trend()}"
    finally:
        tmp.unlink(missing_ok=True)
    # Insufficient data
    tmp.write_text(json.dumps({"fitness_history": [0.5]}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        assert engine.fitness_trend() == "insufficient_data"
    finally:
        tmp.unlink(missing_ok=True)

check("evolution_engine.fitness_trend direction classification", test_fitness_trend)


# 25. fitness_volatility returns sensible values
def test_fitness_volatility():
    from evolution_engine import EvolutionEngine
    from pathlib import Path
    import tempfile, json
    tmp = Path(tempfile.mktemp(suffix=".json"))
    # High volatility: widely spread values
    tmp.write_text(json.dumps({"fitness_history": [0.1, 0.9, 0.2, 0.8, 0.15, 0.85]}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        vol = engine.fitness_volatility()
        assert isinstance(vol, float), f"expected float, got {type(vol)}"
        assert vol > 0.2, f"highly spread values should have high volatility, got {vol}"
    finally:
        tmp.unlink(missing_ok=True)
    # Low volatility: tightly clustered
    tmp.write_text(json.dumps({"fitness_history": [0.5, 0.51, 0.49, 0.5, 0.50]}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        vol = engine.fitness_volatility()
        assert vol < 0.02, f"tightly clustered should have low volatility, got {vol}"
    finally:
        tmp.unlink(missing_ok=True)
    # Empty/short history
    tmp.write_text(json.dumps({"fitness_history": [0.5]}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        assert engine.fitness_volatility() == 0.0, "short history should return 0.0"
    finally:
        tmp.unlink(missing_ok=True)

check("evolution_engine.fitness_volatility", test_fitness_volatility)


# 26. high volatility biases choose_cycle_type toward stabilize
def test_volatility_bias():
    from evolution_engine import EvolutionEngine
    from pathlib import Path
    import tempfile, json
    tmp = Path(tempfile.mktemp(suffix=".json"))
    # Very high volatility history
    tmp.write_text(json.dumps({"fitness_history": [0.1, 0.9, 0.1, 0.9, 0.1, 0.9, 0.1, 0.9]}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        vol = engine.fitness_volatility()
        assert vol > 0.12, f"test setup: need high vol, got {vol}"
        # Run 50 trials — stabilize should appear significantly
        types = [engine.choose_cycle_type() for _ in range(50)]
        stab_count = types.count("stabilize")
        assert stab_count > 10, f"high volatility should bias toward stabilize, only got {stab_count}/50"
    finally:
        tmp.unlink(missing_ok=True)

check("volatility biases cycle selection toward stabilize", test_volatility_bias)


def test_infra_failure_fitness_zero():
    from evolution_engine import EvolutionEngine
    from pathlib import Path
    import tempfile, json
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text(json.dumps({"fitness_history": [0.8, 0.8]}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        summary = engine.record_cycle(
            cycle_type="mutate", fitness=0.65, success=False, files_changed=[],
            provider="claude", failure_class="auth", fingerprint=""
        )
        assert summary["fitness"] == 0.0, f"auth failure should yield 0.0 fitness, got {summary['fitness']}"
        assert summary["infra_failure"] is True
        assert engine.state["fitness_history"][-1] == 0.0
        # Bandit should NOT have been updated for auth failure
        arms = engine.state.get("bandit", {}).get("arms", {})
        mutate_arm = arms.get("mutate", {})
        assert mutate_arm.get("n", 0) == 0, "bandit should not update on infra failure"
    finally:
        tmp.unlink(missing_ok=True)

check("infra failures (auth/rate_limit) get 0.0 fitness and skip bandit", test_infra_failure_fitness_zero)


def test_code_failure_preserves_fitness():
    from evolution_engine import EvolutionEngine
    from pathlib import Path
    import tempfile, json
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text(json.dumps({"fitness_history": [0.8]}))
    try:
        engine = EvolutionEngine(state_file=tmp)
        summary = engine.record_cycle(
            cycle_type="mutate", fitness=0.55, success=False, files_changed=[],
            provider="claude", failure_class="unknown", fingerprint=""
        )
        assert summary["fitness"] == 0.55, f"code failure should preserve fitness, got {summary['fitness']}"
        assert summary["infra_failure"] is False
    finally:
        tmp.unlink(missing_ok=True)

check("code failures preserve computed fitness in history", test_code_failure_preserves_fitness)


# 29. _parse_interval edge cases
def test_parse_interval():
    from orchestrator import _parse_interval
    assert _parse_interval("5m") == 300
    assert _parse_interval("1h") == 3600
    assert _parse_interval("30s") == 30
    assert _parse_interval("120") == 120
    # Edge cases that should raise
    for bad in ("", "0m", "-5s", "m", "h"):
        try:
            _parse_interval(bad)
            assert False, f"_parse_interval({bad!r}) should have raised ValueError"
        except ValueError:
            pass

check("orchestrator._parse_interval edge cases", test_parse_interval)


# 30. fitness_score clamps out-of-range inputs
def test_fitness_score_clamp():
    from evolution_engine import EvolutionEngine
    engine = EvolutionEngine()
    # Negative novelty, utility > 1, risk > 1, negative duration
    score = engine.fitness_score(True, -5.0, -0.5, 1.5, 2.0)
    assert 0.0 <= score <= 1.0, f"clamped fitness out of range: {score}"
    # All zeros
    score2 = engine.fitness_score(False, 0.0, 0.0, 0.0, 0.0)
    assert 0.0 <= score2 <= 1.0, f"zero-input fitness out of range: {score2}"

check("evolution_engine.fitness_score clamps out-of-range inputs", test_fitness_score_clamp)


# 31. _provider_order parses env var correctly
def test_provider_order():
    from agent import _provider_order, DEFAULT_PROVIDER_ORDER
    old = os.environ.get("EVOLVE_PROVIDER_ORDER")
    try:
        os.environ["EVOLVE_PROVIDER_ORDER"] = "gemini,claude"
        order = _provider_order()
        assert order == ["gemini", "claude"], f"unexpected order: {order}"
        # Invalid providers filtered
        os.environ["EVOLVE_PROVIDER_ORDER"] = "invalid,claude"
        order2 = _provider_order()
        assert order2 == ["claude"], f"invalid provider not filtered: {order2}"
        # Empty falls back to default
        os.environ["EVOLVE_PROVIDER_ORDER"] = ""
        assert _provider_order() == DEFAULT_PROVIDER_ORDER
    finally:
        if old is None:
            os.environ.pop("EVOLVE_PROVIDER_ORDER", None)
        else:
            os.environ["EVOLVE_PROVIDER_ORDER"] = old

check("agent._provider_order parses env correctly", test_provider_order)


# 32. build_patch_fingerprint is deterministic
def test_fingerprint_deterministic():
    from evolution_engine import build_patch_fingerprint
    fp1 = build_patch_fingerprint(["a.py", "b.py"], "test reason")
    fp2 = build_patch_fingerprint(["b.py", "a.py"], "test reason")
    assert fp1 == fp2, "fingerprint should be order-independent"
    fp3 = build_patch_fingerprint(["a.py"], "different reason")
    assert fp1 != fp3, "different inputs should produce different fingerprints"
    assert len(fp1) == 18, f"fingerprint should be 18 chars, got {len(fp1)}"

check("build_patch_fingerprint is deterministic and order-independent", test_fingerprint_deterministic)


# 33. _extract_files_from_response heuristic
def test_extract_files():
    from orchestrator import _extract_files_from_response
    files = _extract_files_from_response("Modified agent.py and tools.py for improvement")
    assert "agent.py" in files, f"should extract agent.py, got {files}"
    assert "tools.py" in files, f"should extract tools.py, got {files}"
    # Paths with slashes are excluded
    files2 = _extract_files_from_response("Check /usr/lib/python3/foo.py")
    assert len(files2) == 0, f"paths with / should be excluded, got {files2}"
    # Empty input
    assert _extract_files_from_response("") == []
    assert _extract_files_from_response(None) == []

check("orchestrator._extract_files_from_response heuristic", test_extract_files)


# 34. tools.execute handles unknown tool gracefully
def test_unknown_tool():
    import tools
    result = tools.execute("nonexistent_tool", {}, "smoke_tid_003", [])
    assert result.is_error, "unknown tool should return error"
    assert "Unknown tool" in result.content

check("tools.execute handles unknown tool gracefully", test_unknown_tool)


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
