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



def imported(path):
    """Everything a file imports from ctxpress, as dotted names without the "ctxpress." prefix."""
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.startswith("ctxpress"):
            out.update((node.module + "." + alias.name).split(".", 1)[1] for alias in node.names)
        elif isinstance(node, ast.Import):
            out.update(alias.name.split(".", 1)[1] for alias in node.names if alias.name.startswith("ctxpress."))
    return out


FAMILIES = sorted(p.name for p in (PACKAGE / "benchmarks").iterdir() if (p / "__init__.py").exists())
# execution engine -> the families built on it (the engine dispatches to them; they use the engine)
ENGINES = {"harbor": {"pro", "deepswe"}, "swe": {"pro", "polybench", "bigcode"}}


def family(name):
    parts = name.split(".")
    return parts[1] if parts[0] == "benchmarks" and len(parts) > 1 and parts[1] in FAMILIES else None


def under(name, *prefixes):
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def test_evaluation_layers():
    """runtime needs no benchmark and no results; jobs reach benchmarks only through the registry and results only to
    write the final report; checks need no benchmark; a family imports another only along an engine; nothing but
    the CLI entry point imports the command line."""
    problems = []

    def check(paths, allowed):
        for path in paths:
            for name in sorted(imported(path)):
                if not allowed(name, path):
                    problems.append(f"{path.relative_to(PACKAGE)} imports {name}")

    harness = PACKAGE / "harness"
    check((harness / "runtime").rglob("*.py"),
          lambda n, p: not under(n, "benchmarks", "harness.results", "harness.checks", "harness.cli"))
    check((harness / "jobs").rglob("*.py"),
          lambda n, p: not family(n) and not under(n, "harness.checks", "harness.cli")
          and (not under(n, "harness.results") or under(n, "harness.results.report")))
    check((harness / "checks").rglob("*.py"), lambda n, p: not under(n, "benchmarks", "harness.cli"))
    check((harness / "results").rglob("*.py"), lambda n, p: not under(n, "harness.checks", "harness.cli"))

    def family_rule(name, path):
        own = path.relative_to(PACKAGE / "benchmarks").parts[0]
        other = family(name)
        if under(name, "harness.results", "harness.checks", "harness.cli"):
            return False
        if own not in FAMILIES:                   # the registry and shared planners at the top
            return True
        return (other is None or other == own or own in ENGINES.get(other, ()) or other in ENGINES.get(own, ()))
    check((PACKAGE / "benchmarks").rglob("*.py"), family_rule)
    assert not problems, "\n".join(problems)


def test_the_package_and_distribution_versions_agree():
    import re
    from ctxpress import __version__
    pyproject = (PACKAGE.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1) == __version__
