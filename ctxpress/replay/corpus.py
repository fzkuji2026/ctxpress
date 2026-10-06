"""Sessions used in the experiments, listed in a manifest (configs/sessions.yaml):

root: <directory the patterns are relative to; a relative root is resolved against the repository root>
sessions:
  - name: navidrome-a
    harness: codex            # codex | claude-code
    pattern: <glob>
    alpha: fit                # fit the token scale on this session, or a number (1.17 when the session's
                              # real input is not the full context, e.g. it ran another method)
Loaded traces are cached in memory per (manifest, keep_text)."""
from __future__ import annotations
import glob, os
import yaml
from ctxpress.replay.loaders import codex
from ctxpress.replay.loaders import claude_code

LOADERS = {"codex": codex.load, "claude-code": claude_code.load}
_cache = {}
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the repository
DEFAULT_MANIFEST = os.path.join(HERE, "configs", "sessions.yaml")


def load_manifest(path=None, names=None, keep_text=True):
    path = path or DEFAULT_MANIFEST
    key = (os.path.abspath(path), tuple(names or ()), keep_text)
    if key in _cache:
        return _cache[key]
    m = yaml.safe_load(open(path, encoding="utf-8"))
    root = os.path.join(HERE, os.path.expanduser(m.get("root", "")))   # an absolute root stays as it is
    out = []
    for s in m["sessions"]:
        if names and s["name"] not in names:
            continue
        files = sorted(glob.glob(os.path.join(root, s["pattern"])))
        if not files:
            raise FileNotFoundError(f"{s['name']}: no file matches {s['pattern']}")
        fit = s.get("alpha", "fit") == "fit"
        tr = LOADERS[s.get("harness", "codex")](files[0], stop_at_compaction=s.get("stop_at_compaction", True),
                                                 keep_text=keep_text, name=s["name"], fit_alpha=fit)
        if not fit:
            tr["alpha"] = float(s["alpha"])
        tr["group"] = s.get("group", s["name"])
        out.append(tr)
    _cache[key] = out
    return out
