"""Atomic JSON artifacts shared by calibration and evaluation."""
from __future__ import annotations
import json, os, tempfile
from pathlib import Path


def atomic_write(path,text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temporary=tempfile.mkstemp(prefix='.ctxpress-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:stream.write(text)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.remove(temporary)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.ctxpress-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
