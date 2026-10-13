"""Complete the author's Cargo closure for offline grading."""
from __future__ import annotations

import copy


def evaluation_policy(policy):
    """Copy the Agent's Cargo dependencies through the author's hashed recipe.

    Published vendor policies list the registry archive cache alone. Cargo's
    source replacement instead reads /opt/vendor, selected by config.toml.
    Vendor manifests must be parsed with the same Rust toolchain as the Agent;
    older milestone Cargo binaries can reject newer editions before testing.
    Raw-cache mode needs its matching index, extracted sources and Git trees.
    Keep the source policy and its forbidden-package rules untouched.
    Missing prepared paths must fail during the offline image build.
    """
    if not isinstance(policy, dict):
        return policy
    closure = policy.get('closure')
    if (policy.get('cargo_offline') is not True or
            'cargo' not in policy.get('ecosystem', []) or
            not isinstance(closure, dict) or
            closure.get('cargo_mechanism', 'vendor') not in ('vendor', 'raw-cache')):
        return policy
    result = copy.deepcopy(policy)
    paths = result['closure'].setdefault('evaluator_replace_paths', [])
    if not isinstance(paths, list):
        raise ValueError('closure.evaluator_replace_paths must be a list')
    additions = (('/opt/vendor', '/usr/local/cargo/config.toml',
                  '/usr/local/rustup', '/usr/local/cargo/bin')
                 if closure.get('cargo_mechanism', 'vendor') == 'vendor' else
                 ('/usr/local/cargo/registry/index', '/usr/local/cargo/registry/src',
                  '/usr/local/cargo/git'))
    for path in additions:
        if path not in paths:
            paths.append(path)
    return result
