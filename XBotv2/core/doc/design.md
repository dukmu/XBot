# Core product contracts

`api/` owns capability-neutral product values and stable construction functions.
It does not register services or open storage on import. Plugins import the
specific module, such as `XBotv2.core.api.messages` or `core.api.tools`; the
package root has no public facade.

| API | Responsibility |
| --- | --- |
| `domain`, `messages`, `parts` | Canonical identities, runtime selections, usage, conversation variants and content parts |
| `provider`, `providers`, `stream` | Provider-neutral requests, provider protocol/capabilities/errors and stream values |
| `tools`, `artifacts` | Tool definition/schema construction, calls/outcomes/directives and logical artifact references/store protocol |
| `history`, `metadata` | History/metadata consumer protocols, durable history entries, paging values, metadata models and change facts |
| `paths`, `variables` | Validated path values and immutable runtime-variable expansion |
| `operations`, `errors` | Typed operations, boundary dispatch validation and coded errors |
| `tokens`, `timing`, `prompts`, `runtime_logging` | Shared estimates, statistics, prompt containers and structured logging |

`Tool.from_function` constructs a definition and schema once; execution lives
in the agentloop registry/guard/dispatcher. The provider protocol exposes
`astream` and capabilities; retry and capability validation are implemented by
`llm/impl/base.py`, with transport-specific behavior in its adapters.

`History` and `Metadata` are actual consumer protocols, not constructors.
The live `ConversationHistory`, metadata state and loop runtime are owned by
`agentloop/impl/`. Provider/session/client projections consume the same
canonical values; they do not maintain alternate copies of product messages.

Artifacts persist logical IDs. Model-facing paths are resolved by the active
ArtifactStore and `RuntimeVariables`, never by joining an ID onto a guessed
thread directory. `filesystem/` still contains physical I/O, session locking,
atomic writes and artifact-store implementation. Its imports remain visible in
the architecture audit; moving that business into the persistence owner is a
pending storage migration, not a public API provided by this package.

There is no core plugin configuration or state namespace. Path roots and
runtime-variable values are explicit construction inputs. Runtime logging uses
the existing logger configuration; token estimates are provider-independent
estimates, not provider usage measurements.

```python
from XBotv2.core.api.tools import Tool, succeeded_text

async def hello(name: str):
    """Greet one person."""
    return succeeded_text(f"Hello, {name}")

# In the owning plugin's apply callback:
# ctx.tools.register(Tool.from_function(hello))
```

Focused tests live in `tests/`. History working-set behavior lives with the
agentloop tests; provider, HTTP, persistence and application integration checks
remain in the central suites.
