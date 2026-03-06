#!/usr/bin/env python3
"""
Evolve Dashboard — web GUI for monitoring and controlling the auto-evolve loop.
Run: python3 dashboard.py
Opens at http://localhost:7842
"""
import sys
import json
import time
import threading
import subprocess
import os
import platform
import shutil
from pathlib import Path
from flask import Flask, jsonify, request, Response

sys.path.insert(0, str(Path(__file__).parent))
from config import DATA_DIR, EVOLUTION_LOG, SOURCE_DIR
import memory as mem

app = Flask(__name__)
PORT = 7842
_dashboard_start_ts = time.time()

# Shared state
_state = {
    "running": False,
    "cycle": 0,
    "last_output": "",
    "proc": None,
    "task_queue": [],
    "log_file": None,
}
_lock = threading.Lock()


HTML = """<!DOCTYPE html>
<html>
<head>
<title>Evolve Dashboard</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'SF Mono', 'Menlo', monospace; background: #0d0d0d; color: #e0e0e0; padding: 20px; }
h1 { color: #00ff88; font-size: 1.4em; margin-bottom: 16px; letter-spacing: 2px; }
h2 { color: #888; font-size: 0.85em; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.card { background: #161616; border: 1px solid #2a2a2a; border-radius: 6px; padding: 16px; }
.card.full { grid-column: 1 / -1; }
.status { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: #444; }
.dot.running { background: #00ff88; box-shadow: 0 0 8px #00ff88; animation: pulse 1.5s infinite; }
.dot.stopped { background: #ff4444; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
.btn { background: #1a1a1a; border: 1px solid #333; color: #e0e0e0; padding: 8px 16px;
       border-radius: 4px; cursor: pointer; font-family: inherit; font-size: 0.85em;
       margin: 4px; transition: all 0.15s; }
.btn:hover { border-color: #00ff88; color: #00ff88; }
.btn.danger:hover { border-color: #ff4444; color: #ff4444; }
.btn.primary { border-color: #00ff88; color: #00ff88; }
#log { background: #0a0a0a; border: 1px solid #1e1e1e; border-radius: 4px; padding: 12px;
       height: 320px; overflow-y: auto; font-size: 0.78em; line-height: 1.6; white-space: pre-wrap;
       word-break: break-all; }
.log-tool { color: #4ec9b0; }
.log-claude { color: #dcdcaa; }
.log-result { color: #00ff88; }
.log-error { color: #f48771; }
.log-cycle { color: #569cd6; font-weight: bold; }
.stat { font-size: 1.8em; color: #00ff88; font-weight: bold; }
.stat-label { font-size: 0.75em; color: #666; margin-top: 2px; }
.stats-row { display: flex; gap: 20px; flex-wrap: wrap; }
.stat-box { text-align: center; }
input[type=text], textarea { background: #1a1a1a; border: 1px solid #333; color: #e0e0e0;
  padding: 8px; border-radius: 4px; font-family: inherit; font-size: 0.85em; width: 100%; }
textarea { height: 60px; resize: vertical; }
.queue-item { background: #1a1a1a; border: 1px solid #2a2a2a; padding: 8px 12px;
              border-radius: 4px; margin: 4px 0; display: flex; justify-content: space-between;
              align-items: center; font-size: 0.82em; }
.tag { background: #1e3a2a; color: #00ff88; padding: 2px 8px; border-radius: 10px;
       font-size: 0.75em; margin-left: 8px; }
.memory-block { font-size: 0.8em; line-height: 1.7; color: #aaa; }
</style>
</head>
<body>
<h1>⟳ EVOLVE DASHBOARD</h1>
<div class="grid">

  <!-- Status + Controls -->
  <div class="card">
    <h2>Status</h2>
    <div class="status">
      <div class="dot" id="status-dot"></div>
      <span id="status-text">Loading...</span>
    </div>
    <div class="stats-row" id="stats-row">
      <div class="stat-box"><div class="stat" id="stat-cycle">0</div><div class="stat-label">Cycle</div></div>
      <div class="stat-box"><div class="stat" id="stat-success">0</div><div class="stat-label">Successes</div></div>
      <div class="stat-box"><div class="stat" id="stat-evolutions">0</div><div class="stat-label">Evolutions</div></div>
      <div class="stat-box"><div class="stat" id="stat-uptime">0s</div><div class="stat-label">Uptime</div></div>
    </div>
    <div style="margin-top:12px">
      <button class="btn primary" onclick="startAuto()">▶ Auto-Evolve</button>
      <button class="btn danger" onclick="stopAgent()">■ Stop</button>
      <button class="btn" onclick="triggerEvolve()">⟳ One Cycle</button>
      <button class="btn" onclick="syncGDrive()">☁ Sync GDrive</button>
      <button class="btn" onclick="refreshProfile()">👤 Profile</button>
    </div>
  </div>

  <!-- Task Queue -->
  <div class="card">
    <h2>Task Queue</h2>
    <textarea id="task-input" placeholder="Enter a task for the agent..."></textarea>
    <div style="margin-top:6px">
      <button class="btn primary" onclick="queueTask()">+ Queue Task</button>
      <button class="btn" onclick="runNextTask()">▶ Run Next</button>
    </div>
    <div id="queue-list" style="margin-top:10px;max-height:120px;overflow-y:auto"></div>
  </div>

  <!-- Live Log -->
  <div class="card full">
    <h2>Live Output <span id="cycle-badge"></span>
      <button class="btn" style="float:right;padding:4px 10px" onclick="clearLog()">clear</button>
    </h2>
    <div id="log"></div>
  </div>

  <!-- Memory -->
  <div class="card">
    <h2>Agent Memory</h2>
    <div id="memory-block" class="memory-block">Loading...</div>
  </div>

  <!-- Evolution Log -->
  <div class="card">
    <h2>Evolution Log</h2>
    <div id="evo-log" class="memory-block">Loading...</div>
  </div>

  <!-- Files -->
  <div class="card full">
    <h2>Files</h2>
    <div id="files-list" style="max-height:300px;overflow-y:auto">
      <table style="width:100%;border-collapse:collapse;font-size:0.82em">
        <thead><tr style="color:#888;text-align:left;border-bottom:1px solid #2a2a2a">
          <th style="padding:6px 8px">Name</th>
          <th style="padding:6px 8px;text-align:right">Size</th>
          <th style="padding:6px 8px;text-align:right">Modified</th>
        </tr></thead>
        <tbody id="files-tbody"><tr><td colspan="3" style="padding:8px;color:#444">Loading...</td></tr></tbody>
      </table>
    </div>
  </div>

</div>

<script>
let lastLogLen = 0;
let startTime = null;

async function api(path, method='GET', body=null) {
  const opts = {method, headers:{'Content-Type':'application/json'}};
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  return r.json();
}

function colorLog(text) {
  return text
    .replace(/\\[TOOL\\][^\\n]*/g, m => `<span class="log-tool">${m}</span>`)
    .replace(/\\[Claude\\][^\\n]*/g, m => `<span class="log-claude">${m}</span>`)
    .replace(/\\[RESULT\\][^\\n]*/g, m => `<span class="log-result">${m}</span>`)
    .replace(/\\[ERROR\\][^\\n]*/g, m => `<span class="log-error">${m}</span>`)
    .replace(/\\[CYCLE[^\\]]*\\][^\\n]*/g, m => `<span class="log-cycle">${m}</span>`);
}

async function poll() {
  const data = await api('/api/status');
  const dot = document.getElementById('status-dot');
  dot.className = 'dot ' + (data.running ? 'running' : 'stopped');
  document.getElementById('status-text').textContent = data.running ? 'Running' : 'Stopped';
  document.getElementById('stat-cycle').textContent = data.cycle;
  document.getElementById('stat-success').textContent = data.successes;
  document.getElementById('stat-evolutions').textContent = data.evolutions;
  if (data.running && !startTime) startTime = Date.now();
  if (!data.running) startTime = null;
  if (startTime) {
    const s = Math.floor((Date.now()-startTime)/1000);
    document.getElementById('stat-uptime').textContent = s > 60 ? Math.floor(s/60)+'m' : s+'s';
  }

  // Log
  if (data.log && data.log.length > lastLogLen) {
    const newText = data.log.slice(lastLogLen);
    lastLogLen = data.log.length;
    const el = document.getElementById('log');
    const div = document.createElement('div');
    div.innerHTML = colorLog(escHtml(newText));
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
  }

  // Queue
  const ql = document.getElementById('queue-list');
  ql.innerHTML = data.queue.map((t,i) =>
    `<div class="queue-item">${escHtml(t.slice(0,60))} <button class="btn danger" style="padding:2px 8px" onclick="removeTask(${i})">×</button></div>`
  ).join('') || '<div style="color:#444;font-size:0.8em">Queue empty</div>';
}

async function loadMemory() {
  const data = await api('/api/memory');
  document.getElementById('memory-block').textContent = data.context;
  document.getElementById('evo-log').textContent = data.evo_log;
}

function escHtml(t) {
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function startAuto() { await api('/api/start', 'POST'); }
async function stopAgent() { await api('/api/stop', 'POST'); startTime=null; }
async function triggerEvolve() { await api('/api/evolve', 'POST'); }
async function syncGDrive() {
  const r = await api('/api/gdrive_sync', 'POST');
  alert(r.result);
}
async function refreshProfile() {
  const r = await api('/api/profile', 'POST');
  alert(r.summary);
}
async function queueTask() {
  const t = document.getElementById('task-input').value.trim();
  if (!t) return;
  await api('/api/queue', 'POST', {task: t});
  document.getElementById('task-input').value = '';
}
async function runNextTask() { await api('/api/run_next', 'POST'); }
async function removeTask(i) { await api('/api/queue/' + i, 'DELETE'); }
function clearLog() {
  document.getElementById('log').innerHTML = '';
  lastLogLen = 0;
  api('/api/clear_log', 'POST');
}

async function loadFiles() {
  const data = await api('/api/files');
  const tbody = document.getElementById('files-tbody');
  if (!data.files || data.files.length === 0) {
    tbody.innerHTML = '<tr><td colspan="3" style="padding:8px;color:#444">No files found</td></tr>';
    return;
  }
  tbody.innerHTML = data.files.map(f =>
    `<tr style="border-bottom:1px solid #1e1e1e">
      <td style="padding:4px 8px;color:#4ec9b0">${escHtml(f.name)}</td>
      <td style="padding:4px 8px;text-align:right">${f.size}</td>
      <td style="padding:4px 8px;text-align:right;color:#888">${escHtml(f.modified)}</td>
    </tr>`
  ).join('');
}

// Poll every 2s, memory every 10s, files every 15s
setInterval(poll, 2000);
setInterval(loadMemory, 10000);
setInterval(loadFiles, 15000);
poll();
loadMemory();
loadFiles();
</script>
</body>
</html>
"""


# ── State management ──────────────────────────────────────────────────────────

def _get_log() -> str:
    lf = _state.get("log_file")
    if lf and Path(lf).exists():
        return Path(lf).read_text(errors="replace")
    return ""


def _get_stats() -> dict:
    hist = mem.get_all().get("task_history", [])
    successes = sum(1 for t in hist if t.get("success"))
    evo_log = []
    if EVOLUTION_LOG.exists():
        try:
            evo_log = json.loads(EVOLUTION_LOG.read_text())
        except Exception:
            pass
    return {"successes": successes, "evolutions": len(evo_log)}


def _start_loop(task_override: str | None = None):
    """Launch orchestrator as subprocess, capture output to a temp log file."""
    import tempfile
    if _state["proc"] and _state["proc"].poll() is None:
        return  # already running

    log_f = tempfile.mktemp(suffix=".log", prefix="evolve_")
    _state["log_file"] = log_f

    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    env["PYTHONUNBUFFERED"] = "1"

    if task_override:
        cmd = ["python3", str(SOURCE_DIR / "orchestrator.py"), task_override]
    else:
        cmd = ["python3", str(SOURCE_DIR / "orchestrator.py"), "--auto-evolve"]

    with open(log_f, "w") as f:
        proc = subprocess.Popen(cmd, stdout=f, stderr=f, env=env, cwd=str(SOURCE_DIR))

    _state["proc"] = proc
    _state["running"] = True
    _state["log_file"] = log_f

    def _watch():
        proc.wait()
        _state["running"] = False
    threading.Thread(target=_watch, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return HTML


@app.route("/api/status")
def status():
    stats = _get_stats()
    log = _get_log()
    return jsonify({
        "running": _state["running"],
        "cycle": _state["cycle"],
        "log": log,
        "queue": _state["task_queue"],
        **stats,
    })


@app.route("/api/start", methods=["POST"])
def start():
    _start_loop()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop():
    proc = _state.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
    _state["running"] = False
    return jsonify({"ok": True})


@app.route("/api/evolve", methods=["POST"])
def evolve_one():
    _start_loop("--evolve")
    return jsonify({"ok": True})


@app.route("/api/queue", methods=["POST"])
def queue_task():
    task = request.json.get("task", "").strip()
    if task:
        _state["task_queue"].append(task)
    return jsonify({"ok": True, "queue": _state["task_queue"]})


@app.route("/api/queue/<int:idx>", methods=["DELETE"])
def remove_task(idx):
    if 0 <= idx < len(_state["task_queue"]):
        _state["task_queue"].pop(idx)
    return jsonify({"ok": True})


@app.route("/api/run_next", methods=["POST"])
def run_next():
    if not _state["task_queue"]:
        return jsonify({"ok": False, "error": "Queue empty"})
    task = _state["task_queue"].pop(0)
    _start_loop(task)
    return jsonify({"ok": True, "task": task})


@app.route("/api/memory")
def memory():
    context = mem.get_memory_context()
    evo_log = ""
    if EVOLUTION_LOG.exists():
        try:
            log = json.loads(EVOLUTION_LOG.read_text())
            lines = []
            for e in log[-10:]:
                ts = time.strftime("%m-%d %H:%M", time.localtime(e.get("timestamp", 0)))
                lines.append(f"[{ts}] {e.get('description','')} — {e.get('reason','')[:80]}")
            evo_log = "\n".join(lines) or "No evolutions yet."
        except Exception:
            evo_log = "Error reading evolution log."
    return jsonify({"context": context, "evo_log": evo_log})


@app.route("/api/gdrive_sync", methods=["POST"])
def gdrive_sync():
    from profiler import sync_to_gdrive
    result = sync_to_gdrive()
    return jsonify({"result": result})


@app.route("/api/profile", methods=["POST"])
def profile():
    from profiler import build_profile, get_profile_summary
    build_profile()
    return jsonify({"summary": get_profile_summary()})


@app.route("/api/files")
def files():
    """Return source files with size and last-modified timestamp."""
    result = []
    for p in sorted(SOURCE_DIR.rglob("*.py")):
        if p.name.startswith(".") or "__pycache__" in str(p):
            continue
        try:
            st = p.stat()
            size_b = st.st_size
            if size_b >= 1024:
                size_str = f"{size_b / 1024:.1f} KB"
            else:
                size_str = f"{size_b} B"
            mod = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            rel = str(p.relative_to(SOURCE_DIR))
            result.append({"name": rel, "size": size_str, "modified": mod})
        except OSError:
            continue
    return jsonify({"files": result})


@app.route("/api/health")
def health_check():
    """Return system health info: Python version, disk, memory, uptime."""
    # Python version
    py_version = sys.version

    # Disk usage for the volume containing the project
    disk = shutil.disk_usage(str(SOURCE_DIR))
    disk_info = {
        "total_gb": round(disk.total / (1 << 30), 2),
        "used_gb": round(disk.used / (1 << 30), 2),
        "free_gb": round(disk.free / (1 << 30), 2),
        "percent_used": round(disk.used / disk.total * 100, 1),
    }

    # Memory stats via vm_stat on macOS, fallback for others
    mem_info = {}
    try:
        if platform.system() == "Darwin":
            vm = subprocess.check_output(["vm_stat"], text=True, timeout=5)
            page_size = 16384
            for line in vm.splitlines():
                if "page size of" in line:
                    page_size = int(line.split()[-2])
                    break
            pages = {}
            for line in vm.splitlines():
                if ":" in line and "page size" not in line:
                    key, val = line.split(":", 1)
                    val = val.strip().rstrip(".")
                    if val.isdigit():
                        pages[key.strip()] = int(val)
            free = pages.get("Pages free", 0)
            active = pages.get("Pages active", 0)
            inactive = pages.get("Pages inactive", 0)
            wired = pages.get("Pages wired down", 0)
            mem_info = {
                "active_gb": round(active * page_size / (1 << 30), 2),
                "wired_gb": round(wired * page_size / (1 << 30), 2),
                "inactive_gb": round(inactive * page_size / (1 << 30), 2),
                "free_gb": round(free * page_size / (1 << 30), 2),
            }
        else:
            with open("/proc/meminfo") as f:
                mi = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        mi[parts[0].rstrip(":")] = int(parts[1])
                total = mi.get("MemTotal", 0)
                avail = mi.get("MemAvailable", 0)
                mem_info = {
                    "total_gb": round(total / (1 << 20), 2),
                    "available_gb": round(avail / (1 << 20), 2),
                    "percent_used": round((total - avail) / total * 100, 1) if total else 0,
                }
    except Exception:
        mem_info = {"error": "Could not read memory stats"}

    # Process uptime
    uptime_s = round(time.time() - _dashboard_start_ts, 1)
    if uptime_s >= 3600:
        uptime_str = f"{uptime_s / 3600:.1f}h"
    elif uptime_s >= 60:
        uptime_str = f"{uptime_s / 60:.1f}m"
    else:
        uptime_str = f"{uptime_s:.0f}s"

    return jsonify({
        "status": "ok",
        "python_version": py_version,
        "platform": platform.platform(),
        "disk": disk_info,
        "memory": mem_info,
        "uptime_seconds": uptime_s,
        "uptime_human": uptime_str,
    })


@app.route("/api/clear_log", methods=["POST"])
def clear_log():
    lf = _state.get("log_file")
    if lf and Path(lf).exists():
        Path(lf).write_text("")
    return jsonify({"ok": True})


if __name__ == "__main__":
    import webbrowser
    print(f"[Dashboard] Starting at http://localhost:{PORT}")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
