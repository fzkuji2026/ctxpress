"""TOML configuration I/O without an external runtime dependency.

Python 3.10 uses the unchanged MIT-licensed parser bundled from this machine's
CPython 3.12 standard library. Newer Python uses its own tomllib.
"""
from __future__ import annotations
import datetime, json, math
try:
    import tomllib
except ModuleNotFoundError:
    from ctxpress._vendor import tomllib

loads = tomllib.loads


def load(path):
    with open(path, 'rb') as stream:
        return tomllib.load(stream)


def value(item):
    """Serialize TOML values, including inline tables and arrays of tables."""
    if isinstance(item, str):
        return json.dumps(item, ensure_ascii=False).replace('\x7f', r'\u007f')
    if isinstance(item, bool):
        return 'true' if item else 'false'
    if isinstance(item, int):
        return str(item)
    if isinstance(item, float):
        return ('nan' if math.isnan(item) else repr(item))
    if isinstance(item, (datetime.datetime, datetime.date, datetime.time)):
        return item.isoformat()
    if isinstance(item, list):
        return '[' + ', '.join(value(x) for x in item) + ']'
    if isinstance(item, dict):
        return '{' + ', '.join(f'{value(key)} = {value(val)}' for key, val in item.items()) + '}'
    raise TypeError(f'unsupported TOML value type: {type(item).__name__}')


def dumps(data):
    return ''.join(f'{value(key)} = {value(val)}\n' for key, val in data.items())
