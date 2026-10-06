"""Container runtime shared by every benchmark: Docker calls and ownership labels, the agent process group, the
pinned Codex binary, model catalog and in-container agent hook, frozen method inputs, model traffic for
network-isolated containers (and its cleanup), GPU reservations, prepared verifier services and execution
health. It imports no benchmark."""
