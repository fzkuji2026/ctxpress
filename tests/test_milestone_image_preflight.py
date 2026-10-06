"""Reject broken closure declarations before CLI/authentication/model dispatch."""
import json
from pathlib import Path
import pytest
from ctxpress.benchmarks import milestone_driver
from ctxpress.harness import milestone_version


def test_closure_check_fails_before_binary_and_authentication(tmp_path, monkeypatch):
    payload = {'resources':'fixture', 'original_task':{}, 'folder':str(tmp_path)}
    monkeypatch.setattr(milestone_driver, 'request', lambda *args:payload)
    monkeypatch.setattr(milestone_driver.task_resources, 'read', lambda *args:({'native_data_version':{}},None))
    monkeypatch.setattr(milestone_version, 'validate', lambda *args:None)
    monkeypatch.delenv('CTXPRESS_CODEX_AUTH_FILE', raising=False)
    monkeypatch.setattr(milestone_driver.subprocess, 'run', lambda *args,**kw:pytest.fail('CLI invoked before image validation'))
    def fail(actual):
        assert actual is payload
        raise ValueError('native harness requested an undeclared prepared image: closure-alias')
    monkeypatch.setattr(milestone_driver, 'check_images', fail)
    with pytest.raises(ValueError, match='undeclared prepared image'):
        milestone_driver.prepare_execution({}, {'class':'NoCompaction'}, {'run':{'grade':True}}, {'task':{}}, tmp_path, 'owner')


def test_image_check_isolated_runtime_and_environment_have_no_provider_credentials(tmp_path, monkeypatch):
    payload = {'resources':'fixture', 'original_task':{}, 'folder':str(tmp_path)}
    monkeypatch.setattr(milestone_driver.task_resources, 'read', lambda *args:({'runtime':{'python':'/frozen/python'}},None))
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-inherit')
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE', '/must-not-read/auth.json')
    monkeypatch.setenv('SWE_MILESTONE_IMAGE_TAG', 'unrelated')
    monkeypatch.setenv('DOCKER_HOST', 'unix:///declared/socket')
    expected = {'author_closure_verified':True, 'model_calls':0, 'containers_started':0}
    def invoke(command, **options):
        assert command[:4] == ['/frozen/python','-I','-S','-B']
        assert command[4] == str(milestone_driver.IMAGE_CHECK)
        assert json.loads(Path(command[5]).read_text(encoding='utf-8')) == payload
        assert not {'OPENAI_API_KEY','CTXPRESS_CODEX_AUTH_FILE','SWE_MILESTONE_IMAGE_TAG'} & options['env'].keys()
        assert options['env']['DOCKER_HOST'] == 'unix:///declared/socket'
        assert options['check'] is True and options['timeout'] == 60
        (tmp_path/'native-images.json').write_text(json.dumps(expected), encoding='utf-8')
    monkeypatch.setattr(milestone_driver.subprocess, 'run', invoke)
    assert milestone_driver.check_images(payload) == expected
