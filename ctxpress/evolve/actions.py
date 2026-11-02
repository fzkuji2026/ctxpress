"""The agent's actions in a Codex rollout: every command and patch with its output, from Code Mode exec cells.

load(rollout) -> [dict(ln, kind, arg, status, out, reads, edit_files, edit_text)]
    kind is "exec" (a shell command; reads = [(path, (a, b) or None)] of simple read commands in it) or "patch"
    (apply_patch; edit_files = the files it touches, edit_text = its added and removed lines).
"""
from __future__ import annotations
import json, re, shlex

_CALL = re.compile(r'tools\.(exec_command|apply_patch|write_stdin)\s*\(')
_ESC = {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\', '"': '"', "'": "'", '`': '`', '0': '\0', 'b': '\b', 'f': '\f',
        'v': '\v', '\n': ''}
_PREFIXES = ('/app/', '/testbed/', '/workspace/', '/repo/', './')
_SEG = re.compile(r'\s*(?:&&|\|\||;|\n)\s*')
_READS = {'cat', 'sed', 'head', 'tail', 'nl', 'less', 'more', 'bat'}
_EDIT = re.compile(r"cat\s+>{1,2}|sed\s+-i|perl\s+-\w*i|\.write\(|write_text\(|writeFileSync|"
                   r"open\([^)]*['\"][wa]\+?['\"]|\btee\b|gofmt\s+-w|prettier\s+--write|\bpatch\s+-p")
_PATCH_FILE = re.compile(r'\*\*\* (?:Update|Add|Delete) File:\s*(\S+)|\*\*\* Move to:\s*(\S+)')


def _js(raw):
    out, i = [], 0
    while i < len(raw):
        c = raw[i]
        if c == '\\' and i + 1 < len(raw):
            n = raw[i + 1]
            if n in _ESC:
                out.append(_ESC[n]); i += 2; continue
            if n == 'u' and re.match(r'[0-9a-fA-F]{4}', raw[i + 2:i + 6]):
                out.append(chr(int(raw[i + 2:i + 6], 16))); i += 6; continue
            if n == 'x' and re.match(r'[0-9a-fA-F]{2}', raw[i + 2:i + 4]):
                out.append(chr(int(raw[i + 2:i + 4], 16))); i += 4; continue
            out.append(n); i += 2; continue
        out.append(c); i += 1
    return ''.join(out)


def _literal(s, i):
    while i < len(s) and s[i] in ' \t\n':
        i += 1
    if i >= len(s) or s[i] not in '"\'`':
        return None
    q, j = s[i], i + 1
    while j < len(s):
        if s[j] == '\\':
            j += 2; continue
        if s[j] == q:
            break
        j += 1
    return _js(s[i + 1:j])


def inner_calls(js):
    """[(kind, argument)] of the tool calls in a Code Mode script, in order."""
    out = []
    for m in _CALL.finditer(js):
        name, i = m.group(1), m.end()
        if name == 'apply_patch':
            lit = _literal(js, i)
            if lit is None:
                mm = re.match(r'\s*\{\s*"?(?:input|patch)"?\s*:\s*', js[i:])
                lit = _literal(js, i + mm.end()) if mm else None
            out.append(('patch', lit or ''))
        elif name == 'exec_command':
            mm = re.compile(r'"?cmd"?\s*:\s*').search(js, i, i + 80)
            lit = _literal(js, mm.end()) if mm else None
            out.append(('exec', lit if lit is not None else js[i:i + 300]))
        else:
            mm = re.compile(r'session_id"?\s*:\s*(\d+)').search(js, i, i + 120)
            out.append(('stdin', mm.group(1) if mm else ''))
    return out


def _chunks(output):
    texts = [x.get('text', '') for x in output if isinstance(x, dict)] if isinstance(output, list) else [str(output)]
    failed = bool(texts) and texts[0].startswith('Script failed')
    chunks = []
    for t in texts[1:]:
        t = t.strip()
        if t.startswith('{'):
            try:
                d = json.loads(t)
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get('value'), dict):
                d = d['value']
            elif isinstance(d, dict) and d.get('status') == 'rejected':
                d = {'exit_code': 'rejected', 'output': str(d.get('reason', ''))}
            chunks.append(d)
    return failed, chunks


def npath(p):
    p = p.strip().strip('"\'')
    for pre in _PREFIXES:
        if p.startswith(pre):
            p = p[len(pre):]
    return p


def same_file(p, q):
    return p == q or p.endswith('/' + q) or q.endswith('/' + p)


def reads(cmd):
    """[(path, (a, b) or None)] read by the simple read commands of a shell command (cat, sed -n, head, ...)."""
    out = []
    if '<<' in cmd:
        cmd = re.sub(r"<<-?\s*'?\"?(\w+)'?\"?.*?\n\1\b", ' ', cmd, flags=re.S)
    for seg in _SEG.split(cmd):
        seg = seg.split('|')[0].strip()
        try:
            tok = shlex.split(seg)
        except ValueError:
            tok = seg.split()
        if tok and tok[0] in ('sudo', 'command'):
            tok = tok[1:]
        if not tok or tok[0] not in _READS or '>' in seg:
            continue
        c, args = tok[0], tok[1:]
        if c == 'sed':
            if '-i' in args or not any(a.startswith('-n') for a in args):
                continue
            files, rng, script = [], None, False
            k = 0
            while k < len(args):
                a = args[k]
                if a == '-e' and k + 1 < len(args):
                    rng, script = rng or args[k + 1], True; k += 2; continue
                if a.startswith('-'):
                    k += 1; continue
                if not script:
                    rng, script = a, True
                else:
                    files.append(a)
                k += 1
            m = re.match(r"^(\d+)\s*,\s*(\d+)\s*p", rng or '')
            out += [(npath(f), (int(m.group(1)), int(m.group(2))) if m else None) for f in files]
        else:
            skip = False
            for a in args:
                if skip:
                    skip = False; continue
                if a in ('-n', '-c') and c in ('head', 'tail'):
                    skip = True; continue
                if a.startswith('-') or (c in ('head', 'tail') and a.isdigit()) or any(ch in a for ch in '*?$`') or a == '-':
                    continue
                out.append((npath(a), None))
    return out


def load(rollout):
    calls, acts, live = {}, [], {}
    with open(rollout, errors='replace') as f:
        lines = f.read().splitlines()
    for ln, line in enumerate(lines, 1):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        p = o.get('payload') if isinstance(o.get('payload'), dict) else {}
        if p.get('type') == 'custom_tool_call' and p.get('name') == 'exec':
            calls[p.get('call_id')] = (ln, p.get('input') or '')
        elif p.get('type') == 'custom_tool_call_output':
            c = calls.pop(p.get('call_id'), None)
            if not c:
                continue
            ln0, js = c
            failed, chunks = _chunks(p.get('output'))
            ci = 0
            for kind, arg in inner_calls(js):
                ch = chunks[ci] if ci < len(chunks) else None
                ci += 1
                if kind == 'stdin':
                    tgt = live.get(arg)
                    if tgt is not None and isinstance(ch, dict):
                        tgt['out'] += ch.get('output', '') or ''
                        if 'exit_code' in ch:
                            tgt['status'] = ch['exit_code']; live.pop(arg, None)
                    continue
                status, out, pending = None, '', None
                if isinstance(ch, dict):
                    if 'exit_code' in ch:
                        status, out = ch['exit_code'], ch.get('output', '') or ''
                    elif kind == 'patch' and ch == {}:
                        status = 0
                    elif 'session_id' in ch:
                        out, pending = ch.get('output', '') or '', str(ch['session_id'])
                elif failed:
                    status = 'script_error'
                a = dict(ln=ln0, kind=kind, arg=arg, status=status, out=out)
                if kind == 'exec':
                    a['reads'] = reads(arg or '')
                    a['edit_files'] = set()
                    a['edit_text'] = (arg or '') if _EDIT.search(arg or '') else ''
                else:
                    a['reads'] = []
                    a['edit_files'] = {npath(x or y) for x, y in _PATCH_FILE.findall(arg or '')}
                    a['edit_text'] = '\n'.join(l[1:] for l in (arg or '').splitlines() if l[:1] in '+-' and l[1:].strip())
                acts.append(a)
                if pending:
                    live[pending] = a
                if ch is None and failed:
                    break
    return acts
