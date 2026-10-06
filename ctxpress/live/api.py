"""Python entry points for the common live Responses request rewriter."""
from __future__ import annotations
import copy, os, threading, uuid
from ctxpress.core.params import DEFAULT
from ctxpress.live.rewrite import Rewriter
from ctxpress.live.factory import frozen_factory
from ctxpress.methods import build, with_budget
from ctxpress.methods.base import Method


def _factory(method, budget=None):
    if isinstance(method, str):
        method = {"class": method}
    if isinstance(method, dict):
        if budget is not None:
            method = with_budget(method, budget)
        return frozen_factory(build(copy.deepcopy(method)))
    if budget is not None:
        raise TypeError("set budget when constructing a Method instance or factory; use a name or configuration to set it here")
    if isinstance(method, Method):
        return frozen_factory(method)
    if callable(method):
        return method
    raise TypeError("choose a method name, configuration, Method instance or fresh-method factory")


def _session(value):
    if not isinstance(value, str) or not value:
        raise ValueError("session must be a nonempty string")
    return value


class ContextManager:
    """Reuse one manager for successive full-history Responses requests.

    Method state and original-output archives are independent per session. No
    model or proxy is started; a supplied summary callback uses the common engine.
    Native Codex settings are exposed for a host to apply itself.
    """
    def __init__(self, method="NoCompaction", *, budget=None, params=DEFAULT, summarizer=None,
                 store_dir=None, store_prefix=None, retrieve_tool=False, log=None):
        if store_prefix and not store_dir:
            raise ValueError("store_prefix requires an archive store_dir")
        factory = _factory(method, budget)
        sample = factory()
        if not isinstance(sample, Method):
            raise TypeError("the method factory must return a Method")
        sample.validate_live()
        self.codex_config = copy.deepcopy(sample.codex_config)
        self.run_id = uuid.uuid4().hex
        self.store_dir = os.path.join(os.fspath(store_dir), self.run_id) if store_dir else None
        prefix = os.fspath(store_prefix or store_dir) if (store_prefix or store_dir) else None
        prefix = prefix.rstrip('/\\') + '/' + self.run_id if prefix and self.store_dir else prefix
        if log:
            os.makedirs(os.path.dirname(os.path.abspath(log)), exist_ok=True)
        self.rewriter = Rewriter(factory, params, log=log, store_dir=self.store_dir,
            store_prefix=prefix, retrieve_tool=retrieve_tool, summarizer=summarizer)
        self._info = {}
        self._lock = threading.Lock()

    def apply(self, messages, *, session=None, return_info=False):
        """Accept Responses input items or a complete request; preserve that shape."""
        is_items = isinstance(messages, list)
        if is_items:
            body = {"input": messages}
        elif isinstance(messages, dict) and "input" in messages:
            body = messages
        else:
            raise TypeError("expected Responses input items or a request containing input")
        if body.get("previous_response_id"):
            raise ValueError("send the full Responses history; previous_response_id hides history from the manager")
        value = body["input"]
        if not isinstance(value, (str, list)) or (isinstance(value, list) and not all(isinstance(x, dict) for x in value)):
            raise TypeError("Responses input must be a string or a list of item dictionaries")
        key = _session(session if session is not None else (body.get("prompt_cache_key") or "default"))
        rewritten, info = self.rewriter.rewrite_body(copy.deepcopy(body), key)
        with self._lock:
            previous = self._info.get(key)
            if not previous or not info or info["request"] >= previous["request"]:
                self._info[key] = copy.deepcopy(info)
        result = rewritten["input"] if is_items else rewritten
        return (result, copy.deepcopy(info)) if return_info else result

    def info(self, session="default"):
        """Last rewrite diagnostics; token sizes are estimates, not API usage."""
        with self._lock:
            return copy.deepcopy(self._info.get(_session(session)))

    def retrieve(self, item_id, *, session="default"):
        """Retrieve this session's archived original text, without creating a session."""
        with self.rewriter.lock:
            active = self.rewriter.sessions.get(_session(session))
        if active is None:
            return None
        with active.lock:
            return active.ctx.retrieve(item_id)


def apply(method, messages, *, session=None, return_info=False, **options):
    """One-shot convenience, or pass a ContextManager to preserve conversation state.

    A new manager starts method ages/counters afresh. Stateful policies should use
    the same manager across requests, each with the host's full original history.
    """
    if isinstance(method, ContextManager):
        if options:
            raise TypeError("configure options when constructing the ContextManager")
        manager = method
    else:
        manager = ContextManager(method, **options)
    return manager.apply(messages, session=session, return_info=return_info)
