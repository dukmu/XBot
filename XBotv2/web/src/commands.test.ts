import { describe, expect, it } from "vitest";
import {
  commandCatalog,
  commandSuggestions,
  detectCommandTrigger,
  parseCommand,
} from "./commands";
import type { CommandInfo } from "./api/types";

const server: CommandInfo[] = [
  { name: "status", slash: "/status", kind: "server", description: "Show status", usage: "/status", examples: [], parameters: {} },
  { name: "clear", slash: "/clear", kind: "server", description: "Server clear", usage: "/clear", examples: [], parameters: {} },
];

describe("web command catalog", () => {
  it("keeps client-owned navigation commands ahead of server collisions", () => {
    const commands = commandCatalog(server);
    expect(commands.filter((item) => item.name === "clear")).toEqual([
      expect.objectContaining({ kind: "client" }),
    ]);
    expect(commands).toContainEqual(expect.objectContaining({ name: "status", kind: "server" }));
  });

  it("parses one-line slash commands without treating multiline prompts as commands", () => {
    expect(parseCommand(" /session demo /workspace ")).toEqual({ name: "session", args: "demo /workspace" });
    expect(parseCommand("/skill\nextra instructions")).toBeNull();
  });

  it("searches and explains discovered commands", () => {
    const commands = commandCatalog(server);
    expect(commandSuggestions(commands, "/st", 3)?.commands.map((item) => item.name)).toEqual(["status"]);
    expect(commandSuggestions(commands, "/ss", 3)?.commands[0].name).toBe("session");
  });

  it("owns the leading token at the caret without firing inside URLs or prompts", () => {
    expect(detectCommandTrigger("/sta", 4)).toEqual({ query: "sta", start: 0, end: 4 });
    expect(detectCommandTrigger("  /sta", 6)).toEqual({ query: "sta", start: 2, end: 6 });
    expect(detectCommandTrigger("say /sta", 8)).toBeNull();
    expect(detectCommandTrigger("https://host", 12)).toBeNull();
    expect(detectCommandTrigger("/status argument", 16)).toBeNull();
  });

  it("uses the caret token rather than reparsing the entire draft", () => {
    const commands = commandCatalog(server);
    expect(commandSuggestions(commands, "/status", 3)?.commands[0].name).toBe("status");
    expect(commandSuggestions(commands, "/status", 7)?.commands[0].name).toBe("status");
  });
});
