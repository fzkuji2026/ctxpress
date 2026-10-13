"""The one SWE-Milestone grading policy: the agent's own source tree is graded.

The author's grader assembles the tested tree as "checkout the reference END tag + untar the agent snapshot", and its
default residue prune (phase 1b) only deletes sources that existed at START and the agent deleted. A source the
reference added and the agent lacks therefore stays in the tested tree and can collide with the agent's own
implementation of the same feature: on Navidrome 003 the reference migration 20250701010108_*.go next to the agent's
own migration makes goose panic (duplicate version) or fail (table already exists), and every suite importing
db/migrations disappears. Every trial is therefore bound to the author's phase-2 semantics: residue prune on, START
provenance guard lifted (`residue_prune_scope: reference-added`). The author's per-repository extensions and keep-list
(metadata.json) are kept; tests, generated files, exclude rules and unlisted extensions stay protected. The effective
policy is written into the trial's SHA-bound repo_config.yaml.
"""
from dataclasses import replace
import types

SCOPE = "reference-added"
FIELDS = ("prune_extensions", "prune_keep_list")


def bound_config(original, metadata):
    value = dict(original, residue_prune=True, residue_prune_scope=SCOPE)
    for key in FIELDS:
        if key not in value and (metadata or {}).get(key) is not None:
            value[key] = metadata[key]
    return value


def bind(repo, metadata):
    import yaml
    config = bound_config(repo.config, metadata)
    raw = yaml.safe_dump(config, sort_keys=True, allow_unicode=True).encode("utf-8")
    return replace(repo, config=config, raw_bytes=raw)


def lifted(prune):
    """The author's evaluator prune pass with its START provenance guard lifted: the same function and module,
    except that compute_prune_set receives start_files=None, the author's phase-2 call."""
    compute = prune.__globals__["compute_prune_set"]
    scope = dict(prune.__globals__, compute_prune_set=lambda base, tar, start, *a, **k: compute(base, tar, None, *a, **k))
    return types.FunctionType(prune.__code__, scope, prune.__name__, prune.__defaults__, prune.__closure__)
