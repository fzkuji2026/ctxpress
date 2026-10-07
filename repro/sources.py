"""Verify pinned comparison sources before executing their functions. No downloads."""
import hashlib
import json
from pathlib import Path


def verify_source(name, root):
    reference = json.loads(Path(__file__).with_name('reference_sources.json').read_text())[name]
    for relative, expected in reference['sha256_lf'].items():
        path = Path(root) / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError('missing or symlinked author reference: ' + relative)
        actual = hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
        if actual != expected:
            raise ValueError('author source differs from pinned comparison reference: ' + relative)
    return reference
