"""Validate mounted method inputs on the host without changing container arguments."""
from __future__ import annotations
import copy
import re
from pathlib import Path
from ctxpress.harness import eval_plan
from ctxpress.methods import build


def host_entry(entry, profiles=None):
    """Resolve only the content-addressed files staged by harbor_driver.method_inputs."""
    entry = copy.deepcopy(entry)

    def visit(value):
        args = value.get('args') or {}
        field = {'CostModel': 'profile', 'AutoCostModel': 'policy'}.get(value.get('class'))
        path = args.get(field) if field else None
        if isinstance(path, str) and path.startswith('/ctxpress-method/'):
            match = re.fullmatch(r'/ctxpress-method/([0-9a-f]{64})\.json', path)
            if not match or not profiles:
                raise ValueError('mounted method input requires its declared host directory and SHA-256 filename')
            root = Path(profiles)
            if not root.is_absolute() or root.is_symlink() or not root.is_dir():
                raise ValueError('mounted method input requires an absolute host directory without a symlink')
            source = root / (match[1] + '.json')
            if source.is_symlink() or not source.is_file() or source.resolve().parent != root.resolve():
                raise ValueError('mounted method input is missing or escapes its host directory')
            if eval_plan.file_sha256(source) != match[1]:
                raise ValueError('mounted method input differs from its frozen SHA-256')
            args[field] = str(source)
        if isinstance(args.get('inner'), dict):
            visit(args['inner'])
        for child in args.get('methods', []):
            visit(child)

    visit(entry)
    return entry


def validate_live(entry, profiles=None):
    build(host_entry(entry, profiles)).validate_live()
