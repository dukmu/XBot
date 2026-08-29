import type { CommandInfo } from "./api/types";

const clientCommands: CommandInfo[] = [
  command("help", "Show commands or help for one command", "/help [command]"),
  command("session", "List, resume, or create sessions", "/session [list | <id> [workspace] | new [workspace]]"),
  command("resume", "Resume a persisted session", "/resume [session-id [workspace]]"),
  command("new", "Create a session in a workspace", "/new [workspace]"),
  command("fork", "Fork the current persisted session", "/fork"),
  command("undo", "Remove recent conversation turns", "/undo [count]"),
  command("clear", "Clear conversation history", "/clear"),
];

export function commandCatalog(serverCommands: CommandInfo[]): CommandInfo[] {
  const clientNames = new Set(clientCommands.map((item) => item.name));
  return [...clientCommands, ...serverCommands.filter((item) => !clientNames.has(item.name))];
}

export function parseCommand(raw: string): { name: string; args: string } | null {
  const input = raw.trim();
  if (!input.startsWith("/") || input.includes("\n")) return null;
  const separator = input.search(/\s/);
  const name = input.slice(1, separator < 0 ? undefined : separator).toLowerCase();
  return { name, args: separator < 0 ? "" : input.slice(separator).trim() };
}

export function matchingCommands(commands: CommandInfo[], input: string): CommandInfo[] {
  const parsed = parseCommand(input);
  if (!parsed || parsed.args) return [];
  return commands.filter((item) => item.name.startsWith(parsed.name));
}

function command(name: string, description: string, usage: string): CommandInfo {
  return { name, slash: `/${name}`, kind: "client", description, usage, examples: [], parameters: {} };
}
