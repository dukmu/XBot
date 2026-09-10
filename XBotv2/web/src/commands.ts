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

export interface CommandTrigger {
  query: string;
  start: number;
  end: number;
}

/** Detect one leading slash token at the caret using DSH's boundary rules. */
export function detectCommandTrigger(
  draft: string,
  caret: number,
): CommandTrigger | null {
  for (let index = caret - 1; index >= 0; index -= 1) {
    const character = draft.charAt(index);
    if (/\s/u.test(character)) return null;
    if (character !== "/") continue;
    if (!slashBoundary(draft, index)) continue;
    if (draft.search(/\S/u) !== index) return null;
    return { query: draft.slice(index + 1, caret), start: index, end: caret };
  }
  return null;
}

export function commandSuggestions(
  commands: CommandInfo[],
  draft: string,
  caret: number,
): { trigger: CommandTrigger; commands: CommandInfo[] } | null {
  const trigger = detectCommandTrigger(draft, caret);
  if (!trigger) return null;
  return { trigger, commands: rankCommands(commands, trigger.query) };
}

function slashBoundary(draft: string, index: number): boolean {
  if (index === 0) return true;
  const previous = draft.charAt(index - 1);
  if (/\s/u.test(previous)) return true;
  if (/[\p{L}\p{N}_]/u.test(previous) || previous === "/") return false;
  return !(previous === ":" && index >= 2 && !/\s/u.test(draft.charAt(index - 2)));
}

function rankCommands(commands: CommandInfo[], rawQuery: string): CommandInfo[] {
  const query = rawQuery.toLowerCase();
  return commands
    .map((command, index) => {
      const name = command.name.toLowerCase();
      const score = fuzzyScore(name, query);
      return score === null ? null : {
        command,
        index,
        prefix: name.startsWith(query),
        score,
      };
    })
    .filter((item): item is NonNullable<typeof item> => item !== null)
    .sort((left, right) => (
      Number(right.prefix) - Number(left.prefix)
      || right.score - left.score
      || left.index - right.index
    ))
    .map((item) => item.command);
}

function fuzzyScore(name: string, query: string): number | null {
  if (!query) return 0;
  let score = 0;
  let cursor = 0;
  let previous = -2;
  for (const character of query) {
    const index = name.indexOf(character, cursor);
    if (index < 0) return null;
    score += 1;
    if (index === 0 || name.charAt(index - 1) === "-" || name.charAt(index - 1) === "_") score += 8;
    if (index === previous + 1) score += 4;
    score -= index - cursor;
    cursor = index + 1;
    previous = index;
  }
  return score;
}

function command(name: string, description: string, usage: string): CommandInfo {
  return { name, slash: `/${name}`, kind: "client", description, usage, examples: [], parameters: {} };
}
