"""Core built-in tools — always available regardless of plugins.

These tools are registered at bootstrap time and form the minimal
tool set the agent needs to function without any plugins.
"""

from langchain_core.tools import tool as langchain_tool


@langchain_tool
def ask(question: str) -> str:
    """Ask the user a question and wait for their response."""
    return f"[AWAITING USER RESPONSE] {question}"


# All base tools exported from this module
BASE_TOOLS = [ask]
