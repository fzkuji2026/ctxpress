"""Write benchmark items as Harbor tasks: instruction.md, task.toml, environment/, tests/, dataset_manifest.json.

Converters keep the publisher's items, references and grading code; only the packaging changes. The generated
directory runs on the shared Harbor task format (ctxpress.benchmarks.harbor). Nothing is downloaded here: the
converters read an official release that is already on disk.
"""
from __future__ import annotations
import json, re, shutil
from pathlib import Path

from ctxpress.core import toml

SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def task_id(value):
    """A Harbor task directory name derived from an official item ID, unchanged where it is already safe."""
    value = str(value)
    if SAFE.fullmatch(value):
        return value
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")[:128]
    if not cleaned:
        raise ValueError("item ID cannot form a task directory: " + value)
    return cleaned


def write_task(root, name, *, instruction, config, environment, tests):
    """One task. `environment` / `tests` map relative paths to text or to a source file to copy."""
    directory = Path(root) / task_id(name)
    if directory.exists():
        raise ValueError("task already exists: " + directory.name)
    if not instruction.strip():
        raise ValueError("empty instruction for " + directory.name)
    for folder, files in (("environment", environment), ("tests", tests)):
        for relative, content in files.items():
            path = directory / folder / relative
            if ".." in Path(relative).parts:
                raise ValueError("file escapes the task: " + relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, Path):
                shutil.copyfile(content, path)
            else:
                path.write_text(content, encoding="utf-8")
    if not (directory / "tests" / "test.sh").is_file():
        raise ValueError("a task needs tests/test.sh")
    (directory / "instruction.md").write_text(instruction, encoding="utf-8")
    (directory / "task.toml").write_text(toml.dumps(dict(version="1.0", task=dict(name=directory.name), **config)), encoding="utf-8")
    return directory


def write_manifest(root, dataset, revision, **provenance):
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("declare the official release (revision) of the source data")
    root = Path(root)
    (root / "dataset_manifest.json").write_text(json.dumps(dict(dataset=dataset, revision=revision, **provenance),
                                                           indent=2, ensure_ascii=False), encoding="utf-8")


def new_output(path):
    path = Path(path).expanduser().resolve()
    if path.exists() and any(path.iterdir()):
        raise ValueError("output directory must be new or empty: " + str(path))
    path.mkdir(parents=True, exist_ok=True)
    return path
