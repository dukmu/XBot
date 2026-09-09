"""Public declarations owned by the subagent capability."""


class SubagentAgentError(RuntimeError):
    code = "agent_not_found"


__all__ = ["SubagentAgentError"]
