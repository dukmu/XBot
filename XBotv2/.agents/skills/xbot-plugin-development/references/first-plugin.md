# First plugin

An external plugin is a regular Python package with `api/`, `impl/`, `doc/`
and `tests/`. Keep package `__init__.py` files empty. For example:

```text
extensions/
└── hello_plugin/
    ├── __init__.py
    ├── api/
    │   ├── __init__.py
    │   └── contracts.py
    ├── impl/
    │   ├── __init__.py
    │   └── plugin.py
    ├── doc/design.md
    └── tests/test_plugin.py
```

The loader discovers `hello_plugin.impl.plugin` under an explicitly supplied
plugin directory. Installed packages can instead use an explicit complete
module name in `plugins.yaml`. Do not export a `plugin` singleton or put
registration at the package root.

Define the shared contract in `api/contracts.py`:

```python
from typing import Protocol
from xcore.api.services import ServiceKey

class Greeting(Protocol):
    def text(self) -> str: ...

GREETING = ServiceKey[Greeting]("hello.greeting")
```

Define the actual facets in `impl/plugin.py`:

```python
from pydantic import BaseModel
from hello_plugin.api.contracts import GREETING

class Config(BaseModel):
    message: str = "Hello"

class GreetingService:
    def __init__(self, message):
        self.message = message

    def text(self):
        return self.message

def mount_process(ctx, config):
    ctx.provide(GREETING, GreetingService(config.message))

mount_process.inject = ()
mount_process.provide = (GREETING,)
mount_process.Config = Config

def mount_thread(ctx, config):
    # This callback owns a thread-local registration. XCore removes it at close.
    ctx.on("hello/read", lambda _: ctx.require(GREETING).text())

mount_thread.inject = (GREETING,)
mount_thread.provide = ()
mount_thread.Config = Config
```

A function is mounted only for its declared scope. There is no factory layer.
Declarations are real runtime boundaries: publish only services in `provide`,
list required services in `inject`, and never depend on a private service
falling through to a parent. Config validation occurs before activation.

An overlay entry can use the exact implementation module:

```yaml
- id: hello
  name: hello_plugin.impl.plugin
  optional: true
  config:
    message: Hello from the plugin
```

Required import/configuration/activation/dependency failures prevent opening.
Optional failures dispose just that facet. A module with no scope functions is
an error. An enabled module with only another scope's facet is valid and skipped.
`no_plugins` removes optional entries, including directory discoveries.

Exercise the same public loader as production in a focused test:

```python
import pytest
from xcore.api.context import Context
from XBotv2.loader.api.service import create_loader
from XBotv2.loader.api.contracts import PluginEntry, PluginTree
from hello_plugin.api.contracts import GREETING

@pytest.mark.asyncio
async def test_process_and_thread():
    root = Context()
    loader = create_loader()
    tree = PluginTree([PluginEntry("hello", "hello_plugin.impl.plugin")])
    try:
        await loader.mount(root, tree, kind="process")
        thread = loader.scope(root, "thread:main", kind="thread", tree=tree)
        await loader.mount(thread, tree, kind="thread")
        assert thread.require(GREETING) is root.require(GREETING)
        await thread.close()
        assert root.require(GREETING).text() == "Hello"
    finally:
        await root.close()
```

Use `ctx.pre_stop` for asynchronous drain: reject new work, cancel and await
owned tasks/started workers, then let disposal revoke services and release
resources. A synchronous disposer cannot stand in for completion. Register
Agent tools through the public `TOOLS` registry and its normal guard/execution
path. Human slash commands, Agent tools and prompt expansion remain separate.

For a full runtime test use `XBotv2.main.open_runtime`, locate/open a thread
through `SESSIONS`, then require the responsible owner's API. The session API
returns an actual Context. Main thread ID is `main`; child IDs identify the
thread independently of the selected Agent definition.
