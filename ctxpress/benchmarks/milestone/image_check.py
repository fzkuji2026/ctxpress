"""Check the author's frozen closure recipe against declared local images."""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path


def verify(task, source, images, directory):
    from harness.e2e import evaluator
    from ctxpress.benchmarks.milestone import images as milestone_images, native as milestone_native
    source = Path(source).resolve()
    if Path(evaluator.__file__).resolve() != source / 'harness/e2e/evaluator.py':
        raise ValueError('native image check imported an undeclared official evaluator')
    # Use the same author binders and selected inputs as continuous dispatch.
    # The policy is read from the frozen trial binding, never from live YAML.
    actual = milestone_native.prepare(task, source, directory)
    binding = actual['runtime_policy_binding']
    prepared = milestone_images.PreparedImages(evaluator, task['id'], images['agent'], images['grading'])
    results = {}
    for mid in task['evaluation']['active_milestones']:
        result = prepared.overlay(repo_name=task['id'], milestone_id=mid,
            milestone_image=prepared.base[mid], quarantine_config=binding.policy,
            expected_closure_image_id=images['agent']['id'])
        results[mid] = dict(reference=images['grading'][mid].get('reference'),
            id=result[0], parent_id='sha256:' + result[1],
            closure_id='sha256:' + result[2] if result[2] else None)
    return dict(schema='ctxpress.eval.milestone_images', version=1, task_id=task['id'],
        runtime_policy_sha256=binding.sha256, runtime_policy_mode=binding.mode,
        grading=results, author_closure_verified=True, model_calls=0,
        containers_started=0, real_run_verified=False)


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    # Import only after enabling the calling frozen ctxpress package. The
    # existing isolated loader verifies runtime, task remapping and author trees.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from ctxpress.harness.jobs import plan as eval_plan, resources as task_resources
    from ctxpress.core import artifacts as artifact_io
    from ctxpress.benchmarks.milestone import worker as milestone_worker, version as milestone_version
    source = milestone_worker.load(request)
    lock, _ = task_resources.read(request['resources'], 'swe-milestone', [request['original_task']])
    folder = Path(request['folder'])
    with tempfile.TemporaryDirectory(prefix='native-image-check-', dir=folder) as temporary:
        with milestone_version.pinned_environment(lock['release']):
            result = verify(request['task'], source, lock['tasks'][request['task']['id']]['images'],
                            Path(temporary) / 'preparation')
    artifact_io.atomic_json(folder / 'native-images.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
