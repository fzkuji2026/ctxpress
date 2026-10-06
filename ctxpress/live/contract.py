"""Host invariants checked after every render, including overflow retries."""
from collections import Counter
import json


class HostContractError(ValueError):
    """Do not send a request whose rewrite discarded protected host state."""


def _key(item):
    return json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def validate(original, rendered):
    from ctxpress.live.rewrite import CALL_TYPES, OUT_TYPES
    def protected(item):
        kind = item.get('type', 'message')
        return (kind in ('additional_tools', 'compaction') or
                (kind == 'message' and item.get('role') in ('system', 'developer')))
    first_user = next((n for n, i in enumerate(original)
                       if i.get('type', 'message') == 'message' and i.get('role') == 'user'), None)
    required = Counter(_key(i) for n, i in enumerate(original) if protected(i) or n == first_user)
    present = Counter(_key(i) for i in rendered if protected(i) or
                      (i.get('type', 'message') == 'message' and i.get('role') == 'user'))
    if required - present:
        # Never include user content or tool payloads in this error.
        raise HostContractError('rewrite removed or modified protected host state')
    def media(value):
        if isinstance(value, dict):
            if value.get('type') in ('input_image', 'output_image', 'image', 'input_audio', 'output_audio', 'input_file', 'document'):
                yield _key(value)
            else:
                for child in value.values():
                    yield from media(child)
        elif isinstance(value, list):
            for child in value:
                yield from media(child)
    def media_inventory(items):
        return Counter((i.get('call_id'), i.get('role'), block) for i in items for block in media(i))
    # Hosts may compact surrounding text while preserving opaque blocks and their association.
    if media_inventory(original) - media_inventory(rendered):
        raise HostContractError('rewrite removed or modified media or its tool association')
    calls = {i.get('call_id') for i in rendered if i.get('type') in CALL_TYPES}
    outputs = {i.get('call_id') for i in rendered if i.get('type') in OUT_TYPES}
    if calls != outputs:
        raise HostContractError('rewrite emitted unpaired tool calls/results')
    return dict(valid=True, protected_items=sum(required.values()), paired_calls=len(calls))
