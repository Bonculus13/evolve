"""
Git + GitHub release automation.
The agent calls these functions to commit its own changes and publish releases.
Usage: python3 git_release.py  — auto-commits any pending changes and pushes
"""
import subprocess
import json
import time
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
CHANGELOG = ROOT / "CHANGELOG.md"
REMOTE = "origin"
BRANCH = "main"


def _run(cmd: str, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          cwd=str(ROOT), check=check)


def git_status() -> str:
    r = _run("git status --short", check=False)
    return r.stdout.strip()


def current_version() -> str:
    """Read latest version tag from CHANGELOG or git tags."""
    try:
        r = _run("git describe --tags --abbrev=0", check=False)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    if CHANGELOG.exists():
        for line in CHANGELOG.read_text().splitlines():
            m = re.search(r"##\s+(v[\d.]+)", line)
            if m:
                return m.group(1)
    return "v0.0.0"


def next_version(bump: str = "patch") -> str:
    v = current_version().lstrip("v")
    parts = v.split(".")
    while len(parts) < 3:
        parts.append("0")
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    if bump == "major":
        return f"v{major+1}.0.0"
    elif bump == "minor":
        return f"v{major}.{minor+1}.0"
    else:
        return f"v{major}.{minor}.{patch+1}"


def commit_and_push(message: str, push: bool = True) -> str:
    """Stage all changes, commit, push."""
    status = git_status()
    if not status:
        return "Nothing to commit."

    _run("git add -A -- ':!.env' ':!data/memory.json' ':!data/permissions.json' ':!data/user_profile.json' ':!data/.current_prompt.txt' ':!*.bak'")
    _run(f'git commit -m "{message}\n\nCo-authored by evolve agent"')

    if push:
        r = _run(f"git push {REMOTE} {BRANCH}", check=False)
        if r.returncode != 0:
            return f"Push failed: {r.stderr.strip()}"
        return f"Pushed: {message}"
    return f"Committed (not pushed): {message}"


def create_release(version: str, notes: str, push: bool = True) -> str:
    """Tag + GitHub release."""
    # Commit any pending changes first
    status = git_status()
    if status:
        commit_and_push(f"chore: pre-release cleanup for {version}", push=push)

    # Tag
    r = _run(f'git tag -a {version} -m "Release {version}"', check=False)
    if r.returncode != 0 and "already exists" not in r.stderr:
        return f"Tag failed: {r.stderr}"

    if push:
        _run(f"git push {REMOTE} {version}", check=False)

    # GitHub release via gh CLI
    notes_file = ROOT / "data" / ".release_notes.md"
    notes_file.write_text(notes)
    r = _run(
        f'gh release create {version} --title "Evolve {version}" --notes-file "{notes_file}"',
        check=False
    )
    notes_file.unlink(missing_ok=True)

    if r.returncode == 0:
        url = r.stdout.strip()
        return f"Released {version}: {url}"
    return f"Release failed: {r.stderr.strip()}"


def auto_release_from_lessons(bump: str = "patch") -> str:
    """Read recent lessons, build release notes, create a release."""
    from memory import get_recent_lessons
    lessons = get_recent_lessons(20)

    version = next_version(bump)
    date = time.strftime("%Y-%m-%d")

    notes_lines = [f"## {version} — {date}\n", "### Changes (auto-generated from agent memory)\n"]
    for l in lessons:
        if l.startswith("NEXT IMPROVEMENT PLAN"):
            continue
        notes_lines.append(f"- {l[:200]}")

    notes = "\n".join(notes_lines)

    # Prepend to CHANGELOG
    existing = CHANGELOG.read_text() if CHANGELOG.exists() else ""
    CHANGELOG.write_text(f"# Changelog\n\n{notes}\n\n" + existing.replace("# Changelog\n\n", ""))

    return create_release(version, notes)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(git_status() or "Clean")
        print(f"Current version: {current_version()}")
        print(f"Next patch: {next_version()}")
    elif cmd == "commit":
        msg = sys.argv[2] if len(sys.argv) > 2 else "chore: agent auto-commit"
        print(commit_and_push(msg))
    elif cmd == "release":
        bump = sys.argv[2] if len(sys.argv) > 2 else "patch"
        print(auto_release_from_lessons(bump))
