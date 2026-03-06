import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = DATA_DIR / "logs"
ARCHIVE_DIR = DATA_DIR / "archive"
MEMORY_FILE = DATA_DIR / "memory.json"
PERMISSIONS_FILE = DATA_DIR / "permissions.json"
EVOLUTION_LOG = DATA_DIR / "evolution_log.json"
SOURCE_DIR = BASE_DIR  # agent can patch files here

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192
MAX_ITERATIONS = 50  # max tool call loops per task

# Load .env file if present (before reading env vars)
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _v and not os.environ.get(_k.strip()):
                os.environ[_k.strip()] = _v.strip()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Commands always auto-approved (no permission check)
ALWAYS_ALLOW_PATTERNS = [
    r"^ls\b",
    r"^pwd$",
    r"^echo\b",
    r"^cat\b",
    r"^python3?\b.*--version",
    r"^which\b",
    r"^env\b",
    r"^date\b",
]
