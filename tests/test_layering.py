"""The framework's layers import only downward; evaluation code is never imported by the framework."""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "ctxpress"
# layer -> the ctxpress packages it may import (besides itself and the vendored libraries)
ALLOWED = {
    "core": set(),
    "methods": {"core"},
    "live": {"core", "methods", "settings"},
    "settings": set(),
    "hosts": {"core", "methods", "live", "settings"},
    "replay": {"core", "methods"},
}


def imports(path):
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.startswith("ctxpress."):
            out.add(node.module.split(".")[1])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "ctxpress":
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            out.update(alias.name.split(".")[1] for alias in node.names if alias.name.startswith("ctxpress."))
    return out


def test_layers_import_only_downward():
    problems = []
    for layer, allowed in ALLOWED.items():
        files = [PACKAGE / "settings.py"] if layer == "settings" else (PACKAGE / layer).rglob("*.py")
        for path in files:
            bad = imports(path) - allowed - {layer, "_vendor"}
            if bad:
                problems.append(f"{path.relative_to(PACKAGE)} imports {sorted(bad)}")
    assert not problems, "\n".join(problems)


def test_the_package_entry_point_uses_the_framework_only():
    assert imports(PACKAGE / "__init__.py") <= {"core", "methods", "live"}


def submodules(path):
    """ctxpress.<a>.<b> imports as "a.b" (or "a" for a top-level module)."""
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.startswith("ctxpress."):
            names = [node.module + "." + alias.name for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names if alias.name.startswith("ctxpress.")]
        for name in names:
            parts = name.split(".")[1:]
            out.add(".".join(parts[:2]) if parts[0] in ("harness", "benchmarks") and len(parts) > 1 else parts[0])
    return out


def test_evaluation_layers():
    """Benchmarks and the container runtime never reach into results or checks; checks need no benchmark."""
    problems = []
    families = [p for p in (PACKAGE / "benchmarks").iterdir() if p.is_dir() and (p / "__init__.py").exists()]
    for folder in families + [PACKAGE / "harness" / "runtime"]:
        for path in folder.rglob("*.py"):
            bad = {m for m in submodules(path) if m in ("harness.results", "harness.checks")}
            if bad:
                problems.append(f"{path.relative_to(PACKAGE)} imports {sorted(bad)}")
    for path in (PACKAGE / "harness" / "checks").rglob("*.py"):
        bad = {m for m in submodules(path) if m.startswith("benchmarks")}
        if bad:
            problems.append(f"{path.relative_to(PACKAGE)} imports {sorted(bad)}")
    assert not problems, "\n".join(problems)
