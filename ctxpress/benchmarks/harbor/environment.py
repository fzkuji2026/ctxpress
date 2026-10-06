"""Harbor Docker lifecycle hook: prepared images, private networks, owned cleanup.

Task services and resource limits are retained from the official Compose model.
Every service image and host bind must be declared by the trial driver. The
resolved model is guarded before any container starts, and images are never
built, pulled, or removed by this hook.
"""
from __future__ import annotations
import asyncio, codecs, copy, json, os, re
from pathlib import Path, PurePosixPath
from ctxpress.harness.jobs import environment as eval_environment, plan as eval_plan
from ctxpress.harness.runtime import gpu as harbor_gpu


async def compose_output(process, stdin_data, on_output):
    """Drain both streams concurrently, including long lines and binary stdin."""
    if on_output is None:
        return await process.communicate() if stdin_data is None else await process.communicate(stdin_data)
    async def read(stream, name):
        chunks = []
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        while True:
            data = await stream.read(65536)
            if not data:
                tail = decoder.decode(b'', final=True)
                if tail:
                    await on_output(tail, name)
                return b''.join(chunks)
            chunks.append(data)
            value = decoder.decode(data)
            if value:
                await on_output(value, name)
    async def write():
        if stdin_data is not None:
            process.stdin.write(stdin_data)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
    async with asyncio.TaskGroup() as group:
        output = group.create_task(read(process.stdout, 'stdout'))
        errors = group.create_task(read(process.stderr, 'stderr'))
        group.create_task(write())
        group.create_task(process.wait())
    return output.result(), errors.result()


def guarded_compose(model, images, bind_roots, run_label, gpu_device_ids=None):
    model = copy.deepcopy(model)
    services = model.get('services')
    if not isinstance(services, dict) or not services or 'main' not in services or set(images) != set(services):
        raise ValueError('declare an immutable image for every Harbor service')
    if not isinstance(run_label, str) or not run_label:
        raise ValueError('declare the owning evaluation job')
    if any(not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value) for value in images.values()):
        raise ValueError('Harbor service images require full content IDs')
    roots = {Path(name).resolve():readonly for name, readonly in bind_roots.items()}
    if any(type(value) is not bool for value in roots.values()):
        raise ValueError('bind roots require explicit read-only or writable policies')
    networks = model.setdefault('networks', {})
    volumes = model.setdefault('volumes', {})
    if not isinstance(networks, dict) or not isinstance(volumes, dict):
        raise ValueError('invalid Harbor Compose networks/volumes')
    for service_name, service in services.items():
        if not isinstance(service, dict) or service.get('privileged') or service.get('pid') == 'host' or service.get('ipc') == 'host':
            raise ValueError('Harbor task requests host privileges')
        if (service.get('devices') or service.get('volumes_from') or service.get('use_api_socket') or
                service.get('external_links') or service.get('cap_add')):
            raise ValueError('Harbor task requests undeclared host devices or volumes')
        mode = service.get('network_mode')
        if mode not in (None, 'none') and not (isinstance(mode, str) and mode.startswith('service:') and mode[8:] in services):
            raise ValueError('Harbor task requests an external network namespace')
        service['image'] = images[service_name]
        service['pull_policy'] = 'never'
        service.pop('build', None)
        service.pop('container_name', None)
        service.pop('ports', None)
        service.pop('extra_hosts', None)
        labels = service.get('labels', {})
        if not isinstance(labels, dict):
            raise ValueError('Harbor resolved labels must be a mapping')
        service['labels'] = dict(labels, **{'ctxpress.managed':'true', 'ctxpress.run':run_label})
        harbor_gpu.guard(service, gpu_device_ids if service_name == 'main' else None)
        for volume in service.get('volumes', []):
            if not isinstance(volume, dict):
                raise ValueError('Harbor Compose must resolve volume declarations')
            source = volume.get('source')
            target = volume.get('target')
            if not isinstance(target, str) or not target.startswith('/') or '..' in PurePosixPath(target).parts:
                raise ValueError('Harbor volume requires an absolute target')
            private = PurePosixPath('/ctxpress-private')
            target_path = PurePosixPath(target)
            if target_path == private or target_path in private.parents or private in target_path.parents:
                raise ValueError('Harbor credentials must remain in the container filesystem')
            if volume.get('type') == 'bind':
                if not isinstance(source, str) or not Path(source).is_absolute():
                    raise ValueError('Harbor bind source must resolve to an absolute path')
                path = Path(source).resolve()
                matches = [(root, readonly) for root, readonly in roots.items() if path == root or root in path.parents]
                if not matches or path.name.lower() in ('auth.json','docker.sock'):
                    raise ValueError('Harbor task requests an undeclared host bind')
                # The most specific policy wins for an explicitly nested root.
                _, readonly = max(matches, key=lambda pair:len(pair[0].parts))
                if readonly:
                    volume['read_only'] = True
                volume['bind'] = dict(volume.get('bind', {}), create_host_path=False)
            elif volume.get('type') == 'volume':
                if source:
                    if source not in volumes or volumes[source].get('external'):
                        raise ValueError('Harbor task requests an undeclared external volume')
                    volumes[source].pop('name', None)
            else:
                raise ValueError('unsupported Harbor volume type')
        if mode is None:
            selected = service.get('networks') or {'default':None}
            if not isinstance(selected, dict):
                raise ValueError('Harbor Compose must resolve network declarations')
            service['networks'] = selected
            for name in selected:
                declaration = networks.setdefault(name, {})
                if not isinstance(declaration, dict):
                    raise ValueError('invalid Harbor network declaration')
                declaration.update(internal=True, external=False)
                declaration.pop('name', None)
    # Never attach unused external resources through a resolved task model.
    for declaration in networks.values():
        if not isinstance(declaration, dict):
            raise ValueError('invalid Harbor network declaration')
        declaration.update(internal=True, external=False)
        declaration.pop('name', None)
        if declaration.get('driver', 'bridge') != 'bridge' or declaration.get('driver_opts'):
            raise ValueError('Harbor private networks require the standard bridge driver')
        declaration['labels'] = dict(declaration.get('labels') or {}, **{'ctxpress.managed':'true', 'ctxpress.run':run_label})
    for declaration in volumes.values():
        if (not isinstance(declaration, dict) or declaration.get('external') or declaration.get('driver_opts') or
                declaration.get('driver', 'local') != 'local'):
            raise ValueError('Harbor task requests external persistent volumes')
        declaration.pop('name', None)
        declaration['labels'] = dict(declaration.get('labels') or {}, **{'ctxpress.managed':'true', 'ctxpress.run':run_label})
    for kind in ('configs','secrets'):
        for declaration in model.get(kind, {}).values():
            if not isinstance(declaration, dict) or declaration.get('external') or declaration.get('environment'):
                raise ValueError('Harbor task requests an undeclared host config/secret')
            source = declaration.get('file')
            if not isinstance(source, str) or not Path(source).is_absolute():
                raise ValueError('Harbor config/secret requires a declared file')
            path = Path(source).resolve()
            if (path.name.lower() in ('auth.json','docker.sock') or
                    not any(path == root or root in path.parents for root in roots)):
                raise ValueError('Harbor task requests an undeclared host config/secret')
    model.pop('name', None)
    return model


def framework(base, exec_result, settings, cleanup, journal=None):
    """Create a process-local environment class for the official factory."""
    settings = copy.deepcopy(settings)
    if {'images','bind_roots','run_label'} - set(settings) or set(settings) - {'images','bind_roots','run_label','gpu_device_ids','compose_name'}:
        raise ValueError('declare service images, bind policies and owner identity')
    compose_name = settings.pop('compose_name', 'ctxpress-compose.json')
    if not re.fullmatch(r'ctxpress-compose(?:-(?:agent|verifier))?\.json', compose_name):
        raise ValueError('invalid managed Compose evidence filename')

    class CtxpressDocker(base):
        def __init__(self, *args, **kwargs):
            self._ctxpress_guard_path = None
            self._ctxpress_started = False
            self.ctxpress_gpu_evidence = None
            super().__init__(*args, **kwargs)
            if not re.fullmatch(r'ctxp-hb-[0-9a-f]{24}', self.session_id):
                raise ValueError('Harbor trial requires a unique managed project identity')
            self._env_vars.prebuilt_image_name = settings['images']['main']
            self.task_env_config.docker_image = settings['images']['main']
            count = getattr(self.task_env_config, 'gpus', None)
            # Official EnvironmentConfig uses None for an omitted GPU request.
            # Keep explicit malformed raw task declarations subject to validation.
            self._ctxpress_gpu_requirements = harbor_gpu.requirements(dict(gpus=0 if count is None else count,
                gpu_types=getattr(self.task_env_config, 'gpu_types', None)))
            if self._ctxpress_gpu_requirements['count'] or settings.get('gpu_device_ids') is not None:
                harbor_gpu.device_ids(settings.get('gpu_device_ids'), self._ctxpress_gpu_requirements['count'])

        @property
        def supports_gpus(self):
            return True

        @property
        def _docker_compose_paths(self):
            if self._ctxpress_guard_path is not None:
                return [self._ctxpress_guard_path]
            return super()._docker_compose_paths

        async def _run_docker_compose_command(self, command, check=True, timeout_sec=None, *, stdin_data=None, on_output=None):
            command = list(command)
            if command and (command[0] in ('pull','build') or '--rmi' in command):
                raise ValueError('Harbor hook cannot build, pull or remove images')
            if command and command[0] == 'up':
                command[1:1] = ['--pull','never','--no-build']
            arguments = ['docker','compose','-p',self.session_id,'--project-directory',str(self.environment_dir.resolve())]
            for path in self._docker_compose_paths:
                arguments += ['-f',str(path.resolve())]
            arguments += command
            # Do not let task Compose interpolation read ambient API credentials.
            env = {key:os.environ[key] for key in ('PATH','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG','XDG_RUNTIME_DIR') if key in os.environ}
            env.update(self._compose_env_vars(include_os_env=False) if hasattr(self, '_compose_env_vars') else
                       self._env_vars.to_env_dict(include_os_env=False))
            process = await asyncio.create_subprocess_exec(*arguments, env=env,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                operation = compose_output(process, stdin_data, on_output)
                output, errors = await asyncio.wait_for(operation, timeout_sec) if timeout_sec else await operation
            except BaseException:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except asyncio.TimeoutError:
                        process.kill(); await process.wait()
                raise
            result = exec_result(stdout=output.decode(errors='replace'), stderr=errors.decode(errors='replace'), return_code=process.returncode)
            if check and result.return_code:
                raise RuntimeError('managed Harbor Compose operation failed')
            return result

        async def start(self, force_build=False):
            if force_build:
                raise ValueError('Harbor hook requires prepared immutable images')
            for image in set(settings['images'].values()):
                if eval_environment.image(image)['id'] != image:
                    raise ValueError('declared Harbor service image changed')
            self._use_prebuilt = True
            if getattr(self, '_mounts', getattr(self, '_mounts_json', None)):
                self._mounts_compose_path = self._write_mounts_compose_file()
            config = await self._run_docker_compose_command(['config','--format','json'])
            model = guarded_compose(json.loads(config.stdout), **settings)
            self._ctxpress_guard_path = self.trial_paths.trial_dir / compose_name
            eval_plan.atomic_json(self._ctxpress_guard_path, model)
            if journal:
                journal('starting', self)
            # No stale project deletion: every attempt has a new project identity.
            self._ctxpress_started = True
            await self._run_docker_compose_command(['up','--detach','--wait'])
            if self._ctxpress_gpu_requirements['count']:
                result = await self.exec(command='nvidia-smi --query-gpu=name,uuid,memory.total,driver_version --format=csv,noheader,nounits',
                                         timeout_sec=30)
                if result.return_code:
                    raise RuntimeError('task GPU runtime is unavailable; nvidia-smi failed before Agent setup')
                self.ctxpress_gpu_evidence = harbor_gpu.observed(result.stdout or '', settings['gpu_device_ids'],
                                                               self._ctxpress_gpu_requirements['types'])
            if journal:
                journal('running', self)

        async def stop(self, delete=True):
            if not self._ctxpress_started:
                return
            # A failed credential deletion preserves the environment for recovery.
            # Never suppress it as a best-effort warning or delete auth on the host.
            await cleanup(self)
            await self._chown_to_host_user('/logs', recursive=True)
            await self._run_docker_compose_command(['down','--volumes','--remove-orphans'])
            self._ctxpress_started = False
            if journal:
                journal('stopped', self)

    return CtxpressDocker
