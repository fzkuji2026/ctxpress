"""Replay recorded host items through LiveContext, optionally with paid DeepSeek summaries.

This is a rendering/capacity regression check, never a fresh task-quality benchmark.
Recorded future actions do not change in response to these summaries.
"""
import argparse
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from ctxpress.core import infinite_policy
from ctxpress.live.rewrite import Rewriter
from ctxpress.live.factory import frozen_factory
from ctxpress.methods.infinite_context import InfiniteContext


class ReplayLedger:
    """Stop new paid requests after an access/billing rejection in this replay.

    Already in-flight requests still finish and are recorded. Local skips are
    not represented as provider calls or as zero-cost provider responses.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.records = []
        self.terminal_status = None
        self.lock = threading.Lock()

    def __call__(self, row):
        with self.lock:
            self.records.append(row)
            with self.path.open('a') as f:
                f.write(json.dumps(row)+'\n')
            if row.get('status') in (401, 402, 403):
                self.terminal_status = row['status']

    def check_available(self):
        with self.lock:
            status = self.terminal_status
        if status is not None:
            raise RuntimeError(f'DeepSeek HTTP {status}; replay stopped pending provider access/billing recovery')


def requests(path):
    items, instructions, last = [], '', None
    for line in Path(path).read_text().splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        p = d.get('payload') or {}
        if d.get('type') == 'session_meta':
            instructions = (p.get('base_instructions') or {}).get('text', '')
        if d.get('type') == 'compacted':
            raise ValueError('regression trace must not contain native compaction')
        if d.get('type') == 'response_item':
            items.append(p)
        if p.get('type') == 'token_count' and (p.get('info') or {}).get('last_token_usage'):
            if len(items) != last:
                yield dict(model='gpt-6.1-sol', instructions=instructions, input=list(items))
                last = len(items)
    if items and len(items) != last:
        yield dict(model='gpt-6.1-sol', instructions=instructions, input=list(items))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--rollout', required=True)
    ap.add_argument('--policy', required=True)
    ap.add_argument('--directory', required=True)
    ap.add_argument('--agent-mode', choices=('off','control'), default='off')
    args = ap.parse_args()
    from ctxpress.harness.jobs.plan import fingerprint, file_sha256
    source_hash = fingerprint()
    policy = infinite_policy.load(args.policy)
    root = Path(args.directory).resolve()
    root.mkdir(mode=0o700)
    record = ReplayLedger(root/'auxiliary.jsonl')
    records = record.records
    mode = args.agent_mode
    factory = frozen_factory(InfiniteContext(policy=policy, agent_mode=mode,
        agent_model=policy['agent']['model'] if mode=='control' else None))
    writer = Rewriter(factory, store_dir=str(root/'archive'), store_prefix='/ctxpress-store',
                      summarizer=SimpleNamespace(record=record))
    changed = count = 0
    error = None
    try:
        for count, body in enumerate(requests(args.rollout), 1):
            record.check_available()
            result, info = writer.rewrite_body(body, 'regression')
            changed += bool(info['changed'])
            with (root/'decisions.jsonl').open('a') as f:
                f.write(json.dumps(info)+'\n')
            if count % 10 == 0 or info['changed']:
                print(json.dumps(dict(request=count, changed=bool(info['changed']),
                    tokens_before=info['tokens_before'], tokens_after=info['tokens_after'],
                    auxiliary_calls=len(records))), flush=True)
            record.check_available()
    except Exception as exc:
        error = type(exc).__name__+': '+str(exc)
    writer.close()
    if error is None and record.terminal_status is not None:
        error = f'DeepSeek HTTP {record.terminal_status}; replay incomplete'
    ctx = writer.sessions['regression'].ctx
    report = dict(scope='recorded-history host regression; not causal quality or task-cost evidence',
        code_sha256=source_hash, code_unchanged=source_hash==fingerprint(), rollout_sha256=file_sha256(args.rollout),
        policy_sha256=policy['sha256'], requests=count, changed_requests=changed,
        compacted_records=ctx.M['infinite_compacted_records'], auxiliary_calls=len(records),
        auxiliary_failed=sum(r.get('completed') is not True for r in records),
        error=error, completed=error is None, provider_terminal_status=record.terminal_status,
        last_decision=ctx.method.last_decision)
    (root/'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    if error:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
