"""The error a rewrite raises when it would discard protected host state (checked in live.contract)."""


class HostContractError(ValueError):
    """Do not send a request whose rewrite discarded protected host state."""
