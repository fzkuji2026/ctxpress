"""Isolated child for official snapshot packaging and evaluation.

Launch with the captured interpreter and -I -S -B; no ambient site packages,
PYTHONPATH or live grading checkout participates in imports.
"""
from __future__ import annotations
import argparse, json, sys, tarfile
from pathlib import Path

# -I deliberately removes the script directory; load the owning frozen framework.
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from ctxpress.harness import eval_grading, eval_plan


def package(roots,record,snapshot,tag,commit,destination):
    import yaml
    from harness.utils.src_filter import SrcFileFilter
    config = yaml.safe_load((Path(roots['trials'])/record['repo_config']).read_text(encoding='utf-8'))
    template = json.loads((Path(roots['trials'])/record['template']).read_text(encoding='utf-8'))
    source_filter = SrcFileFilter(src_dirs=config['repo_src_dirs'],test_dirs=config.get('test_dirs') or [],
        exclude_patterns=config.get('exclude') or [],generated_patterns=config.get('generated_patterns') or [],
        modifiable_test_patterns=config.get('modifiable_test_patterns') or [])
    source_filter.modifiable_test_patterns = template['capture_filter'].get('modifiable_test_patterns') or []
    if hasattr(source_filter,'_compile'):
        source_filter._compile()
    manifests = ('go.mod','go.sum','go.work','go.work.sum')
    destination = Path(destination); destination.mkdir(parents=True,exist_ok=True)
    output = destination/'source_snapshot.tar'
    names = set()
    import fnmatch
    with tarfile.open(snapshot) as source,tarfile.open(output,'w') as target:
        for member in source.getmembers():
            name = member.name.lstrip('./')
            keep = member.isdir() or name in manifests or (member.isfile() and source_filter.should_include_in_snapshot(name)) or (
                member.isfile() and any(fnmatch.fnmatch(name,pattern) for pattern in source_filter.modifiable_test_patterns))
            if keep:
                target.addfile(member,source.extractfile(member) if member.isfile() else None)
                if member.isfile():
                    names.add(name)
    upserts = sorted(name for name in manifests if name in names)
    metadata = dict(template)
    metadata.update(tag=tag,ok=True,missing_count=0,missing_sample=[],expected_count=len(names),
        uncommitted_lost_count=0,uncommitted_lost_sample=[],uncommitted_outside_count=0,uncommitted_outside_sample=[],
        committed_outside_count=0,committed_outside_sample=[],agent_tag_commit=commit,snapshot_sha256=eval_plan.file_sha256(output),
        manifest_overlay={**template['manifest_overlay'],'upserts':upserts,'deletes':[]},
        go_manifest_projection=dict(schema_version=1,present=upserts),build_manifests=upserts)
    eval_plan.atomic_json(destination/'source_snapshot.integrity.json',metadata)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--manifest',required=True); ap.add_argument('--root',required=True)
    ap.add_argument('--n',type=int,required=True); ap.add_argument('--j',type=int,required=True)
    sub = ap.add_subparsers(dest='operation',required=True)
    p = sub.add_parser('package'); p.add_argument('--snapshot',required=True); p.add_argument('--tag',required=True)
    p.add_argument('--commit',required=True); p.add_argument('--package',required=True)
    p = sub.add_parser('grade'); p.add_argument('--snapshot',required=True); p.add_argument('--output',required=True)
    sub.add_parser('check',help='import the official entrypoint without running tests or starting a container')
    args = ap.parse_args(argv)
    lock = eval_grading.load(args.manifest,[(args.n,args.j)])
    if sys.version != lock['runtime']['version'] or sys.platform != lock['runtime']['platform']:
        raise ValueError('grading interpreter version/platform differs from the declared runtime')
    roots,record = eval_grading.runtime(lock,args.root,args.n,args.j)
    # These are verified copies; -S prevents a live installation winning resolution.
    sys.path[:0] = [roots['code'],str(Path(roots['yaml']).parent)]
    if args.operation == 'package':
        package(roots,record,args.snapshot,args.tag,args.commit,args.package)
        return
    from harness.e2e import image_version
    def resolve_image(reference):
        try:
            return lock['images'][reference]['id']
        except KeyError:
            raise ValueError('official evaluator requested an undeclared image: '+reference) from None
    image_version.resolve_image = resolve_image
    from harness.e2e import evaluator
    # A copied checkout inside another Git repository must not report its parent's HEAD.
    evaluator._harness_revision = lambda: 'ctxpress-frozen:'+lock['sha256']
    if args.operation == 'check':
        if not callable(getattr(evaluator,'main',None)):
            raise ValueError('official grading entrypoint has no callable main')
        print(json.dumps(dict(grading_manifest_sha256=lock['sha256'],entrypoint_imported=True,experiments_started=0)))
        return
    repo_config = Path(roots['trials'])/record['repo_config']
    runtime_policy = Path(roots['trials'])/record['runtime_policy']
    sys.argv = ['official-frozen-evaluator','--workspace-root',roots['data'],'--milestone-id',record['milestone'],
        '--patch-file',args.snapshot,'--baseline-classification',str(Path(roots['data'])/record['classification']),
        '--repo-config',str(repo_config),'--repo-config-sha256',eval_plan.file_sha256(repo_config),
        '--runtime-policy',str(runtime_policy),'--runtime-policy-sha256',eval_plan.file_sha256(runtime_policy),
        '--runtime-policy-mode','protected','--output',args.output]
    evaluator.main()


if __name__ == '__main__':
    main()
