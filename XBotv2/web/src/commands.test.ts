import { describe, expect, it } from "vitest";
import { commandCatalog, matchingCommands, parseCommand } from "./commands";
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
    expect(matchingCommands(commands, "/st").map((item) => item.name)).toEqual(["status"]);
  });
});
