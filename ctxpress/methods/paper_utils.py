"""Host protection and complete-pair selection shared by paper adaptations."""


def complete_items(sim, candidates):
    candidates = [s for s in candidates if not s.get('protected') and not s.get('has_media')
                  and s.get('role') not in ('user', 'system', 'developer')]
    ids = {s['id'] for s in candidates}
    crossing = {s.get('call_id') for s in sim.ctx if s['id'] not in ids and s.get('call_id')}
    calls = {s.get('call_id') for s in candidates if s['seg'] == 'call'}
    outputs = {s.get('call_id') for s in candidates if s['seg'] == 'out'}
    blocked = crossing | (calls ^ outputs)
    return [s for s in candidates if not s.get('call_id') or s['call_id'] not in blocked]


def tool_name(s):
    return (s.get('name') or '').rsplit('__', 1)[-1].rsplit('.', 1)[-1]


def positive_int(name, value, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')
    return value
