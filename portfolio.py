"""
Portfolio management for app replacement and archival decisions.
"""
import json
import shutil
import time
from pathlib import Path
from typing import Any

from config import DATA_DIR, ARCHIVE_DIR


PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
ARCHIVE_LOG_FILE = DATA_DIR / "archive_log.jsonl"


def _load_portfolio() -> dict[str, Any]:
    if PORTFOLIO_FILE.exists():
        try:
            data = json.loads(PORTFOLIO_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"apps": []}


def _save_portfolio(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_FILE.write_text(json.dumps(data, indent=2))


def _append_archive_log(entry: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ARCHIVE_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def register_app(
    name: str,
    path: str,
    capability_tags: list[str],
    owner: str = "external",
    status: str = "active",
) -> None:
    data = _load_portfolio()
    apps = data.get("apps", [])

    existing = next((a for a in apps if a.get("name") == name), None)
    payload = {
        "name": name,
        "path": str(path),
        "capability_tags": sorted(set(capability_tags)),
        "owner": owner,
        "status": status,
        "updated_at": time.time(),
    }

    if existing is None:
        apps.append(payload)
    else:
        existing.update(payload)

    data["apps"] = apps
    _save_portfolio(data)


def build_replacement_report(orchestrator_tags: list[str]) -> dict[str, Any]:
    data = _load_portfolio()
    apps = data.get("apps", [])
    orchestrator_set = set(orchestrator_tags)
    report_apps: list[dict[str, Any]] = []

    for app in apps:
        tags = set(app.get("capability_tags", []))
        overlap = sorted(tags.intersection(orchestrator_set))
        coverage = 0.0 if not tags else len(overlap) / len(tags)
        decision = "keep"
        if coverage >= 0.8:
            decision = "archive_candidate"
        elif coverage >= 0.4:
            decision = "merge_candidate"

        report_apps.append({
            "name": app.get("name"),
            "path": app.get("path"),
            "status": app.get("status", "active"),
            "capability_tags": sorted(tags),
            "overlap_tags": overlap,
            "coverage_ratio": round(coverage, 2),
            "decision": decision,
        })

    return {
        "generated_at": time.time(),
        "orchestrator_tags": sorted(orchestrator_set),
        "apps": report_apps,
    }


def archive_app(name: str, reason: str) -> dict[str, Any]:
    data = _load_portfolio()
    apps = data.get("apps", [])
    app = next((a for a in apps if a.get("name") == name), None)
    if app is None:
        raise ValueError(f"Unknown app: {name}")

    src = Path(app.get("path", "")).expanduser()
    if not src.exists():
        raise ValueError(f"Path does not exist: {src}")

    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    dest_dir = ARCHIVE_DIR / f"{name}-{ts}"
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    if src.is_dir():
        shutil.move(str(src), str(dest_dir))
    else:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest_dir / src.name))

    app["status"] = "archived"
    app["archived_at"] = time.time()
    app["archive_path"] = str(dest_dir)
    app["archive_reason"] = reason
    _save_portfolio(data)

    entry = {
        "ts": time.time(),
        "app": name,
        "from": str(src),
        "to": str(dest_dir),
        "reason": reason,
    }
    _append_archive_log(entry)
    return entry


def render_replacement_report(report: dict[str, Any]) -> str:
    lines = [
        "=== REPLACEMENT REPORT ===",
        f"Apps tracked: {len(report.get('apps', []))}",
        f"Orchestrator capabilities: {', '.join(report.get('orchestrator_tags', []))}",
        "",
    ]
    for app in report.get("apps", []):
        lines.append(
            f"- {app['name']} [{app['status']}] decision={app['decision']} "
            f"coverage={app['coverage_ratio']:.2f}"
        )
        lines.append(f"  path={app['path']}")
        lines.append(f"  overlap={', '.join(app.get('overlap_tags', [])) or '(none)'}")
    return "\n".join(lines)
