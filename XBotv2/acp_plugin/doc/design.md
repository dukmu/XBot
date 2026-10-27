# ACP carrier

The ACP plugin adapts ACP stdio requests and notifications to existing XBot session, Agent, command and interaction APIs. `api/contracts.py` declares `ACPLaunch`: provider name, plugin toggle, selected Agent and an optional provider override. The implementation maps events and starts the stdio carrier.

It depends on the ACP SDK and existing session and interaction owners. The adapter tracks active prompt and connection resources; session history, thread state and interactions remain with their owners. Running the carrier reads stdin, writes stdout, and opens and closes the application context. For example, a launch with `provider_name=None` uses the configured default provider; `selected_agent` can select an Agent.
