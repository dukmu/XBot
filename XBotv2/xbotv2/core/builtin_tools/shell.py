"""Shell execution tool."""

from langchain_core.tools import tool as langchain_tool


@langchain_tool
def shell(command: str, cwd: str | None = None) -> str:
    """Execute a shell command and return the output.

    Args:
        command: The shell command to execute.
        cwd: Working directory. Defaults to the session workspace root.
    """
    import subprocess
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
        return result.stdout or result.stderr or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 30 seconds"
    except Exception as exc:
        return f"Error: {exc}"


SHELL_TOOLS = [shell]
