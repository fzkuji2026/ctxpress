"""Preserve native evaluator-cache validation while forbidding image preparation."""
from __future__ import annotations
import contextlib,json,subprocess,types
from ctxpress.harness.milestone_resources import docker,image_id


class PreparedImages:
    def __init__(self,module,repo,agent,grading):
        self.module,self.repo,self.agent,self.grading=module,repo,agent,grading
        self.prefix=module.OFFLINE_CACHE_LABEL_PREFIX
        self.allowed={};self.base={};self.references={}
        self.bind(agent)
        for mid,record in grading.items():
            self.bind(record)
            observed=self.inspect(record['id']);labels=observed.get('Config',{}).get('Labels') or {}
            parent=labels.get(self.prefix+'.milestone-image-id')
            closure=labels.get(self.prefix+'.closure-image-id')
            if parent or closure:
                if not parent or not closure:raise ValueError('prepared native overlay lacks parent/closure provenance')
                parent=image_id('sha256:'+parent);closure=image_id('sha256:'+closure)
                if closure!=agent['id']:raise ValueError('prepared native overlay differs from the Agent closure image')
                self.allowed[parent]=parent
            else:parent=record['id']
            self.base[mid]=parent
            self.references[module.local_ref(repo,mid)]=parent
        self.references[module.local_ref(repo,'base-offline')]=agent['id']
        original=module.ensure_offline_evaluation_image
        # Run the actual author's recipe hashing, provenance checks and lock.
        # Only its Docker boundary is replaced. Its build/tag paths must fail
        # before changing any local image, even if a prepared alias disappears.
        scope=dict(original.__globals__,subprocess=self,_docker_image_id=self.native_id,
            _docker_image_labels=self.native_labels,resolve_image=self.resolve,
            _bind_local_image_alias=self.no_preparation)
        self.native=types.FunctionType(original.__code__,scope,original.__name__,original.__defaults__,original.__closure__)
        self.native.__kwdefaults__=original.__kwdefaults__

    def bind(self,record):
        value=image_id(record['id']);self.allowed[value]=value
        if record.get('reference'):
            reference=record['reference']
            if reference in self.allowed and self.allowed[reference]!=value:raise ValueError('native prepared image references are ambiguous')
            self.allowed[reference]=value

    def inspect(self,reference):
        expected=self.allowed.get(reference)
        if expected is None:raise ValueError('native harness requested an undeclared prepared image: '+reference)
        rows=json.loads(docker('image','inspect',reference))
        if len(rows)!=1 or rows[0].get('Id')!=expected or rows[0].get('Os')!='linux':
            raise ValueError('native prepared image alias/content changed')
        return rows[0]

    def resolve(self,reference):
        if reference not in self.references:raise ValueError('native image selector is outside the declared itinerary')
        selected=self.references[reference];self.inspect(selected);return selected

    def native_id(self,reference):return self.inspect(reference)['Id'].removeprefix('sha256:')
    def native_labels(self,reference):return self.inspect(reference).get('Config',{}).get('Labels') or {}
    def no_preparation(self,*args,**kwargs):raise ValueError('native evaluator image must already be prepared; build/tag is disabled')

    def __getattr__(self,name):
        if name not in ('DEVNULL','PIPE','STDOUT','TimeoutExpired'):
            raise ValueError('native evaluator requested an unsupported subprocess operation')
        return getattr(subprocess,name)

    def run(self,command,**kwargs):
        if len(command)!=4 or command[:3]!=['docker','image','inspect']:
            self.no_preparation()
        row=self.inspect(command[3])
        output=json.dumps([row]);text=kwargs.get('text') or kwargs.get('encoding')
        return subprocess.CompletedProcess(command,0,output if text else output.encode(),'' if text else b'')

    def overlay(self,*,repo_name,milestone_id,milestone_image,quarantine_config,expected_closure_image_id=''):
        if (repo_name!=self.repo or milestone_id not in self.base or
                milestone_image!=self.base[milestone_id] or
                expected_closure_image_id and image_id('sha256:'+expected_closure_image_id.removeprefix('sha256:'))!=self.agent['id']):
            raise ValueError('native evaluator source/closure differs from the frozen itinerary')
        result=self.native(repo_name=repo_name,milestone_id=milestone_id,milestone_image=milestone_image,
            quarantine_config=quarantine_config,expected_closure_image_id=expected_closure_image_id)
        if result[0]!=self.grading[milestone_id]['id'] or 'sha256:'+result[3]!=self.grading[milestone_id]['id']:
            raise ValueError('native effective evaluator differs from its prepared image binding')
        return result


@contextlib.contextmanager
def installed(module,repo,agent,grading):
    prepared=PreparedImages(module,repo,agent,grading)
    original_resolve=module.resolve_image;original_overlay=module.ensure_offline_evaluation_image
    module.resolve_image=prepared.resolve;module.ensure_offline_evaluation_image=prepared.overlay
    try:yield prepared
    finally:module.resolve_image=original_resolve;module.ensure_offline_evaluation_image=original_overlay
