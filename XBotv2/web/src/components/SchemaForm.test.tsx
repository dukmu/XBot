import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SchemaForm } from "./SchemaForm";
import { schemaErrors } from "./schemaForm";

/** The shapes below mirror real pydantic v2 `model_json_schema` output. */
const compactSchema = {
  additionalProperties: false,
  properties: {
    automatic: { default: true, title: "Automatic", type: "boolean" },
    output_reservation: {
      anyOf: [{ minimum: 0, type: "integer" }, { type: "null" }],
      default: null,
      title: "Output Reservation",
    },
    trigger_ratio: {
      default: 0.8,
      exclusiveMinimum: 0,
      maximum: 1,
      title: "Trigger Ratio",
      type: "number",
    },
    keep_recent_turns: { default: 4, minimum: 1, title: "Keep Recent Turns", type: "integer" },
  },
  title: "CompactConfig",
  type: "object",
};

const sandboxSchema = {
  $defs: {
    SandboxResourceConfig: {
      additionalProperties: false,
      properties: {
        path: { title: "Path", type: "string" },
        access: {
          default: "readonly",
          enum: ["allow", "readwrite", "readonly", "deny"],
          title: "Access",
          type: "string",
        },
      },
      required: ["path"],
      title: "SandboxResourceConfig",
      type: "object",
    },
  },
  additionalProperties: false,
  properties: {
    enabled: { default: true, title: "Enabled", type: "boolean" },
    resources: { items: { $ref: "#/$defs/SandboxResourceConfig" }, title: "Resources", type: "array" },
  },
  title: "SandboxConfig",
  type: "object",
};

const permissionsSchema = {
  $defs: {
    JsonValue: {},
    PermissionRuleConfig: {
      additionalProperties: false,
      properties: {
        tool: { default: ".*", title: "Tool", type: "string" },
        params: { additionalProperties: { type: "string" }, title: "Params", type: "object" },
      },
      title: "PermissionRuleConfig",
      type: "object",
    },
  },
  additionalProperties: false,
  properties: {
    allow: { items: { $ref: "#/$defs/PermissionRuleConfig" }, title: "Allow", type: "array" },
    extra: { $ref: "#/$defs/JsonValue", title: "Extra" },
  },
  title: "PermissionsConfig",
  type: "object",
};

const mcpSchema = {
  $defs: {
    MCPServerConfig: {
      additionalProperties: true,
      properties: {
        enabled: { default: true, title: "Enabled", type: "boolean" },
        required: { default: false, title: "Required", type: "boolean" },
      },
      title: "MCPServerConfig",
      type: "object",
    },
  },
  additionalProperties: false,
  properties: {
    servers: {
      additionalProperties: { $ref: "#/$defs/MCPServerConfig" },
      title: "Servers",
      type: "object",
    },
  },
  title: "MCPConfig",
  type: "object",
};

describe("SchemaForm", () => {
  it("edits an optional field through its inner type instead of a JSON blob", () => {
    const onChange = vi.fn();
    render(<SchemaForm schema={compactSchema} value={{ output_reservation: 128 }} onChange={onChange} />);

    const field = screen.getByLabelText("Output Reservation");
    expect(field).toHaveAttribute("type", "number");
    expect(field).toHaveValue(128);

    fireEvent.change(field, { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith({ output_reservation: null });
  });

  it("shows number constraints and reports a violated bound", () => {
    const onChange = vi.fn();
    render(<SchemaForm schema={compactSchema} value={{ trigger_ratio: 4 }} onChange={onChange} />);

    expect(screen.getByText("> 0 · ≤ 1")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Trigger Ratio must be ≤ 1.");
  });

  it("keeps booleans as labelled checkboxes", () => {
    const onChange = vi.fn();
    render(<SchemaForm schema={compactSchema} value={{ automatic: true }} onChange={onChange} />);

    const field = screen.getByLabelText("Automatic");
    expect(field).toBeChecked();
    fireEvent.click(field);
    expect(onChange).toHaveBeenCalledWith({ automatic: false });
  });

  it("clears a layer override instead of writing the schema default", () => {
    const onChange = vi.fn();
    render(
      <SchemaForm
        schema={compactSchema}
        value={{ keep_recent_turns: 9, trigger_ratio: 0.5 }}
        onChange={onChange}
      />,
    );

    expect(screen.getByText("≥ 1 · whole number")).toBeVisible();
    fireEvent.click(screen.getAllByRole("button", { name: "Clear" })[0]);
    expect(onChange).toHaveBeenCalledWith({ keep_recent_turns: 9 });
  });

  it("advertises the declared default when the layer sets no value", () => {
    render(<SchemaForm schema={compactSchema} value={{}} onChange={vi.fn()} />);

    expect(screen.getByText("≥ 1 · whole number · default 4")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Clear" })).toBeNull();
  });

  it("resolves $ref array items into repeatable cards", () => {
    const onChange = vi.fn();
    render(
      <SchemaForm
        schema={sandboxSchema}
        value={{ resources: [{ path: "/tmp", access: "readonly" }] }}
        onChange={onChange}
      />,
    );

    expect(screen.getByText("Resources 1")).toBeVisible();
    expect(screen.getByLabelText("Path")).toHaveValue("/tmp");
    const access = screen.getByLabelText("Access");
    expect(access).toHaveValue("readonly");
    expect(Array.from(access.querySelectorAll("option")).map((option) => option.textContent)).toEqual([
      "allow",
      "readwrite",
      "readonly",
      "deny",
    ]);

    fireEvent.change(access, { target: { value: "deny" } });
    expect(onChange).toHaveBeenCalledWith({ resources: [{ path: "/tmp", access: "deny" }] });
  });

  it("starts a new array entry from the item schema default", () => {
    const onChange = vi.fn();
    render(<SchemaForm schema={sandboxSchema} value={{ resources: [] }} onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Add Resources" }));
    expect(onChange).toHaveBeenCalledWith({ resources: [{ path: "", access: "readonly" }] });
  });

  it("starts a new scalar array entry from the item schema default", () => {
    const schema = {
      properties: {
        labels: { items: { type: "string" }, title: "Labels", type: "array" },
        modalities: { items: { enum: ["text", "image"], type: "string" }, title: "Modalities", type: "array" },
      },
      type: "object",
    };
    const onChange = vi.fn();
    render(<SchemaForm schema={schema} value={{ labels: ["a"], modalities: [] }} onChange={onChange} />);

    expect(screen.getByLabelText("Labels 1")).toHaveValue("a");
    fireEvent.click(screen.getByRole("button", { name: "Add Labels" }));
    expect(onChange).toHaveBeenCalledWith({ labels: ["a", ""], modalities: [] });

    fireEvent.click(screen.getByRole("button", { name: "Add Modalities" }));
    expect(onChange).toHaveBeenLastCalledWith({ labels: ["a"], modalities: ["text"] });
  });

  it("removes an array entry", () => {
    const onChange = vi.fn();
    render(
      <SchemaForm
        schema={sandboxSchema}
        value={{ resources: [{ path: "/a", access: "deny" }] }}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Remove Resources 1" }));
    expect(onChange).toHaveBeenCalledWith({ resources: [] });
  });

  it("edits a string map as key/value rows", () => {
    const onChange = vi.fn();
    render(
      <SchemaForm
        schema={permissionsSchema}
        value={{ allow: [{ tool: "shell", params: { mode: "read" } }] }}
        onChange={onChange}
      />,
    );

    expect(screen.getByLabelText("Params name")).toHaveValue("mode");
    fireEvent.change(screen.getByLabelText("Params mode"), { target: { value: "write" } });
    expect(onChange).toHaveBeenCalledWith({ allow: [{ tool: "shell", params: { mode: "write" } }] });

    fireEvent.click(screen.getByRole("button", { name: "Remove Params mode" }));
    expect(onChange).toHaveBeenLastCalledWith({ allow: [{ tool: "shell", params: {} }] });
  });

  it("keeps an unconstrained value in a JSON editor with an inline error", () => {
    const onChange = vi.fn();
    render(<SchemaForm schema={permissionsSchema} value={{ extra: { a: 1 } }} onChange={onChange} />);

    const editor = screen.getByLabelText("Extra JSON");
    expect(JSON.parse((editor as HTMLTextAreaElement).value)).toEqual({ a: 1 });

    fireEvent.change(editor, { target: { value: "{not json" } });
    expect(screen.getByRole("alert")).toHaveTextContent("Enter valid JSON to apply this field.");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("edits object-valued map entries without dropping undeclared keys", () => {
    const onChange = vi.fn();
    render(
      <SchemaForm
        schema={mcpSchema}
        value={{ servers: { files: { enabled: true, required: true, command: "npx" } } }}
        onChange={onChange}
      />,
    );

    expect(screen.getByLabelText("Servers name")).toHaveValue("files");
    fireEvent.click(screen.getByLabelText("Enabled"));
    expect(onChange).toHaveBeenCalledWith({
      servers: { files: { enabled: false, required: true, command: "npx" } },
    });
  });

  it("adds a map entry from the value schema defaults", () => {
    const onChange = vi.fn();
    render(<SchemaForm schema={mcpSchema} value={{ servers: {} }} onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Add entry" }));
    expect(onChange).toHaveBeenCalledWith({ servers: { "": { enabled: true, required: false } } });
  });

  it("keeps a typed JSON edit verbatim instead of reformatting it", () => {
    const onChange = vi.fn();
    render(<SchemaForm schema={permissionsSchema} value={{ extra: { a: 1 } }} onChange={onChange} />);

    const editor = screen.getByLabelText<HTMLTextAreaElement>("Extra JSON");
    fireEvent.change(editor, { target: { value: '{"b":2}' } });
    expect(onChange).toHaveBeenCalledWith({ extra: { b: 2 } });
    expect(editor).toHaveValue('{"b":2}');
  });

  it("reports problems inside array entries for the save gate", () => {
    expect(schemaErrors(sandboxSchema, { resources: [{ path: "" }] })).toEqual({
      "resources.0.path": "Path is required.",
    });
    expect(schemaErrors(sandboxSchema, { resources: [{ path: "/tmp" }] })).toEqual({});
  });
});
