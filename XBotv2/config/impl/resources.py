"""Configuration resource registry and config-owned resources."""
from __future__ import annotations

from functools import partial
from xcore.api.plugins import bound_effect
from XBotv2.agentloop.api.contracts import AGENT_LOOP
from XBotv2.config.api.contracts import GET_POLICY, UPDATE_POLICY, PatchPluginConfig, PatchPolicy
from XBotv2.config.api.resources import ConfigurationCatalog, ConfigurationDocument, ConfigurationResource, ConfigurationResourceNotFound
from XBotv2.config.impl.policy import policy_revision
from XBotv2.config.impl.plugin_catalog import PluginOverlayDocument, plugin_config_catalog, update_plugin_config, read_overlay_document, write_overlay_document
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.permissions.api.models import PermissionPolicy
from XBotv2.sandbox.api.models import SandboxConfig


class ConfigurationResources:
    def __init__(self):
        self._providers = {}

    def register(self, namespace, provider):
        if not namespace or ":" in namespace or namespace in self._providers:
            raise ValueError(f"Invalid or duplicate configuration namespace: {namespace}")
        self._providers[namespace] = provider
        if bound_effect(partial(self._remove, namespace, provider)) is False:
            self._remove(namespace, provider)
            raise RuntimeError("Configuration resources must be registered by a plugin")

    def _remove(self, namespace, provider):
        if self._providers.get(namespace) is provider:
            del self._providers[namespace]

    async def catalog(self, ctx):
        resources = []
        for namespace, provider in self._providers.items():
            for resource in await provider.catalog(ctx):
                if not resource.id.startswith(namespace + ":") or resource.id in {r.id for r in resources}:
                    raise ValueError(f"Invalid configuration resource id: {resource.id}")
                resources.append(resource)
        return ConfigurationCatalog(resources=tuple(resources))

    async def _provider(self, ctx, resource_id, scope):
        provider = self._providers.get(resource_id.partition(":")[0])
        if provider is None:
            raise ConfigurationResourceNotFound(f"Unknown configuration resource: {resource_id}")
        resource = next((r for r in await provider.catalog(ctx) if r.id == resource_id), None)
        if resource is None:
            raise ConfigurationResourceNotFound(f"Unknown configuration resource: {resource_id}")
        if scope not in resource.scopes:
            raise ValueError(f"Unsupported scope for {resource_id}: {scope}")
        return provider

    async def read(self, ctx, resource_id, scope):
        return await (await self._provider(ctx, resource_id, scope)).read(ctx, resource_id, scope)

    async def save(self, ctx, document):
        return await (await self._provider(ctx, document.id, document.scope)).save(ctx, document)


class ConfigResources:
    def __init__(self, paths, loader):
        self.paths, self.loader = paths, loader

    def _catalog(self, ctx, scope):
        return plugin_config_catalog(self.paths, ctx.thread_info.workspace_root, scope,
                                     ctx.thread_info.session_id, loader=self.loader)

    async def catalog(self, ctx):
        plugins = {}
        for scope in ("global", "workspace", "session"):
            for plugin in self._catalog(ctx, scope).plugins:
                if plugin.editable:
                    if plugin.plugin_id not in plugins:
                        plugins[plugin.plugin_id] = (plugin.name, [])
                    plugins[plugin.plugin_id][1].append(scope)
        return (
            ConfigurationResource(id="config:overlay", title="Plugin overlay", scopes=("global", "workspace", "session"), format="yaml", description="Raw plugin declarations, including plugins without editable schemas. Applied on the next mount."),
            *(ConfigurationResource(id=f"config:plugin.{plugin_id}", title=name, scopes=tuple(scopes), format="json", description="Plugin configuration for the selected scope; applied on the next mount.") for plugin_id, (name, scopes) in plugins.items()),
            ConfigurationResource(id="config:permissions", title="Session permissions", scopes=("session",), format="json", description="Effective policy; changes apply to the current session immediately."),
            ConfigurationResource(id="config:sandbox", title="Session sandbox", scopes=("session",), format="json", description="Effective policy; changes apply to the current session immediately."),
        )

    async def read(self, ctx, resource_id, scope):
        session_id, workspace = ctx.thread_info.session_id, ctx.thread_info.workspace_root
        if resource_id == "config:overlay":
            doc = read_overlay_document(self.paths, workspace, scope, session_id)
            return ConfigurationDocument(id=resource_id, scope=scope, revision=doc.revision, content=doc.text)
        if resource_id.startswith("config:plugin."):
            catalog = self._catalog(ctx, scope)
            plugin = next(p for p in catalog.plugins if p.plugin_id == resource_id.removeprefix("config:plugin."))
            return ConfigurationDocument(id=resource_id, scope=scope, revision=catalog.revision, content=plugin.scope_config, config_schema=plugin.config_schema)
        key = resource_id.removeprefix("config:")
        snapshot = await ctx.require(AGENT_LOOP).invoke(GET_POLICY, EmptyRequest())
        model = PermissionPolicy if key == "permissions" else SandboxConfig
        effective = snapshot.effective_permissions if key == "permissions" else snapshot.effective_sandbox
        content = model.model_validate(effective).model_dump(mode="json")
        revision = policy_revision(snapshot.policy.get(key, {}))
        return ConfigurationDocument(id=resource_id, scope=scope, revision=revision, content=content, config_schema=model.model_json_schema())

    async def save(self, ctx, document):
        session_id, workspace = ctx.thread_info.session_id, ctx.thread_info.workspace_root
        if document.id == "config:overlay":
            if not isinstance(document.content, str):
                raise ValueError("Plugin overlay must be YAML text")
            write_overlay_document(self.paths, workspace, PluginOverlayDocument(scope=document.scope, revision=document.revision, text=document.content), session_id)
        elif document.id.startswith("config:plugin."):
            if not isinstance(document.content, dict):
                raise ValueError("Plugin configuration must be an object")
            update_plugin_config(self.paths, workspace, document.id.removeprefix("config:plugin."), PatchPluginConfig(scope=document.scope, revision=document.revision, config=document.content), session_id, loader=self.loader)
        else:
            key = document.id.removeprefix("config:")
            model = PermissionPolicy if key == "permissions" else SandboxConfig
            value = model.model_validate(document.content).model_dump(mode="json")
            patch = (PatchPolicy(replace_permissions=value, revision_key=key, expected_revision=document.revision)
                     if key == "permissions" else PatchPolicy(replace_sandbox=value, revision_key=key, expected_revision=document.revision))
            await ctx.require(AGENT_LOOP).invoke(UPDATE_POLICY, patch)
        return await self.read(ctx, document.id, document.scope)
