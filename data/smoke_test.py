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
