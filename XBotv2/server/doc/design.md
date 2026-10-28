# HTTP server carrier

The server owns the FastAPI application and its contributed routes. `api/contracts.py` declares `RouteContribution`, `REGISTER_ROUTE`, `QUERY_STATUS` and `contribute_router`. A plugin calls `await contribute_router(ctx, owner="example.http", router=router)` to mount routes for its XCore fiber; disposal removes the contribution. `QUERY_STATUS` obtains the session owner's health projection.

The carrier depends on XCore events and FastAPI, and does not own session or Agent state. It retains the application, route registrations and process start time; registering routes mutates the application and installs disposable exception handlers. It has no plugin-specific configuration. The implementation rejects duplicate routes or handlers and provides `/health` and `/hello`.
