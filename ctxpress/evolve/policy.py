"""The evolvable tool policy: format, where candidates live, and the checks a candidate must pass before it runs.

A policy is one module ctxpress/methods/readless_policies/evolved/<id>.py, served by ReadLess(policy=<module>) in
front of infinite-context:

    GUIDANCE: str                    added to the agent's instructions (at most MAX_GUIDANCE characters)
    view(cmd, out, ctx) -> str|None  how a command output is shown when it enters the context (None: as it is);
                                     ctx = dict(tokens=<count function>, seen=..., args=...)
    TOOLS: {name: fn(argv) -> str}   commands the agent runs as `python3 -m <module> NAME ARG ...` inside its
                                     own shell commands (they run in the task container, cwd = the repository)
    if __name__ == "__main__": raise SystemExit(cli(TOOLS))     (from ctxpress.evolve.runtime import cli)

A joint policy (space "joint" or "retention" of the loop) may also decide how long an output stays whole:

    retain(info, ctx) -> str|None    the shorter text an output already in the context should become now (None:
                                     keep it); info = dict(cmd, text, tokens, age, later, refetched), served by
                                     ctxpress.methods.retain_hook.RetainHook after our method
    RETAIN: {switch: value}          our method's own retention switches (RETAIN_BOUNDS), clamped by retain_overrides

`TOOL = "python3 -m " + __spec__.name` gives the command prefix to write into GUIDANCE and view footers.
The module may import the standard library parts in ALLOWED and ctxpress.tools.*, ctxpress.evolve.runtime and
ctxpress.methods.readless_policies.seed; it may read files but not write, delete, spawn processes or open sockets.
It must parse as Python 3.8 (the task containers' interpreters vary).

check(path) runs static_check, then dynamic_check in a separate interpreter with a time limit: the module imports,
the fields have the right types, view() on sample outputs returns None or a shorter text, and every tool runs on
sample files within TOOL_SECONDS and prints at most TOOL_CHARS characters.
"""
from __future__ import annotations
import ast, hashlib, json, os, re, subprocess, sys, tempfile

PACKAGE = "ctxpress.methods.readless_policies.evolved"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))      # the repository
DIRECTORY = os.path.join(ROOT, *PACKAGE.split("."))
ALLOWED = {"__future__", "re", "ast", "sys", "json", "textwrap", "collections", "itertools", "functools", "math",
           "hashlib", "shlex", "typing", "dataclasses", "bisect", "heapq", "string", "difflib", "fnmatch", "os",
           "os.path", "tokenize", "io", "keyword"}
ALLOWED_CTXPRESS = ("ctxpress.tools", "ctxpress.evolve.runtime", "ctxpress.methods.readless_policies.seed")
FORBIDDEN_NAMES = {"eval", "exec", "compile", "__import__", "globals", "breakpoint", "input", "setattr", "delattr"}
FORBIDDEN_OS = {"system", "popen", "remove", "unlink", "rmdir", "removedirs", "rename", "renames", "replace", "kill",
                "fork", "execv", "execve", "execl", "execlp", "execvp", "spawnl", "spawnv", "chmod", "chown", "makedirs",
                "mkdir", "write", "truncate", "symlink", "link", "putenv", "chdir", "environ"}
FORBIDDEN_PATH = {"write_text", "write_bytes", "unlink", "rmdir", "mkdir", "touch", "chmod", "symlink_to",
                  "hardlink_to"}      # pathlib is not importable, so rename/replace can only be str methods
MAX_GUIDANCE = 2000
RETAIN_BOUNDS = dict(t=(40000, 100000), keep_recent=(1, 12), output_keep_chars=(0, 4000), guard_keep_frac=(0.0, 0.3),
                     spec_keep_frac=(0.0, 0.3))
MAX_SOURCE = 40000
TOOL_SECONDS = 10
TOOL_CHARS = 60000


def retain_overrides(retain):
    """Our method's switches from a policy's RETAIN, clamped to RETAIN_BOUNDS; unknown keys and bad values dropped."""
    out = {}
    for k, v in (retain or {}).items() if isinstance(retain, dict) else ():
        if k in RETAIN_BOUNDS and isinstance(v, (int, float)) and not isinstance(v, bool):
            lo, hi = RETAIN_BOUNDS[k]
            v = min(hi, max(lo, v))
            out[k] = int(v) if isinstance(lo, int) else float(v)
    return out


def module_name(cid):
    return PACKAGE + "." + cid


def path_of(cid):
    return os.path.join(DIRECTORY, cid + ".py")


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def static_check(src):
    """[problems] of a candidate's source; empty when it may be loaded."""
    if len(src) > MAX_SOURCE:
        return [f"source is {len(src)} characters, more than {MAX_SOURCE}"]
    try:
        tree = ast.parse(src, feature_version=(3, 8))
    except SyntaxError as e:
        return [f"does not parse as Python 3.8: {e}"]
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for n in names:
                if not (n in ALLOWED or n.split(".")[0] in ALLOWED and n.split(".")[0] != "os"
                        or any(n == p or n.startswith(p + ".") for p in ALLOWED_CTXPRESS)):
                    bad.append(f"line {node.lineno}: import {n} is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            bad.append(f"line {node.lineno}: {node.id} is not allowed")
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr in FORBIDDEN_OS:
                bad.append(f"line {node.lineno}: os.{node.attr} is not allowed")
            elif node.attr in FORBIDDEN_PATH:
                bad.append(f"line {node.lineno}: .{node.attr} is not allowed")
            elif node.attr.startswith("__") and node.attr not in ("__name__", "__spec__", "__doc__", "__file__"):
                bad.append(f"line {node.lineno}: attribute {node.attr} is not allowed")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
            if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                                         and not set(mode.value) & set("wax+")):
                bad.append(f"line {node.lineno}: open() only for reading")
    names = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    names |= {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for need in ("GUIDANCE", "view", "TOOLS"):
        if need not in names:
            bad.append(f"module-level {need} is missing")
    if "cli(TOOLS)" not in src or "__main__" not in src:
        bad.append('the module must end with: if __name__ == "__main__": raise SystemExit(cli(TOOLS))')
    return bad


_SAMPLE_PY = '''"""A sample module."""
import os


class Store:
    """Keeps items."""

    def __init__(self, root):
        self.root = root
        self.items = {}

    def add(self, key, value):
        if key in self.items:
            raise KeyError(key)
        self.items[key] = value
        return value

    def get(self, key, default=None):
        return self.items.get(key, default)


def load(path):
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]
''' + "\n".join(f"\n\ndef helper_{i}(x):\n    y = x + {i}\n    if y > 10:\n        return y * 2\n    return y - 1" for i in range(60))

_SAMPLE_GO = '''package store

import "errors"

type Store struct {
\titems map[string]string
}

func New() *Store {
\treturn &Store{items: map[string]string{}}
}

func (s *Store) Add(key, value string) error {
\tif _, ok := s.items[key]; ok {
\t\treturn errors.New("exists")
\t}
\ts.items[key] = value
\treturn nil
}
''' + "\n".join(f"\nfunc Helper{i}(x int) int {{\n\tif x > {i} {{\n\t\treturn x * 2\n\t}}\n\treturn x - 1\n}}" for i in range(60))

_DYNAMIC = r'''
import importlib, json, os, subprocess, sys, time
spec, workdir, seconds, chars = sys.argv[1], sys.argv[2], float(sys.argv[3]), int(sys.argv[4])
from ctxpress.core import tokens
m = importlib.import_module(spec)
bad = []
if not isinstance(getattr(m, "GUIDANCE", None), str): bad.append("GUIDANCE is not a string")
elif len(m.GUIDANCE) > %d: bad.append("GUIDANCE is longer than %d characters")
if not callable(getattr(m, "view", None)): bad.append("view is not a function")
tools = getattr(m, "TOOLS", None)
if not isinstance(tools, dict) or not all(isinstance(k, str) and callable(v) for k, v in tools.items()):
    bad.append("TOOLS is not a dict of name -> function"); tools = {}
py, go = os.path.join(workdir, "pkg/store.py"), os.path.join(workdir, "pkg/store.go")
src = {p: open(p).read() for p in (py, go)}
samples = [("cat pkg/store.py", src[py]), ("nl -ba pkg/store.go", "\n".join(f"{i+1:>6}\t{l}" for i, l in enumerate(src[go].split("\n")))),
           ("sed -n '1,40p' pkg/store.py", "\n".join(src[py].split("\n")[:40])),
           ("grep -rn Store pkg", "\n".join(f"pkg/store.py:{i}:Store x" for i in range(300))),
           ("go test ./...", "ok  \tstore\t0.01s\n" * 200)]
ctx = dict(tokens=tokens.count, seen=set(), args={})
views = 0
for cmd, out in samples:
    t = time.time()
    try:
        v = m.view(cmd, out, ctx)
    except Exception as e:
        bad.append(f"view({cmd!r}) raised {type(e).__name__}: {e}"); continue
    if time.time() - t > seconds: bad.append(f"view({cmd!r}) took more than {seconds} s")
    if v is not None and not isinstance(v, str): bad.append(f"view({cmd!r}) returned {type(v).__name__}")
    elif isinstance(v, str):
        views += 1
        if tokens.count(v) >= tokens.count(out): bad.append(f"view({cmd!r}) is not shorter than the output")
if hasattr(m, "retain"):
    if not callable(m.retain): bad.append("retain is not a function")
    else:
        for cmd, out in samples:
            info = dict(cmd=cmd, text=out, tokens=tokens.count(out), age=12, later="apply_patch Store.add", refetched=False)
            try:
                v = m.retain(info, dict(tokens=tokens.count, request=40))
            except Exception as e:
                bad.append(f"retain({cmd!r}) raised {type(e).__name__}: {e}"); continue
            if v is not None and not isinstance(v, str): bad.append(f"retain({cmd!r}) returned {type(v).__name__}")
            elif isinstance(v, str) and tokens.count(v) >= tokens.count(out): bad.append(f"retain({cmd!r}) is not shorter")
if hasattr(m, "RETAIN") and not isinstance(m.RETAIN, dict): bad.append("RETAIN is not a dict")
runs = []
for name in tools:
    for args in ([py], [go], [py, "Store.add"], [go, "Add"], ["missing.py"]):
        cmd = [sys.executable, "-m", spec, name] + args
        t = time.time()
        try:
            r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=seconds)
        except subprocess.TimeoutExpired:
            bad.append(f"{name} {' '.join(args)} took more than {seconds} s"); continue
        out = r.stdout + r.stderr
        if len(out) > chars: bad.append(f"{name} {' '.join(args)} printed {len(out)} characters")
        if "Traceback" in out: bad.append(f"{name} {' '.join(args)} crashed: {out.strip().splitlines()[-1][:200]}")
        runs.append(dict(tool=name, args=args, code=r.returncode, chars=len(out), head=out[:300]))
print(json.dumps(dict(problems=bad, views=views, runs=runs)))
''' % (MAX_GUIDANCE, MAX_GUIDANCE)


def dynamic_check(cid, timeout=120):
    """(problems, details) from loading and exercising the candidate in a separate interpreter."""
    with tempfile.TemporaryDirectory(prefix="ctxpress-policy-check-") as wd:
        os.makedirs(os.path.join(wd, "pkg"))
        for name, text in (("store.py", _SAMPLE_PY), ("store.go", _SAMPLE_GO)):
            with open(os.path.join(wd, "pkg", name), "w") as f:
                f.write(text)
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
        try:
            r = subprocess.run([sys.executable, "-c", _DYNAMIC, module_name(cid), wd, str(TOOL_SECONDS), str(TOOL_CHARS)],
                               cwd=wd, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return [f"the checks took more than {timeout} s"], {}
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [f"the module failed to load: {(r.stderr.strip().splitlines() or ['?'])[-1][:300]}"], {}
    return res["problems"], res


def write(cid, src):
    os.makedirs(DIRECTORY, exist_ok=True)
    init = os.path.join(DIRECTORY, "__init__.py")
    if not os.path.exists(init):
        with open(init, "w") as f:
            f.write('"""Tool policies written by the evolution loop (ctxpress.evolve); one module per candidate."""\n')
    with open(path_of(cid), "w") as f:
        f.write(src)
    return path_of(cid)


def check(cid, src):
    """(problems, details): static checks first; the module is written and exercised only when they pass."""
    bad = static_check(src)
    if bad:
        return bad, {}
    write(cid, src)
    return dynamic_check(cid)
