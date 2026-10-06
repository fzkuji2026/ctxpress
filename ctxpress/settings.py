"""User settings of the installed ctxpress (which method, proxy port, upstream), in $CTXPRESS_HOME/config.json
(default ~/.ctxpress). `ctxpress use` edits it; each Codex launch snapshots the
chosen method. Switching methods takes effect on the next launch."""
from __future__ import annotations
import json, os

DEFAULTS = dict(method="NoCompaction", args={}, port=8899, upstream="https://chatgpt.com/backend-api/codex", via=None)


def home():
    return os.environ.get("CTXPRESS_HOME") or os.path.join(os.path.expanduser("~"), ".ctxpress")


def path():
    return os.path.join(home(), "config.json")


def load():
    try:
        return {**DEFAULTS, **json.load(open(path(), encoding="utf-8"))}
    except (FileNotFoundError, ValueError):
        return dict(DEFAULTS)


def save(cfg):
    os.makedirs(home(), exist_ok=True)
    with open(path(), "w", encoding="utf-8") as fh:
        json.dump({**DEFAULTS, **cfg}, fh, ensure_ascii=False, indent=1)


def store_dir():
    return os.environ.get("CTXPRESS_STORE_DIR") or os.path.join(home(), "store")


def log_path():
    return os.environ.get("CTXPRESS_LOG_PATH") or os.path.join(home(), "requests.jsonl")
