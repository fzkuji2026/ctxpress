"""Combine a native named profile with the run's transport and method settings."""
from __future__ import annotations
import copy, re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from ctxpress.core import toml

PROFILE_NAME = re.compile(r'[A-Za-z0-9_-]+\Z')
VALUE_FLAGS = {'-m','--model','-s','--sandbox','-C','--cd','--add-dir','-a','--ask-for-approval',
               '-i','--image','-o','--output-last-message','--output-schema','--remote-auth-token-env'}


def merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def _override(text):
    key, sep, raw = text.partition('=')
    if not sep or not key.strip():
        raise ValueError('Codex --config requires key=value')
    try:
        return toml.loads(text)
    except ValueError:
        # Native Codex treats an unparseable value as a literal string. Parse the
        # key again independently; invalid keys must still fail before launch.
        return toml.loads(key + '=' + toml.value(raw))


def arguments(args):
    """Consume one profile; hoist global overrides without reading prompt values.

    Overrides keep their order. Runtime overrides can then be placed after them,
    ahead of the subcommand, even when the user supplied config flags to `exec`.
    The explicit `--` delimiter and everything after it stay opaque.
    """
    args, remaining, overrides, profile = list(args), [], [], None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--':
            remaining.extend(args[i:]); break
        if arg in ('--ignore-user-config','--remote') or arg.startswith('--remote='):
            raise ValueError(f'{arg.split("=")[0]} bypasses the local ctxpress runtime profile')
        if arg in ('--oss','--local-provider') or arg.startswith('--local-provider='):
            raise ValueError('native local providers require a Responses-compatible ctxpress serve endpoint')
        kind, text, consumed = None, None, 1
        for long, short, target in (('--profile','-p','profile'),('--config','-c','config'),('--enable',None,'enable'),('--disable',None,'disable')):
            if arg in (long, short):
                if i + 1 >= len(args):
                    raise ValueError(f'{long} requires a value')
                kind, text, consumed = target, args[i+1], 2
                break
            if arg.startswith(long + '='):
                kind, text = target, arg[len(long)+1:]
                break
            if short and arg.startswith(short) and len(arg) > len(short):
                kind, text = target, arg[len(short):].removeprefix('=')
                break
        if kind == 'profile':
            if profile is not None:
                raise ValueError('select one Codex configuration profile')
            if not PROFILE_NAME.fullmatch(text):
                raise ValueError('Codex profile names use letters, numbers, hyphens and underscores')
            profile = text
        elif kind == 'config':
            _override(text)
            overrides.append(text)
        elif kind in ('enable','disable'):
            if not re.fullmatch(r'[A-Za-z0-9_]+', text):
                raise ValueError('invalid Codex feature name')
            overrides.append(f'features.{text}={"true" if kind == "enable" else "false"}')
        elif arg in VALUE_FLAGS and i + 1 < len(args):
            remaining.extend(args[i:i+2]); consumed = 2
        else:
            remaining.append(arg)
        i += consumed
    return profile, overrides, remaining


@dataclass
class Prepared:
    profile: str | None
    data: dict
    overrides: list[str]
    args: list[str]
    provider: str
    upstream: str | None
    providers: dict

    def runtime(self, port, codex_config=None):
        endpoint = f'http://127.0.0.1:{port}'
        owned = dict(codex_config or {})
        owned['openai_base_url'] = endpoint
        owned['features.enable_request_compression'] = False
        # CLI overrides are a higher layer than profiles/project config. The
        # built-in URL is also written into the profile for ChatGPT login.
        data = copy.deepcopy(self.data)
        data['openai_base_url'] = endpoint
        for key, val in (codex_config or {}).items():
            data = merge(data, _override(key + '=' + toml.value(val)))
        data = merge(data, {'features':{'enable_request_compression':False}})
        overrides = self.overrides
        if self.provider != 'openai':
            # Native -c paths split on dots and do not unquote map keys. Resolve
            # provider overrides into the private profile instead. Provider
            # fields cannot be overridden by project config, so this keeps their
            # effective precedence and avoids putting auth fields in argv.
            data['model_providers'] = merge(self.providers, {self.provider:{'base_url':endpoint}})
            overrides = [text for text in overrides if 'model_providers' not in _override(text)]
        flags = [part for text in overrides for part in ('-c', text)]
        flags += [part for key, val in owned.items() for part in ('-c', key + '=' + toml.value(val))]
        return data, flags + self.args


def prepare(args, home):
    profile, overrides, remaining = arguments(args)
    root = Path(home)
    base = toml.load(root / 'config.toml') if (root / 'config.toml').is_file() else {}
    data = toml.load(root / f'{profile}.config.toml') if profile else {}
    effective = merge(base, data)
    for text in overrides:
        effective = merge(effective, _override(text))
    provider = effective.get('model_provider', 'openai')
    if not isinstance(provider, str) or not provider:
        raise ValueError('Codex model_provider must be a nonempty string')
    if provider == 'openai':
        upstream = effective.get('openai_base_url')
    else:
        definition = effective.get('model_providers', {}).get(provider, {})
        if definition.get('wire_api', 'responses') != 'responses':
            raise ValueError('ctxpress supports Responses model providers')
        upstream = definition.get('base_url')
        if not upstream:
            raise ValueError('selected model provider needs a Responses base_url for ctxpress routing')
    if upstream:
        parts = urlsplit(upstream)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.fragment:
            raise ValueError('model provider base_url must be an HTTP(S) endpoint without a fragment')
    return Prepared(profile, data, overrides, remaining, provider, upstream, effective.get("model_providers", {}))
