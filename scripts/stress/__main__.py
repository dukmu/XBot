"""Entry point for ``python -m scripts.stress``."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from .config import parse_args
from .models import ScenarioReport, StressReport
from .report import print_report, write_report
from .resources import snapshot
from .scenarios import run_server_scenarios
from .server import (
    LiveMockServerHarness,
    MockServerHarness,
    MultiMockServerHarness,
    real_client,
)
from .tui import run_tui
from .web import run_web
from .web_server import WebServerHarness


async def _run_client_layers(config, report, layers) -> None:
    """Run browser/terminal probes while their selected server is alive."""
    if "web" in layers:
        report.scenarios.append(await run_web(config))
    if "tui" in layers:
        report.scenarios.append(await run_tui(config))


async def _run_managed_server(config, report, layers) -> None:
    """Start disposable server state and run all dependent probes in scope."""
    requested = {value.strip() for value in config.scenario.split(",") if value.strip()}
    failure_mode = ""
    if "incomplete" in requested:
        failure_mode = "http_truncate"
    elif "stream_failure" in requested:
        failure_mode = config.failure_mode
    live_http = (
        config.in_process_http
        or (config.auto_server and not config.in_process)
        or bool(layers & {"tui", "web"})
    )
    if config.servers > 1:
        async with MultiMockServerHarness(
            config.servers,
            config.timeout,
            live_http=live_http,
            data_dir=config.data_dir,
            mock_tools=config.mock_tools,
            failure_mode=failure_mode,
        ) as multi:
            effective = replace(
                config,
                base_url=multi.base_urls[0],
                server_urls=tuple(multi.base_urls),
                in_process=False,
                in_process_http=live_http,
                auto_server=False,
                failure_mode=failure_mode or config.failure_mode,
            )
            async def server_probe():
                return await run_server_scenarios(
                    multi.clients[0],
                    effective,
                    live_sse=live_http,
                    multi_clients=multi.clients,
                )

            if "server" in layers and layers & {"web", "tui"}:
                server_reports, _ = await asyncio.gather(
                    server_probe(),
                    _run_client_layers_with_web(effective, report, layers),
                )
                report.scenarios.extend(server_reports)
            else:
                if "server" in layers:
                    report.scenarios.extend(await server_probe())
                await _run_client_layers_with_web(effective, report, layers)
        return

    harness_type = LiveMockServerHarness if live_http else MockServerHarness
    async with harness_type(
        config.timeout,
        config.data_dir,
        mock_tools=config.mock_tools,
        failure_mode=failure_mode,
    ) as harness:
        if harness.client is None:
            raise RuntimeError("stress harness did not create a client")
        effective = replace(
            config,
            base_url=harness.base_url,
            server_urls=(harness.base_url,),
            in_process=False,
            in_process_http=live_http,
            auto_server=False,
            failure_mode=failure_mode or config.failure_mode,
        )
        async def server_probe():
            return await run_server_scenarios(
                harness.client,
                effective,
                live_sse=live_http,
            )

        if "server" in layers and layers & {"web", "tui"}:
            server_reports, _ = await asyncio.gather(
                server_probe(),
                _run_client_layers_with_web(effective, report, layers),
            )
            report.scenarios.extend(server_reports)
        else:
            if "server" in layers:
                report.scenarios.extend(await server_probe())
            await _run_client_layers_with_web(effective, report, layers)


async def _run_client_layers_with_web(config, report, layers) -> None:
    """Run WebUI through an automatic Vite process when no URL was supplied."""
    if "web" not in layers or config.web_url:
        await _run_client_layers(config, report, layers)
        return
    async with WebServerHarness(config.base_url, config.timeout) as web:
        await _run_client_layers(replace(config, web_url=web.base_url), report, layers)


async def _run() -> int:
    config = parse_args()
    report = StressReport(configuration=config.as_dict())
    report.environment["process_before"] = snapshot()
    layers = {config.layer} if config.layer != "all" else {"server", "web", "tui"}
    managed_server = (
        config.in_process
        or config.in_process_http
        or (
            config.auto_server
            and bool(layers & {"server", "tui"} or ("web" in layers and not config.web_url))
        )
    )
    if managed_server and (layers & {"server", "tui", "web"}):
        await _run_managed_server(config, report, layers)
    else:
        if "server" in layers:
            clients = [
                real_client(url, config.timeout) for url in config.server_urls
            ]
            try:
                try:
                    for client in clients:
                        await client.health()
                except Exception as exc:  # noqa: BLE001 - report endpoint failures
                    connection = ScenarioReport("server.connection")
                    detail = str(exc) or repr(exc)
                    connection.errors.append(f"{type(exc).__name__}: {detail}")
                    connection.finish()
                    report.scenarios.append(connection)
                else:
                    report.scenarios.extend(
                        await run_server_scenarios(
                            clients[0],
                            config,
                            multi_clients=clients if len(clients) > 1 else None,
                        )
                    )
            finally:
                for client in clients:
                    await client.close()
        await _run_client_layers_with_web(config, report, layers)
    report.finish()
    report.environment["process_after"] = snapshot()
    print_report(report)
    if config.report_path:
        write_report(report, config.report_path)
        print(f"report: {config.report_path}")
    return 1 if any(s.failed and not s.invariants.get("skipped") for s in report.scenarios) else 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_run()))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
