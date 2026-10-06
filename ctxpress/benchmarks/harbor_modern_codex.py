"""Pinned command bridge for Harbor's SingleStepTrial installed-agent API."""
from types import SimpleNamespace
import shlex
from ctxpress.core import toml
from . import harbor_codex


def framework(base, settings, limit_error, credential_state):
    class Bridge(base):
        def _build_register_mcp_servers_command(self):
            # Let the author map transports/names, retaining only task MCP data.
            servers = self._build_effective_config().get('mcp_servers')
            if not servers:
                return None
            data = toml.dumps({'mcp_servers':servers})
            source = 'from pathlib import Path; p=Path(' + repr(harbor_codex.HOME + '/config.toml') + \
                '); p.write_text(p.read_text() + "\\n" + ' + repr(data) + ')'
            return 'python3 -c ' + shlex.quote(source)

        async def run(self, instruction, environment, context):
            instruction = self.render_instruction(instruction)
            for command in self.create_run_agent_commands(instruction):
                result = await environment.exec(command='set -o pipefail; ' + command.command, env=command.env)
                if result.return_code:
                    raise limit_error('pinned Codex command exited unsuccessfully')
            # Trial retains official prompt rendering and trajectory conversion.

    return harbor_codex.framework(Bridge, SimpleNamespace, settings,
        limit_error=limit_error, credential_state=credential_state)
