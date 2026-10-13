"""Read-only bounded retrieval over a run-scoped archive. Sources remain immutable."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re

MEMORY_TOOLS = (
    dict(name='infinite_context_memory_read', description='Read archived conversation units and stage summaries. Follow children to recover exact sources and previous versions.', inputSchema=dict(type='object',properties=dict(key=dict(type='string'),session=dict(type='string'),offset=dict(type='integer',minimum=0),chars=dict(type='integer',minimum=1,maximum=16000)),required=['key'])),
    dict(name='infinite_context_memory_search', description='Search archived history and stages by text, goals, paths or failures.', inputSchema=dict(type='object',properties=dict(query=dict(type='string'),session=dict(type='string'),limit=dict(type='integer',minimum=1,maximum=20)),required=['query'])),
)

TOOLS = (
    dict(name='infinite_context_search', description='Find archived context by literal text; returns IDs and short excerpts, not full history.',
         inputSchema=dict(type='object', properties=dict(query=dict(type='string'), session=dict(type='string'),
                         limit=dict(type='integer', minimum=1, maximum=20)), required=['query'])),
    dict(name='infinite_context_read', description='Read a page of archived original text. Source ID and session are in the retained context. Read more only if needed.',
         inputSchema=dict(type='object', properties=dict(id=dict(type='integer',minimum=0),session=dict(type='string'),
                         offset=dict(type='integer',minimum=0),chars=dict(type='integer',minimum=1,maximum=16000)), required=['id'])),
)


def directory(root, session=None):
    root=Path(root).resolve()
    if session is None:
        return root
    if not isinstance(session,str) or not re.fullmatch('[0-9a-f]{32}',session):
        raise ValueError('invalid archive session')
    candidate=root/session
    if candidate.is_symlink() or candidate.resolve().parent!=root:
        raise ValueError('archive session escapes run directory')
    return candidate


def read(root, iid, *, session=None, offset=0, chars=4000):
    if type(iid) is not int or iid<0 or type(offset) is not int or offset<0 or type(chars) is not int or not 1<=chars<=16000:
        raise ValueError('invalid archive ID or page bounds')
    base=directory(root,session)
    path=base/f'{iid}.txt'
    if path.is_symlink():raise ValueError('archive source must not be a symlink')
    text=path.read_text(encoding='utf-8')
    meta=base/f'{iid}.infinite.json'
    if meta.exists():
        expected=json.loads(meta.read_text())['sha256']
        if hashlib.sha256(text.encode()).hexdigest()!=expected:
            raise ValueError('archive integrity check failed')
    stop=min(len(text),offset+chars)
    return dict(id=iid,session=session,text=text[offset:stop],offset=offset,
                next_offset=stop if stop<len(text) else None,total_chars=len(text))


def search(root, query, *, session=None, limit=5):
    if not isinstance(query,str) or not query.strip() or len(query)>512 or type(limit) is not int or not 1<=limit<=20:
        raise ValueError('query and bounded result limit required')
    base=directory(root,session)
    found=[]
    for path in sorted(base.glob('*.txt')):
        if not path.stem.isdecimal() or path.is_symlink():continue
        text=path.read_text(encoding='utf-8')
        meta=base/f'{path.stem}.infinite.json'
        if meta.exists() and hashlib.sha256(text.encode()).hexdigest()!=json.loads(meta.read_text())['sha256']:
            continue
        at=text.casefold().find(query.casefold())
        if at<0:continue
        found.append(dict(id=int(path.stem),session=session,excerpt=text[max(0,at-80):at+240]))
        if len(found)>=limit:break
    return dict(matches=found,limit=limit,scope='selected archive session only')


def call(name,args,root):
    if name.startswith('infinite_context_memory_'):
        from ctxpress.methods import infinite_memory
        base=directory(root,args.get('session'))
        if name=='infinite_context_memory_read':
            return infinite_memory.read(base,args['key'],offset=args.get('offset',0),chars=args.get('chars',4000))
        if name=='infinite_context_memory_search':
            return infinite_memory.search(base,args['query'],limit=args.get('limit',5))
    if name=='infinite_context_search':
        return search(root,args['query'],session=args.get('session'),limit=args.get('limit',5))
    if name=='infinite_context_read':
        return read(root,args['id'],session=args.get('session'),offset=args.get('offset',0),chars=args.get('chars',4000))
    raise KeyError(name)
