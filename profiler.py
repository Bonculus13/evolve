"""
User behavioral profiler: learns about Jake from shell history, browser history,
recently opened files, and running processes. Builds a profile that informs
the agent's evolution decisions.
"""
import os
import re
import json
import time
import subprocess
from pathlib import Path
from collections import Counter
from config import DATA_DIR

PROFILE_FILE = DATA_DIR / "user_profile.json"
GDRIVE = Path.home() / "Library/CloudStorage/GoogleDrive-jakedieter@gmail.com/My Drive"


def _load_profile() -> dict:
    if PROFILE_FILE.exists():
        return json.loads(PROFILE_FILE.read_text())
    return {}


def _save_profile(p: dict):
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_FILE.write_text(json.dumps(p, indent=2))


def _read_zsh_history(n: int = 500) -> list[str]:
    hist = Path.home() / ".zsh_history"
    if not hist.exists():
        return []
    raw = hist.read_bytes().decode("utf-8", errors="replace")
    cmds = []
    for line in raw.splitlines()[-n:]:
        # zsh history format: `: timestamp:elapsed;command`
        m = re.match(r"^: \d+:\d+;(.+)", line)
        if m:
            cmds.append(m.group(1).strip())
        elif line and not line.startswith(":"):
            cmds.append(line.strip())
    return [c for c in cmds if c]


def _read_browser_history(limit: int = 200) -> list[str]:
    """Read Chrome history via SQLite (copy to avoid lock)."""
    chrome_hist = Path.home() / "Library/Application Support/Google/Chrome/Default/History"
    if not chrome_hist.exists():
        return []
    try:
        import sqlite3, shutil, tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name
        shutil.copy2(chrome_hist, tmp_path)
        con = sqlite3.connect(tmp_path)
        rows = con.execute(
            "SELECT url, title, visit_count FROM urls ORDER BY last_visit_time DESC LIMIT ?",
            (limit,)
        ).fetchall()
        con.close()
        os.unlink(tmp_path)
        return [f"{r[0]} ({r[1]}, {r[2]}x)" for r in rows]
    except Exception:
        return []


def _recent_files(n: int = 50) -> list[str]:
    """Use macOS mdls/Spotlight to find recently modified files."""
    try:
        result = subprocess.run(
            ["mdfind", "-onlyin", str(Path.home()), "kMDItemLastUsedDate >= $time.today(-7)"],
            capture_output=True, text=True, timeout=5
        )
        lines = [l for l in result.stdout.splitlines() if l and ".git" not in l and "Cache" not in l]
        return lines[:n]
    except Exception:
        return []


def _running_apps() -> list[str]:
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get name of (processes where background only is false)'],
            capture_output=True, text=True, timeout=5
        )
        return [a.strip() for a in result.stdout.split(",") if a.strip()]
    except Exception:
        return []


def build_profile() -> dict:
    """Collect all signals and build/update user profile."""
    profile = _load_profile()
    now = time.time()

    # Shell commands
    cmds = _read_zsh_history(500)
    cmd_bases = [c.split()[0] for c in cmds if c.split()]
    cmd_freq = dict(Counter(cmd_bases).most_common(30))

    # Extract project dirs from cd commands
    cd_targets = [c[3:].strip() for c in cmds if c.startswith("cd ")]
    proj_freq = dict(Counter(cd_targets).most_common(20))

    # Keywords from all commands
    all_text = " ".join(cmds).lower()
    tech_keywords = ["python", "node", "git", "docker", "claude", "anthropic", "react",
                     "flask", "sql", "aws", "gcloud", "vim", "code", "npm", "pip",
                     "curl", "ssh", "make", "go", "rust", "terraform"]
    tech_signals = {k: all_text.count(k) for k in tech_keywords if all_text.count(k) > 0}

    # Browser
    browser_urls = _read_browser_history(200)
    domains = []
    for url_line in browser_urls:
        m = re.search(r"https?://([^/]+)", url_line)
        if m:
            domains.append(m.group(1).replace("www.", ""))
    domain_freq = dict(Counter(domains).most_common(20))

    # Recent files
    recent = _recent_files(50)
    file_exts = Counter(Path(f).suffix for f in recent if Path(f).suffix)
    ext_freq = dict(file_exts.most_common(15))

    # Apps
    apps = _running_apps()

    profile.update({
        "last_updated": now,
        "last_updated_human": time.strftime("%Y-%m-%d %H:%M"),
        "shell": {
            "top_commands": cmd_freq,
            "top_directories": proj_freq,
            "tech_signals": tech_signals,
            "recent_commands": cmds[-20:],
        },
        "browser": {
            "top_domains": domain_freq,
        },
        "files": {
            "top_extensions": ext_freq,
            "recent_sample": recent[:10],
        },
        "apps": {
            "currently_running": apps,
        },
    })

    _save_profile(profile)
    return profile


def get_profile_summary() -> str:
    """Return a concise summary for agent prompts."""
    profile = _load_profile()
    if not profile:
        return "No user profile yet. Run profiler.build_profile() first."

    lines = [f"## Jake's Profile (updated {profile.get('last_updated_human', 'unknown')})"]

    shell = profile.get("shell", {})
    if shell.get("top_commands"):
        top = list(shell["top_commands"].items())[:8]
        lines.append("Top shell commands: " + ", ".join(f"{k}({v})" for k, v in top))

    tech = shell.get("tech_signals", {})
    if tech:
        top_tech = sorted(tech.items(), key=lambda x: x[1], reverse=True)[:8]
        lines.append("Tech stack signals: " + ", ".join(f"{k}" for k, _ in top_tech))

    if shell.get("top_directories"):
        top_dirs = list(shell["top_directories"].items())[:5]
        lines.append("Most visited dirs: " + ", ".join(d for d, _ in top_dirs))

    browser = profile.get("browser", {})
    if browser.get("top_domains"):
        top_sites = list(browser["top_domains"].items())[:8]
        lines.append("Top sites: " + ", ".join(f"{d}({n}x)" for d, n in top_sites))

    apps = profile.get("apps", {}).get("currently_running", [])
    if apps:
        lines.append("Running apps: " + ", ".join(apps[:8]))

    return "\n".join(lines)


def sync_to_gdrive():
    """Sync the evolve project to Jake's Google Drive."""
    src = Path(__file__).parent
    dst = GDRIVE / "evolve"
    if not GDRIVE.exists():
        return f"Google Drive not mounted at {GDRIVE}"
    try:
        dst.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["rsync", "-av", "--exclude=__pycache__", "--exclude=*.bak",
             "--exclude=.env", f"{src}/", f"{dst}/"],
            capture_output=True, text=True, timeout=60
        )
        return f"Synced to GDrive: {dst}\n{result.stdout[-300:]}"
    except Exception as e:
        return f"GDrive sync failed: {e}"


if __name__ == "__main__":
    print("Building user profile...")
    profile = build_profile()
    print(get_profile_summary())
    print("\nSyncing to Google Drive...")
    print(sync_to_gdrive())
