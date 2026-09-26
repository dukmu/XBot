"""The footer shows context-sensitive key hints, not session facts."""

from __future__ import annotations

from rich.cells import cell_len

from XBotv2.tui.status import Connection, Interaction, ServerTurn, StatusFacts
from XBotv2.tui.view.composer import ComposerModel, composer_hint
from XBotv2.tui.view.footer import render_footer_hints


def render(model: ComposerModel, width: int = 120) -> str:
    return render_footer_hints(model, width=width).plain


def test_idle_footer_shows_client_actions_without_claiming_interrupt_is_available() -> None:
    model = ComposerModel(facts=StatusFacts(connection=Connection.CONNECTED))

    shown = render(model)

    assert "Ctrl+P" in shown
    assert "Ctrl+T agents" in shown
    assert "F2 settings" in shown
    assert "? for shortcuts" in shown
    assert "Esc interrupt" not in shown


def test_running_footer_promotes_interrupt_and_steer() -> None:
    model = ComposerModel(
        facts=StatusFacts(
            connection=Connection.CONNECTED,
            server_turn=ServerTurn.RUNNING,
            turn_open=True,
        )
    )

    shown = render(model)

    assert "Esc interrupt" in shown
    assert "Enter queues" in shown
    assert "Alt+S steer" in shown


def test_running_delivery_keys_are_shown_once_in_the_footer() -> None:
    model = ComposerModel(
        facts=StatusFacts(
            connection=Connection.CONNECTED,
            server_turn=ServerTurn.RUNNING,
            turn_open=True,
        )
    )

    composer = composer_hint(model)
    footer = render(model)

    assert composer == ""
    assert "Enter queues" in footer
    assert "Alt+S steer" in footer


def test_interaction_footer_does_not_offer_turn_interrupt() -> None:
    model = ComposerModel(
        facts=StatusFacts(
            connection=Connection.CONNECTED,
            interaction=Interaction.PERMISSION,
        )
    )

    shown = render(model)

    assert "Ctrl+C" in shown
    assert "Ctrl+P" in shown
    assert "Esc interrupt" not in shown


def test_read_only_agent_thread_offers_agent_switch_and_return() -> None:
    shown = render(ComposerModel(
        facts=StatusFacts(connection=Connection.CONNECTED),
        read_only=True,
    ))

    assert "Ctrl+T agents" in shown
    assert "Esc main" in shown
    assert "interrupt" not in shown.lower()


def test_footer_is_clipped_by_terminal_cell_width() -> None:
    model = ComposerModel(
        facts=StatusFacts(connection=Connection.CONNECTED, server_turn=ServerTurn.RUNNING)
    )

    for width in range(1, 36):
        assert cell_len(render(model, width)) <= width
