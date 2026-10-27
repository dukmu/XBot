"""None is unlimited; omitted reconfiguration preserves an explicit limit."""
from types import SimpleNamespace
from XBotv2.agentloop.impl.engine import Engine


def test_iteration_limit_configuration_distinguishes_none_and_omission():
    state = SimpleNamespace(session=SimpleNamespace())
    engine = Engine(model_client=object(), tools=object(), events=object(), state=state, user_identity=object(), memory="", inbox=object())
    assert engine.max_iterations is None
    engine.configure(max_iterations=7)
    engine.configure(model_client=object())
    assert engine.max_iterations == 7
    engine.configure(max_iterations=None)
    assert engine.max_iterations is None
