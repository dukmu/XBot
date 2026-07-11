"""Shell execution tool. Uses session sandbox capabilities when available."""

from xbotv2.contracts import ToolResult
from xbotv2.tools.types import XBotTool


async def execute_shell(command: str, cwd: str | None = None, *, sandbox=None) -> ToolResult:
    """Execute a shell command and return the output.

    Args:
        command: The shell command to execute.
        cwd: Working directory. Defaults to the session workspace root.
    """
    if sandbox is not None and sandbox.enabled:
        return ToolResult.success(await sandbox.run_shell(command, cwd=cwd))

    import subprocess
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=30, cwd=cwd,
        )
        content = result.stdout or result.stderr or "(no output)"
        if result.returncode:
            return ToolResult.failure("command_failed", content)
        return ToolResult.success(content)
    except subprocess.TimeoutExpired:
        return ToolResult.failure("command_timeout", "Command timed out after 30 seconds")
    except Exception as exc:
        return ToolResult.failure("command_failed", str(exc))


shell = XBotTool.from_function(execute_shell, name="shell")
SHELL_TOOLS = [shell]
