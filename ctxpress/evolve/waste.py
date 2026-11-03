"""Per run: score, bill, and how much of the bill pays for read content the agent never used afterwards.

    report(attempt_dir) -> dict      one Harbor attempt (the directory holding ctxp-hb-*/)

Bill (gpt-6.1-sol prices in PRICES): total = every request's uncached input, cached input and output. Fixed = the
system prompt, tool declarations and the task message of each main request (their sizes from the first request's
composition, billed cached when the request's cached tokens cover them) plus all output. Manageable = total - fixed.

Read waste. The manageable bill of tool outputs is split over the outputs: entry (first time, uncached) by tokens,
carry and rebuild (later requests) by tokens x the requests the output stays (until the request whose tool-output
size falls by DROP or more, i.e. infinite-context shrank old outputs). For every read output (cat / sed -n / head /
nl of a file, or a read-tool command of a policy) a line is
    edit-used   within NEAR lines of a line that appears (normalised, >= 8 characters) in a later edit of that file
    mentioned   holds an identifier (>= 4 characters, not a common word) that later agent-written text contains
                (later commands and patches, code-looking spans of later assistant messages)
and is unused when it is neither. unused_share = the bill of unused read lines / manageable bill.
"""
from __future__ import annotations
import bisect, collections, glob, json, os, re
from ctxpress.core import tokens
from ctxpress.evolve import actions

PRICES = dict(uncached=2.0e-6, cached=0.1e-6, output=10e-6)
KINDS = ['instructions', 'other', 'other_extra', 'user', 'user_extra', 'assistant', 'tool_call', 'tool_output', 'reasoning']
HIST = ['other_extra', 'user_extra', 'assistant', 'tool_call', 'tool_output', 'reasoning']
NEAR = 10
DROP = 0.8
TOOL_CMD = re.compile(r'python3?\s+-m\s+ctxpress\.(?:tools|methods\.readless_policies)\.[\w.]+')
_NUM = re.compile(r'^\s*\d+[\t:| ]\s?')
_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]{3,}')
_CODESPAN = re.compile(r'`([^`]+)`|\b\w+(?:_\w+)+\b|\b[a-z]+[A-Z]\w*\b|\b\w+\.\w+\b')
_PATHLIKE = re.compile(r'[\w./-]*[\w-]+\.[A-Za-z0-9]{1,6}\b')
_STOP = set('''self this that from import return class def function const true false none null else elif while with
async await raise assert lambda yield print string value type list dict tuple bool float pass break continue except
finally try export default public private static final struct interface package module object new let var func range
len make error errors nil args kwargs param params result results data name names test tests file files path line lines
then also into only when what will have been your their there here should would could just some more each such same
than them they other first last next before after using used uses echo grep head tail python python3 bash cat sed cmd
exec tools output input exit code'''.split())


def _norm(line):
    return re.sub(r'\s+', ' ', line).strip()


def _idents(text):
    return {w for w in _IDENT.findall(text or '') if w.lower() not in _STOP}


def _usage_rows(path):
    with open(path, errors='replace') as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get('usage'):
                yield d


def bill(requests, skip=0):
    """total, fixed, manageable and the tool-output split (entry, later, per-request tool-output size). skip: leave
    out the first `skip` main requests (and anything billed before the next one), e.g. a fork's cache warm-up."""
    U, C, O = PRICES['uncached'], PRICES['cached'], PRICES['output']
    total = fixed = aux = 0.0
    rows, base, main = [], None, 0
    for d in _usage_rows(requests):
        u = d['usage']
        I, Ca, Out = u.get('input_tokens') or 0, u.get('cached_tokens') or 0, u.get('output_tokens') or 0
        is_main = 'request' in d and d.get('composition')
        if is_main:
            base = base or d['composition']
            main += 1
            if d.get('status', 200) == 200:
                rows.append(d)
        if main <= skip:
            continue
        total += (I - Ca) * U + Ca * C + Out * O
        if is_main:
            f = min(sum(base.get(k, 0) for k in ('instructions', 'other', 'user')), I)
            fixed += f * (C if Ca >= f else U)
        elif 'request' not in d:
            aux += (I - Ca) * U + Ca * C
        fixed += Out * O
    entry = later = 0.0
    prev, sizes = None, []
    base = base or {}
    bo, bu = base.get('other', 0), base.get('user', 0)
    for i, d in enumerate(rows):
        u = d['usage']
        I, Ca = u.get('input_tokens') or 0, u.get('cached_tokens') or 0
        Un = I - Ca
        f = min(I, sum(base.get(k, 0) for k in ('instructions', 'other', 'user')))
        if Ca < f:
            Un = max(0, Un - f)
        c = dict(d['composition'])
        oo, uu = c.get('other', 0), c.get('user', 0)
        c['other'], c['other_extra'] = min(oo, bo), max(0, oo - bo)
        c['user'], c['user_extra'] = min(uu, bu), max(0, uu - bu)
        s = I / (sum(c.get(k, 0) for k in KINDS) or 1)
        size = {k: c.get(k, 0) * s for k in KINDS}
        Ca = min(Ca, sum(size.values()) - Un)
        new = {k: max(0.0, size[k] - (prev[k] if prev else 0.0)) for k in KINDS}
        nsum = sum(new.values())
        if nsum >= Un:
            un = {k: Un * new[k] / nsum if nsum else 0 for k in KINDS}
        else:
            rest, hs = Un - nsum, sum(size[k] - new[k] for k in HIST) or 1
            un = {k: new[k] + (rest * (size[k] - new[k]) / hs if k in HIST else 0) for k in KINDS}
        ca = {k: max(0.0, size[k] - un[k]) for k in KINDS}
        cs = sum(ca.values()) or 1
        if i >= skip:
            entry += min(un['tool_output'], new['tool_output']) * U
            later += max(0, un['tool_output'] - new['tool_output']) * U + Ca * ca['tool_output'] / cs * C
        sizes.append(size['tool_output'])
        prev = size
    return dict(total=total, fixed=fixed, manage=total - fixed, entry=entry, later=later, sizes=sizes,
                requests=len(rows))


def _assistant_texts(rollout):
    out = []
    with open(rollout, errors='replace') as f:
        for ln, line in enumerate(f, 1):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            p = o.get('payload') if isinstance(o.get('payload'), dict) else {}
            if p.get('type') == 'message' and p.get('role') == 'assistant':
                t = ' '.join(x.get('text', '') for x in p.get('content') or [] if isinstance(x, dict))
                out.append((ln, ' '.join(m.group(0) for m in _CODESPAN.finditer(t))))
            elif p.get('type') in ('custom_tool_call', 'function_call'):
                out.append((ln, str(p.get('input') or p.get('arguments') or '')))
    return out


def classify(a, edited):
    if a['kind'] != 'exec' or a['edit_text']:
        return 'edit'
    cmd = a['arg'] or ''
    if TOOL_CMD.search(cmd):
        return 'read tool'
    if a['reads']:
        files = [p for p, _ in a['reads']]
        if all(f.startswith('/tmp/') or f.endswith('.log') for f in files):
            return 'test/log'
        return 'read, edited file' if any(actions.same_file(f, e) for f in files for e in edited) else 'read, never-edited file'
    if re.search(r'\b(go (test|build|vet)|npm|pytest|vitest|jest|cargo|make\b|yarn|pnpm|tox|unittest)', cmd):
        return 'test/log'
    if re.match(r'^\s*(cd \S+ && )?(grep|rg|find|ls|tree|git (log|grep|show|diff|status)|wc)\b', cmd):
        return 'search/listing'
    return 'other'


def _files(a):
    files = [p for p, _ in a['reads']]
    if TOOL_CMD.search(a['arg'] or ''):
        files += [actions.npath(p) for p in _PATHLIKE.findall(TOOL_CMD.sub(' ', a['arg'] or ''))]
    return files


def report(attempt, examples=6, skip=0, rows=False):
    """Score, bill and read waste of one attempt; None if its records are incomplete. skip: see bill(). rows=True
    also returns every output's line (kind, command, tokens, unused and edit-used tokens, entry request, life,
    entry and carry dollars), for lifetime-cost analyses and the proposer's evidence."""
    rq = sorted(glob.glob(attempt + '/ctxp-hb-*/agent/ctxpress-requests.jsonl'))
    rolls = sorted(glob.glob(attempt + '/ctxp-hb-*/agent/sessions/**/rollout-*.jsonl', recursive=True))
    rew = glob.glob(attempt + '/ctxp-hb-*/verifier/reward.txt')
    if not rq or not rolls:
        return None
    b = bill(rq[0], skip)
    sizes, nreq = b['sizes'], max(1, len(b['sizes']))
    acts = actions.load(rolls[0])
    lns = sorted({a['ln'] for a in acts})
    enter = {ln: min(nreq - 1, i + 1) for i, ln in enumerate(lns)}
    drops = [r for r in range(1, len(sizes)) if sizes[r] < DROP * sizes[r - 1]]
    edits = []
    for k, a in enumerate(acts):
        if a['kind'] == 'patch' and a['edit_files']:
            edits.append((k, a['edit_files'], '\n'.join(_norm(x) for x in a['edit_text'].splitlines())))
        elif a['kind'] == 'exec' and a['edit_text']:
            files = {actions.npath(p) for p in _PATHLIKE.findall(a['edit_text'])}
            edits.append((k, files, '\n'.join(_norm(x) for x in a['edit_text'].replace('\\n', '\n').splitlines())))
    edited = set().union(*[f for _, f, _ in edits]) if edits else set()
    texts = sorted(_assistant_texts(rolls[0]))
    suffix, acc = [set()] * (len(texts) + 1), set()
    for j in range(len(texts) - 1, -1, -1):
        acc = acc | _idents(texts[j][1]); suffix[j] = acc
    text_lns = [t[0] for t in texts]
    out_rows = []
    for k, a in enumerate(acts):
        out = a.get('out') or ''
        if not out:
            continue
        kind = classify(a, edited)
        e = enter[a['ln']]
        life = max(0, next((r for r in drops if r > e), nreq) - e)
        if e < skip:                     # entered during the skipped requests (a fork's restored history)
            continue
        lines = out.splitlines()
        ltok = [tokens.count(l) + 1 for l in lines]
        unused = 0
        if kind.startswith('read'):
            files = _files(a)
            later = '\n'.join(t for i, fs, t in edits if i > k and any(actions.same_file(f, p) for f in fs for p in files))
            used = [False] * len(lines)
            if later:
                for j, l in enumerate(lines):
                    t = _norm(_NUM.sub('', l))
                    if len(t) >= 8 and len(re.sub(r'[\s{}()\[\];,]', '', t)) >= 4 and t in later:
                        for q in range(max(0, j - NEAR), min(len(lines), j + NEAR + 1)):
                            used[q] = True
            ids = suffix[bisect.bisect_right(text_lns, a['ln'])]
            unused = sum(t for t, u, l in zip(ltok, used, lines) if not u and not (_idents(l) & ids))
            edit_used = sum(t for t, u in zip(ltok, used) if u)
        else:
            edit_used = 0
        out_rows.append(dict(kind=kind, cmd=(a['arg'] or '')[:300], tok=sum(ltok), unused=unused, edit_used=edit_used,
                             life=life, enter=e, k=k, out=out if rows else None))
    rows_, rows = rows, out_rows
    we = sum(r['tok'] for r in rows) or 1
    wl = sum(r['tok'] * r['life'] for r in rows) or 1
    for r in rows:
        r['usd_entry'] = b['entry'] * r['tok'] / we
        r['usd_carry'] = b['later'] * r['tok'] * r['life'] / wl
        r['usd'] = r['usd_entry'] + r['usd_carry']
        r['usd_unused'] = r['usd'] * r['unused'] / r['tok'] if r['tok'] else 0.0
    by = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        g = by[r['kind']]
        g['n'] += 1; g['tokens'] += r['tok']; g['unused_tokens'] += r['unused']; g['usd'] += r['usd']; g['usd_unused'] += r['usd_unused']
        g['usd_carry'] += r['usd_carry']
    man = b['manage'] or 1e-9
    task = None
    req = glob.glob(attempt + '/harbor-request.json')
    if req:
        with open(req[0]) as f:
            task = json.load(f).get('task_id')
    worst = sorted((r for r in rows if r['kind'].startswith('read')), key=lambda r: -r['usd_unused'])[:examples]
    reads = [k for k in by if k.startswith('read')]
    return dict(task=task, attempt=attempt, reward=float(open(rew[0]).read().strip()) if rew else None,
                total=b['total'], manage=b['manage'], requests=b['requests'],
                read_tokens=sum(by[k]['tokens'] for k in reads),
                tool_tokens=sum(g['tokens'] for g in by.values()),
                unused_share=sum(by[k]['usd_unused'] for k in reads) / man,
                read_share=sum(by[k]['usd'] for k in reads) / man,
                tool_calls=sum(1 for r in rows if r['kind'] == 'read tool'), calls=len(acts),
                by_kind={k: {x: round(v, 4) for x, v in g.items()} for k, g in by.items()},
                worst=[dict(cmd=r['cmd'], tokens=r['tok'], unused_tokens=r['unused'], usd_unused=round(r['usd_unused'], 4),
                            enter=r['enter'], life=r['life'], usd_carry=round(r['usd_carry'], 4))
                       for r in worst],
                **(dict(rows=rows, nreq=nreq) if rows_ else {}))


def attempts(directory, method_prefix='m000'):
    """The last attempt of every job of a run directory (jobs/<method>-t*-r*/attempt-N)."""
    out = []
    for jd in sorted(glob.glob(os.path.join(directory, 'jobs', method_prefix + '-t*-r*'))):
        atts = sorted(glob.glob(jd + '/attempt-*'), key=lambda p: int(p.rsplit('-', 1)[1]))
        if atts:
            out.append(atts[-1])
    return out
