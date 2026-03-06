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

errors = []

def check(name, fn):
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


# Report
print()
if errors:
    print(f"SMOKE TEST FAILED — {len(errors)} error(s):")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)
else:
    print(f"SMOKE TEST PASSED — all {5} checks OK")
    sys.exit(0)
