"""Optional team runtime and durable fork ownership."""
from dataclasses import replace
from XBotv2.agentloop.api.contracts import TOOLS
from XBotv2.core.api.tools import Tool
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.server.api.contracts import contribute_router
from XBotv2.session.api.contracts import SESSION_INFO, THREAD_INFO, SESSIONS
from XBotv2.session.api.storage import FORK_COPY_STATE
from XBotv2.team.api.models import TeamConfig
from XBotv2.team.api.service import TEAM
from XBotv2.team.impl.http import build_router
from XBotv2.team.impl.service import TeamService
from XBotv2.team.impl.tools import TeamTools


async def mount_http(ctx):
    await contribute_router(ctx, owner='xbot.team.http', router=build_router(ctx.require(SESSIONS)))


def mount_process(ctx, config):
    ctx.on(FORK_COPY_STATE, lambda event: event.handled.add('team'))
    ctx.inject(['server', SESSIONS], mount_http)


mount_process.inject = ()
mount_process.provide = ()
mount_process.Config = TeamConfig


def mount_session(ctx, config):
    info = ctx.require(SESSION_INFO)
    ctx.provide(TEAM, TeamService(ctx.require(PERSISTENCE), ctx.require(SESSIONS), info.session_id, config.enabled))


mount_session.inject = (PERSISTENCE, SESSIONS, SESSION_INFO)
mount_session.provide = (TEAM,)
mount_session.Config = TeamConfig


def mount_thread(ctx, config):
    if not config.enabled:
        return
    info = ctx.require(THREAD_INFO)
    tools = TeamTools(ctx.require(TEAM), ctx.require('artifacts'), info.thread_id)
    for name in ('team_members', 'team_add_member', 'team_remove_member', 'team_count',
                 'team_latest', 'team_after', 'team_get', 'team_publish', 'team_retry'):
        ctx.require(TOOLS).register(replace(Tool.from_function(getattr(tools, name)), kind='other'))


mount_thread.inject = (TEAM, THREAD_INFO, 'artifacts', TOOLS)
mount_thread.provide = ()
mount_thread.Config = TeamConfig
