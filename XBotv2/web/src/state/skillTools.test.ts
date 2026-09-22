import { describe, expect, it } from "vitest";
import type { ToolInfo } from "../api/types";
import { skillToolNames } from "./skillTools";

function tool(name: string, namespace: string, registered = name): ToolInfo {
  return { name, registered_name: registered, namespace, description: "", parameters: {}, timeout_seconds: null };
}

describe("skillToolNames", () => {
  it("keeps only the tools the catalog registers under the skills namespace", () => {
    const names = skillToolNames([
      tool("report", "core"),
      tool("snapshot-skill", "skills:workspace"),
      tool("policy-user-only", "skills:user"),
      tool("read_file", "core"),
    ]);
    expect(names).toEqual(["snapshot-skill", "policy-user-only"]);
  });

  it("accepts both the model-facing and the registered name", () => {
    expect(skillToolNames([tool("snapshot-skill", "skills:workspace", "skills:workspace/snapshot-skill")]))
      .toEqual(["snapshot-skill", "skills:workspace/snapshot-skill"]);
  });
});
