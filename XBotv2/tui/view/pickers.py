"""One chooser for every selection the client offers.

``/session``, ``/thread``, ``/provider``, ``/model``, ``/effort`` and ``/agent``
all ask the same question -- "which of these?" -- so they share one screen and
one runner. What differs is only where the rows come from, which is a pure
function of a model the server already sends: a session list, a thread summary,
a provider catalogue, an agent list.

Keeping the row building pure is what makes the pickers testable without a
terminal, and keeping it here is what stops each command growing its own copy of
"list, push a screen, choose, apply".
"""

from __future__ import annotations

from typing import Any, Sequence

from XBotv2.llm.contracts import ProviderCatalog
from XBotv2.session.contracts import ThreadSummary
from XBotv2.tui.view.selection import Option


def filter_options(options: Sequence[Option], query: str) -> tuple[Option, ...]:
    """Rows matching every term of ``query``, in the order they were built.

    The chooser offers a filter box for every list it shows, so a catalogue with
    dozens of models stays usable with the same three keystrokes it takes to
    narrow the palette.
    """
    terms = query.strip().lower().split()
    if not terms:
        return tuple(options)
    kept = []
    for option in options:
        haystack = f"{option.label} {option.detail}".lower()
        if all(term in haystack for term in terms):
            kept.append(option)
    return tuple(kept)


def _detail(*parts: Any) -> str:
    return " ".join(str(part) for part in parts if part)


def session_options(items: Sequence[Any]) -> tuple[Option, ...]:
    """Rows for ``/session``: one per stored session."""
    return tuple(
        Option(
            value=str(getattr(item, "session_id", "")),
            label=str(getattr(item, "title", "") or getattr(item, "session_id", "")),
            detail=_detail(
                getattr(item, "workspace_root", "") or getattr(item, "status", "")
            ),
        )
        for item in items
    )


def thread_options(items: Sequence[ThreadSummary]) -> tuple[Option, ...]:
    """Rows for ``/thread``: the id is what the user would type, so it is shown."""
    return tuple(
        Option(
            value=item.thread_id,
            label=(
                f"{item.title}  {item.thread_id}"
                if item.title and item.title != item.thread_id
                else item.thread_id
            ),
            detail=_detail(
                item.kind,
                item.turn_status,
                item.agent,
                f"{item.message_count} msg" if item.message_count else "",
            ),
        )
        for item in items
    )


def provider_options(catalog: ProviderCatalog, *, current: str = "") -> tuple[Option, ...]:
    """Rows for ``/provider``: one per configured provider."""
    return tuple(
        Option(
            value=provider.name,
            label=provider.name,
            detail=_detail(
                "current" if provider.name == current else "",
                provider.provider,
                f"{len(provider.models)} models",
                # "starts at", not "default": every row saying "default <model>"
                # made a filter for the provider literally named ``default``
                # match all of them.
                f"starts at {provider.default_model}",
                "default" if provider.name == catalog.default else "",
            ),
        )
        for provider in catalog.providers
    )


def model_options(
    catalog: ProviderCatalog,
    *,
    provider: str = "",
) -> tuple[Option, ...]:
    """Rows for ``/model``: the models of one provider.

    The provider is part of the choice in the wire API, not part of the row, so
    the rows stay model names -- which is also what the user types.
    """
    chosen = _provider(catalog, provider)
    if chosen is None:
        return ()
    return tuple(
        Option(
            value=model.model,
            label=model.model,
            detail=_detail(
                f"default for {chosen.name}"
                if model.model == chosen.default_model
                else "",
                model.reasoning_effort,
                f"{model.max_context_tokens} ctx",
                " ".join(model.input_modalities) if model.input_modalities else "",
            ),
        )
        for model in chosen.models
    )


def effort_options(tiers: Sequence[str], *, current: str = "") -> tuple[Option, ...]:
    """Rows for ``/effort``: the tiers the current model advertises."""
    return tuple(
        Option(
            value=str(tier),
            label=str(tier),
            detail="current" if str(tier) == current else "",
        )
        for tier in tiers
    )


def effort_tiers(catalog: ProviderCatalog, *, provider: str = "", model: str = "") -> tuple[str, ...]:
    """The tiers the current model advertises; empty when it advertises none."""
    chosen = _provider(catalog, provider)
    if chosen is None:
        return ()
    for candidate in chosen.models:
        if candidate.model == model:
            return tuple(candidate.effort)
    return ()


def agent_options(response: Any, **_: Any) -> tuple[Option, ...]:
    """Rows for ``/agent``: one per agent this thread can switch to."""
    active = str(getattr(response, "active", "") or "")
    return tuple(
        Option(
            value=item.name,
            label=item.name,
            detail=_detail(
                "active" if item.name == active else "",
                item.mode,
                item.provider,
                item.model,
                item.description,
            ),
        )
        for item in getattr(response, "agents", ()) or ()
    )


def _provider(catalog: ProviderCatalog, name: str) -> Any:
    """The named provider, or the default one when no name was given.

    A name that is not in the catalogue yields nothing: falling back to another
    provider would show models the user did not ask about.
    """
    if name:
        return next(
            (provider for provider in catalog.providers if provider.name == name), None
        )
    return next(
        (provider for provider in catalog.providers if provider.name == catalog.default),
        catalog.providers[0] if catalog.providers else None,
    )


__all__ = [
    "agent_options",
    "filter_options",
    "effort_options",
    "effort_tiers",
    "model_options",
    "provider_options",
    "session_options",
    "thread_options",
]
