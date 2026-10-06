"""Pinned Codex bridge for Pier's newer installed-agent API.

Pier owns prompt rendering and post-run trajectory conversion. ctxpress owns
the Codex command, context proxy, tool budget and checked credential cleanup.
"""
from types import SimpleNamespace
from ctxpress.benchmarks.harbor import codex_hook as harbor_codex


def framework(base, install_spec, settings, limit_error, credential_state):
    class Bridge(base):
        def install_spec(self):
            # Pier needs a nonempty declarative spec at factory construction.
            # No build or install is performed: setup checks the mounted binary.
            return install_spec(agent_name=self.name(), version=settings['binary_version'],
                                steps=[dict(run='/cxbin/codex --version', user='agent')])

        async def run(self, instruction, environment, context):
            instruction = self.render_instruction(instruction)
            for command in self.create_run_agent_commands(instruction):
                result = await environment.exec(command='set -o pipefail; ' + command.command,
                    env=command.env)
                if result.return_code:
                    raise limit_error('pinned Codex command exited unsuccessfully')
            # The official Trial calls populate_context_post_run after logs are
            # available, including the timeout and tool-budget paths.

    return harbor_codex.framework(Bridge, SimpleNamespace, settings,
        limit_error=limit_error, credential_state=credential_state)
