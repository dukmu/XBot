"""Plugin scope lifecycle; business owners seed their own scope facts."""
from __future__ import annotations
from typing import Literal, Protocol
from xcore.api.context import Context
from xcore.api.services import ServiceKey
from XBotv2.loader.api.contracts import PluginTree

ScopeKind = Literal["process", "session", "thread"]

class ScopeLoader(Protocol):
    def close(self) -> None: ...
    def config_schema(self, entry) -> object: ...
    def scope(self, parent: Context, name: str, *, kind: ScopeKind, tree: PluginTree,
              provides: tuple[ServiceKey | str, ...] = ()) -> Context: ...
    async def mount(self, ctx: Context, tree: PluginTree, *, kind: ScopeKind,
                    required: tuple[ServiceKey | str, ...] = ()) -> None: ...

LOADER: ServiceKey[ScopeLoader] = ServiceKey("loader")


def create_loader(*, plugin_dirs=()) -> ScopeLoader:
    """Construct the loader when the process owner starts its resources."""
    from XBotv2.loader.impl.runtime import Loader
    return Loader(plugin_dirs=plugin_dirs)
