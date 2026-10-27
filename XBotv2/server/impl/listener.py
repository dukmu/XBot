"""HTTP listener and connection drain owned by the server carrier."""
from __future__ import annotations
import asyncio
import uvicorn


class HttpListener:
    def __init__(self, context, app):
        self._context = context
        self._app = app
        self._server = None
        self._task = None
        self._stopping = False

    async def serve(self, *, host="127.0.0.1", port=4096, uds=None):
        if self._server is not None:
            raise RuntimeError("HTTP listener is already running")
        context = self._context
        owner = self
        self._stopping = False
        class Server(uvicorn.Server):
            async def shutdown(self, sockets=None):
                # Stop admissions before draining accepted work. Uvicorn's
                # default graceful wait otherwise waits forever for SSE bodies.
                for listener in self.servers:
                    listener.close()
                if not owner._stopping:
                    await context.root.close()
                await owner._drain_connections(self)
                await super().shutdown(sockets)
        server = Server(uvicorn.Config(self._app, host=host, port=port, uds=uds, log_config=None, ws="none"))
        self._server = server
        self._task = asyncio.current_task()
        try:
            await server.serve()
        finally:
            self._server = None
            self._task = None

    async def _drain_connections(self, server):
        requests = list(server.server_state.tasks)
        for task in requests:
            task.cancel()
        if requests:
            await asyncio.gather(*requests, return_exceptions=True)

    async def stop(self):
        if self._server is not None:
            self._stopping = True
            self._server.should_exit = True
            await self._drain_connections(self._server)
            if self._task is not asyncio.current_task():
                await self._task
