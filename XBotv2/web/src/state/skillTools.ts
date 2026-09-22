/**
 * A tool call loads a skill when the catalog registers it under the skills
 * namespace.  XBot names each skill tool after the skill itself and gives it no
 * arguments, so the namespace is the only signal the transcript has.
 */
import type { ToolInfo } from "../api/types";

/** Tool names (model-facing) that the catalog marks as skill loaders. */
export function skillToolNames(tools: ToolInfo[]): string[] {
  const names = new Set<string>();
  for (const tool of tools) {
    if (!tool.namespace.startsWith("skills")) continue;
    for (const name of [tool.name, tool.registered_name]) {
      if (name) names.add(name);
    }
  }
  return [...names];
}
