"""Rejected summaries expose an actionable category without provider payloads."""
import json
import pytest

from ctxpress.live.summarize import ResponsesSummarizer, response_error


@pytest.mark.parametrize('body, category, fields', [
    ({'error': {'code': 'model_not_found', 'type': 'invalid_request_error', 'param': 'model'}},
     'model_unavailable', {'code': 'model_not_found', 'type': 'invalid_request_error', 'param': 'model'}),
    ({'detail': 'The model is not supported when using this account'}, 'model_unsupported', {}),
    ({'error': {'message': 'You do not have access to this model'}}, 'model_unavailable', {}),
    ({'error': {'code': 'unsupported_parameter', 'param': 'reasoning.effort',
               'message': 'Unsupported parameter for this model'}},
     'invalid_request_parameter', {'code': 'unsupported_parameter', 'param': 'reasoning.effort'}),
    ({'error': {'code': 'unsupported_value', 'param': 'text.verbosity'}},
     'invalid_request_parameter', {'code': 'unsupported_value', 'param': 'text.verbosity'}),
    ({'error': {'code': 'unsupported_value', 'param': 'verbosity'}},
     'invalid_request_parameter', {'code': 'unsupported_value', 'param': 'verbosity'}),
    ({'error': {'code': 'unsupported_value', 'message':
               "Unsupported value: 'medium' is not supported with the 'text.verbosity' parameter. Supported values are: 'low'. PRIVATE"}},
     'invalid_request_parameter', {'code': 'unsupported_value', 'param': 'text.verbosity', 'param_source': 'message'}),
    ({'error': {'code': 'context_length_exceeded'}}, 'context_length_exceeded', {'code': 'context_length_exceeded'}),
    ({'error': {'type': 'permission_error'}}, 'authentication_or_permission', {'type': 'permission_error'}),
])
def test_only_fixed_categories_and_known_field_values_are_retained(body, category, fields):
    assert response_error(json.dumps(body).encode()) == dict(category=category, **fields)


@pytest.mark.parametrize('raw', [b'not json', b'[]', b'null', b'\xff', b'x' * 65537,
    json.dumps({'error': {'message': 'secret prompt', 'type': 'secret credential',
                         'code': 'secret credential', 'param': 'secret prompt'}}).encode(),
    json.dumps({'error': {'code': [], 'type': {}, 'param': 3, 'message': {}}}).encode()],
    ids=['text', 'list', 'null', 'invalid-utf8', 'oversized', 'secret-strings', 'wrong-types'])   # short ids: the test id goes into PYTEST_CURRENT_TEST
def test_unknown_or_malformed_error_payloads_are_not_logged(raw):
    assert response_error(raw) == {'category': 'unclassified_provider_error'}


def test_http_failure_is_recorded_once_without_leaking_provider_text_or_retrying():
    records, sent = [], []
    class Response:
        status = 400
        def read(self):
            return json.dumps({'error': {'code': 'model_not_found', 'param': 'model',
                                        'message': 'SECRET model does not exist: PRIVATE PROMPT'}}).encode()
    class Connection:
        def request(self, *args, **kwargs):
            sent.append(kwargs['body'])
        def getresponse(self):
            return Response()
        def close(self):
            pass
    client = ResponsesSummarizer(Connection, '/responses', {}, {'model': 'main'}, records.append)
    with pytest.raises(ValueError, match='summary HTTP status 400'):
        client.summarize_prompt('private system', 'private user', model='reflect')
    assert len(sent) == len(records) == 1
    assert records[0]['provider_error']['category'] == 'model_unavailable'
    assert records[0]['completed'] is False and records[0]['usage'] is None
    serialized = json.dumps(records)
    assert all(text not in serialized for text in ('SECRET', 'PRIVATE PROMPT', 'private system', 'private user'))


def test_message_parameter_diagnostics_are_whitelisted_and_need_an_explicit_label():
    for message in ("PRIVATE 'secret credential' parameter", "PRIVATE mentions 'text.verbosity'"):
        raw = json.dumps({'error': {'code': 'unsupported_value', 'message': message}}).encode()
        result = response_error(raw)
        assert result['category'] == 'invalid_request_parameter' and result['code'] == 'unsupported_value'
        assert 'param' not in result and result['message_present'] is True
        assert not any(word in json.dumps(result) for word in ('PRIVATE', 'secret', 'credential'))
    raw = json.dumps({'error': {'code': 'unsupported_value', 'param': 'reasoning.effort',
                              'message': "PRIVATE 'text.verbosity' parameter"}}).encode()
    assert response_error(raw) == {'category': 'invalid_request_parameter', 'code': 'unsupported_value',
                                   'param': 'reasoning.effort'}


def test_generic_value_code_does_not_hide_a_model_transport_rejection():
    raw = json.dumps({'error': {'type': 'invalid_request_error', 'code': 'unsupported_value',
        'param': None, 'message': 'The model PRIVATE is not supported when using this account'}}).encode()
    assert response_error(raw) == {'category': 'model_unsupported',
        'type': 'invalid_request_error', 'code': 'unsupported_value'}
    raw = json.dumps({'error': {'code': 'unsupported_value', 'param': 'text.verbosity',
        'message': "Unsupported value: 'medium' is not supported with this model"}}).encode()
    assert response_error(raw) == {'category': 'invalid_request_parameter',
        'code': 'unsupported_value', 'param': 'text.verbosity'}


def test_nonstandard_provider_message_retains_only_closed_vocabulary():
    raw = json.dumps({'error': {'code': 'unsupported_value', 'message':
        'PRIVATE TOKEN abc123 rejects auto for reasoning.summary; allowed detailed'}}).encode()
    assert response_error(raw) == {'category': 'invalid_request_parameter',
        'code': 'unsupported_value', 'message_present': True,
        'message_terms': ['allowed', 'auto', 'detailed', 'reasoning.summary']}


def test_required_context_error_identifies_backtick_parameter_without_provider_text():
    raw = json.dumps({'error': {'code': 'unsupported_value', 'message':
        'PRIVATE requires `reasoning.context` to be `all_turns`.'}}).encode()
    assert response_error(raw) == {'category': 'invalid_request_parameter', 'code': 'unsupported_value',
        'param': 'reasoning.context', 'param_source': 'message'}


@pytest.mark.parametrize('context', ['all_turns', 'current_turn', 'auto', None])
@pytest.mark.parametrize('parallel', [True, False, None])
def test_reflection_keeps_context_contract_without_inheriting_model_effort(context, parallel):
    records, sent = [], []
    class Response:
        status = 200
        def read(self):
            return json.dumps(dict(status='completed', model='reflect', output=[dict(type='message',
                content=[dict(type='output_text', text='summary')])])).encode()
    class Connection:
        def request(self, *args, **kwargs): sent.append(json.loads(kwargs['body']))
        def getresponse(self): return Response()
        def close(self): pass
    reasoning = dict(effort='high', summary='auto')
    if context is not None:
        reasoning['context'] = context
    request = dict(model='main', reasoning=reasoning, service_tier='priority')
    if parallel is not None:
        request['parallel_tool_calls'] = parallel
    before = json.dumps(request)
    client = ResponsesSummarizer(Connection, '/responses', {}, request, records.append)
    assert client.summarize_prompt('system', 'user', model='reflect') == 'summary'
    assert sent[0].get('reasoning') == ({'context': context} if context is not None else None)
    assert 'service_tier' not in sent[0] and json.dumps(request) == before
    assert records[0].get('reasoning_context') == context
    assert sent[0].get('parallel_tool_calls') is parallel
    assert records[0].get('parallel_tool_calls') is parallel
    assert sent[0]['tools'] == []
    assert len(sent) == len(records) == 1
