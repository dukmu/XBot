"""Shell execution tool. Uses session sandbox capabilities when available."""

from xbotv2.tools.types import XBotTool


async def execute_shell(command: str, cwd: str | None = None, *, sandbox=None) -> str:
    """Execute a shell command and return the output.

    Args:
        command: The shell command to execute.
        cwd: Working directory. Defaults to the session workspace root.
    """
    if sandbox is not None and sandbox.enabled:
        return await sandbox.run_shell(command, cwd=cwd)

    import subprocess
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=30, cwd=cwd,
        )
        return result.stdout or result.stderr or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 30 seconds"
    except Exception as exc:
        return f"Error: {exc}"


shell = XBotTool.from_function(execute_shell, name="shell")
SHELL_TOOLS = [shell]
