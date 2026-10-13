"""Immutable, content-addressed whole-history archive and hierarchical provenance."""
import hashlib
import json
from pathlib import Path
from ctxpress.core.artifacts import atomic_json


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def put(root, value):
    raw = canonical(value)
    key = hashlib.sha256(raw.encode()).hexdigest()
    directory = Path(root)/'memory'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/(key+'.json')
    if path.is_symlink():
        raise ValueError('memory source must not be a symlink')
    if path.exists():
        if canonical(json.loads(path.read_text())) != raw:
            raise ValueError('memory archive integrity failure')
    else:
        atomic_json(str(path), value)
    return key


def get(root, key):
    if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('invalid memory key')
    p = Path(root)/'memory'/(key+'.json')
    if p.is_symlink():
        raise ValueError('memory source must not be a symlink')
    value = json.loads(p.read_text())
    if hashlib.sha256(canonical(value).encode()).hexdigest() != key:
        raise ValueError('memory archive integrity failure')
    return value


def read(root, key, offset=0, chars=4000):
    if type(offset) is not int or offset < 0 or type(chars) is not int or not 1 <= chars <= 16000:
        raise ValueError('invalid memory page')
    value = get(root, key)
    raw = canonical(value)
    stop = min(len(raw), offset+chars)
    return dict(key=key, text=raw[offset:stop], offset=offset,
                next_offset=stop if stop < len(raw) else None, total_chars=len(raw),
                children=value.get('children', []))


def search(root, query, limit=5):
    if not isinstance(query, str) or not query.strip() or len(query)>512 or type(limit) is not int or not 1<=limit<=20:
        raise ValueError('invalid memory query')
    found=[]
    for p in sorted((Path(root)/'memory').glob('*.json')):
        try:
            value=get(root,p.stem)
        except (OSError,ValueError):
            continue
        if value.get('kind')=='proposal_audit':continue
        # Search readable material, never opaque host payloads or cryptographic blobs.
        raw=canonical({k:v for k,v in value.items() if k!='wire'})
        at=raw.casefold().find(query.casefold())
        if at>=0:
            found.append(dict(key=p.stem,kind=value.get('kind'),excerpt=raw[max(0,at-80):at+240]))
        if len(found)>=limit:break
    return dict(matches=found,limit=limit)
