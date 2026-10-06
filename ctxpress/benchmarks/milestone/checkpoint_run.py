"""Real Codex continuation and official grading on local SWE-Milestone snapshots.

Local benchmark helpers are loaded on execution only. Docker images must already
exist; this harness never pulls images. Historical C03 defaults remain explicit.
"""
from __future__ import annotations
import argparse, importlib, json, os, re, shlex, shutil, subprocess, sys, tempfile, threading, time, uuid
from pathlib import Path
from types import SimpleNamespace
from ctxpress.live.telemetry import summary
from ctxpress.live.proxy import serve
from ctxpress.live.factory import frozen_factory
from ctxpress.methods import build as build_method
from ctxpress.harness.jobs.plan import atomic_json
from ctxpress.harness.jobs import environment as eval_environment
from ctxpress.benchmarks.milestone import checkpoint_grading as eval_grading
from ctxpress.benchmarks.milestone.adapter import POINTS, boundaries as read_boundaries

DATA = Path(os.environ.get("CTXPRESS_DATA", Path(__file__).resolve().parents[4] / "data"))
SCRIPTS = DATA / "tb4-jobs/recompaction-20261003/scripts"
UPSTREAM = "https://chatgpt.com/backend-api/codex"


def boundaries(scripts=None):
    return read_boundaries(scripts or SCRIPTS)


def environment(scripts=None, bindir=None, e2e=None):
    """Import trusted benchmark helpers without reading the credential contents."""
    scripts = Path(scripts or SCRIPTS).resolve()
    sys.path.insert(0, str(scripts))
    try:
        probe = importlib.import_module("miss_probe_docker")
        c03 = importlib.import_module("c03_run")
        multi = importlib.import_module("multi_probe")
    finally:
        sys.path.pop(0)
    if any(Path(module.__file__).resolve().parent != scripts for module in (probe, c03, multi)):
        raise ValueError("another benchmark environment is loaded; use a separate worker process")
    return SimpleNamespace(scripts=scripts, auth=probe.AUTH, host_proxy=probe.host_proxy, build_kept=probe.build,
        bindir=str(bindir or c03.BINDIR), image=c03.IMAGE, agent_env=c03.AGENT_ENV, off_features=c03.OFF_FEATURES,
        src_dirs=c03.SRC_DIRS, root_files=c03.ROOT_FILES, workspace=multi.WS, last_queue=multi.last_queue,
        e2e=str(e2e or Path(multi.WS).parent.parent), points=boundaries(scripts))


def docker_gateway():
    result = subprocess.run(["docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"],
                            capture_output=True, text=True, check=True)
    if not result.stdout.strip():
        raise RuntimeError("Docker bridge has no gateway")
    return result.stdout.strip()


def _installed_log(home, destination):
    files = sorted((home / "ctxpress/runs").glob("*/requests.jsonl"))
    legacy = home / "ctxpress/requests.jsonl"
    if legacy.exists():
        files.insert(0, legacy)
    with open(destination, "w", encoding="utf-8") as output:
        for path in files:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                output.write(json.dumps(row, ensure_ascii=False) + "\n")


def _cleanup(env, home, workspace, store=None):
    """Verify auth removal in Docker before removing its home from the host."""
    mounts = ["-v", f"{home}:/h", "-v", f"{workspace}:/w"] + (["-v", f"{store}:/s"] if store else [])
    cleaned = subprocess.run(["docker", "run", "--pull", "never", "--rm", "--network", "none", *mounts,
        "--entrypoint", "", env.image, "sh", "-c", "rm -rf /h/* /h/.[!.]* /w/* " + ("/s/*" if store else "")], capture_output=True)
    if cleaned.returncode or (home / "auth.json").exists():
        raise RuntimeError("container credential cleanup failed; temporary directories preserved")
    for path in (home, workspace, store):
        if path:
            shutil.rmtree(path, ignore_errors=True)


def recover(manifest, run_label):
    """An explicit retry cleans only this job's staged resources after its worker died."""
    manifest = Path(manifest)
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    if metadata.get("run_label") != run_label:
        raise ValueError("resource manifest belongs to another job")
    name = metadata.get("container", "")
    if not re.fullmatch(r"ctxp-[0-9a-f]{10}", name):
        raise ValueError("invalid managed container name")
    if metadata.get("cleaned"):
        subprocess.run(["docker", "rmi", "-f", name + "-end"], capture_output=True)
        return
    paths = {}
    for key, prefix in (("home", "cxh-"), ("workspace", "ctxws-"), ("store", "ctxstore-")):
        value = metadata.get(key)
        if value is None:
            if key != "store":
                raise ValueError("resource manifest is missing a required temporary directory")
            paths[key] = None
            continue
        path = Path(value).resolve()
        if path.parent != Path(tempfile.gettempdir()).resolve() or not path.name.startswith(prefix):
            raise ValueError("resource path is outside this harness's temporary directories")
        paths[key] = path
    found = subprocess.run(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"], capture_output=True, text=True, check=True)
    if found.stdout.strip():
        result = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Labels}}", name], capture_output=True, text=True, check=True)
        labels = json.loads(result.stdout)
        if labels.get("ctxpress.managed") != "true" or labels.get("ctxpress.run") != run_label:
            raise ValueError("container belongs to another experiment")
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)
    if paths["home"].exists():
        _installed_log(paths["home"], manifest.parent / "recovered-requests.jsonl")
    if any(path and path.exists() for path in paths.values()):
        _cleanup(SimpleNamespace(image=metadata["image"]), paths["home"], paths["workspace"], paths["store"])
    subprocess.run(["docker", "rmi", "-f", name + "-end"], capture_output=True)
    metadata["cleaned"] = True
    atomic_json(manifest, metadata)


def _container_entry(entry, home):
    """Mount frozen cost statistics even when they live outside the project checkout."""
    import copy, hashlib
    entry = copy.deepcopy(entry)
    def visit(value):
        if not isinstance(value, dict):
            return
        args = value.get("args") or {}
        key = {"CostModel": "profile", "AutoCostModel": "policy"}.get(value.get("class"))
        if key and args.get(key) and not isinstance(args[key], dict):
            raw = Path(args[key]).expanduser().read_bytes()
            name = hashlib.sha256(raw).hexdigest() + ".json"
            directory = home / "calibration"
            directory.mkdir(exist_ok=True)
            (directory / name).write_bytes(raw)
            args[key] = "/cxhome/calibration/" + name
        visit(args.get("inner"))
        for method in args.get("methods", []):
            visit(method)
    visit(entry)
    return entry


def _remove_container(name):
    found = subprocess.run(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"],
                           check=True, capture_output=True, text=True)
    if found.stdout.strip():
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)


def _stage_rollout(path, rows):
    """Import a flat exported history without claiming its missing paginated store."""
    if not rows or rows[0].get("type") != "session_meta":
        raise ValueError("recorded history must begin with session metadata")
    metadata = rows[0]["payload"]
    mode = metadata.get("history_mode")
    if mode not in (None, "legacy", "paginated"):
        raise ValueError("unsupported recorded history storage mode")
    first = dict(rows[0], payload=dict(metadata, history_mode="legacy"))
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in [first, *rows[1:]]), encoding="utf-8")
    return dict(source_history_mode=mode, staged_history_mode="legacy", rows=len(rows),
                metadata_only=True)


def run(n, j, entry, max_calls=20, timeout=1800, prompt="Continue with the task.", log=None, compact_limit=None, memory=False,
        submit=False, outdir=None, installed=False, cli_base_url=False, scripts=None, bindir=None, e2e=None,
        model="gpt-5.6-sol", reasoning="low", upstream=UPSTREAM, via=None, run_label=None, input_paths=None,
        snapshot=None, workspace=None, grading=None, grading_root=None):
    """Run one approved experiment. Installed mode uses the normal `codex` shell command."""
    if max_calls < 1 or timeout <= 0 or not model:
        raise ValueError("positive call/time limits and an explicit model are required")
    method = build_method(entry)
    method.validate_live()
    env = environment(scripts, bindir, e2e)
    env.grading, env.grading_root = grading, grading_root
    if grading:
        if grading_root is None:
            raise ValueError('grading manifests require the prepared evaluation input copies')
        eval_grading.preflight(grading,grading_root,n,j)
    elif grading_root is not None:
        raise ValueError('grading root requires a manifest')
    if snapshot:
        lock = eval_environment.load(snapshot, [(n,j)])
        env.workspace = Path(workspace or lock['workspace']['root'])
        env.image, boundary_image = eval_environment.runtime(lock, env.workspace, n, j)
        environment_evidence = dict(manifest_sha256=lock['sha256'], workspace_frozen=True,
                                    base_image_id=env.image, boundary_image_id=boundary_image)
    else:
        if workspace is not None:
            raise ValueError('workspace overrides require an environment manifest')
        env.image = eval_environment.image(env.image)['id']
        boundary_image = eval_environment.image(f'hsnap-{n}:{j}')['id']
        environment_evidence = dict(manifest_sha256=None, workspace_frozen=False,
                                    base_image_id=env.image, boundary_image_id=boundary_image)
    binary = Path(env.bindir) / "codex"
    if not binary.is_file():
        raise FileNotFoundError("selected Codex binary does not exist")
    from ctxpress.harness.runtime import codex_binary
    codex_binary.preflight(binary.parent)
    v = dict(env.points[(n, j)])
    rollout_name = Path(v['src']).name
    if input_paths is not None:
        source = Path(v['src']).expanduser()
        if source.is_absolute():
            source = source.resolve()
        else:
            catalog = (Path(env.scripts) / 'valid_points.json').resolve()
            original = next(Path(name) for name, target in input_paths.items() if Path(target).resolve() == catalog)
            source = (original.parent / source).resolve()
        v['src'] = input_paths[str(source)]
    milestone, trial, _ = POINTS.get((n, j), (None, None, None))
    if submit and not milestone:
        raise ValueError("this boundary has no official grading mapping")
    version = subprocess.run(["docker", "run", "--pull", "never", "--rm", "--network", "none", "-v", f"{env.bindir}:/cxbin:ro",
                              "--entrypoint", "", env.image, "/cxbin/codex", "--version"], check=True, capture_output=True, text=True).stdout.strip()
    kept, _ = env.build_kept(v["src"], v["i"], j, "keep")
    sid = kept[0]["payload"]["id"]
    gw, px = docker_gateway(), via if via is not None else env.host_proxy()
    name = "ctxp-" + uuid.uuid4().hex[:10]
    outdir = Path(outdir or "/tmp/ctxpress-runs").resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    log = str(Path(log or outdir / f"{name}.jsonl").resolve())
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    home, ws = Path(tempfile.mkdtemp(prefix="cxh-")), Path(tempfile.mkdtemp(prefix="ctxws-"))
    store = Path(tempfile.mkdtemp(prefix="ctxstore-")) if memory or method.memory else None
    srv, started, graded_snapshot = None, False, False
    stop, new, logs = "startup_failed", [], ""
    t0 = time.monotonic()
    resources = dict(container=name, image=env.image, home=str(home), workspace=str(ws), store=str(store) if store else None,
                     run_label=run_label, cleaned=False)
    resource_path = outdir / f"resources-{name}.json"
    try:
        atomic_json(resource_path, resources)
        sd = home / "sessions/2026/09/22"
        sd.mkdir(parents=True)
        rollout = sd / rollout_name
        session_restore = _stage_rollout(rollout, kept)
        shutil.copytree(env.workspace, ws / "e2e_workspace")
        if snapshot:
            eval_environment.verify_workspace(lock, ws / 'e2e_workspace')
        queue = env.last_queue(kept)
        if queue:
            (ws / "e2e_workspace/TASK_QUEUE.md").write_text(queue, encoding="utf-8")
        port = None
        if not installed:
            srv, _ = serve(frozen_factory(method), 0, upstream, via=px, log=log,
                store_dir=str(store) if store else None, store_prefix="/ctx_store" if store else None, retrieve_tool=False)
            port = srv.server_port
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        effective_limit = method.codex_config.get("model_auto_compact_token_limit", compact_limit)
        from ctxpress.core import toml
        native_config = dict(method.codex_config)
        if effective_limit is not None:
            native_config['model_auto_compact_token_limit'] = effective_limit
        limit = toml.dumps(native_config)
        (home / "config.toml").write_text(f'model = {json.dumps(model)}\nmodel_reasoning_effort = {json.dumps(reasoning)}\nweb_search = "disabled"\n' + limit +
            ("" if installed or cli_base_url else f'openai_base_url = "http://{gw}:{port}"\n') +
            '[shell_environment_policy]\ninherit = "all"\nexclude = ["HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY"]\n' +
            '[features]\nenable_request_compression = false\n' + "".join(f"{f} = false\n" for f in env.off_features), encoding="utf-8")
        shutil.copy(env.auth, home / "auth.json")
        agent_env = env.agent_env + ["-e", "CODEX_HOME=/cxhome", "-e", "CODEX_WORKING_VIEW=0", "-e", f"HTTPS_PROXY={px}",
            "-e", f"HTTP_PROXY={px}", "-e", f"NO_PROXY=localhost,127.0.0.1,{gw}"]
        volumes = ["-v", f"{home}:/cxhome", "-v", f"{env.bindir}:/cxbin:ro", "-v", f"{ws}/e2e_workspace:/e2e_workspace"]
        if installed:
            agent_env += ["-e", "PYTHONPATH=/ctxpress", "-e", "CTXPRESS_HOME=/cxhome/ctxpress"]
            volumes += ["-v", f"{Path(__file__).resolve().parents[3]}:/ctxpress:ro"]
        if store:
            volumes += ["-v", f"{store}:/ctx_store:ro"]
        codex = ["/cxbin/codex", "--no-daemon"] + (["-c", f'openai_base_url="http://{gw}:{port}"'] if cli_base_url else []) + [
            "exec", "resume", sid, "--model", model, "--json", "--dangerously-bypass-approvals-and-sandbox", "-c", f"model_reasoning_effort={reasoning}", prompt]
        if installed:
            inner = _container_entry(entry, home)
            install = ["ctxpress", "install", "codex", "--method", inner["class"], "--args", json.dumps(inner.get("args") or {}), "--upstream", upstream]
            shim = 'mkdir -p /cxhome/bin && printf \'#!/bin/sh\\nexec python3 -m ctxpress "$@"\\n\' > /cxhome/bin/ctxpress && chmod +x /cxhome/bin/ctxpress'
            codex = ["sh", "-c", shim + " && export PATH=/cxhome/bin:/cxbin:$PATH && " + shlex.join(install) +
                " >/dev/null && exec bash -ic " + shlex.quote(shlex.join(["codex", *codex[1:]]))]
        command = ["docker", "run", "--pull", "never", "-d", "--init", "--name", name, "--label", "ctxpress.managed=true", "--user", "fakeroot", "--dns", "127.0.0.1"]
        if run_label:
            command += ["--label", f"ctxpress.run={run_label}"]
        subprocess.run([*command, *agent_env, *volumes, "-w", "/testbed", boundary_image, *codex], check=True, capture_output=True)
        started = True
        tag = f"agent-impl-{milestone}" if milestone else None
        def appended():
            rows = []
            for line in rollout.read_text(encoding="utf-8").splitlines()[len(kept):]:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
            return rows
        stop = "timeout"
        while time.monotonic() - t0 < timeout:
            new = appended()
            calls = [d for d in new if d.get("type") == "response_item" and (d.get("payload") or {}).get("type") in ("custom_tool_call", "function_call")]
            outputs = {(d.get("payload") or {}).get("call_id") for d in new if (d.get("payload") or {}).get("type") in ("custom_tool_call_output", "function_call_output")}
            if submit and tag:
                tagged = subprocess.run(["docker", "exec", "--user", "fakeroot", name, "git", "-C", "/testbed", "tag", "-l", tag], capture_output=True, text=True)
                if tagged.returncode == 0 and tagged.stdout.strip() == tag:
                    stop = "submitted"
                    break
            if len(calls) >= max_calls and calls[-1]["payload"].get("call_id") in outputs:
                stop = "max_calls"
                break
            state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True, text=True, check=True)
            if state.stdout.strip() != "true":
                stop = "exited"
                break
            time.sleep(1)
        if submit:
            subprocess.run(["docker", "pause", name], capture_output=True)
            subprocess.run(["docker", "commit", name, f"{name}-end"], check=True, capture_output=True)
            graded_snapshot = True
        new = appended()
    finally:
        removed = False
        try:
            if started:
                subprocess.run(["docker", "kill", name], capture_output=True)
                tail = subprocess.run(["docker", "logs", "--tail", "20", name], capture_output=True, text=True)
                logs = (tail.stdout + tail.stderr)[-1500:]
            _remove_container(name)
            removed = True
            if installed:
                _installed_log(home, log)
        finally:
            if srv:
                srv.shutdown()
                srv.server_close()
            if removed:
                _cleanup(env, home, ws, store)
                resources["cleaned"] = True
                atomic_json(resource_path, resources)
    grade = capture_and_grade(name, n, j, milestone, trial, stop, tag, outdir, env) if graded_snapshot else None
    rows = []
    if Path(log).exists():
        for line in Path(log).read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    telemetry = summary(log)
    calls = [d for d in new if d.get("type") == "response_item" and (d.get("payload") or {}).get("type") in ("custom_tool_call", "function_call")]
    from ctxpress.harness.runtime import execution_health
    return dict(n=n, j=j, milestone=milestone, method=entry, model=model, reasoning=reasoning, binary_dir=env.bindir,
        binary_version=version, environment=environment_evidence, session_restore=session_restore, stop=stop, grade=grade, seconds=round(time.monotonic() - t0, 1), calls=len(calls), requests=telemetry["requests"],
        usage=telemetry, proxy_log=log, rewrites=rows, execution_health=execution_health.observe_rows(new),
        log_tail=logs if stop in ("exited", "timeout") or not rows else "")


def capture_and_grade(name, n, j, milestone, trial, stop, tag, outdir, env=None):
    env = env or environment()
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ref = tag if stop == "submitted" else "HEAD"
    snapshot = outdir / f"{name}.tar"
    directories = " ".join(shlex.quote(path) for path in env.src_dirs + env.root_files)
    archive = "cd /testbed && git archive --format=tar " + shlex.quote(ref) + " -- $(ls -d " + directories + " 2>/dev/null)"
    try:
        with snapshot.open("wb") as stream:
            result = subprocess.run(["docker", "run", "--pull", "never", "--rm", "--network", "none", "--user", "fakeroot",
                "--entrypoint", "", f"{name}-end", "bash", "-c", archive], stdout=stream, stderr=subprocess.PIPE)
        commit = subprocess.run(["docker", "run", "--pull", "never", "--rm", "--network", "none", "--user", "fakeroot",
            "--entrypoint", "", f"{name}-end", "git", "-C", "/testbed", "rev-parse", ref], capture_output=True, text=True)
    finally:
        subprocess.run(["docker", "rmi", "-f", f"{name}-end"], capture_output=True)
    if result.returncode or commit.returncode or not commit.stdout.strip():
        return dict(error="no snapshot", infra_invalid=True)
    package, output = outdir / f"pkg-{name}", outdir / f"grade-{name}.json"
    if getattr(env,'grading',None):
        return eval_grading.execute(env.grading,env.grading_root,n,j,snapshot,f'agent-impl-{milestone}',
                                    commit.stdout.strip(),package,output)
    subprocess.run([sys.executable, str(env.scripts / "make_sidecar.py"), str(snapshot), f"{env.e2e}/{trial}", milestone,
        f"agent-impl-{milestone}", commit.stdout.strip(), str(package)], check=True, capture_output=True)
    process = subprocess.run(["bash", str(env.scripts / "grade.sh"), f"{env.e2e}/{trial}", milestone, str(package / "source_snapshot.tar"), str(output)], capture_output=True)
    if process.returncode or not output.exists():
        return dict(error="official evaluator failed", infra_invalid=True)
    with output.open(encoding="utf-8") as stream:
        grade = json.load(stream)
    return dict(resolved=grade.get("resolved"), test_summary=grade.get("test_summary"), infra_invalid=grade.get("infra_invalid"), report=str(output))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, required=True); ap.add_argument("--j", type=int, required=True)
    ap.add_argument("--method", default="NoCompaction"); ap.add_argument("--args", default="{}")
    ap.add_argument("--max-calls", type=int, default=20); ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--memory", action="store_true"); ap.add_argument("--out"); ap.add_argument("--submit", action="store_true")
    ap.add_argument("--compact-limit", type=int); ap.add_argument("--outdir", default="/tmp/ctxpress-runs")
    ap.add_argument("--installed", action="store_true"); ap.add_argument("--cli-base-url", action="store_true")
    ap.add_argument("--scripts"); ap.add_argument("--bindir"); ap.add_argument("--e2e")
    ap.add_argument("--model", default="gpt-5.6-sol"); ap.add_argument("--reasoning", default="low")
    a = ap.parse_args(argv)
    result = run(a.n, a.j, {"class": a.method, "args": json.loads(a.args)}, a.max_calls, a.timeout, memory=a.memory,
        submit=a.submit, outdir=a.outdir, compact_limit=a.compact_limit, installed=a.installed, cli_base_url=a.cli_base_url,
        scripts=a.scripts, bindir=a.bindir, e2e=a.e2e, model=a.model, reasoning=a.reasoning)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        with open(a.out, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(json.dumps({key: result[key] for key in ("n", "j", "stop", "seconds", "calls", "requests", "grade", "usage")}))


if __name__ == "__main__":
    main()
